#!/usr/bin/env python3
"""Warg pursuit ledger (W01) verbs -- DGN-238 v3 sections 1.2 + 4.

Design contract (spec verbatim):
  - single SoT = Warg's own lifekit.db, W01 instance-local tables.
  - one writer: Warg MAIN session only; subagents are report-only.
  - overlay state machine: full closure, all transitions are DB status
    changes (zero prompt judgement), v1 = single overlay, mid layer only.
  - every multi-row transition = ONE transaction (BEGIN IMMEDIATE ..
    COMMIT, rollback on any error) -- a mid-crash can never land in an
    undefined half-state.
  - safety caps live as seeded inviolable safety constraint rows;
    constraint registration validates against them; NO override path.
  - layer activation requires user approval (why belongs to the user).

English/ASCII only. All functions take an explicit sqlite3 connection.
"""

import datetime
import json
import os
import re
import sqlite3

from handoff import new_ulid   # same package dir; byte-compatible ulid

BASE_LAYERS = ("long", "mid", "short")
OVERLAY = "event_overlay"

# W01 was designed and harness-proven against the migration-005 schema;
# anything older lacks the substrate (health tables / config shape).
MIN_USER_VERSION = 5

# Seeded safety quadrant (V2). Only the value pinned by the spec is seeded
# with a number; phase-minimum lives per-mid in detail.min_weeks (enforced
# by thresholds.demotion_due), supplement dose ranges are per-supplement
# data (OPEN QUESTION OQ-P in package docs).
SAFETY_SEEDS = (
    ("safety", "max_deficit_cap", "750"),   # kcal/day, spec-pinned
)

# nutrition keys validated against a safety cap key at registration
CAP_RULES = {
    "kcal_deficit": "max_deficit_cap",
}


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def vendor_expected_version(db_path):
    """Resolve the expected user_version from the vendored framework
    lifekit.py sitting NEXT TO the db (deployed layout <root>/database/).

    update.sh bumps that pin in lockstep with the migration it applies
    (framework convention, e.g. DGN-240 ships lifekit.py
    EXPECTED_USER_VERSION = 6 alongside migration 006), so a forward
    framework migration auto-aligns this gate instead of bricking the
    Warg daily jobs on a stale hardcode (grill-final MAJOR-2).
    Returns int or None (vendor file missing / pin unparseable)."""
    vendor = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                          "lifekit.py")
    if not os.path.isfile(vendor):
        return None
    with open(vendor) as f:
        for line in f:
            m = re.match(r"EXPECTED_USER_VERSION\s*=\s*(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def get_warg_conn(db_path, assert_version=True, expected_user_version=None):
    """Hardened connection to Warg's own lifekit.db (self-DB verbs).

    Version gate (fail-closed): expected version comes from the vendored
    lifekit.py next to the db (see vendor_expected_version) unless the
    caller pins expected_user_version explicitly (tests/tools). A pin
    below MIN_USER_VERSION refuses too: a freshly minted db is
    user_version 4 -- the go-live checklist runs update.sh first (v3
    section 0). W01 does not change user_version.
    """
    if assert_version:
        expected = expected_user_version
        if expected is None:
            expected = vendor_expected_version(db_path)
        if expected is None:
            raise RuntimeError(
                "warg db version gate: no vendored lifekit.py with "
                "EXPECTED_USER_VERSION next to %s and no explicit pin -- "
                "fail-closed (is this a deployed instance db?)" % db_path)
        if expected < MIN_USER_VERSION:
            raise RuntimeError(
                "warg db version gate: expected user_version %d < minimum "
                "%d (W01 substrate needs migration 005) -- run update.sh "
                "first (go-live checklist item 1)"
                % (expected, MIN_USER_VERSION))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None   # autocommit; verbs own explicit BEGIN..COMMIT
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    if assert_version:
        v = conn.execute("PRAGMA user_version;").fetchone()[0]
        if v != expected:
            conn.close()
            raise RuntimeError(
                "warg db user_version=%d, expected %d (vendored lifekit.py "
                "pin) -- run update.sh migration step first (go-live "
                "checklist item 1)" % (v, expected))
    return conn


# -- W01 apply ---------------------------------------------------------------
def default_w01_path():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        # sandbox package layout: lib/ -> warg/database/migrations/
        os.path.join(here, "..", "warg", "database", "migrations",
                     "W01_ledger.sql"),
        # deployed layout: <root>/routines/lib/ -> <root>/database/migrations/
        os.path.join(here, "..", "..", "database", "migrations",
                     "W01_ledger.sql"),
    )
    for c in candidates:
        c = os.path.normpath(c)
        if os.path.isfile(c):
            return c
    return os.path.normpath(candidates[0])


