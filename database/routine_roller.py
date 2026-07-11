#!/usr/bin/env python3
"""
DGN-240 routine roller (spec v3 section 2, T9) -- materialization + renewal
engine. Also the single home of the shared instance machinery (materialize /
regen path / group cancel / occupied) that the lifekit routine verbs call
inline (registration / rule change / resume), so nightly and inline paths run
the SAME code.

Nightly pass order (2.1, fixed):
    (1) lapse -> (2) rule-conformance -> (3) materialize
    (Monday run adds (4) renewal/retire + (5) roller_log prune)

Entry: launchd com.telegram-skill-bot.<agent-label>.routine-roller, daily
03:40 local time, via routines/routine-roller.sh (flock single-execution).
This module never
pushes to Telegram itself -- anomaly push lines go to stdout and the shell
wrapper routes them through routines/push.sh (notify seam, 2.5).

Deadman property (2.5): materialization and valid_until extension BOTH live
here and only here -- if this process dies, future instances stop at the
28-day horizon and autonomous windows stop extending. No infinite production
path exists.

English/ASCII only.
"""

import argparse
import datetime
import os
import sqlite3
import sys
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import lifekit  # noqa: E402
import routine_projection as rp  # noqa: E402

HORIZON_DAYS = 28
LAPSE_MARGIN_HOURS = 24
ROLLER_LOG_KEEP_DAYS = 180
OWNING_AGENT = "user"   # per-instance: replace with the agent's label
ROLLER_BY = "roller"
DISPLAY_TZ = "Asia/Seoul"

# self-locating default mirror state db (adapter convention: module home
# database/ -> ../mirror/mirror_state.db).
STATE_DB_PATH = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "mirror", "mirror_state.db"))

# minimal outbox DDL (byte-compatible subset of mirror/mirror_state.sql):
# used only when the state db does not exist yet (fresh sandbox).
_STATE_MIN_DDL = """
CREATE TABLE IF NOT EXISTS mirror_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_outbox (
    id           INTEGER PRIMARY KEY,
    event_ulid   TEXT NOT NULL,
    op           TEXT NOT NULL DEFAULT 'sync',
    status       TEXT NOT NULL DEFAULT 'queued',
    lease_at     TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    requeues     INTEGER NOT NULL DEFAULT 0,
    dead         INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK (status IN ('queued','claimed','pushed','failed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_ulid_pending
    ON mirror_outbox(event_ulid) WHERE status IN ('queued','claimed');
CREATE TABLE IF NOT EXISTS push_snapshot (
    event_ulid   TEXT PRIMARY KEY,
    surface      TEXT NOT NULL,
    field_hash   TEXT NOT NULL,
    field_json   TEXT NOT NULL DEFAULT '{}',
    pushed_at    TEXT NOT NULL
);
"""


def open_state_db(path=None):
    """Open (or create) the mirror state db for outbox writes + read-only
    never-mirrored lookups. Returns None on failure -- callers treat an
    unavailable state db as 'tombstone path, never DELETE' (spec 4.2)."""
    path = path or STATE_DB_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_STATE_MIN_DDL)
        conn.commit()
        return conn
    except sqlite3.Error:
        return None


def outbox_enqueue(state_conn, event_ulid):
    """Idempotent enqueue -- identical semantics to adapter.outbox_enqueue
    (unique pending index collapses duplicates)."""
    if state_conn is None:
        return False
    now = lifekit.now_utc()
    try:
        state_conn.execute(
            "INSERT INTO mirror_outbox(event_ulid, op, status, created_at, "
            "updated_at) VALUES(?, 'sync', 'queued', ?, ?)",
            (event_ulid, now, now))
        state_conn.commit()
        return True
    except sqlite3.IntegrityError:
        state_conn.rollback()
        return False


def roller_log(conn, category, recurrence_id=None, detail=None, now=None):
    conn.execute(
        "INSERT INTO roller_log(ts, recurrence_id, category, detail) "
        "VALUES(?,?,?,?);",
        (now or lifekit.now_utc(), recurrence_id, category, detail))
    conn.commit()


