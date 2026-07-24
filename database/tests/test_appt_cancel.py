#!/usr/bin/env python3
"""DGN-559: test suite for appt-cancel / appt-del verbs.

Covers: cancel happy path (rc0, output cols, settled_outcome='abandoned');
drop from appt-find after cancel; re-cancel rejection (already settled,
rc!=0); missing id (rc!=0); appt-del alias (same operation as appt-cancel);
ulid-addressed cancel.

Harness: temp copy of lifekit.py + temp lifekit.db from schema.sql (v9).
Never touches any live DB. Exit 0 = ALL PASS.

Run: python3 database/tests/test_appt_cancel.py
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.dirname(HERE)       # .../database
SCHEMA = os.path.join(DB_DIR, "schema.sql")
LIFEKIT_SRC = os.path.join(DB_DIR, "lifekit.py")

_failures = []


# ── harness helpers ──────────────────────────────────────────


def _build_instance(tmp):
    """Stand up tmp/database/{lifekit.py, lifekit.db} from schema.sql v9.
    Returns path to the CLI copy."""
    dbdir = os.path.join(tmp, "database")
    os.makedirs(dbdir)
    shutil.copy(LIFEKIT_SRC, os.path.join(dbdir, "lifekit.py"))
    dbpath = os.path.join(dbdir, "lifekit.db")
    conn = sqlite3.connect(dbpath)
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    return os.path.join(dbdir, "lifekit.py")


def _run(cli, *args):
    """Run the CLI, return (returncode, stdout, stderr)."""
    p = subprocess.run([sys.executable, cli, *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        _failures.append(name)


def _db(cli):
    """Return a fresh sqlite3 connection to the temp DB next to cli."""
    return sqlite3.connect(os.path.join(os.path.dirname(cli), "lifekit.db"))


def _seed_appt(cli, title, ulid, start_at="2026-07-25T10:00:00Z",
               end_at="2026-07-25T11:00:00Z"):
    """Insert a bare appointment row directly into the temp DB.
    Returns the integer id assigned by SQLite."""
    now = "2026-07-25T00:00:00Z"
    conn = _db(cli)
    cur = conn.execute(
        "INSERT INTO event "
        "(ulid, kind, title, schedule_kind, start_at, end_at, "
        " display_tz, slot_exclusive, owning_agent, created_by, "
        " created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?);",
        (ulid, "appointment", title, "timed", start_at, end_at,
         "Asia/Seoul", 1, "test-agent", "test-agent", now, now))
    conn.commit()
    eid = cur.lastrowid
    conn.close()
    return eid


# ── 1. cancel happy path ─────────────────────────────────────


def test_cancel_happy():
    """appt-cancel <id> rc=0, output = id<TAB>title<TAB>cancelled,
    settled_outcome = 'abandoned' in DB."""
    print("appt-cancel happy path:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        eid = _seed_appt(cli, "치과 예약", "01J9AAAA0000000000000001")
        rc, out, err = _run(cli, "appt-cancel", str(eid))
        _check("rc0", rc == 0, f"rc={rc} err={err}")
        cols = out.strip().split("\t")
        _check("3-col output", len(cols) == 3, out)
        _check("id in output", cols[0] == str(eid), out)
        _check("title in output", cols[1] == "치과 예약", out)
        _check("cancelled label", cols[2] == "cancelled", out)
        conn = _db(cli)
        row = conn.execute(
            "SELECT settled_outcome FROM event WHERE id=?;", (eid,)).fetchone()
        conn.close()
        _check("settled_outcome=abandoned", row is not None and row[0] == "abandoned",
               str(row))


# ── 2. drop from appt-find after cancel ─────────────────────


def test_drop_from_appt_find():
    """After cancel, the appointment must not appear in appt-find."""
    print("drop from appt-find after cancel:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        eid = _seed_appt(cli, "기타 연습", "01J9AAAA0000000000000002",
                         start_at="2026-07-25T10:00:00Z",
                         end_at="2026-07-25T11:00:00Z")
        rc_find_before, out_before, _ = _run(cli, "appt-find", "2026-07-25")
        _check("visible before cancel", str(eid) in out_before,
               f"id={eid} out={out_before!r}")
        rc_cancel, _, err_cancel = _run(cli, "appt-cancel", str(eid))
        _check("cancel rc0", rc_cancel == 0, f"err={err_cancel}")
        rc_find_after, out_after, _ = _run(cli, "appt-find", "2026-07-25")
        _check("absent from appt-find after cancel",
               str(eid) not in out_after,
               f"out_after={out_after!r}")


# ── 3. re-cancel rejection ───────────────────────────────────


def test_recancel_rejection():
    """Cancelling an already-cancelled appointment must exit non-zero."""
    print("re-cancel rejection:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        eid = _seed_appt(cli, "미용실", "01J9AAAA0000000000000003")
        _run(cli, "appt-cancel", str(eid))      # first cancel
        rc, out, err = _run(cli, "appt-cancel", str(eid))   # second
        _check("rc!=0 on re-cancel", rc != 0, f"rc={rc} out={out!r}")
        _check("error mentions already cancelled",
               "이미 취소된" in err or "이미 취소된" in out,
               f"err={err!r}")


# ── 4. missing id ────────────────────────────────────────────


def test_missing_id():
    """appt-cancel on a nonexistent id must exit non-zero."""
    print("missing id:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, err = _run(cli, "appt-cancel", "9999")
        _check("rc!=0 on missing id", rc != 0, f"rc={rc} out={out!r}")
        _check("error mentions 약속 없음",
               "약속 없음" in err or "약속 없음" in out,
               f"err={err!r}")


# ── 5. appt-del alias ────────────────────────────────────────


def test_appt_del_alias():
    """appt-del must cancel the appointment exactly like appt-cancel."""
    print("appt-del alias:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        eid = _seed_appt(cli, "헬스 PT", "01J9AAAA0000000000000004")
        rc, out, err = _run(cli, "appt-del", str(eid))
        _check("rc0", rc == 0, f"rc={rc} err={err}")
        cols = out.strip().split("\t")
        _check("3-col output", len(cols) == 3, out)
        _check("cancelled label", cols[2] == "cancelled", out)
        conn = _db(cli)
        row = conn.execute(
            "SELECT settled_outcome FROM event WHERE id=?;", (eid,)).fetchone()
        conn.close()
        _check("settled_outcome=abandoned via appt-del",
               row is not None and row[0] == "abandoned", str(row))


# ── 6. ulid-addressed cancel ─────────────────────────────────


def test_ulid_addressed():
    """appt-cancel accepts a ulid token (non-digit string) and resolves it."""
    print("ulid-addressed cancel:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        ulid = "01J9BBBB0000000000000001"
        eid = _seed_appt(cli, "독서 모임", ulid)
        rc, out, err = _run(cli, "appt-cancel", ulid)
        _check("rc0", rc == 0, f"rc={rc} err={err}")
        cols = out.strip().split("\t")
        _check("3-col output", len(cols) == 3, out)
        _check("id in output", cols[0] == str(eid), out)
        conn = _db(cli)
        row = conn.execute(
            "SELECT settled_outcome FROM event WHERE id=?;", (eid,)).fetchone()
        conn.close()
        _check("settled_outcome=abandoned via ulid", row is not None and row[0] == "abandoned",
               str(row))


# ── 7. no-arg usage error ────────────────────────────────────


def test_no_arg_usage_error():
    """appt-cancel with no argument must exit non-zero."""
    print("no-arg usage error:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, err = _run(cli, "appt-cancel")
        _check("rc!=0 on no arg", rc != 0, f"rc={rc}")


# ── runner ───────────────────────────────────────────────────


if __name__ == "__main__":
    test_cancel_happy()
    test_drop_from_appt_find()
    test_recancel_rejection()
    test_missing_id()
    test_appt_del_alias()
    test_ulid_addressed()
    test_no_arg_usage_error()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} test(s) failed: {_failures}")
        sys.exit(1)
    else:
        print("ALL PASS")
        sys.exit(0)
