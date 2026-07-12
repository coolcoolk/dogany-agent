#!/usr/bin/env python3
"""DGN-273: notify policy tests -- migration, verb round-trip, remind
selection, backward compatibility, and a synthetic silent-routine e2e.

Runs the real CLI end-to-end against throwaway lifekit.db instances built
from schema.sql (test_reconcile.py harness pattern) plus direct imports of
the selection engine. No live data is touched.

Run: python3 database/tests/test_notify_policy.py   (exit 0 = all pass)
"""
import datetime
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.dirname(HERE)                 # .../database
SCHEMA = os.path.join(DB_DIR, "schema.sql")
MIG_DIR = os.path.join(DB_DIR, "migrations")

if DB_DIR not in sys.path:
    sys.path.insert(0, DB_DIR)
import lifekit          # noqa: E402
import remind_select    # noqa: E402

_failures = []
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        _failures.append(name)


def _build_instance(tmp):
    """Lay out tmp/database/{lifekit.py + engine modules, lifekit.db} so the
    copied CLI resolves its own DB_PATH there (SCRIPT_DIR-relative)."""
    dbdir = os.path.join(tmp, "database")
    os.makedirs(dbdir)
    for mod in ("lifekit.py", "routine_roller.py", "routine_projection.py",
                "remind_select.py"):
        shutil.copy(os.path.join(DB_DIR, mod), os.path.join(dbdir, mod))
    dbpath = os.path.join(dbdir, "lifekit.db")
    conn = sqlite3.connect(dbpath)
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.execute("INSERT INTO areas (name, domain) VALUES ('신체건강', '건강');")
    conn.commit()
    conn.close()
    return dbdir