def _today(tz_name=DISPLAY_TZ):
    return datetime.datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def _local_date(ts_utc, tz_name=DISPLAY_TZ):
    dt = datetime.datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def _minus_hours(ts_utc, hours):
    dt = datetime.datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _plus_days(date_str, days):
    return (datetime.date.fromisoformat(date_str)
            + timedelta(days=days)).isoformat()


def upper(defn, today):
    """Spec 2.2 materialization cap:
        min(today+28d, valid_until)   if end_date IS NULL
        min(today+28d, end_date)      if end_date IS NOT NULL
    (valid_until never binds an end_date def -- F-2 separation).
    OQ-B3 Metal ruling (2026-07-10): the 28-day horizon bound is EXCLUSIVE
    -- last materializable day = today+27, i.e. max 28 daily instances
    (charter '4 weeks' / spec 5.3 ceil(28/interval) is normative; the 2.2
    inclusive pseudocode was the drift -- spec erratum logged in the
    ticket). end_date / valid_until caps stay inclusive."""
    horizon = _plus_days(today, HORIZON_DAYS - 1)
    cap = defn["end_date"] if defn["end_date"] is not None else defn["valid_until"]
    return min(horizon, cap)


def occupied(conn, rid, d):
    """Spec 2.2 exact SQL (NOT inside the row predicate, NULL-safe IS):
    every settled/exception/user-skip row occupies its date; the ONLY
    carve-out is the rule-regen tombstone."""
    return conn.execute(
        "SELECT EXISTS ("
        "    SELECT 1 FROM event"
        "    WHERE recurrence_id = :rid AND rec_date = :d"
        "      AND NOT (settled_outcome IS 'abandoned'"
        "               AND settled_by IS 'rule-regen')"
        ");", {"rid": rid, "d": d}).fetchone()[0] == 1


def include_day(defn, d, today, now):
    """Spec 2.2 start-day rule (m1+m11 unified guard):
        d > today  -> true
        d == today -> all_day: true (day not over yet)
                      timed:   rule instant > now (never born-expired)
        d < today  -> false (past instants are never created)."""
    if d > today:
        return True
    if d < today:
        return False
    if defn["schedule_kind"] == "all_day":
        return True
    sa, _ea, _sk = rp.rule_instants(defn, d)
    return sa > now


def active_defs(conn):
    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM routine_def WHERE status='active' "
            "AND cadence IS NOT NULL ORDER BY id;")]
    finally:
        conn.row_factory = old


def _all_cadence_defs(conn):
    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM routine_def WHERE cadence IS NOT NULL "
            "ORDER BY id;")]
    finally:
        conn.row_factory = old


def advisory_displaced_check(conn, defn, d, sa, ea, now=None):
    """Spec 2.2 displaced advisory metric (M6 redefinition): instances are
    non-occupying and ALWAYS insert; if a timed occurrence overlaps a live
    exclusive row (a pre-booked appointment won the advisory warning), log
    roller_log('displaced', rid, d) -- (rid, rec_date) deduped. Pure metric:
    no blocking, no forcing."""
    if defn["schedule_kind"] != "timed":
        return False
    rid = defn["recurrence_id"]
    dup = conn.execute(
        "SELECT 1 FROM roller_log WHERE category='displaced' "
        "AND recurrence_id=? AND detail=? LIMIT 1;", (rid, d)).fetchone()
    if dup:
        return False
    hit = conn.execute(
        "SELECT e.ulid FROM event e WHERE " + lifekit.LIVE_FILTER +
        " AND ? < " + lifekit.EFF_END_SQL + " AND e.start_at < ? "
        "AND (e.recurrence_id IS NULL OR e.recurrence_id != ?) LIMIT 1;",
        (sa, ea, rid)).fetchone()
    if hit:
        roller_log(conn, "displaced", rid, d, now=now)
        return True
    return False


