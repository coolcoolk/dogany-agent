#!/usr/bin/env python3
"""DGN-333: bypass overlap notice deferred to batch end (MAJOR-5 rev).

Regression for the 2026-07-16 false alarm: a sync cycle applying several
owner calendar moves sequentially fired the overlap notice mid-batch, while
the overlap only existed transiently against the OLD slot of a not-yet-moved
row. Fix: per-apply DETECTION stays (bypass_overlap_notice audit line), the
user NOTIFICATION is deferred and re-checked against the FINAL state at the
end of the cycle (adapter.overlap_flush, called by poll_cycle).

Tests run the LIVE mirror modules (dogany-agent/mirror) against a throwaway
DB built from schema.sql + a fresh mirror-state db.
No network, no live-db writes, no Telegram pushes.

Run: python3 database/tests/test_mirror_overlap_defer.py   (exit 0 = pass)
"""
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))   # .../dogany-agent
DB_DIR = os.path.dirname(HERE)                        # .../database
MIRROR_DIR = os.path.join(REPO_ROOT, "mirror")
SCHEMA = os.path.join(DB_DIR, "schema.sql")

sys.path.insert(0, MIRROR_DIR)
import adapter as A       # noqa: E402
import sdk_bridge as SB   # noqa: E402

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


def _build_db(dst_path):
    """Fresh lifekit.db from schema.sql (no live data required)."""
    conn = sqlite3.connect(dst_path)
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.execute("INSERT INTO areas (name, domain) VALUES ('test-area', 'health');")
    conn.commit()
    conn.close()


def _seed_appt(src_conn, title, start_at, end_at):
    """Exclusive timed appointment; returns its ulid."""
    eid = SB.ec.event_add(
        src_conn, "appointment", title, "timed",
        start_at=start_at, end_at=end_at,
        owning_agent="dgn333-test", created_by="dgn333-test")
    assert eid is not None, "seed insert failed: %s" % title
    return src_conn.execute(
        "SELECT ulid FROM event WHERE id=?", (eid,)).fetchone()["ulid"]