def _run(dbdir, *args):
    p = subprocess.run([sys.executable, os.path.join(dbdir, "lifekit.py"),
                        *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _utc(ts):
    return datetime.datetime.fromtimestamp(ts, tz=timezone.utc).strftime(_FMT)


def _apply_pending_migrations(dbpath):
    """Emulate update.sh 3f-migrate: apply every migrations/NNN_*.sql whose
    NNN > the DB's user_version, ascending. Returns the list applied."""
    conn = sqlite3.connect(dbpath)
    cur_ver = conn.execute("PRAGMA user_version;").fetchone()[0]
    applied = []
    for base in sorted(os.listdir(MIG_DIR)):
        m = re.match(r"^(\d{3})_.*\.sql$", base)
        if not m or int(m.group(1)) <= cur_ver:
            continue
        with open(os.path.join(MIG_DIR, base), encoding="utf-8") as f:
            conn.executescript(f.read())
        cur_ver = conn.execute("PRAGMA user_version;").fetchone()[0]
        applied.append(base)
    conn.close()
    return applied


def test_migration():
    """007 applies on a v6-shaped DB, stamps version 7, leaves existing rows
    at NULL (= default behavior), and the update.sh guard loop is idempotent
    (second pass applies nothing)."""
    print("migration 007:")
    with tempfile.TemporaryDirectory() as tmp:
        dbpath = os.path.join(tmp, "lifekit.db")
        conn = sqlite3.connect(dbpath)
        # v6-shaped base: the REAL v6 routine_def DDL (from the 006 migration
        # file) + a minimal event table (007 only ALTERs; the full event DDL
        # is exercised by every other test via schema.sql).
        with open(os.path.join(MIG_DIR, "006_routine_recurrence.sql"),
                  encoding="utf-8") as f:
            sql = f.read()
        # terminator = ');' alone at line start (comments contain inner ');')
        m = re.search(r"CREATE TABLE IF NOT EXISTS routine_def \(.*?\n\);",
                      sql, re.S)
        assert m, "routine_def DDL not found in 006"
        conn.execute("CREATE TABLE event (id INTEGER PRIMARY KEY, "
                     "ulid TEXT, kind TEXT, title TEXT, start_at TEXT, "
                     "settled_at TEXT, schedule_kind TEXT);")
        conn.execute(m.group(0))
        conn.execute("INSERT INTO event (ulid, kind, title) "
                     "VALUES ('u1', 'task', 'pre-existing');")
        conn.execute("PRAGMA user_version = 6;")
        conn.commit()
        conn.close()

        applied = _apply_pending_migrations(dbpath)
        _check("first pass applies exactly 007",
               applied == ["007_notify_policy.sql"], str(applied))
        conn = sqlite3.connect(dbpath)
        ver = conn.execute("PRAGMA user_version;").fetchone()[0]
        _check("user_version stamped 7", ver == 7, f"ver={ver}")
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(event);")}
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(routine_def);")}
        _check("event notify columns added",
               {"notify_policy", "notify_lead_min"} <= ecols, str(ecols))
        _check("routine_def notify columns added",
               {"notify_policy", "notify_lead_min"} <= rcols, str(rcols))
        row = conn.execute("SELECT notify_policy, notify_lead_min FROM event "
                           "WHERE ulid='u1';").fetchone()
        _check("pre-existing row lands at NULL (= default behavior)",
               row == (None, None), str(row))
        conn.close()
        applied2 = _apply_pending_migrations(dbpath)
        _check("second pass is a no-op (guard idempotence)",
               applied2 == [], str(applied2))

    # fresh schema.sql DB is born at 7 -> the loop must apply nothing.
    with tempfile.TemporaryDirectory() as tmp:
        dbdir = _build_instance(tmp)
        applied = _apply_pending_migrations(os.path.join(dbdir, "lifekit.db"))
        _check("fresh schema DB gets no migrations", applied == [],
               str(applied))


def _def_notify(dbdir, token):
    conn = sqlite3.connect(os.path.join(dbdir, "lifekit.db"))
    row = conn.execute("SELECT notify_policy, notify_lead_min FROM "
                       "routine_def WHERE ulid=?;", (token,)).fetchone()
    conn.close()
    return row


def _instance_notify(dbdir, rid):
    conn = sqlite3.connect(os.path.join(dbdir, "lifekit.db"))
    rows = conn.execute(
        "SELECT DISTINCT notify_policy, notify_lead_min FROM event "
        "WHERE recurrence_id=? AND settled_at IS NULL;", (rid,)).fetchall()
    conn.close()
    return rows


def test_routine_verbs():
    print("routine verbs:")
    with tempfile.TemporaryDirectory() as tmp:
        dbdir = _build_instance(tmp)
        # silent register: def carries it, instances are stamped.
        rc, out, err = _run(dbdir, "routine", "add", "quiet walk", "D",
                            "time=06:00", "notify=silent")
        _check("routine add notify=silent rc0", rc == 0, f"rc={rc} err={err}")
        ulid = out.strip().split("\t")[1]
        _check("def stores silent", _def_notify(dbdir, ulid) == ("silent", None),
               str(_def_notify(dbdir, ulid)))
        _check("instances stamped silent",
               _instance_notify(dbdir, "rd:%s" % ulid) == [("silent", None)],
               str(_instance_notify(dbdir, "rd:%s" % ulid)))
        # show prints the pair
        rc, out, err = _run(dbdir, "routine", "show", ulid)
        _check("show prints notify_policy=silent",
               "notify_policy=silent" in out, out)
        # custom register via bare minutes
        rc, out, err = _run(dbdir, "routine", "add", "morning run", "D",
                            "time=06:30", "notify=45")
        _check("routine add notify=45 rc0", rc == 0, f"rc={rc} err={err}")
        ulid2 = out.strip().split("\t")[1]
        _check("def stores custom/45",
               _def_notify(dbdir, ulid2) == ("custom", 45),
               str(_def_notify(dbdir, ulid2)))
        _check("instances stamped custom/45",
               _instance_notify(dbdir, "rd:%s" % ulid2) == [("custom", 45)],
               str(_instance_notify(dbdir, "rd:%s" % ulid2)))
        # default register leaves NULLs (backward-compatible shape)
        rc, out, err = _run(dbdir, "routine", "add", "stretching", "D",
                            "time=07:00")
        _check("routine add w/o notify rc0", rc == 0, f"rc={rc} err={err}")
        ulid3 = out.strip().split("\t")[1]
        _check("def w/o notify stays NULL",
               _def_notify(dbdir, ulid3) == (None, None),
               str(_def_notify(dbdir, ulid3)))
        _check("instances w/o notify stay NULL",
               _instance_notify(dbdir, "rd:%s" % ulid3) == [(None, None)],
               str(_instance_notify(dbdir, "rd:%s" % ulid3)))
        # update: silent def -> start_only; regen re-stamps the instances.
        rc, out, err = _run(dbdir, "routine", "update", ulid,
                            "notify=start_only")
        _check("routine update notify=start_only rc0", rc == 0,
               f"rc={rc} err={err}")
        _check("def updated to start_only",
               _def_notify(dbdir, ulid) == ("start_only", None),
               str(_def_notify(dbdir, ulid)))
        _check("instances re-stamped start_only",
               _instance_notify(dbdir, "rd:%s" % ulid) ==
               [("start_only", None)],
               str(_instance_notify(dbdir, "rd:%s" % ulid)))
        # update: reset custom def back to default via notify=
        rc, out, err = _run(dbdir, "routine", "update", ulid2, "notify=")
        _check("routine update notify= (reset) rc0", rc == 0,
               f"rc={rc} err={err}")
        _check("def reset to NULL",
               _def_notify(dbdir, ulid2) == (None, None),
               str(_def_notify(dbdir, ulid2)))
        # bad value is a loud one-liner
        rc, out, err = _run(dbdir, "routine", "add", "bogus", "D",
                            "time=08:00", "notify=sometimes")
        _check("routine add bad notify rc1", rc == 1 and "notify" in err,
               f"rc={rc} err={err}")


def _event_notify_by(dbdir, col, val):
    conn = sqlite3.connect(os.path.join(dbdir, "lifekit.db"))
    row = conn.execute(
        "SELECT notify_policy, notify_lead_min FROM event WHERE %s=?;" % col,
        (val,)).fetchone()
    conn.close()
    return row


def test_event_verbs():
    print("event verbs (one-off override):")
    with tempfile.TemporaryDirectory() as tmp:
        dbdir = _build_instance(tmp)
        rc, out, err = _run(dbdir, "task-add", "pay bill", "2099-01-05", "",
                            "--notify", "silent")
        _check("task-add --notify silent rc0", rc == 0, f"rc={rc} err={err}")
        tid = out.strip().split("\t")[0]
        _check("task row stores silent",
               _event_notify_by(dbdir, "id", tid) == ("silent", None),
               str(_event_notify_by(dbdir, "id", tid)))
        rc, out, err = _run(dbdir, "task-add", "watering", "2099-01-06", "",
                            "--notify", "15")
        _check("task-add --notify 15 rc0", rc == 0, f"rc={rc} err={err}")
        tid2 = out.strip().split("\t")[0]
        _check("task row stores custom/15",
               _event_notify_by(dbdir, "id", tid2) == ("custom", 15),
               str(_event_notify_by(dbdir, "id", tid2)))
        rc, out, err = _run(dbdir, "task-add", "plain task", "2099-01-07")
        _check("plain task-add rc0 (legacy path untouched)", rc == 0,
               f"rc={rc} err={err}")
        tid3 = out.strip().split("\t")[0]
        _check("plain task stays NULL",
               _event_notify_by(dbdir, "id", tid3) == (None, None),
               str(_event_notify_by(dbdir, "id", tid3)))
        rc, out, err = _run(dbdir, "appt-add", "dentist",
                            "2099-01-08T14:00:00+09:00",
                            "--notify", "start_only")
        _check("appt-add --notify start_only rc0", rc == 0,
               f"rc={rc} err={err}")
        aid = out.strip().split("\t")[0]
        _check("appt row stores start_only",
               _event_notify_by(dbdir, "id", aid) == ("start_only", None),
               str(_event_notify_by(dbdir, "id", aid)))
        # event-notify: override after the fact + reset
        rc, out, err = _run(dbdir, "event-notify", tid3, "30")
        _check("event-notify <id> 30 rc0", rc == 0, f"rc={rc} err={err}")
        _check("event-notify stored custom/30",
               _event_notify_by(dbdir, "id", tid3) == ("custom", 30),
               str(_event_notify_by(dbdir, "id", tid3)))
        rc, out, err = _run(dbdir, "event-notify", tid3, "")
        _check("event-notify reset rc0", rc == 0, f"rc={rc} err={err}")
        _check("event-notify reset to NULL",
               _event_notify_by(dbdir, "id", tid3) == (None, None),
               str(_event_notify_by(dbdir, "id", tid3)))
        rc, out, err = _run(dbdir, "event-notify", tid3, "never")
        _check("event-notify bad value rc1", rc == 1 and "notify" in err,
               f"rc={rc} err={err}")
        # event-window appends the notify columns (cols 7-8; timed rows only,
        # so the dentist appointment is the probe). No strip(): the trailing
        # lead column is legitimately empty here.
        rc, out, err = _run(dbdir, "event-window", "2099-01-04T00:00:00Z",
                            "2099-01-09T00:00:00Z")
        lines = [l.split("\t") for l in out.splitlines() if l]
        _check("event-window emits 8 cols",
               lines and all(len(l) == 8 for l in lines), out)
        by_title = {l[2]: (l[6], l[7]) for l in lines}
        _check("event-window carries the policy",
               by_title.get("dentist") == ("start_only", ""), str(by_title))


def _mk_event(conn, kind, title, start_ts, policy=None, lead=None):
    return lifekit.event_add(
        conn, kind=kind, title=title, schedule_kind="timed",
        start_at=_utc(start_ts), end_at=_utc(start_ts + 1800),
        owning_agent="test", created_by="test",
        completion_rule="manual",
        notify_policy=policy, notify_lead_min=lead)


def test_remind_selection():
    print("remind selection:")
    now_ts = 4102444800  # 2100-01-01T00:00:00Z, fixed
    with tempfile.TemporaryDirectory() as tmp:
        dbdir = _build_instance(tmp)
        conn = lifekit.event_conn(os.path.join(dbdir, "lifekit.db"))
        # rows (spaced hours apart -- appointments are slot-exclusive):
        _mk_event(conn, "task", "t-default-lead", now_ts + 30 * 60)
        _mk_event(conn, "task", "t-default-start", now_ts + 60)
        _mk_event(conn, "task", "t-silent-lead", now_ts + 30 * 60,
                  "silent")
        _mk_event(conn, "task", "t-silent-start", now_ts + 60, "silent")
        _mk_event(conn, "task", "t-startonly-lead", now_ts + 30 * 60,
                  "start_only")
        _mk_event(conn, "task", "t-startonly-start", now_ts + 120,
                  "start_only")
        _mk_event(conn, "task", "t-custom-45", now_ts + 45 * 60,
                  "custom", 45)
        _mk_event(conn, "task", "t-custom-not30", now_ts + 30 * 60,
                  "custom", 45)   # custom REPLACES the 30-min default
        _mk_event(conn, "task", "t-outside", now_ts + 40 * 60)  # no alert
        _mk_event(conn, "appointment", "a-default-lead", now_ts + 120 * 60)
        _mk_event(conn, "appointment", "a-start", now_ts + 30)
        _mk_event(conn, "appointment", "a-silent", now_ts + 125 * 60 + 3600,
                  "silent")
        # explicit 'default' string == NULL behavior
        _mk_event(conn, "task", "t-explicit-default", now_ts + 31 * 60,
                  "default")
        alerts = remind_select.select_alerts(conn, now_ts)
        got = {(a["title"], a["alert"]) for a in alerts}
        want = {
            ("t-default-lead", "lead"), ("t-default-start", "start"),
            ("t-startonly-start", "start"),
            ("t-custom-45", "lead"),
            ("a-default-lead", "lead"), ("a-start", "start"),
            ("t-explicit-default", "lead"),
        }
        _check("selection set matches policy table", got == want,
               f"got={sorted(got)} want={sorted(want)}")
        by_title = {(a["title"], a["alert"]): a for a in alerts}
        _check("custom lead minutes carried",
               by_title[("t-custom-45", "lead")]["lead_min"] == 45,
               str(by_title.get(("t-custom-45", "lead"))))
        _check("appt default lead is 120",
               by_title[("a-default-lead", "lead")]["lead_min"] == 120,
               str(by_title.get(("a-default-lead", "lead"))))
        _check("marker keys keep the live prefixes",
               by_title[("a-default-lead", "lead")]["key"].startswith("appt:")
               and by_title[("a-start", "start")]["key"].startswith("appt_start:")
               and by_title[("t-default-lead", "lead")]["key"].startswith("task:")
               and by_title[("t-default-start", "start")]["key"].startswith("task_start:"),
               str(sorted(a["key"] for a in alerts)))
        conn.close()

    # backward compatibility: a pre-007 DB (no notify columns) must keep
    # alerting with the legacy defaults instead of crashing or going dark.
    with tempfile.TemporaryDirectory() as tmp:
        dbpath = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(dbpath)
        conn.execute(
            "CREATE TABLE event (id INTEGER PRIMARY KEY, ulid TEXT, "
            "kind TEXT, title TEXT, start_at TEXT, end_at TEXT, "
            "display_tz TEXT DEFAULT 'Asia/Seoul', location TEXT, "
            "purpose TEXT, settled_at TEXT, schedule_kind TEXT);")
        conn.execute(
            "INSERT INTO event (ulid, kind, title, start_at, schedule_kind) "
            "VALUES ('u-old', 'task', 'old-task', ?, 'timed');",
            (_utc(now_ts + 30 * 60),))
        conn.commit()
        alerts = remind_select.select_alerts(conn, now_ts)
        _check("pre-007 DB: legacy default alert still selected",
               [(a["title"], a["alert"]) for a in alerts] ==
               [("old-task", "lead")],
               str(alerts))
        conn.close()


def test_e2e_silent_routine():
    """Ticket verification line: register a silent routine, roll instances,
    run remind selection over a window covering an instance -> zero alerts.
    Control: the same shape with default policy DOES alert (proves the
    window covers the instance)."""
    print("e2e silent routine:")
    for policy_arg, expect_alert, label in (
            ([], True, "control default routine alerts"),
            (["notify=silent"], False, "silent routine selects ZERO alerts")):
        with tempfile.TemporaryDirectory() as tmp:
            dbdir = _build_instance(tmp)
            local_now = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
            tod = (local_now + timedelta(minutes=31)).strftime("%H:%M")
            rc, out, err = _run(dbdir, "routine", "add", "commute", "D",
                                "time=%s" % tod, *policy_arg)
            assert rc == 0, err
            ulid = out.strip().split("\t")[0:2][1]
            conn = sqlite3.connect(os.path.join(dbdir, "lifekit.db"))
            row = conn.execute(
                "SELECT start_at FROM event WHERE recurrence_id=? "
                "ORDER BY start_at LIMIT 1;", ("rd:%s" % ulid,)).fetchone()
            conn.close()
            assert row, "no instance materialized"
            start_ts = int(datetime.datetime.strptime(row[0], _FMT)
                           .replace(tzinfo=timezone.utc).timestamp())
            # synthetic poll instant = the default 30-min lead moment
            probe = start_ts - 30 * 60
            p = subprocess.run(
                [sys.executable, os.path.join(dbdir, "remind_select.py"),
                 "--db", os.path.join(dbdir, "lifekit.db"),
                 "--now", str(probe)],
                capture_output=True, text=True)
            _check("remind_select CLI rc0 (%s)" % label, p.returncode == 0,
                   p.stderr)
            hits = [l for l in p.stdout.splitlines() if "commute" in l]
            if expect_alert:
                _check(label, len(hits) == 1 and "\tlead\t" in hits[0],
                       p.stdout or "(no output)")
            else:
                _check(label, hits == [], p.stdout)
                # also probe the start instant: still zero
                p2 = subprocess.run(
                    [sys.executable, os.path.join(dbdir, "remind_select.py"),
                     "--db", os.path.join(dbdir, "lifekit.db"),
                     "--now", str(start_ts)],
                    capture_output=True, text=True)
                _check("silent routine: zero at start instant too",
                       "commute" not in p2.stdout, p2.stdout)


def main():
    for t in (test_migration, test_routine_verbs, test_event_verbs,
              test_remind_selection, test_e2e_silent_routine):
        t()
    print()
    if _failures:
        print(f"FAILED {len(_failures)}: {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