def materialize_def(conn, state_conn, defn, today=None, now=None,
                    floor_date=None):
    """Spec 2.2 materialize pass for ONE def (idempotent; shared verbatim by
    the nightly roller and the inline registration/rule-change paths).
    Returns the list of local dates materialized in this call."""
    if defn["status"] != "active" or defn["cadence"] is None:
        return []
    today = today or _today(defn["display_tz"])
    now = now or lifekit.now_utc()
    if state_conn is None:
        state_conn = open_state_db()
    rid = defn["recurrence_id"]
    # G-2 (grill-final): rule_effective_from joins the lower bound -- the
    # nightly run must never seed NEW-rule instances inside the protection
    # window [today, effective_from) (F-A(ii): that window lives under the
    # OLD rule; existing rows there stay as-is and occupy their dates).
    lo = max(today, defn["start_date"],
             defn["rule_effective_from"] or today, floor_date or today)
    hi = upper(defn, today)
    made = []
    for dd in rp.occurrence_dates(defn, lo, hi):
        d = dd.isoformat()
        if not include_day(defn, d, today, now):
            continue
        if occupied(conn, rid, d):
            continue
        sa, ea, sk = rp.rule_instants(defn, d)
        try:
            eid = lifekit.event_add(
                conn, kind="task", title=defn["title"], schedule_kind=sk,
                start_at=sa, end_at=ea, owning_agent=OWNING_AGENT,
                created_by=ROLLER_BY, completion_rule="manual",
                area_id=defn["area_id"], display_tz=defn["display_tz"],
                recurrence_id=rid, rec_date=d, is_routine=1)
        except sqlite3.IntegrityError as e:
            # n3: inline-vs-nightly race -- the later INSERT hits
            # idx_event_rec_live. Benign: skip this (rid, d), keep looping.
            if "idx_event_rec_live" in str(e) or "UNIQUE" in str(e):
                roller_log(conn, "materialized", rid,
                           "unique_skip d=%s" % d, now=now)
                continue
            raise
        ulid = conn.execute("SELECT ulid FROM event WHERE id=?;",
                            (eid,)).fetchone()[0]
        outbox_enqueue(state_conn, ulid)
        roller_log(conn, "materialized", rid, d, now=now)
        advisory_displaced_check(conn, defn, d, sa, ea, now=now)
        made.append(d)
    return made


# ---------------------------------------------------------------------------
# regen path (spec 4.2 step 2 row handling; shared by routine_update, pause,
# and the nightly conformance pass)
# ---------------------------------------------------------------------------

def _never_mirrored(state_conn, row):
    """Never-mirrored test (spec 4.2): bookkeeping columns NULL AND no
    push_snapshot AND no pending outbox row. State db unavailable ->
    False (tombstone path, never DELETE)."""
    if row["gcal_event_id"] is not None or row["gtask_id"] is not None:
        return False
    if state_conn is None:
        return False
    try:
        snap = state_conn.execute(
            "SELECT 1 FROM push_snapshot WHERE event_ulid=? LIMIT 1;",
            (row["ulid"],)).fetchone()
        if snap:
            return False
        pending = state_conn.execute(
            "SELECT 1 FROM mirror_outbox WHERE event_ulid=? "
            "AND status IN ('queued','claimed') LIMIT 1;",
            (row["ulid"],)).fetchone()
        return pending is None
    except sqlite3.Error:
        return False