def _snap_current(state_conn, src_conn, ulid):
    row = dict(src_conn.execute(
        "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone())
    A.store_push_snapshot(state_conn, ulid, "calendar",
                          A.calendar_projection_from_event(row))


def _move_item(ulid, title, start_z, end_z, etag):
    """Surface item expressing an owner drag of the event to a new slot."""
    return {
        "id": A.ulid_to_hex(ulid),
        "status": "confirmed",
        "summary": title,
        "start": {"dateTime": start_z},
        "end": {"dateTime": end_z},
        "etag": etag,
    }


def _log_count(state_conn, category, ulid=None):
    if ulid:
        q = ("SELECT COUNT(*) FROM mirror_log WHERE category=? "
             "AND event_ulid=?")
        return state_conn.execute(q, (category, ulid)).fetchone()[0]
    return state_conn.execute(
        "SELECT COUNT(*) FROM mirror_log WHERE category=?",
        (category,)).fetchone()[0]


def _notice_count(state_conn):
    return state_conn.execute(
        "SELECT COUNT(*) FROM notify_outbox WHERE kind='overlap_notice'"
    ).fetchone()[0]


def test_transient_overlap_no_notice():
    """(a) Two moves in one batch: A's new slot transiently overlaps B's OLD
    slot; B then moves away -> final state clean -> NO notification."""
    print("transient mid-batch overlap (incident shape):")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "lifekit.db")
        _build_db(db_path)
        src_conn = SB.get_conn(db_path)
        A.STATE_DB_PATH = os.path.join(tmp, "mirror_state_test.db")
        state_conn = A.open_state_db()
        A._OVERLAP_PENDING[:] = []

        # B holds 07:00-08:00; A holds 08:00-09:00 (far-future day).
        ulid_b = _seed_appt(src_conn, "DGN333 prep",
                            "2031-05-01T07:00:00Z", "2031-05-01T08:00:00Z")
        ulid_a = _seed_appt(src_conn, "DGN333 move",
                            "2031-05-01T08:00:00Z", "2031-05-01T09:00:00Z")
        _snap_current(state_conn, src_conn, ulid_b)
        _snap_current(state_conn, src_conn, ulid_a)

        # Batch apply 1: A dragged onto B's OLD slot (transient overlap).
        res_a = A._apply_calendar_3way(
            _move_item(ulid_a, "DGN333 move",
                       "2031-05-01T07:00:00Z", "2031-05-01T08:00:00Z",
                       "etag-dgn333-a1"),
            state_conn, src_conn)
        _check("move A applied", res_a == "applied(schedule:applied)",
               "res=%r" % res_a)
        _check("per-apply detection logged (audit stays)",
               _log_count(state_conn, "bypass_overlap_notice", ulid_a) == 1)
        _check("no notice queued mid-batch", _notice_count(state_conn) == 0)
        _check("candidate deferred", len(A._OVERLAP_PENDING) == 1)

        # Batch apply 2: B dragged earlier -> overlap resolves.
        res_b = A._apply_calendar_3way(
            _move_item(ulid_b, "DGN333 prep",
                       "2031-05-01T06:00:00Z", "2031-05-01T07:00:00Z",
                       "etag-dgn333-b1"),
            state_conn, src_conn)
        _check("move B applied", res_b == "applied(schedule:applied)",
               "res=%r" % res_b)

        # Batch end: recheck against final state -> transient, no notice.
        n = A.overlap_flush(state_conn, src_conn)
        _check("flush queues zero notices", n == 0, "n=%d" % n)
        _check("notify_outbox has no overlap_notice",
               _notice_count(state_conn) == 0)
        _check("recheck-cleared audit line written",
               _log_count(state_conn, "overlap_recheck_cleared", ulid_a) == 1)
        _check("pending drained", len(A._OVERLAP_PENDING) == 0)

        state_conn.close()
        src_conn.close()


def test_persistent_overlap_one_notice():
    """(b) A batch ending with a genuine remaining overlap -> exactly ONE
    notification."""
    print("persistent overlap at batch end:")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "lifekit.db")
        _build_db(db_path)
        src_conn = SB.get_conn(db_path)
        A.STATE_DB_PATH = os.path.join(tmp, "mirror_state_test.db")
        state_conn = A.open_state_db()
        A._OVERLAP_PENDING[:] = []

        ulid_d = _seed_appt(src_conn, "DGN333 fixed",
                            "2031-05-02T12:00:00Z", "2031-05-02T13:00:00Z")
        ulid_c = _seed_appt(src_conn, "DGN333 mover",
                            "2031-05-02T10:00:00Z", "2031-05-02T11:00:00Z")
        _snap_current(state_conn, src_conn, ulid_d)
        _snap_current(state_conn, src_conn, ulid_c)

        # Owner drags C onto D's slot; nothing else moves this batch.
        res_c = A._apply_calendar_3way(
            _move_item(ulid_c, "DGN333 mover",
                       "2031-05-02T12:00:00Z", "2031-05-02T13:00:00Z",
                       "etag-dgn333-c1"),
            state_conn, src_conn)
        _check("move C applied", res_c == "applied(schedule:applied)",
               "res=%r" % res_c)
        _check("no notice queued mid-batch", _notice_count(state_conn) == 0)

        n = A.overlap_flush(state_conn, src_conn)
        _check("flush queues exactly one notice", n == 1, "n=%d" % n)
        _check("notify_outbox has exactly one overlap_notice",
               _notice_count(state_conn) == 1)
        _check("recheck-confirmed audit line written",
               _log_count(state_conn, "overlap_recheck_confirmed",
                          ulid_c) == 1)
        row = state_conn.execute(
            "SELECT event_ulid, message FROM notify_outbox "
            "WHERE kind='overlap_notice'").fetchone()
        _check("notice addresses the moved row", row["event_ulid"] == ulid_c)
        _check("notice detail names the final-state hit",
               ulid_d in row["message"], "msg=%r" % row["message"])

        # Idempotence: a second flush (empty pending) adds nothing.
        n2 = A.overlap_flush(state_conn, src_conn)
        _check("re-flush queues nothing", n2 == 0
               and _notice_count(state_conn) == 1)

        state_conn.close()
        src_conn.close()


def main():
    test_transient_overlap_no_notice()
    test_persistent_overlap_one_notice()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
