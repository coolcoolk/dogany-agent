#!/usr/bin/env python3
"""
DGN-273 remind selection engine -- the single testable home of the alert
selection that routines/remind.sh sends. The shell script stays a thin pipe:
this module decides WHICH alerts are due; the script dedups (sent markers)
and pushes.

Selection contract (per event row, timed + unsettled only):
    notify_policy NULL / 'default'
        -> lead alert at (start - default lead) + on-time alert at start
           default lead: task = 30 min, appointment = 120 min (legacy remind
           behavior, byte-compatible with the pre-007 fixed windows)
    'silent'     -> no alerts
    'start_only' -> on-time alert only
    'custom'     -> lead alert at (start - notify_lead_min) + on-time alert
An alert is DUE when its instant falls within +/- SLACK_SEC of `now`
(SLACK_SEC = 300 matches the 5-minute poll cadence: the legacy 30-min task
window [now+25m, now+35m] is exactly |start-30m - now| <= 5m).

Dedup keys (emitted for the sent-marker file) preserve the live shapes so a
cutover never double-fires:
    appt lead   -> appt:<id>:<local_iso>          appt start -> appt_start:...
    task lead   -> task:<ulid>:<start_at_utc>     task start -> task_start:...

Backward compatibility: on a pre-007 DB (no notify columns) every row is
treated as 'default' -- identical alerts to today.

CLI: remind_select.py [--db PATH] [--now EPOCH]
Output: TSV  key kind alert lead_min local_hhmm title location purpose

English/ASCII only.
"""

import argparse
import datetime
import os
import sqlite3
import sys
from datetime import timezone
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import lifekit  # noqa: E402

DEFAULT_LEAD_MIN = {"task": 30, "appointment": 120}
SLACK_SEC = 300          # +/- band around each alert instant (5-min poll)
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def policy_alerts(kind, notify_policy, notify_lead_min):
    """[(alert, lead_min)] for one row. alert = 'lead' | 'start'.
    Unknown future policy values fall back to 'default' (fail-open to the
    legacy behavior rather than silently dropping alerts)."""
    if notify_policy == "silent":
        return []
    if notify_policy == "start_only":
        return [("start", 0)]
    if notify_policy == "custom" and notify_lead_min is not None:
        return [("lead", int(notify_lead_min)), ("start", 0)]
    return [("lead", DEFAULT_LEAD_MIN.get(kind, 30)), ("start", 0)]


def _utc_str(ts):
    return datetime.datetime.fromtimestamp(ts, tz=timezone.utc).strftime(_FMT)


def _has_notify_cols(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event);")}
    return "notify_policy" in cols


def _get_participants(conn, event_id):
    """Return comma-joined participant names for an event (empty string if none)."""
    try:
        rows = conn.execute(
            "SELECT p.name FROM persons p "
            "JOIN event_persons ep ON ep.person_id = p.id "
            "WHERE ep.event_id = ? ORDER BY p.name;",
            (event_id,)).fetchall()
        return ", ".join(r[0] for r in rows)
    except Exception:
        return ""


def select_alerts(conn, now_ts):
    """All due alerts at `now_ts` (epoch seconds). Returns a list of dicts:
    {key, kind, alert, lead_min, hhmm, title, location, purpose}. Pure read;
    dedup against already-sent keys is the caller's job."""
    has_notify = _has_notify_cols(conn)
    if has_notify:
        max_custom = conn.execute(
            "SELECT COALESCE(MAX(notify_lead_min), 0) FROM event "
            "WHERE settled_at IS NULL;").fetchone()[0]
    else:
        max_custom = 0
    max_lead_min = max(max(DEFAULT_LEAD_MIN.values()), int(max_custom or 0))
    lo = _utc_str(now_ts - SLACK_SEC)
    hi = _utc_str(now_ts + max_lead_min * 60 + SLACK_SEC + 1)
    notify_sel = ("e.notify_policy, e.notify_lead_min" if has_notify
                  else "NULL, NULL")
    rows = conn.execute(
        "SELECT e.id, e.ulid, e.kind, e.title, e.start_at, e.display_tz, "
        "e.location, e.purpose, " + notify_sel + " FROM event e "
        "WHERE e.settled_at IS NULL AND e.schedule_kind='timed' "
        "AND e.start_at >= ? AND e.start_at <= ? "
        "ORDER BY e.start_at, e.id;", (lo, hi)).fetchall()
    out = []
    for eid, ulid, kind, title, sa, tz, loc, purpose, np, nl in rows:
        start_dt = (datetime.datetime.strptime(sa, _FMT)
                    .replace(tzinfo=timezone.utc))
        start_ts = int(start_dt.timestamp())
        local = start_dt.astimezone(ZoneInfo(tz or "Asia/Seoul"))
        persons = ""
        if kind == "appointment":
            persons = _get_participants(conn, eid)
        for alert, lead_min in policy_alerts(kind, np, nl):
            alert_ts = start_ts - lead_min * 60
            if not (now_ts - SLACK_SEC <= alert_ts <= now_ts + SLACK_SEC):
                continue
            if kind == "appointment":
                # live marker shape: appt[<_start>]:<id>:<local_iso>
                prefix = "appt" if alert == "lead" else "appt_start"
                key = "%s:%s:%s" % (prefix, eid, local.isoformat())
            else:
                prefix = "task" if alert == "lead" else "task_start"
                key = "%s:%s:%s" % (prefix, ulid, sa)
            out.append({
                "key": key, "kind": kind, "alert": alert,
                "lead_min": lead_min, "hhmm": local.strftime("%H:%M"),
                "title": title, "location": loc or "",
                "purpose": purpose or "",
                "persons": persons,
            })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="DGN-273 remind selection")
    ap.add_argument("--db", default=None, help="lifekit.db path override")
    ap.add_argument("--now", type=int, default=None,
                    help="epoch seconds override (tests)")
    args = ap.parse_args(argv)
    now_ts = args.now if args.now is not None else int(
        datetime.datetime.now(tz=timezone.utc).timestamp())
    db_path = args.db or lifekit.DB_PATH
    # Fail-open: if the DB does not exist yet, produce no alerts rather than
    # creating a phantom empty file (which would open with no rows and break
    # the version assert on subsequent update.sh runs).
    if not os.path.isfile(db_path):
        return 0
    # Open read-only via URI to ensure no phantom creation on a missing path
    # that slips through the check above (e.g. race). assert_version=False:
    # a pre-007 DB must keep alerting with the legacy defaults, not go dark.
    uri = "file:%s?mode=ro" % db_path.replace("?", "%3F")
    conn = sqlite3.connect(uri, uri=True)
    # busy_timeout is safe read-only; WAL and foreign_keys are write-level
    # pragmas but are silently ignored (or succeed) under mode=ro.
    conn.execute("PRAGMA busy_timeout = 5000;")
    try:
        for a in select_alerts(conn, now_ts):
            sys.stdout.write(
                "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n"
                % (a["key"], a["kind"], a["alert"], a["lead_min"],
                   a["hhmm"], a["title"], a["location"], a["purpose"],
                   a.get("persons", "")))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