def regen_path(conn, state_conn, row, now=None):
    """One cancel-set row (4.2 step 2, row-per-txn). Returns
    'deleted' | 'cancelled' | 'skipped'."""
    if _never_mirrored(state_conn, row):
        conn.execute("BEGIN IMMEDIATE;")
        try:
            cur = conn.execute(
                "DELETE FROM event WHERE id=? AND version=? "
                "AND settled_at IS NULL;", (row["id"], row["version"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if cur.rowcount == 1:
            if state_conn is not None:
                # purge any pending outbox rows for the deleted ulid
                state_conn.execute(
                    "DELETE FROM mirror_outbox WHERE event_ulid=? "
                    "AND status IN ('queued','claimed');", (row["ulid"],))
                state_conn.commit()
            roller_log(conn, "regen_delete", row["recurrence_id"],
                       row["rec_date"], now=now)
            return "deleted"
        return "skipped"           # CAS moved under us -> converge nightly
    res = lifekit.cancel(conn, row["id"], row["version"], "rule-regen")
    if res == lifekit.MutationResult.APPLIED:
        outbox_enqueue(state_conn, row["ulid"])
        roller_log(conn, "regen_cancel", row["recurrence_id"],
                   row["rec_date"], now=now)
        return "cancelled"
    return "skipped"


def _fetch_rows(conn, sql, params):
    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.row_factory = old


def cancel_set_regen(conn, state_conn, defn, effective_from, now=None):
    """4.2 step 2 cancel-set: roller-produced, unsettled, non-exception,
    version=0 rows from effective_from on. Returns (cancelled, deleted)."""
    rows = _fetch_rows(
        conn,
        "SELECT * FROM event WHERE recurrence_id=? AND rec_date >= ? "
        "AND settled_at IS NULL AND rec_exception=0 "
        "AND created_by='roller' AND version=0;",
        (defn["recurrence_id"], effective_from))
    cancelled = deleted = 0
    for row in rows:
        r = regen_path(conn, state_conn, row, now=now)
        if r == "cancelled":
            cancelled += 1
        elif r == "deleted":
            deleted += 1
    return cancelled, deleted


def retire_group_cancel(conn, state_conn, defn, today, now=None,
                        by="routine-retire"):
    """5.4 retire cleanup: ALL future unsettled instances, exceptions
    INCLUDED (retire means stop -- user-shaped rows go too). 'Future' =
    rec_date >= today OR actual end still ahead (covers exception rows
    moved forward past their rec_date). Past/settled rows untouched
    (lifelog). Idempotent -- re-runnable as crash backstop."""
    now = now or lifekit.now_utc()
    rows = _fetch_rows(
        conn,
        "SELECT * FROM event WHERE recurrence_id=? AND settled_at IS NULL "
        "AND (rec_date >= ? OR (end_at IS NOT NULL AND end_at > ?));",
        (defn["recurrence_id"], today, now))
    n = 0
    for row in rows:
        res = lifekit.cancel(conn, row["id"], row["version"], by)
        if res == lifekit.MutationResult.APPLIED:
            outbox_enqueue(state_conn, row["ulid"])
            n += 1
    if n:
        roller_log(conn, "retired", defn["recurrence_id"],
                   "group_cancel n=%d" % n, now=now)
    return n


def rerun_group_cancel(conn, state_conn, defn, today, now=None):
    """2.4 non-active backstop: re-run the idempotent group-cancel sweep of
    pause/retire for any leftover live rows (crash recovery)."""
    if defn["status"] == "paused":
        return sum(cancel_set_regen(conn, state_conn, defn, today, now=now))
    if defn["status"] == "retired":
        return retire_group_cancel(conn, state_conn, defn, today, now=now)
    return 0


# ---------------------------------------------------------------------------
# nightly passes
# ---------------------------------------------------------------------------

def lapse_pass(conn, state_conn, now=None):
    """Spec 2.3: settle overdue roller instances by ACTUAL schedule end
    (end_at + 24h margin; rec_date takes no part -- F1). all_day (GTasks
    routing) -> cancel_abandoned('roller-lapse') + outbox (surface delete);
    timed (Calendar routing) -> untouched, expired graphite lifelog.
    rec_exception=1 rows are untouchable."""
    now = now or lifekit.now_utc()
    cutoff = _minus_hours(now, LAPSE_MARGIN_HOURS)
    rows = _fetch_rows(
        conn,
        "SELECT * FROM event WHERE recurrence_id IS NOT NULL "
        "AND created_by='roller' AND rec_exception=0 "
        "AND settled_at IS NULL AND end_at IS NOT NULL AND end_at < ? "
        "AND schedule_kind='all_day';", (cutoff,))
    n = 0
    for row in rows:
        res = lifekit.cancel(conn, row["id"], row["version"], "roller-lapse")
        if res == lifekit.MutationResult.APPLIED:
            outbox_enqueue(state_conn, row["ulid"])
            roller_log(conn, "lapse_settled", row["recurrence_id"],
                       row["rec_date"], now=now)
            n += 1
        # CAS_FAIL = inbound settle won the race -> harmless convergence (m3)
    return n


def _frozen(conn, defn):
    """2.4 churn breaker state: a conformance_frozen log row newer than the
    def's last owner edit freezes conformance for that def. Release =
    routine_update (CAS bump refreshes updated_at)."""
    return conn.execute(
        "SELECT 1 FROM roller_log WHERE category='conformance_frozen' "
        "AND recurrence_id=? AND ts > ? LIMIT 1;",
        (defn["recurrence_id"], defn["updated_at"])).fetchone() is not None


def _regen_on_previous_run(conn, defn, today):
    """Did the PREVIOUS nightly run regen this def? (churn breaker arm)"""
    prev = _plus_days(today, -1)
    for r in conn.execute(
            "SELECT ts FROM roller_log WHERE category='conformance_regen' "
            "AND recurrence_id=?;", (defn["recurrence_id"],)):
        if _local_date(r[0], defn["display_tz"]) == prev:
            return True
    return False


def conformance_pass(conn, state_conn, today=None, now=None):
    """Spec 2.4 rule-conformance (nightly self-heal, F-A time scope):
    scope floor = max(today, rule_effective_from) -- past lifelog rows and
    the pre-effective_from window are INVIOLABLE, forever."""
    today = today or _today()
    now = now or lifekit.now_utc()
    total = 0
    for defn in _all_cadence_defs(conn):
        if defn["status"] != "active":
            rerun_group_cancel(conn, state_conn, defn, today, now=now)
            continue
        if _frozen(conn, defn):
            continue
        floor = max(today, defn["rule_effective_from"])
        up = upper(defn, today)
        rows = _fetch_rows(
            conn,
            "SELECT * FROM event WHERE recurrence_id=? "
            "AND created_by='roller' AND settled_at IS NULL "
            "AND rec_exception=0 AND version=0 "
            "AND rec_date >= ? AND rec_date <= ?;",
            (defn["recurrence_id"], floor, up))
        regen_count = 0
        for row in rows:
            conforms = (
                rp.occurs(defn["cadence"], row["rec_date"])
                and defn["start_date"] <= row["rec_date"]
                and ((row["start_at"], row["end_at"], row["schedule_kind"])
                     == rp.rule_instants(defn, row["rec_date"])))  # SAME fn
            if not conforms:
                regen_path(conn, state_conn, row, now=now)
                roller_log(conn, "conformance_regen", defn["recurrence_id"],
                           row["rec_date"], now=now)
                regen_count += 1
        total += regen_count
        if regen_count > 0 and _regen_on_previous_run(conn, defn, today):
            # M-A churn breaker: 2 consecutive nightly regen runs -> freeze
            # conformance for this def (materialization NOT frozen). Release
            # path = owner decision via routine_update.
            roller_log(conn, "conformance_frozen", defn["recurrence_id"],
                       "regen_count=%d" % regen_count, now=now)
    return total


def renewal_pass(conn, state_conn, today=None, now=None):
    """Spec 2.5 weekly (Monday) pass. Returns (extended, retired,
    anomaly_push_lines). Silent extension; anomaly = exception-based ping,
    ack-suppressed; NO automatic pause/stop (retro is the only lifecycle
    gate). Push delivery is the caller's seam (shell wrapper -> push.sh)."""
    import json as _json
    today = today or _today()
    now = now or lifekit.now_utc()
    extended = retired = 0
    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        defs = [dict(r) for r in conn.execute(
            "SELECT * FROM routine_def WHERE status='active' ORDER BY id;")]
    finally:
        conn.row_factory = old
    for defn in defs:
        if defn["end_date"] is None:
            new_valid = _plus_days(today, 56)
            # G-1 (grill-final): pure roller BOOKKEEPING -- updated_at (and
            # version, OQ-B7) untouched. Only the owner's routine_update
            # refreshes updated_at and thereby releases conformance_frozen
            # (spec 2.4: owner decision is the ONLY release path).
            conn.execute(
                "UPDATE routine_def SET valid_until=? WHERE id=?;",
                (new_valid, defn["id"]))
            conn.commit()
            roller_log(conn, "extended", defn["recurrence_id"], new_valid,
                       now=now)
            extended += 1
        elif today > defn["end_date"]:
            conn.execute(
                "UPDATE routine_def SET status='retired', updated_at=? "
                "WHERE id=?;", (now, defn["id"]))
            conn.commit()
            retire_group_cancel(conn, state_conn, defn, today, now=now)
            roller_log(conn, "retired", defn["recurrence_id"],
                       "end_date reached", now=now)
            retired += 1
    # anomaly scan: ALL active defs, end_date defs included (n1 -- anomaly +
    # retro is the only brake for a dead end_date routine).
    push_lines = []
    health = lifekit.routine_health(conn, window=28, as_of=today)
    by_rid = {h["rid"]: h for h in health}
    for defn in defs:
        if today > (defn["end_date"] or "9999-12-31"):
            continue                      # just retired above
        h = by_rid.get(defn["recurrence_id"])
        anomaly = h["anomaly"] if h is not None else None
        if anomaly not in ("rate", "streak"):
            # improvement week: an existing ack auto-clears (2.5). m-4: only
            # a NO-anomaly week is an improvement -- conformance_frozen is
            # not (keeps the ack). G-1: bookkeeping clear leaves updated_at
            # untouched (freeze release stays owner-only, 2.4).
            if anomaly is None and defn["anomaly_ack"] is not None:
                conn.execute(
                    "UPDATE routine_def SET anomaly_ack=NULL WHERE id=?;",
                    (defn["id"],))
                conn.commit()
            continue
        suppressed = False
        if defn["anomaly_ack"]:
            try:
                ack = _json.loads(defn["anomaly_ack"])
            except ValueError:
                ack = {}
            if ack.get("type") == h["anomaly"]:
                if (h["anomaly"] == "rate" and h["rate"] is not None
                        and h["rate"] > float(ack.get("rate") or 0) - 0.10):
                    suppressed = True
                if (h["anomaly"] == "streak"
                        and h["consec_miss"] < int(ack.get("streak") or 0) + 3):
                    suppressed = True
        roller_log(conn, "anomaly_ping", defn["recurrence_id"],
                   ("suppressed " if suppressed else "") + h["anomaly"],
                   now=now)
        if not suppressed:
            push_lines.append(
                "%s: %s (rate=%s, consec_miss=%s)"
                % (h["title"], h["anomaly"], h["rate"], h["consec_miss"]))
    # roller_log prune (m10): 180-day retention
    cutoff_d = _plus_days(today, -ROLLER_LOG_KEEP_DAYS)
    conn.execute("DELETE FROM roller_log WHERE ts < ?;",
                 (cutoff_d + "T00:00:00Z",))
    conn.commit()
    return extended, retired, push_lines


def run_nightly(conn, state_conn, today=None, now=None, weekly=None):
    """Full nightly run, fixed order (2.1). Returns a summary dict."""
    today = today or _today()
    now = now or lifekit.now_utc()
    if weekly is None:
        weekly = datetime.date.fromisoformat(today).weekday() == 0  # Monday
    summary = {"today": today, "lapsed": 0, "conformance_regen": 0,
               "materialized": 0, "extended": 0, "retired": 0,
               "anomaly_push": []}
    summary["lapsed"] = lapse_pass(conn, state_conn, now=now)
    summary["conformance_regen"] = conformance_pass(conn, state_conn,
                                                    today=today, now=now)
    for defn in active_defs(conn):
        summary["materialized"] += len(
            materialize_def(conn, state_conn, defn, today=today, now=now))
    if weekly:
        ext, ret, lines = renewal_pass(conn, state_conn, today=today, now=now)
        summary["extended"], summary["retired"] = ext, ret
        summary["anomaly_push"] = lines
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(prog="routine_roller")
    ap.add_argument("--db", default=None, help="lifekit.db override (tests)")
    ap.add_argument("--state-db", default=None)
    ap.add_argument("--today", default=None)
    ap.add_argument("--weekly", action="store_true", default=None)
    args = ap.parse_args(argv)
    conn = lifekit.event_conn(args.db)
    state_conn = open_state_db(args.state_db)
    try:
        s = run_nightly(conn, state_conn, today=args.today,
                        weekly=args.weekly)
        print("roller: today=%s lapsed=%d conformance=%d materialized=%d "
              "extended=%d retired=%d"
              % (s["today"], s["lapsed"], s["conformance_regen"],
                 s["materialized"], s["extended"], s["retired"]))
        for line in s["anomaly_push"]:
            # notify seam: the shell wrapper greps this prefix and routes it
            # through routines/push.sh (single aggregated weekly ping, 2.5).
            print("ANOMALY_PUSH: %s" % line)
        return 0
    finally:
        conn.close()
        if state_conn is not None:
            state_conn.close()


if __name__ == "__main__":
    sys.exit(main())
