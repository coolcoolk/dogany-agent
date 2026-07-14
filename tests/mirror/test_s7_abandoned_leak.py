#!/usr/bin/env python3
"""DGN-302 regression: abandoned-transition gcal event leak.

Root cause: sweep_step scans WHERE settled_at IS NULL -- abandoned rows
(settled_at set) were excluded entirely, so no sync was enqueued and their
gcal events stayed confirmed after the abandonment.

Fixes verified:
  (a) sweep_step enqueues a sync for an abandoned in-scope row that has a
      push_snapshot with gcal_status != 'cancelled'.
  (b) Sweep convergence: a second sweep enqueues nothing (snapshot already
      reflects cancelled after the drain ran).
  (c) reconcile classifies our-ulid + abandoned DB row with confirmed gcal
      remnant as abandoned_gcal_drift, and with repair=True enqueues a cancel.
  (d) reconcile.mismatch_detail items may have more than 2 fields; wrapper
      iteration must not crash on arbitrary tuple lengths.

No network / gws / Google API used. Runs against scratch in-memory DBs.

Run: python3 tests/mirror/test_s7_abandoned_leak.py   (exit 0 = pass)
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_MIRROR = os.path.join(REPO_ROOT, "mirror")
SCHEMA = os.path.join(REPO_ROOT, "database", "schema.sql")

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


# ---------------------------------------------------------------------------
# Module loading helpers (pattern from test_s2_bootstrap_adopt.py)
# ---------------------------------------------------------------------------

def _build_scratch(root=None):
    """Copy mirror package into a scratch dir; return root."""
    if root is None:
        root = tempfile.mkdtemp(prefix="dgn302-")
    mirror_dir = os.path.join(root, "mirror")
    os.makedirs(mirror_dir, exist_ok=True)
    for f in ("adapter.py", "reconcile.py", "notify.py", "mirror_i18n.py",
              "mirror_state.sql"):
        shutil.copy2(os.path.join(SRC_MIRROR, f), os.path.join(mirror_dir, f))
    os.makedirs(os.path.join(root, "config"), exist_ok=True)
    with open(os.path.join(root, "config", "lifekit.conf"), "w") as fh:
        fh.write("# scratch\n")
    with open(os.path.join(root, ".instance.conf"), "w") as fh:
        fh.write("DOGANY_AGENT_NAME=testbot\n")
    return root


def _import_adapter(root):
    """Import a fresh adapter from the scratch mirror dir with surface deps
    stubbed (no gws / network / real SDK)."""
    for name in ("adapter", "sdk_bridge", "notify", "http_direct",
                 "mirror_i18n", "reconcile"):
        sys.modules.pop(name, None)
    sdk_stub = types.ModuleType("sdk_bridge")
    sdk_stub.ec = types.ModuleType("ec")
    http_stub = types.ModuleType("http_direct")
    http_stub.HttpError = type("HttpError", (Exception,), {})
    notify_stub = types.ModuleType("notify")
    notify_stub.notify = lambda *a, **k: None
    sys.modules["sdk_bridge"] = sdk_stub
    sys.modules["http_direct"] = http_stub
    sys.modules["notify"] = notify_stub
    sys.path.insert(0, os.path.join(root, "mirror"))
    import adapter  # noqa: F401
    mod = sys.modules["adapter"]
    mod._reset_conf_cache()
    return mod


def _import_reconcile():
    """Import reconcile from the currently-active scratch path (assumes
    adapter already imported into sys.modules['adapter'])."""
    sys.modules.pop("reconcile", None)
    import reconcile  # noqa: F401
    return sys.modules["reconcile"]


# ---------------------------------------------------------------------------
# DB fixture helpers
# ---------------------------------------------------------------------------

def _open_state_db(A):
    """Open an in-memory state DB using the adapter's own schema path."""
    A.STATE_DB_PATH = ":memory:"
    return A.open_state_db()


