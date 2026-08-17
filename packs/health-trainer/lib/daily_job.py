#!/usr/bin/env python3
"""Warg daily job (launchd 04:05) -- DGN-238 v3 section 4 (daily line).

One run = (in order):
  1. overlay expiry state machine (ledger.overlay_daily_check; each
     branch a single transaction; branch (i) resume emits ONE user
     notification through the push gate, trigger 'overlay-expire-resume')
  2. volatile resource stale marking
  3. consult_state watchdog (v3 section 6, grill-2 MINOR-6): digesting
     older than 24h -> one self-retry, then loud failure (pending_data +
     Metal flag)
  4. handoff retention policy (monthly idx rotation + 60d archive sweep)
  5. inbox sweep is handled by the shell wrapper calling
     handoff-consume.sh (dec-013 backstop sweep #1)

English/ASCII only; all paths explicit; push goes through the gated
wrapper command passed in (never a raw telegram call from here).
"""

import datetime
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handoff      # noqa: E402
import ledger       # noqa: E402


def _cfg(conn, key, default=None):
    row = conn.execute("SELECT value FROM config WHERE key=?",
                       (key,)).fetchone()
    return row[0] if row else default


def _set_cfg(conn, key, value):
    conn.execute("INSERT INTO config (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def watchdog_consult(conn, now_dt, flag_dir=None, digest_cmd=None,
                     log=lambda s: None):
    """consult_state watchdog. Returns action string or None."""
    state = _cfg(conn, "consult_state")
    if state != "digesting":
        return None
    started = _cfg(conn, "digest_started_at")
    retries = int(_cfg(conn, "digest_retry_count", "0"))
    if not started:
        _set_cfg(conn, "digest_started_at",
                 now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return "stamped digest_started_at (was missing)"
    started_dt = datetime.datetime.strptime(
        started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    if (now_dt - started_dt) < datetime.timedelta(hours=24):
        return None
    if retries == 0:
        # one self-retry: the watchdog itself is the trigger (independent
        # of the archived migration notice).
        _set_cfg(conn, "digest_retry_count", "1")
        _set_cfg(conn, "digest_started_at",
                 now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if digest_cmd:
            subprocess.Popen(digest_cmd, shell=True)
        log("watchdog: digestion >24h, self-retry fired (retry 1)")
        return "digest-retry"
    # second timeout: loud failure
    _set_cfg(conn, "consult_state", "pending_data")
    _set_cfg(conn, "digest_retry_count", "0")
    if flag_dir:
        os.makedirs(flag_dir, exist_ok=True)
        with open(os.path.join(flag_dir, "digest-failed.flag"), "a") as f:
            f.write("%s digestion failed twice -> pending_data\n"
                    % now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    log("watchdog: digestion failed twice -> pending_data + Metal flag")
    return "digest-failed-loud"


def run(warg_root, db_path=None, now_dt=None, push_cmd=None,
        digest_cmd=None):
    """Full daily pass. push_cmd: argv prefix for the gated push wrapper
    (e.g. ['<root>/routines/push-gated.sh', '--trigger',
    'overlay-expire-resume', '--text', ...]) -- composed here per event."""
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    today_local = now_dt.astimezone().strftime("%Y-%m-%d")
    db = db_path or os.path.join(warg_root, "database", "lifekit.db")
    flag_dir = os.path.join(warg_root, ".telegram_bot", "state")
    logw = _mklog(warg_root)
    report = {"overlay": [], "stale": 0, "watchdog": None, "retention": []}

    conn = ledger.get_warg_conn(db)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='ledger_goal'").fetchone():
            results = ledger.overlay_daily_check(
                conn, today_local, now=now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
            report["overlay"] = results
            for branch, ulid, note in results:
                if branch == "i" and push_cmd:
                    # single user notification, gated trigger (V3 registry)
                    subprocess.run(
                        list(push_cmd) + [
                            "--trigger", "overlay-expire-resume",
                            "--text",
                            "이벤트 프로그램이 끝나서 원래 프로그램으로 "
                            "복귀했어요."],
                        check=False)
            report["stale"] = ledger.mark_stale_volatiles(conn, today_local)
        report["watchdog"] = watchdog_consult(conn, now_dt,
                                              flag_dir=flag_dir,
                                              digest_cmd=digest_cmd,
                                              log=logw)
    finally:
        conn.close()
    report["retention"] = handoff.retention(warg_root, now_dt)
    logw("daily: %s" % json.dumps(report, ensure_ascii=False))
    return report


def _mklog(root):
    logdir = os.path.join(root, ".telegram_bot", "logs")
    os.makedirs(logdir, exist_ok=True)

    def write(msg):
        with open(os.path.join(logdir, "daily.log"), "a") as f:
            f.write("%s %s\n" % (handoff.now_utc(), msg))
    return write


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    push = [os.path.join(args.root, "routines", "push-gated.sh")]
    run(args.root, db_path=args.db, push_cmd=push)