def apply_w01(conn, sql_path=None):
    """Apply the W01 ledger DDL once (sqlite_master-guarded, idempotent)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='ledger_goal'").fetchone():
        return False
    with open(sql_path or default_w01_path()) as f:
        conn.executescript(f.read())
    conn.commit()
    return True


def seed_safety_caps(conn, now=None):
    """Insert-if-absent inviolable safety rows. Returns actions."""
    now = now or now_utc()
    actions = []
    for class_, key, value in SAFETY_SEEDS:
        row = conn.execute(
            "SELECT 1 FROM ledger_constraint WHERE class=? AND key=? "
            "AND status='active'", (class_, key)).fetchone()
        if row:
            continue
        conn.execute(
            "INSERT INTO ledger_constraint (ulid, class, key, value, "
            "inviolable, status, source, recorded_at) "
            "VALUES (?,?,?,?,1,'active','seed',?)",
            (new_ulid(), class_, key, value, now))
        actions.append("seeded %s.%s=%s" % (class_, key, value))
    conn.commit()
    return actions


# -- audit --------------------------------------------------------------------
def _audit(conn, kind, goal_ulid, detail, now):
    """Module-level so tests can monkeypatch it to prove txn atomicity."""
    conn.execute(
        "INSERT INTO ledger_audit (ts, kind, goal_ulid, detail) "
        "VALUES (?,?,?,?)", (now, kind, goal_ulid, detail))


# -- goal verbs ----------------------------------------------------------------
class Refused(Exception):
    """A verb-level refusal (guidance, not a crash)."""


def goal_add(conn, layer, title, source, detail=None, status="proposed",
             starts_on=None, ends_on=None, parent_id=None, now=None):
    """Register a base-layer goal row. Overlays go through overlay_propose."""
    if layer == OVERLAY:
        raise Refused("use overlay_propose for event_overlay rows")
    if layer not in BASE_LAYERS:
        raise ValueError("unknown layer %r" % layer)
    detail = dict(detail or {})
    if layer == "mid" and "freq_per_week" not in detail:
        # spec 1.2: mid detail JSON MUST carry freq_per_week (the pinned
        # adherence denominator when routine_def is absent).
        raise Refused("mid goal requires detail.freq_per_week")
    now = now or now_utc()
    ulid = new_ulid()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute(
            "INSERT INTO ledger_goal (ulid, layer, parent_id, title, detail, "
            "status, starts_on, ends_on, source, recorded_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ulid, layer, parent_id, title, json.dumps(detail), status,
             starts_on, ends_on, source, now, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute("SELECT id FROM ledger_goal WHERE ulid=?",
                        (ulid,)).fetchone()[0]


def _goal(conn, goal_id):
    row = conn.execute("SELECT * FROM ledger_goal WHERE id=?",
                       (goal_id,)).fetchone()
    if row is None:
        raise Refused("no such goal id=%s" % goal_id)
    return row


def _active_in_layer(conn, layer):
    return conn.execute(
        "SELECT * FROM ledger_goal WHERE layer=? AND status='active'",
        (layer,)).fetchone()


def goal_activate(conn, goal_id, user_approved=False, now=None):
    """Activate a proposed base-layer goal (single txn). User-gated:
    layer changes go live only after user approval (spec section 4)."""
    if not user_approved:
        raise Refused("layer activation requires user approval")
    now = now or now_utc()
    g = _goal(conn, goal_id)
    if g["layer"] not in BASE_LAYERS:
        raise Refused("goal_activate is for base layers only")
    if g["status"] != "proposed":
        raise Refused("goal %s is %s, not proposed" % (goal_id, g["status"]))
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = _active_in_layer(conn, g["layer"])
        if cur is not None:
            conn.execute(
                "UPDATE ledger_goal SET status='superseded', updated_at=?, "
                "version=version+1 WHERE id=?", (now, cur["id"]))
        conn.execute(
            "UPDATE ledger_goal SET status='active', starts_on=COALESCE("
            "starts_on, ?), updated_at=?, version=version+1 WHERE id=?",
            (now[:10], now, goal_id))
        _audit(conn, "phase_start", g["ulid"],
               json.dumps({"layer": g["layer"],
                           "superseded": cur["ulid"] if cur else None}), now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def mid_demote(conn, mid_id, new_short_title, new_short_detail,
               user_approved=False, now=None):
    """Phase death: mid -> failed + new habit short proposed (single txn).
    long NEVER goes to failed (return path stays open). Execution only
    after the user approved the weekly-review proposal."""
    if not user_approved:
        raise Refused("demotion execution requires user approval")
    now = now or now_utc()
    g = _goal(conn, mid_id)
    if g["layer"] != "mid" or g["status"] != "active":
        raise Refused("mid_demote needs an active mid goal")
    detail = dict(new_short_detail or {})
    ulid = new_ulid()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute(
            "UPDATE ledger_goal SET status='failed', updated_at=?, "
            "version=version+1 WHERE id=?", (now, mid_id))
        conn.execute(
            "INSERT INTO ledger_goal (ulid, layer, parent_id, title, detail, "
            "status, source, recorded_at, updated_at) "
            "VALUES (?,?,?,?,?,'proposed','review',?,?)",
            (ulid, "short", g["parent_id"], new_short_title,
             json.dumps(detail), now, now))
        _audit(conn, "phase_fail", g["ulid"], None, now)
        _audit(conn, "habit_restart", ulid, None, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute("SELECT id FROM ledger_goal WHERE ulid=?",
                        (ulid,)).fetchone()[0]


# -- overlay state machine (v3 1.2 transition table, full closure) -------------
def overlay_propose(conn, title, detail, ends_on, source, now=None):
    """Create a proposed overlay. Preconditions (verb-level pre-refusal):
    an active mid exists (nothing to cover otherwise) and no overlay is
    currently active (v1 no stacking -- guidance, not an error)."""
    now = now or now_utc()
    mid = _active_in_layer(conn, "mid")
    if mid is None:
        raise Refused("no active mid phase to overlay")
    if _active_in_layer(conn, OVERLAY) is not None:
        raise Refused("an overlay is already active (v1: no stacking)")
    ulid = new_ulid()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute(
            "INSERT INTO ledger_goal (ulid, layer, title, detail, status, "
            "ends_on, resume_to_id, source, recorded_at, updated_at) "
            "VALUES (?,?,?,?,'proposed',?,?,?,?,?)",
            (ulid, OVERLAY, title, json.dumps(dict(detail or {})), ends_on,
             mid["id"], source, now, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute("SELECT id FROM ledger_goal WHERE ulid=?",
                        (ulid,)).fetchone()[0]


def overlay_start(conn, overlay_id, user_approved=False, now=None):
    """overlay_start (user approved): [mid active -> suspended] + [overlay
    proposed -> active] + audit, ONE transaction. resume_to_id is
    re-validated and re-pointed to the row actually suspended right now
    (covers a layer swap between proposal and approval)."""
    if not user_approved:
        raise Refused("overlay activation requires user approval")
    now = now or now_utc()
    ov = _goal(conn, overlay_id)
    if ov["layer"] != OVERLAY or ov["status"] != "proposed":
        raise Refused("overlay_start needs a proposed overlay")
    if _active_in_layer(conn, OVERLAY) is not None:
        raise Refused("an overlay is already active (v1: no stacking)")
    mid = _active_in_layer(conn, "mid")
    if mid is None:
        raise Refused("no active mid phase to suspend")
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute(
            "UPDATE ledger_goal SET status='suspended', updated_at=?, "
            "version=version+1 WHERE id=?", (now, mid["id"]))
        conn.execute(
            "UPDATE ledger_goal SET status='active', resume_to_id=?, "
            "starts_on=COALESCE(starts_on, ?), updated_at=?, "
            "version=version+1 WHERE id=?",
            (mid["id"], now[:10], now, overlay_id))
        _audit(conn, "overlay_start", ov["ulid"],
               json.dumps({"suspended": mid["ulid"]}), now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def overlay_daily_check(conn, today, now=None):
    """Daily expiry state machine (v3 1.2 branches i-v). Each branch runs
    as ONE transaction. Returns [(branch, overlay_ulid, note)].

    today: 'YYYY-MM-DD' local date; an overlay is expired when
    ends_on <= today has passed, i.e. ends_on < today (deadline day is
    still inside the program; expiry fires the morning after).
    """
    now = now or now_utc()
    results = []
    # (v) proposed overlays past ends_on -> archived + overlay_gc
    for ov in conn.execute(
            "SELECT * FROM ledger_goal WHERE layer=? AND status='proposed' "
            "AND ends_on < ?", (OVERLAY, today)).fetchall():
        conn.execute("BEGIN IMMEDIATE;")
        try:
            conn.execute("UPDATE ledger_goal SET status='archived', "
                         "updated_at=?, version=version+1 WHERE id=?",
                         (now, ov["id"]))
            _audit(conn, "overlay_gc", ov["ulid"], None, now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        results.append(("v", ov["ulid"], "proposed expired -> archived"))
    # active overlays past ends_on
    for ov in conn.execute(
            "SELECT * FROM ledger_goal WHERE layer=? AND status='active' "
            "AND ends_on < ?", (OVERLAY, today)).fetchall():
        target = _goal(conn, ov["resume_to_id"])
        layer = target["layer"]
        active_now = _active_in_layer(conn, layer)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            if target["status"] == "suspended" and active_now is None:
                # (i) normal resume
                conn.execute("UPDATE ledger_goal SET status='active', "
                             "updated_at=?, version=version+1 WHERE id=?",
                             (now, target["id"]))
                conn.execute("UPDATE ledger_goal SET status='completed', "
                             "updated_at=?, version=version+1 WHERE id=?",
                             (now, ov["id"]))
                _audit(conn, "overlay_expire", ov["ulid"],
                       json.dumps({"resumed": target["ulid"]}), now)
                branch, note = "i", "resumed %s" % target["ulid"]
            elif active_now is not None and active_now["id"] != target["id"]:
                # (ii) layer was swapped under the overlay -> no revert
                conn.execute("UPDATE ledger_goal SET status='completed', "
                             "updated_at=?, version=version+1 WHERE id=?",
                             (now, ov["id"]))
                if target["status"] == "suspended":
                    conn.execute(
                        "UPDATE ledger_goal SET status='superseded', "
                        "updated_at=?, version=version+1 WHERE id=?",
                        (now, target["id"]))
                _audit(conn, "overlay_expire", ov["ulid"],
                       json.dumps({"no_revert": target["ulid"],
                                   "active": active_now["ulid"]}), now)
                branch, note = "ii", "no revert, weekly note rides next run"
            elif target["status"] in ("superseded", "completed", "failed",
                                      "archived"):
                # (iii) resume target already retired -> orphaned
                conn.execute("UPDATE ledger_goal SET status='archived', "
                             "updated_at=?, version=version+1 WHERE id=?",
                             (now, ov["id"]))
                _audit(conn, "overlay_orphaned", ov["ulid"],
                       json.dumps({"retired_target": target["ulid"]}), now)
                branch, note = "iii", "resume target retired"
            elif target["status"] == "active":
                # (iv) defensive closure: target already active -> noop
                conn.execute("UPDATE ledger_goal SET status='completed', "
                             "updated_at=?, version=version+1 WHERE id=?",
                             (now, ov["id"]))
                _audit(conn, "overlay_noop", ov["ulid"], None, now)
                branch, note = "iv", "target already active"
            else:
                # proposed resume target: treat as retired-equivalent is
                # NOT defined by the spec; leave untouched and surface.
                conn.rollback()
                results.append(("skip", ov["ulid"],
                                "resume target in state %r -- manual review"
                                % target["status"]))
                continue
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        results.append((branch, ov["ulid"], note))
    return results


# -- constraints ----------------------------------------------------------------
def _active_cap(conn, cap_key):
    row = conn.execute(
        "SELECT value FROM ledger_constraint WHERE class='safety' AND key=? "
        "AND status='active'", (cap_key,)).fetchone()
    return float(row["value"]) if row else None


def constraint_add(conn, class_, key, value, source, goal_id=None,
                   inviolable=0, now=None):
    """Register a constraint row. Safety caps are enforced HERE, always,
    approval-independent (v3 section 3). Cap exceeded = refusal; there is
    NO override parameter by design."""
    now = now or now_utc()
    cap_key = CAP_RULES.get(key)
    if cap_key is not None:
        cap = _active_cap(conn, cap_key)
        if cap is not None and float(value) > cap:
            raise Refused(
                "safety cap %s=%s exceeded by %s=%s -- rejected (no "
                "override; cap changes go through the safety-quadrant "
                "update procedure)" % (cap_key, cap, key, value))
    ulid = new_ulid()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute(
            "INSERT INTO ledger_constraint (ulid, class, key, value, goal_id, "
            "inviolable, status, source, recorded_at) "
            "VALUES (?,?,?,?,?,?,'active',?,?)",
            (ulid, class_, key, str(value), goal_id, int(inviolable),
             source, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return ulid


def constraint_supersede(conn, ulid, user_approved=False, now=None):
    """Supersede a constraint row. Inviolable (safety quadrant) rows only
    move through the user-approved safety-quadrant update procedure."""
    now = now or now_utc()
    row = conn.execute("SELECT * FROM ledger_constraint WHERE ulid=?",
                       (ulid,)).fetchone()
    if row is None:
        raise Refused("no such constraint %s" % ulid)
    if row["inviolable"] and not user_approved:
        raise Refused("inviolable safety constraint: change requires the "
                      "safety-quadrant update procedure (user approval)")
    conn.execute("UPDATE ledger_constraint SET status='superseded' "
                 "WHERE ulid=?", (ulid,))
    conn.commit()


# -- resources / volatile freshness ----------------------------------------------
def resource_add(conn, kind, name, detail=None, volatile=0, as_of=None,
                 source="user", now=None):
    now = now or now_utc()
    ulid = new_ulid()
    conn.execute(
        "INSERT INTO ledger_resource (ulid, kind, name, detail, volatile, "
        "as_of, status, source, recorded_at) "
        "VALUES (?,?,?,?,?,?,'active',?,?)",
        (ulid, kind, name, json.dumps(detail) if detail else None,
         int(volatile), as_of or now[:10], source, now))
    conn.commit()
    return ulid


def mark_stale_volatiles(conn, today, max_age_days=1):
    """Daily job: volatile resources (pantry) older than max_age_days go
    status='stale' (still visible; freshness rides the inject line)."""
    cutoff = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=max_age_days)).isoformat()
    cur = conn.execute(
        "UPDATE ledger_resource SET status='stale' "
        "WHERE volatile=1 AND status='active' AND as_of < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# -- apply CLI (MANIFEST step 4 entrypoint; grill-final MAJOR-3) ---------------
def apply_package(conn, sql_path=None, now=None):
    """W01 DDL + safety seeds + threshold defaults, all idempotent.
    Returns the action list (empty strings never; no-ops are reported)."""
    import thresholds   # same package dir; lazy to keep import graph flat
    actions = []
    if apply_w01(conn, sql_path):
        actions.append("applied W01 ledger DDL")
    else:
        actions.append("W01 already applied (no-op)")
    actions += seed_safety_caps(conn, now=now) or ["safety caps present (no-op)"]
    actions += thresholds.register_defaults(conn) or \
        ["threshold defaults present (no-op)"]
    return actions


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser(
        "apply",
        help="apply W01 DDL + safety seeds + threshold defaults (idempotent)")
    a.add_argument("--db", required=True,
                   help="Warg lifekit.db (deployed: <root>/database/lifekit.db)")
    a.add_argument("--w01", default=None,
                   help="W01_ledger.sql override (default: package-relative)")
    a.add_argument("--expected-user-version", type=int, default=None,
                   help="override the vendored lifekit.py pin (tests/tools)")
    args = ap.parse_args(argv)
    if args.cmd == "apply":
        conn = get_warg_conn(
            args.db, expected_user_version=args.expected_user_version)
        try:
            for act in apply_package(conn, args.w01):
                print(act)
        finally:
            conn.close()
        return 0
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