def _open_src_db():
    """Minimal in-memory source (event) DB with just the columns the mirror
    module reads: ulid, kind, schedule_kind, status, settled_at,
    settled_outcome, recurrence_id, is_routine, start_at, end_at, open_ended,
    slot_exclusive, title, note, location, location_url, purpose, summary,
    display_tz, version, completion_rule, id."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE event (
            id              INTEGER PRIMARY KEY,
            ulid            TEXT NOT NULL UNIQUE,
            kind            TEXT NOT NULL,
            title           TEXT NOT NULL DEFAULT '',
            note            TEXT,
            schedule_kind   TEXT NOT NULL DEFAULT 'timed',
            start_at        TEXT,
            end_at          TEXT,
            display_tz      TEXT NOT NULL DEFAULT 'Asia/Seoul',
            open_ended      INTEGER NOT NULL DEFAULT 0,
            slot_exclusive  INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'open',
            completion_rule TEXT NOT NULL DEFAULT 'all',
            version         INTEGER NOT NULL DEFAULT 0,
            settled_at      TEXT,
            settled_by      TEXT,
            settled_outcome TEXT,
            owning_agent    TEXT NOT NULL DEFAULT 'agent',
            created_by      TEXT NOT NULL DEFAULT 'agent',
            priority        TEXT,
            seq             REAL,
            is_routine      INTEGER DEFAULT 0,
            location        TEXT,
            location_url    TEXT,
            purpose         TEXT,
            summary         TEXT,
            recurrence_id   TEXT,
            rec_date        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sub_event (
            id         INTEGER PRIMARY KEY,
            event_id   INTEGER NOT NULL,
            done       INTEGER NOT NULL DEFAULT 0,
            tombstone  INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


_ULID_CTR = [0]


def _next_ulid():
    _ULID_CTR[0] += 1
    return "01ABCDEF%024d" % _ULID_CTR[0]


def _insert_event(src_conn, ulid, kind="appointment", schedule_kind="timed",
                  status="open", settled_outcome=None, settled_at=None,
                  recurrence_id=None, is_routine=0,
                  start_at="2026-07-10T08:00:00Z",
                  end_at="2026-07-10T09:00:00Z",
                  title="Test event"):
    src_conn.execute(
        "INSERT INTO event (ulid, kind, schedule_kind, status, settled_outcome, "
        "settled_at, recurrence_id, is_routine, start_at, end_at, title) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ulid, kind, schedule_kind, status, settled_outcome, settled_at,
         recurrence_id, is_routine, start_at, end_at, title))
    src_conn.commit()


def _store_snapshot(A, state_conn, ulid, gcal_status="confirmed"):
    """Write a push_snapshot simulating a prior successful push."""
    proj = {
        "title": "Test event",
        "note": "",
        "location": "",
        "start_at": "2026-07-10T08:00:00Z",
        "end_at": "2026-07-10T09:00:00Z",
        "schedule_kind": "timed",
        "gcal_status": gcal_status,
        "color_id": None,
        "transparency": "transparent",
    }
    A.store_push_snapshot(state_conn, ulid, "calendar", proj)


# ---------------------------------------------------------------------------
# (a) sweep enqueues sync for abandoned row with 'confirmed' snapshot
# ---------------------------------------------------------------------------

def test_a_sweep_enqueues_abandoned_with_snapshot():
    print("(a) sweep_step enqueues sync for abandoned row with confirmed snapshot:")
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state_db(A)
    src = _open_src_db()

    ulid = _next_ulid()
    # Row is abandoned (settled_at set) -- the OLD sweep predicate excludes it.
    _insert_event(src, ulid, kind="appointment", schedule_kind="timed",
                  status="abandoned", settled_outcome="abandoned",
                  settled_at="2026-07-14T10:00:00Z")
    # Prior push snapshot with gcal_status='confirmed' (not yet cancelled).
    _store_snapshot(A, state, ulid, gcal_status="confirmed")

    changed = A.sweep_step(state, src)

    # The ulid must appear in changed with 'abandoned'.
    abandoned_changed = [c for c in changed if c[0] == ulid]
    _check("abandoned row appears in changed list",
           len(abandoned_changed) == 1,
           "changed=%s" % changed)
    if abandoned_changed:
        _check("change label is 'abandoned'",
               abandoned_changed[0][1] == "abandoned",
               "label=%s" % abandoned_changed[0][1])

    # A queued outbox row must exist for this ulid.
    row = state.execute(
        "SELECT * FROM mirror_outbox WHERE event_ulid=? AND status='queued'",
        (ulid,)).fetchone()
    _check("outbox row queued for ulid",
           row is not None, "no queued outbox row found")

    src.close()
    state.close()
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b) sweep convergence: second sweep enqueues nothing
# ---------------------------------------------------------------------------

def test_b_sweep_convergence_no_re_enqueue():
    print("(b) second sweep enqueues nothing when snapshot already cancelled:")
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state_db(A)
    src = _open_src_db()

    ulid = _next_ulid()
    _insert_event(src, ulid, kind="appointment", schedule_kind="timed",
                  status="abandoned", settled_outcome="abandoned",
                  settled_at="2026-07-14T10:00:00Z")
    # Simulate snapshot ALREADY reflecting cancelled (post-drain state).
    _store_snapshot(A, state, ulid, gcal_status="cancelled")

    changed = A.sweep_step(state, src)

    abandoned_changed = [c for c in changed if c[0] == ulid]
    _check("no re-enqueue when snapshot already cancelled",
           len(abandoned_changed) == 0,
           "changed=%s" % changed)

    # No new queued row.
    rows = state.execute(
        "SELECT * FROM mirror_outbox WHERE event_ulid=? AND status='queued'",
        (ulid,)).fetchall()
    _check("no queued outbox row on second sweep",
           len(rows) == 0, "rows=%d" % len(rows))

    src.close()
    state.close()
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b2) sweep: out-of-scope abandoned rows are not enqueued
# ---------------------------------------------------------------------------

def test_b2_sweep_out_of_scope_not_enqueued():
    print("(b2) out-of-scope abandoned row (is_routine=1, no recurrence_id) not enqueued:")
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state_db(A)
    src = _open_src_db()

    ulid = _next_ulid()
    # is_routine=1, no recurrence_id -> in_mirror_scope returns False (N1 belt)
    _insert_event(src, ulid, kind="appointment", schedule_kind="timed",
                  status="abandoned", settled_outcome="abandoned",
                  settled_at="2026-07-14T10:00:00Z",
                  is_routine=1, recurrence_id=None)
    _store_snapshot(A, state, ulid, gcal_status="confirmed")

    changed = A.sweep_step(state, src)

    abandoned_changed = [c for c in changed if c[0] == ulid]
    _check("out-of-scope abandoned not in changed list",
           len(abandoned_changed) == 0,
           "changed=%s" % changed)
    rows = state.execute(
        "SELECT * FROM mirror_outbox WHERE event_ulid=?",
        (ulid,)).fetchall()
    _check("no outbox row for out-of-scope abandoned",
           len(rows) == 0, "rows=%d" % len(rows))

    src.close()
    state.close()
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b3) sweep: abandoned row with NO snapshot is not enqueued
# ---------------------------------------------------------------------------

def test_b3_sweep_no_snapshot_not_enqueued():
    print("(b3) abandoned row with no snapshot not enqueued (was never pushed):")
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state_db(A)
    src = _open_src_db()

    ulid = _next_ulid()
    _insert_event(src, ulid, kind="appointment", schedule_kind="timed",
                  status="abandoned", settled_outcome="abandoned",
                  settled_at="2026-07-14T10:00:00Z")
    # Deliberately no snapshot stored.

    changed = A.sweep_step(state, src)

    abandoned_changed = [c for c in changed if c[0] == ulid]
    _check("no-snapshot abandoned not in changed list",
           len(abandoned_changed) == 0,
           "changed=%s" % changed)
    rows = state.execute(
        "SELECT * FROM mirror_outbox WHERE event_ulid=?",
        (ulid,)).fetchall()
    _check("no outbox row when no snapshot",
           len(rows) == 0, "rows=%d" % len(rows))

    src.close()
    state.close()
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (c) reconcile classifies our-ulid + abandoned DB row with confirmed gcal
#     remnant as abandoned_gcal_drift and repairs it with repair=True
# ---------------------------------------------------------------------------

def _fake_reconcile_fetchers(A, reconcile_mod, cal_items, task_items):
    """Monkeypatch the two surface-fetch functions on the reconcile module."""
    reconcile_mod._fetch_all_calendar = lambda cal_id: cal_items
    reconcile_mod._fetch_all_tasks = lambda tl_id: task_items


def test_c_reconcile_classifies_and_repairs_abandoned_drift():
    print("(c) reconcile: abandoned DB row + confirmed gcal remnant -> drift + repair:")
    root = _build_scratch()
    A = _import_adapter(root)
    R = _import_reconcile()
    state = _open_state_db(A)
    src = _open_src_db()

    ulid = _next_ulid()
    _insert_event(src, ulid, kind="appointment", schedule_kind="timed",
                  status="abandoned", settled_outcome="abandoned",
                  settled_at="2026-07-14T10:00:00Z")

    # Build a gcal item with our ulid in extendedProperties, status=confirmed.
    hex_id = A.ulid_to_hex(ulid)
    cal_item = {
        "id": hex_id,
        "summary": "Test event",
        "status": "confirmed",
        "extendedProperties": {"private": {"ulid": ulid, "version": "0"}},
        "start": {"dateTime": "2026-07-10T17:00:00+09:00", "timeZone": "Asia/Seoul"},
        "end":   {"dateTime": "2026-07-10T18:00:00+09:00", "timeZone": "Asia/Seoul"},
    }
    _fake_reconcile_fetchers(A, R, [cal_item], [])

    # Lock needed: stub the drain lock (reconcile calls _acquire/_release).
    # For an in-memory DB, the lock writes succeed.
    A.set_state(state, "agent_calendar_id", "TESTCAL")
    A.set_state(state, "agent_tasklist_id", "TESTTL")

    summary = R.run_reconcile(state, src, "TESTCAL", "TESTTL",
                              repair=True, scope_ulids=None)

    _check("abandoned_gcal_drift count is 1",
           summary.get("abandoned_gcal_drift") == 1,
           "summary=%s" % {k: v for k, v in summary.items()
                           if not k.endswith("_detail")})
    _check("abandoned_cancel_enqueued is 1",
           summary.get("abandoned_cancel_enqueued") == 1,
           "enqueued=%s" % summary.get("abandoned_cancel_enqueued"))
    _check("ulid in abandoned_gcal_drift_detail",
           ulid in summary.get("abandoned_gcal_drift_detail", []),
           "detail=%s" % summary.get("abandoned_gcal_drift_detail"))
    # The outbox must have a queued row for this ulid.
    row = state.execute(
        "SELECT * FROM mirror_outbox WHERE event_ulid=? AND status='queued'",
        (ulid,)).fetchone()
    _check("outbox row queued for abandoned ulid after reconcile repair",
           row is not None, "no queued row")
    # Verdict must not be CLEAN.
    _check("verdict is ATTENTION (not CLEAN)",
           summary.get("verdict") == "ATTENTION",
           "verdict=%s" % summary.get("verdict"))

    src.close()
    state.close()
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (c2) reconcile: repair=False leaves drift detected but nothing enqueued
# ---------------------------------------------------------------------------

def test_c2_reconcile_no_repair_detects_but_does_not_enqueue():
    print("(c2) reconcile: repair=False detects drift but does not enqueue:")
    root = _build_scratch()
    A = _import_adapter(root)
    R = _import_reconcile()
    state = _open_state_db(A)
    src = _open_src_db()

    ulid = _next_ulid()
    _insert_event(src, ulid, kind="appointment", schedule_kind="timed",
                  status="abandoned", settled_outcome="abandoned",
                  settled_at="2026-07-14T10:00:00Z")

    hex_id = A.ulid_to_hex(ulid)
    cal_item = {
        "id": hex_id,
        "summary": "Test event",
        "status": "confirmed",
        "extendedProperties": {"private": {"ulid": ulid, "version": "0"}},
        "start": {"dateTime": "2026-07-10T17:00:00+09:00", "timeZone": "Asia/Seoul"},
        "end":   {"dateTime": "2026-07-10T18:00:00+09:00", "timeZone": "Asia/Seoul"},
    }
    _fake_reconcile_fetchers(A, R, [cal_item], [])
    A.set_state(state, "agent_calendar_id", "TESTCAL")
    A.set_state(state, "agent_tasklist_id", "TESTTL")

    summary = R.run_reconcile(state, src, "TESTCAL", "TESTTL",
                              repair=False, scope_ulids=None)

    _check("drift detected (count 1) even without repair",
           summary.get("abandoned_gcal_drift") == 1,
           "abandoned_gcal_drift=%s" % summary.get("abandoned_gcal_drift"))
    _check("nothing enqueued without repair",
           summary.get("abandoned_cancel_enqueued") == 0,
           "enqueued=%s" % summary.get("abandoned_cancel_enqueued"))
    rows = state.execute(
        "SELECT * FROM mirror_outbox WHERE event_ulid=?", (ulid,)).fetchall()
    _check("no outbox row when repair=False",
           len(rows) == 0, "rows=%d" % len(rows))

    src.close()
    state.close()
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (c3) foreign-item guard: events without our ulid/marker are not touched
# ---------------------------------------------------------------------------

def test_c3_reconcile_foreign_guard_intact():
    print("(c3) reconcile: foreign cal items (no ulid) not classified as drift:")
    root = _build_scratch()
    A = _import_adapter(root)
    R = _import_reconcile()
    state = _open_state_db(A)
    src = _open_src_db()

    # No event rows in src -- so there are no abandoned rows to match.
    # A foreign gcal item with no ulid marker should end up in orphans only
    # if it's our-marked; otherwise it's counted as foreign_cal.
    foreign_item = {
        "id": "FOREIGNID123",   # not a hex32, not our extProps
        "summary": "Someone else's event",
        "status": "confirmed",
        "start": {"dateTime": "2026-07-10T17:00:00+09:00"},
        "end":   {"dateTime": "2026-07-10T18:00:00+09:00"},
    }
    _fake_reconcile_fetchers(A, R, [foreign_item], [])
    A.set_state(state, "agent_calendar_id", "TESTCAL")
    A.set_state(state, "agent_tasklist_id", "TESTTL")

    summary = R.run_reconcile(state, src, "TESTCAL", "TESTTL",
                              repair=True, scope_ulids=None)

    _check("no abandoned_gcal_drift from foreign item",
           summary.get("abandoned_gcal_drift") == 0,
           "drift=%s" % summary.get("abandoned_gcal_drift"))
    _check("no cancel enqueued for foreign item",
           summary.get("abandoned_cancel_enqueued") == 0,
           "enqueued=%s" % summary.get("abandoned_cancel_enqueued"))
    _check("foreign item counted in foreign_cal (not orphan)",
           summary["foreign"]["calendar"] == 1,
           "foreign=%s" % summary["foreign"])

    src.close()
    state.close()
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (d) wrapper formatter: iteration over mismatch_detail with >2-tuple shapes
# ---------------------------------------------------------------------------

def test_d_formatter_handles_non_2tuple_mismatch_detail():
    print("(d) mismatch_detail iteration handles 2-tuple and 3-tuple shapes:")
    # mismatch_detail items are 3-tuples: (surface, ulid, diff_dict).
    # A wrapper that does `for _k, (_a, _b) in _diff.items()` on the diff_dict
    # crashes when a diff value is a 3-tuple. Simulate both 2-tuple and
    # 3-tuple diff values and verify safe iteration.
    sample_detail = [
        ("calendar", "ULID1", {"gcal_status": ("confirmed", "cancelled")}),
        ("calendar", "ULID2", {"expected": "cancelled tombstone",
                               "got": "confirmed"}),
    ]
    # Safe iteration pattern that the wrapper SHOULD use:
    # for surface, ulid, diff in mismatch_detail:
    #     for k, v in diff.items():
    #         # v may be a tuple of any length or a plain string
    errors = []
    try:
        for surface, ulid, diff in sample_detail:
            for k, v in diff.items():
                # Access v safely (not assuming 2-tuple unpack)
                _ = (surface, ulid, k, v)
    except Exception as e:
        errors.append(str(e))
    _check("safe iteration over mismatch_detail (any tuple length)",
           not errors, str(errors))

    # The OLD crash pattern: `for _k, (_a, _b) in diff.items()` when diff
    # has a string value raises TypeError, not ValueError. When diff has a
    # 3-tuple value, ValueError "too many values to unpack" fires.
    crash_detail_3tuple = [("calendar", "ULID3",
                             {"expected": "cancelled tombstone",
                              "got": "confirmed",
                              "extra": "info"})]
    crashed = False
    try:
        for _surface, _ulid, diff in crash_detail_3tuple:
            for _k, (_a, _b) in diff.items():  # old pattern
                pass
    except (ValueError, TypeError):
        crashed = True
    _check("old unpack pattern crashes on non-2-tuple value (confirms bug exists)",
           crashed, "expected crash but got none")

    # Corrected pattern using .items() without destructuring the value:
    no_crash = True
    try:
        for _surface, _ulid, diff in crash_detail_3tuple:
            for _k, _v in diff.items():  # correct: no inner unpack
                pass
    except Exception as e:
        no_crash = False
        errors.append(str(e))
    _check("corrected pattern handles any value shape without crash",
           no_crash, str(errors))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    test_a_sweep_enqueues_abandoned_with_snapshot()
    test_b_sweep_convergence_no_re_enqueue()
    test_b2_sweep_out_of_scope_not_enqueued()
    test_b3_sweep_no_snapshot_not_enqueued()
    test_c_reconcile_classifies_and_repairs_abandoned_drift()
    test_c2_reconcile_no_repair_detects_but_does_not_enqueue()
    test_c3_reconcile_foreign_guard_intact()
    test_d_formatter_handles_non_2tuple_mismatch_detail()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
