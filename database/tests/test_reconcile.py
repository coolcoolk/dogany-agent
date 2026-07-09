#!/usr/bin/env python3
"""DGN-231: reconcile-before-write tests for lifekit add verbs.

Each verb gets 3 cases: (1) 0-match register (legacy behavior, byte-identical
output), (2) match -> "EXISTS n" + rows + exit 3 (registers nothing), (3) --new
forces the insert past a match. Runs the real CLI end-to-end against a throwaway
lifekit.db built from schema.sql -- no live data is touched.

Run: python3 database/tests/test_reconcile.py   (exit 0 = all pass)
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.dirname(HERE)                 # .../database
SCHEMA = os.path.join(DB_DIR, "schema.sql")
LIFEKIT_SRC = os.path.join(DB_DIR, "lifekit.py")

_failures = []


def _build_instance(tmp):
    """Lay out tmp/database/{lifekit.py, lifekit.db} so the copied CLI resolves
    its own DB_PATH there (SCRIPT_DIR-relative), and seed the two areas."""
    dbdir = os.path.join(tmp, "database")
    os.makedirs(dbdir)
    shutil.copy(LIFEKIT_SRC, os.path.join(dbdir, "lifekit.py"))
    dbpath = os.path.join(dbdir, "lifekit.db")
    conn = sqlite3.connect(dbpath)
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.execute("INSERT INTO areas (name, domain) VALUES ('식습관', '건강');")
    conn.execute("INSERT INTO areas (name, domain) VALUES ('신체건강', '건강');")
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


def test_meal():
    print("meal-add:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        # (1) 0-match register -> legacy shape "id\tname\tkcal"
        rc, out, err = _run(cli, "meal-add", "2026-07-09", "점심", "김치찌개",
                             "10", "20", "5")
        cols = out.strip().split("\t")
        _check("meal 0-match register rc0", rc == 0, f"rc={rc} err={err}")
        _check("meal 0-match 3-col output", len(cols) == 3, out)
        # (2) same (date, slot) -> EXISTS 1 + exit 3, nothing registered
        rc, out, err = _run(cli, "meal-add", "2026-07-09", "점심", "제육볶음")
        _check("meal match exit 3", rc == 3, f"rc={rc}")
        _check("meal match EXISTS header", out.startswith("EXISTS 1"), out)
        _check("meal match echoes existing row", "김치찌개" in out, out)
        # a different slot on the same date is NOT a match
        rc, out, err = _run(cli, "meal-add", "2026-07-09", "저녁", "된장찌개")
        _check("meal other-slot registers rc0", rc == 0, f"rc={rc} out={out}")
        # (3) --new forces past the match
        rc, out, err = _run(cli, "meal-add", "2026-07-09", "점심", "라면", "--new")
        _check("meal --new register rc0", rc == 0, f"rc={rc} err={err}")
        _check("meal --new 3-col output", len(out.strip().split("\t")) == 3, out)


def test_workout():
    print("workout-add:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, err = _run(cli, "workout-add", "2026-07-09", "근력", "벤치프레스",
                             "40", "300")
        _check("workout 0-match register rc0", rc == 0, f"rc={rc} err={err}")
        # rstrip newline only -- the 5th col (avg_hr) is legitimately empty here.
        _check("workout 0-match 5-col output",
               len(out.rstrip("\n").split("\t")) == 5, out)
        # same (date, category) -> EXISTS
        rc, out, err = _run(cli, "workout-add", "2026-07-09", "근력", "스쿼트")
        _check("workout match exit 3", rc == 3, f"rc={rc}")
        _check("workout match EXISTS header", out.startswith("EXISTS 1"), out)
        _check("workout match echoes category", "근력" in out, out)
        # different category same day -> registers
        rc, out, err = _run(cli, "workout-add", "2026-07-09", "유산소", "러닝",
                             "30", "250")
        _check("workout other-category registers rc0", rc == 0,
               f"rc={rc} out={out}")
        # --new forces past the match
        rc, out, err = _run(cli, "workout-add", "2026-07-09", "근력", "데드리프트",
                            "--new")
        _check("workout --new register rc0", rc == 0, f"rc={rc} err={err}")


def test_person():
    print("person-add:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, err = _run(cli, "person-add", "김철수", "친구", "철수,cheolsu")
        _check("person 0-match register rc0", rc == 0, f"rc={rc} err={err}")
        # exact name match -> EXISTS
        rc, out, err = _run(cli, "person-add", "김철수")
        _check("person name-match exit 3", rc == 3, f"rc={rc}")
        _check("person name-match EXISTS header", out.startswith("EXISTS 1"), out)
        # exact alias match -> EXISTS
        rc, out, err = _run(cli, "person-add", "cheolsu")
        _check("person alias-match exit 3", rc == 3, f"rc={rc} out={out}")
        # substring (not exact) is NOT a match: '김' alone registers
        rc, out, err = _run(cli, "person-add", "김")
        _check("person substring registers rc0", rc == 0, f"rc={rc} out={out}")
        # --new forces a duplicate name
        rc, out, err = _run(cli, "person-add", "김철수", "동명이인", "--new")
        _check("person --new register rc0", rc == 0, f"rc={rc} err={err}")


def test_appt():
    print("appt-add:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, err = _run(cli, "appt-add", "치과", "2026-07-10T14:00:00+09:00")
        _check("appt 0-match register rc0", rc == 0, f"rc={rc} err={err}")
        _check("appt 0-match id+title output",
               len(out.strip().split("\t")) == 2, out)
        # same local date (different time) -> EXISTS
        rc, out, err = _run(cli, "appt-add", "미팅", "2026-07-10T16:00:00+09:00")
        _check("appt same-date exit 3", rc == 3, f"rc={rc}")
        _check("appt same-date EXISTS header", out.startswith("EXISTS 1"), out)
        _check("appt same-date echoes existing", "치과" in out, out)
        # a different date registers
        rc, out, err = _run(cli, "appt-add", "회의", "2026-07-11T10:00:00+09:00")
        _check("appt other-date registers rc0", rc == 0, f"rc={rc} out={out}")
        # --new forces a same-date add
        rc, out, err = _run(cli, "appt-add", "저녁약속",
                            "2026-07-10T19:00:00+09:00", "--new")
        _check("appt --new register rc0", rc == 0, f"rc={rc} err={err}")


def main():
    for t in (test_meal, test_workout, test_person, test_appt):
        t()
    print()
    if _failures:
        print(f"FAILED {len(_failures)}: {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
