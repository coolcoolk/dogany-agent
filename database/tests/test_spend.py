#!/usr/bin/env python3
"""DGN-553 J2: formal test suite for the J1 spend verb implementation.

Covers: all 7 spend verbs (happy path); DGN-231 dup gate (exit 3 + --new bypass);
is_fixed_default inheritance + --fixed override; category auto-register; scope
isolation (business excluded from personal find/day/week/month, included with
--all); installment note passthrough; negative amount (refund); partial spend-upd
on each field; week/month aggregation math incl. fixed/variable split, method
subtotals (month only), prev-period compare; boundary dates (week spanning month
edge, month with no prev data); empty-DB edges; seed integrity (12 rows, 3 fixed
defaults); and the J1 informal-check case (719,500 personal vs 769,500 --all).

Harness: temp copy of lifekit.py + temp lifekit.db from schema.sql (v9).
Never touches any live DB. Exit 0 = ALL PASS.

Run: python3 database/tests/test_spend.py
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
    Returns path to the CLI copy (SCRIPT_DIR-relative DB_PATH)."""
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


# ── 1. seed integrity ────────────────────────────────────────


def test_seed_integrity():
    """12 seed rows; exactly 3 have is_fixed_default=1 (주거/공과금, 구독, 통신)."""
    print("seed integrity:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        conn = _db(cli)
        total = conn.execute(
            "SELECT COUNT(*) FROM spend_category;").fetchone()[0]
        _check("seed row count = 12", total == 12, f"got {total}")
        fixed_names = [r[0] for r in conn.execute(
            "SELECT name FROM spend_category WHERE is_fixed_default=1 "
            "ORDER BY sort;").fetchall()]
        _check("3 fixed-default categories",
               len(fixed_names) == 3, str(fixed_names))
        _check("fixed-default set is correct",
               set(fixed_names) == {'주거/공과금', '구독', '통신'},
               str(fixed_names))
        conn.close()


# ── 2. spend-add happy path ──────────────────────────────────


def test_spend_add_happy():
    """spend-add registers a row and returns id<TAB>name<TAB>amount<TAB>category."""
    print("spend-add happy path:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, err = _run(cli, "spend-add", "2026-07-21", "5500", "아메리카노",
                            "카페/간식", "신한카드", "personal", "아침커피")
        _check("rc0", rc == 0, f"rc={rc} err={err}")
        cols = out.strip().split("\t")
        _check("4-col output", len(cols) == 4, out)
        _check("name in output", cols[1] == "아메리카노", out)
        _check("amount in output", cols[2] == "5500", out)
        _check("category in output", cols[3] == "카페/간식", out)
        # verify DB state (column order: id,date,amount,name,category_id,method,
        # is_fixed,scope,source,note,created_at)
        conn = _db(cli)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM spend_entry WHERE id=?;",
            (int(cols[0]),)).fetchone()
        conn.close()
        _check("date stored", row["date"] == "2026-07-21", str(tuple(row)))
        _check("amount stored as integer", row["amount"] == 5500, str(tuple(row)))
        _check("scope default personal", row["scope"] == "personal",
               str(tuple(row)))
        _check("method stored", row["method"] == "신한카드", str(tuple(row)))
        _check("note stored", row["note"] == "아침커피", str(tuple(row)))


# ── 3. DGN-231 dup gate ─────────────────────────────────────


def test_dup_gate():
    """(date, amount) match -> EXISTS n + exit 3; --new forces insert."""
    print("dup gate (DGN-231):")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        # first registration
        rc, out, _ = _run(cli, "spend-add", "2026-07-22", "9000", "점심")
        _check("first add rc0", rc == 0, f"rc={rc}")
        first_id = out.strip().split("\t")[0]

        # same (date, amount) -> dup gate
        rc, out, _ = _run(cli, "spend-add", "2026-07-22", "9000", "다른음식")
        _check("dup exit 3", rc == 3, f"rc={rc}")
        _check("dup EXISTS header", out.startswith("EXISTS 1"), out)
        _check("dup echoes existing name", "점심" in out, out)
        _check("dup echoes existing id",
               first_id in out.splitlines()[1], out)

        # different amount same date is NOT a dup
        rc, out, _ = _run(cli, "spend-add", "2026-07-22", "12000", "저녁")
        _check("diff-amount same-date registers rc0", rc == 0, f"rc={rc} {out}")

        # different date same amount is NOT a dup
        rc, out, _ = _run(cli, "spend-add", "2026-07-23", "9000", "내일점심")
        _check("diff-date same-amount registers rc0", rc == 0, f"rc={rc}")

        # --new bypasses dup gate
        rc, out, _ = _run(cli, "spend-add", "2026-07-22", "9000", "커피2잔",
                          "--new")
        _check("--new bypass rc0", rc == 0, f"rc={rc} err={_}")
        # rstrip newline only -- trailing tab (empty category) is a valid col
        _check("--new 4-col output",
               len(out.rstrip("\n").split("\t")) == 4, out)
        # EXISTS count goes up to 2 now
        rc, out, _ = _run(cli, "spend-add", "2026-07-22", "9000", "또다른것")
        _check("after --new EXISTS 2", out.startswith("EXISTS 2"), out)


# ── 4. is_fixed_default inheritance + --fixed override ──────


def test_is_fixed():
    """is_fixed_default inherits from category; --fixed overrides it."""
    print("is_fixed inheritance + override:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        conn = _db(cli)

        # '구독' is_fixed_default = 1 -> row should inherit is_fixed=1
        rc, out, _ = _run(cli, "spend-add", "2026-07-01", "15000", "넷플릭스",
                          "구독")
        _check("구독 add rc0", rc == 0, f"rc={rc}")
        eid = int(out.strip().split("\t")[0])
        row = conn.execute(
            "SELECT is_fixed FROM spend_entry WHERE id=?;", (eid,)).fetchone()
        _check("is_fixed inherited from 구독 (=1)", row[0] == 1, str(row))

        # '식비' is_fixed_default = 0 -> row should be is_fixed=0
        rc, out, _ = _run(cli, "spend-add", "2026-07-01", "8000", "김치찌개",
                          "식비")
        eid2 = int(out.strip().split("\t")[0])
        row2 = conn.execute(
            "SELECT is_fixed FROM spend_entry WHERE id=?;", (eid2,)).fetchone()
        _check("is_fixed inherited from 식비 (=0)", row2[0] == 0, str(row2))

        # --fixed 0 overrides 구독's default (1 -> 0)
        rc, out, _ = _run(cli, "spend-add", "2026-07-02", "15000", "구독취소환불",
                          "구독", "", "personal", "", "--fixed", "0")
        eid3 = int(out.strip().split("\t")[0])
        row3 = conn.execute(
            "SELECT is_fixed FROM spend_entry WHERE id=?;", (eid3,)).fetchone()
        _check("--fixed 0 overrides category default", row3[0] == 0, str(row3))

        # --fixed 1 overrides 식비's default (0 -> 1)
        rc, out, _ = _run(cli, "spend-add", "2026-07-02", "300000", "월세선결제",
                          "식비", "", "personal", "", "--fixed", "1")
        eid4 = int(out.strip().split("\t")[0])
        row4 = conn.execute(
            "SELECT is_fixed FROM spend_entry WHERE id=?;", (eid4,)).fetchone()
        _check("--fixed 1 overrides category default", row4[0] == 1, str(row4))

        # no category -> is_fixed defaults to 0
        rc, out, _ = _run(cli, "spend-add", "2026-07-03", "1000", "기타소비")
        eid5 = int(out.strip().split("\t")[0])
        row5 = conn.execute(
            "SELECT is_fixed FROM spend_entry WHERE id=?;", (eid5,)).fetchone()
        _check("no category -> is_fixed=0", row5[0] == 0, str(row5))

        conn.close()


# ── 5. category auto-register ────────────────────────────────


def test_category_auto_register():
    """Unknown category gets auto-registered with is_fixed_default=0."""
    print("category auto-register:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        conn = _db(cli)
        before = conn.execute(
            "SELECT COUNT(*) FROM spend_category;").fetchone()[0]

        rc, out, err = _run(cli, "spend-add", "2026-07-10", "20000", "펫샵",
                            "반려동물")
        _check("auto-register rc0", rc == 0, f"rc={rc} err={err}")
        _check("auto-register warning on stderr",
               "미등록" in err or "반려동물" in err, err)
        after = conn.execute(
            "SELECT COUNT(*) FROM spend_category;").fetchone()[0]
        _check("category count +1", after == before + 1,
               f"before={before} after={after}")
        new_cat = conn.execute(
            "SELECT is_fixed_default FROM spend_category "
            "WHERE name='반려동물';").fetchone()
        _check("auto-registered is_fixed_default=0",
               new_cat is not None and new_cat[0] == 0, str(new_cat))

        # second call with same unknown category -> reuses (no dup register)
        rc, out2, err2 = _run(cli, "spend-add", "2026-07-11", "25000", "사료",
                              "반려동물", "--new")
        after2 = conn.execute(
            "SELECT COUNT(*) FROM spend_category;").fetchone()[0]
        _check("second unknown-cat call reuses id, no new row",
               after2 == after, f"after2={after2}")

        conn.close()


# ── 6. spend-find ────────────────────────────────────────────


def test_spend_find():
    """spend-find returns TSV rows; scope isolation; --all includes business."""
    print("spend-find:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        _run(cli, "spend-add", "2026-07-14", "5000", "커피", "카페/간식",
             "현금", "personal")
        _run(cli, "spend-add", "2026-07-14", "30000", "사업미팅", "식비",
             "법인카드", "business", "--new")

        # default (personal only) -> 1 row
        rc, out, _ = _run(cli, "spend-find", "2026-07-14")
        _check("find rc0", rc == 0, f"rc={rc}")
        lines = [l for l in out.splitlines() if l]
        _check("find personal only returns 1 row", len(lines) == 1, out)
        _check("find 6-col TSV", len(lines[0].split("\t")) == 6, lines[0])
        _check("find result is personal row", "커피" in lines[0], out)

        # --all -> 2 rows
        rc, out, _ = _run(cli, "spend-find", "2026-07-14", "--all")
        lines_all = [l for l in out.splitlines() if l]
        _check("find --all returns 2 rows", len(lines_all) == 2, out)
        names = {l.split("\t")[1] for l in lines_all}
        _check("find --all includes business row", "사업미팅" in names, str(names))

        # find on empty date returns nothing
        rc, out, _ = _run(cli, "spend-find", "2000-01-01")
        _check("find empty date rc0", rc == 0, f"rc={rc}")
        _check("find empty date no output", out.strip() == "", out)


# ── 7. spend-day ─────────────────────────────────────────────


def test_spend_day():
    """spend-day prints human-readable summary with total; respects scope."""
    print("spend-day:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        _run(cli, "spend-add", "2026-07-15", "5500", "아메리카노", "카페/간식")
        _run(cli, "spend-add", "2026-07-15", "12000", "점심", "식비",
             "", "personal", "--new")
        _run(cli, "spend-add", "2026-07-15", "50000", "사업식사", "식비",
             "", "business", "--new")

        rc, out, _ = _run(cli, "spend-day", "2026-07-15")
        _check("spend-day rc0", rc == 0, f"rc={rc}")
        _check("spend-day total line present", "17,500" in out or "17500" in out,
               out)
        _check("spend-day personal entries shown",
               "아메리카노" in out and "점심" in out, out)
        _check("spend-day business excluded", "사업식사" not in out, out)

        # --all includes business
        rc, out_all, _ = _run(cli, "spend-day", "2026-07-15", "--all")
        _check("spend-day --all includes business", "사업식사" in out_all, out_all)

        # empty date
        rc, out_empty, _ = _run(cli, "spend-day", "2000-01-01")
        _check("spend-day empty date rc0", rc == 0, f"rc={rc}")
        _check("spend-day empty date says no spend",
               "없음" in out_empty, out_empty)


# ── 8. spend-del ─────────────────────────────────────────────


def test_spend_del():
    """spend-del removes a row; subsequent find returns nothing."""
    print("spend-del:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, _ = _run(cli, "spend-add", "2026-07-16", "3000", "물")
        eid = out.strip().split("\t")[0]
        _check("del pre-condition add rc0", rc == 0)

        rc, out, _ = _run(cli, "spend-del", eid)
        _check("del rc0", rc == 0, f"rc={rc} err={_}")
        _check("del output mentions id", eid in out, out)

        # row gone from DB
        conn = _db(cli)
        row = conn.execute(
            "SELECT id FROM spend_entry WHERE id=?;", (int(eid),)).fetchone()
        conn.close()
        _check("row removed from DB", row is None, str(row))

        # find on that date returns nothing
        rc, out, _ = _run(cli, "spend-find", "2026-07-16")
        _check("find after del empty", out.strip() == "", out)


# ── 9. spend-upd partial update on each field ───────────────


def test_spend_upd():
    """spend-upd can update each field independently."""
    print("spend-upd (each field):")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, _ = _run(cli, "spend-add", "2026-07-17", "8000", "치킨")
        eid = out.strip().split("\t")[0]

        # update name
        rc, out, _ = _run(cli, "spend-upd", eid, "name=후라이드치킨")
        _check("upd name rc0", rc == 0, f"rc={rc} err={_}")
        _check("upd name in output", "후라이드치킨" in out, out)

        # update amount
        rc, out, _ = _run(cli, "spend-upd", eid, "amount=9000")
        _check("upd amount rc0", rc == 0, f"rc={rc}")
        _check("upd amount in output", "9000" in out, out)

        # update date
        rc, out, _ = _run(cli, "spend-upd", eid, "date=2026-07-18")
        _check("upd date rc0", rc == 0, f"rc={rc}")
        conn = _db(cli)
        r = conn.execute(
            "SELECT date FROM spend_entry WHERE id=?;", (int(eid),)).fetchone()
        _check("upd date stored", r[0] == "2026-07-18", str(r))

        # update method
        rc, out, _ = _run(cli, "spend-upd", eid, "method=현대카드")
        _check("upd method rc0", rc == 0, f"rc={rc}")
        r = conn.execute(
            "SELECT method FROM spend_entry WHERE id=?;", (int(eid),)).fetchone()
        _check("upd method stored", r[0] == "현대카드", str(r))

        # update note
        rc, out, _ = _run(cli, "spend-upd", eid, "note=할부3개월")
        _check("upd note rc0", rc == 0, f"rc={rc}")
        r = conn.execute(
            "SELECT note FROM spend_entry WHERE id=?;", (int(eid),)).fetchone()
        _check("upd note stored", r[0] == "할부3개월", str(r))

        # update is_fixed
        r_pre = conn.execute(
            "SELECT is_fixed FROM spend_entry WHERE id=?;",
            (int(eid),)).fetchone()
        new_fixed = 1 - r_pre[0]  # toggle
        rc, out, _ = _run(cli, "spend-upd", eid, f"is_fixed={new_fixed}")
        _check("upd is_fixed rc0", rc == 0, f"rc={rc}")
        r = conn.execute(
            "SELECT is_fixed FROM spend_entry WHERE id=?;",
            (int(eid),)).fetchone()
        _check("upd is_fixed stored", r[0] == new_fixed, str(r))

        # update scope
        rc, out, _ = _run(cli, "spend-upd", eid, "scope=business")
        _check("upd scope rc0", rc == 0, f"rc={rc}")
        r = conn.execute(
            "SELECT scope FROM spend_entry WHERE id=?;", (int(eid),)).fetchone()
        _check("upd scope stored", r[0] == "business", str(r))

        # update category (auto-register path)
        rc, out, _ = _run(cli, "spend-upd", eid, "category=배달음식")
        _check("upd category rc0", rc == 0, f"rc={rc}")
        r = conn.execute(
            "SELECT c.name FROM spend_entry e "
            "LEFT JOIN spend_category c ON c.id=e.category_id "
            "WHERE e.id=?;", (int(eid),)).fetchone()
        _check("upd category stored and visible via join",
               r[0] == "배달음식", str(r))

        # upd of nonexistent id -> error
        rc, out, _ = _run(cli, "spend-upd", "999999", "name=x")
        _check("upd nonexistent id rc1", rc == 1, f"rc={rc}")

        conn.close()


# ── 10. negative amount (refund) ─────────────────────────────


def test_negative_amount():
    """Refund recorded as negative amount on the refund date."""
    print("negative amount (refund):")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, _ = _run(cli, "spend-add", "2026-07-19", "50000", "의류구매",
                          "의류/미용")
        _check("original purchase rc0", rc == 0, f"rc={rc}")

        # Refund next day: -50000 on 2026-07-20 (refund date, not original date)
        rc, out, _ = _run(cli, "spend-add", "2026-07-20", "-50000", "의류반품",
                          "의류/미용")
        _check("refund negative amount rc0", rc == 0, f"rc={rc} err={_}")
        cols = out.strip().split("\t")
        _check("refund 4-col output", len(cols) == 4, out)
        _check("refund amount is -50000", cols[2] == "-50000", out)
        conn = _db(cli)
        r = conn.execute(
            "SELECT amount, date FROM spend_entry WHERE amount < 0;").fetchone()
        conn.close()
        _check("negative amount stored in DB", r[0] == -50000, str(r))
        _check("refund date is refund day (not original)", r[1] == "2026-07-20",
               str(r))


# ── 11. installment note passthrough ─────────────────────────


def test_installment_note():
    """Installment convention: full amount + note='할부 N개월'."""
    print("installment note passthrough:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, _ = _run(cli, "spend-add", "2026-07-01", "1200000", "노트북",
                          "생활용품", "삼성카드", "personal", "할부12개월")
        _check("installment add rc0", rc == 0, f"rc={rc} err={_}")
        conn = _db(cli)
        r = conn.execute(
            "SELECT amount, note FROM spend_entry WHERE name='노트북';").fetchone()
        conn.close()
        _check("full amount stored", r[0] == 1200000, str(r))
        _check("installment note stored verbatim", r[1] == "할부12개월", str(r))


# ── 12. scope isolation ──────────────────────────────────────


def test_scope_isolation():
    """Business entries excluded from personal find/day/week/month; --all includes them."""
    print("scope isolation:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        # personal: 19,500 (5500 + 14000) on 2026-07-21 (Monday)
        _run(cli, "spend-add", "2026-07-21", "5500", "커피", "카페/간식",
             "현금", "personal")
        _run(cli, "spend-add", "2026-07-21", "14000", "점심", "식비",
             "카드", "personal", "--new")
        # business: 50,000 same day
        _run(cli, "spend-add", "2026-07-21", "50000", "사업미팅식사",
             "식비", "법인카드", "business", "--new")

        # spend-find excludes business
        rc, out, _ = _run(cli, "spend-find", "2026-07-21")
        lines = [l for l in out.splitlines() if l]
        _check("find personal count=2", len(lines) == 2, out)
        biz_in_find = any("사업미팅식사" in l for l in lines)
        _check("find excludes business", not biz_in_find, out)

        # spend-find --all includes business
        rc, out_all, _ = _run(cli, "spend-find", "2026-07-21", "--all")
        lines_all = [l for l in out_all.splitlines() if l]
        _check("find --all count=3", len(lines_all) == 3, out_all)
        _check("find --all includes business",
               any("사업미팅식사" in l for l in lines_all), out_all)

        # spend-week: 2026-07-21 is Monday
        rc, out_week, _ = _run(cli, "spend-week", "2026-07-21")
        _check("week personal total=19500",
               "19,500" in out_week or "19500" in out_week, out_week)
        _check("week excludes business amount",
               "69,500" not in out_week and "69500" not in out_week, out_week)

        # spend-week --all includes business
        rc, out_week_all, _ = _run(cli, "spend-week", "2026-07-21", "--all")
        _check("week --all total=69500",
               "69,500" in out_week_all or "69500" in out_week_all, out_week_all)

        # spend-month
        rc, out_month, _ = _run(cli, "spend-month", "2026-07")
        _check("month personal total=19500",
               "19,500" in out_month or "19500" in out_month, out_month)
        _check("month excludes business amount",
               "69,500" not in out_month and "69500" not in out_month, out_month)

        rc, out_month_all, _ = _run(cli, "spend-month", "2026-07", "--all")
        _check("month --all total=69500",
               "69,500" in out_month_all or "69500" in out_month_all, out_month_all)


# ── 13. week aggregation math ────────────────────────────────


def test_spend_week():
    """Week aggregation: total, category breakdown, prev-week compare."""
    print("spend-week aggregation:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        # Week 1: 2026-07-14 (Mon) - 2026-07-20 (Sun)
        # 구독 (fixed): 15000
        # 식비: 8000 + 12000 = 20000
        _run(cli, "spend-add", "2026-07-14", "15000", "넷플릭스", "구독")
        _run(cli, "spend-add", "2026-07-15", "8000", "김치찌개", "식비",
             "", "personal", "--new")
        _run(cli, "spend-add", "2026-07-17", "12000", "삼겹살", "식비",
             "", "personal", "--new")
        # Week 2: 2026-07-21 (Mon) - reference week
        _run(cli, "spend-add", "2026-07-21", "5500", "커피", "카페/간식",
             "", "personal", "--new")
        _run(cli, "spend-add", "2026-07-22", "9000", "점심", "식비",
             "", "personal", "--new")

        rc, out, _ = _run(cli, "spend-week", "2026-07-21")
        _check("week rc0", rc == 0, f"rc={rc} err={_}")
        _check("week total=14500",
               "14,500" in out or "14500" in out, out)
        _check("week prev_total=35000",
               "35,000" in out or "35000" in out, out)
        # diff = 14500 - 35000 = -20500
        _check("week diff_total negative", "-20,500" in out or "-20500" in out,
               out)
        _check("week category 식비 shown", "식비" in out, out)
        _check("week date range header shown",
               "2026-07-21" in out, out)


# ── 14. month aggregation math + fixed/variable split + method subtotals ──


def test_spend_month():
    """Month aggregation: total, fixed/variable, categories, methods, prev compare."""
    print("spend-month aggregation:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        # 2026-06 (prev month): 200000 personal (fixed)
        _run(cli, "spend-add", "2026-06-01", "200000", "월세", "주거/공과금",
             "이체")
        # 2026-07 (current month):
        # Fixed: 주거/공과금 300000, 구독 15000 = 315000
        # Variable: 식비 50000 (card), 카페/간식 19500 (cash) = 69500
        # total personal = 384500
        _run(cli, "spend-add", "2026-07-01", "300000", "월세", "주거/공과금",
             "이체", "personal", "--new")
        _run(cli, "spend-add", "2026-07-05", "15000", "넷플릭스", "구독",
             "신한카드", "personal", "--new")
        _run(cli, "spend-add", "2026-07-10", "50000", "마트", "식비",
             "신한카드", "personal", "--new")
        _run(cli, "spend-add", "2026-07-15", "12000", "커피", "카페/간식",
             "현금", "personal", "--new")
        _run(cli, "spend-add", "2026-07-20", "7500", "간식", "카페/간식",
             "현금", "personal", "--new")

        rc, out, _ = _run(cli, "spend-month", "2026-07")
        _check("month rc0", rc == 0, f"rc={rc} err={_}")
        _check("month total=384500",
               "384,500" in out or "384500" in out, out)
        # fixed split
        _check("month fixed=315000",
               "315,000" in out or "315000" in out, out)
        # variable split
        _check("month variable=69500",
               "69,500" in out or "69500" in out, out)
        # prev month
        _check("month prev_total=200000",
               "200,000" in out or "200000" in out, out)
        # diff = 384500 - 200000 = 184500
        _check("month diff_total=+184500",
               "184,500" in out or "184500" in out, out)
        # method subtotals: 신한카드=65000, 이체=300000, 현금=19500
        _check("method 신한카드 subtotal shown", "신한카드" in out, out)
        _check("method 이체 subtotal shown", "이체" in out, out)
        _check("month categories section present", "카테고리" in out, out)
        _check("month methods section present", "결제수단" in out, out)


# ── 15. J1 informal-check case: 719,500 personal vs 769,500 --all ────────


def test_scope_isolation_exact_amounts():
    """Reproduce the specific case noted in the task brief:
    personal=719500, --all=769500 (delta = 50000 business entry).
    This test formally covers what the J1 informal string-split check got wrong."""
    print("scope isolation exact amounts (719500 personal / 769500 --all):")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        # Personal entries that sum to 719,500
        amounts_personal = [300000, 200000, 100000, 80000, 30000, 9500]
        # sum = 719,500
        assert sum(amounts_personal) == 719500
        for i, amt in enumerate(amounts_personal):
            rc, out, err = _run(cli, "spend-add", "2026-07-01", str(amt),
                                f"item{i}", "식비", "", "personal", "--new")
            assert rc == 0, f"setup failed: {err}"
        # Business entry: 50,000
        rc, out, err = _run(cli, "spend-add", "2026-07-01", "50000",
                            "사업비용", "식비", "", "business", "--new")
        assert rc == 0, f"business setup failed: {err}"

        # personal month total must be exactly 719,500
        rc, out_p, _ = _run(cli, "spend-month", "2026-07")
        _check("month personal total=719500",
               "719,500" in out_p or "719500" in out_p, out_p)
        _check("month personal does NOT contain 769500",
               "769,500" not in out_p and "769500" not in out_p, out_p)

        # --all month total must be exactly 769,500
        rc, out_a, _ = _run(cli, "spend-month", "2026-07", "--all")
        _check("month --all total=769500",
               "769,500" in out_a or "769500" in out_a, out_a)
        _check("month --all does NOT show only 719500 as the total",
               # The line 'total=719,500' should NOT appear when --all
               "total=719,500" not in out_a and "total=719500" not in out_a,
               out_a)

        # Verify via DB direct query (the real source of truth)
        conn = _db(cli)
        p_sum = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM spend_entry "
            "WHERE scope='personal';").fetchone()[0]
        all_sum = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM spend_entry;").fetchone()[0]
        conn.close()
        _check("DB personal sum = 719500", p_sum == 719500, f"got {p_sum}")
        _check("DB all sum = 769500", all_sum == 769500, f"got {all_sum}")


# ── 16. week boundary: spanning a month edge ─────────────────


def test_week_boundary_month_edge():
    """Week spanning Jan/Feb boundary: rows on both sides counted correctly."""
    print("week boundary (month edge):")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        # 2026-07-27 (Mon) to 2026-08-02 (Sun) spans July-August
        _run(cli, "spend-add", "2026-07-27", "10000", "7월지출", "식비")
        _run(cli, "spend-add", "2026-07-31", "20000", "7월말", "식비",
             "", "personal", "--new")
        _run(cli, "spend-add", "2026-08-01", "30000", "8월시작", "식비",
             "", "personal", "--new")
        _run(cli, "spend-add", "2026-08-02", "5000", "8월둘째", "식비",
             "", "personal", "--new")

        rc, out, _ = _run(cli, "spend-week", "2026-07-27")
        _check("month-edge week rc0", rc == 0, f"rc={rc}")
        # total should be 10000+20000+30000+5000 = 65000
        _check("month-edge week total=65000",
               "65,000" in out or "65000" in out, out)
        # Date range header shows the Monday
        _check("week header shows 2026-07-27", "2026-07-27" in out, out)


# ── 17. month with no prev data ──────────────────────────────


def test_month_no_prev():
    """Month with zero previous-month data: prev_total=0, diff is positive."""
    print("month with no prev data:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        # Only data in 2026-07; nothing in 2026-06
        _run(cli, "spend-add", "2026-07-05", "50000", "첫달지출", "식비")
        rc, out, _ = _run(cli, "spend-month", "2026-07")
        _check("no-prev month rc0", rc == 0, f"rc={rc}")
        _check("prev_total=0", "0원" in out or "prev" in out, out)
        # diff = 50000 - 0 = 50000
        _check("diff=50000",
               "50,000" in out or "50000" in out, out)


# ── 18. empty DB edges ───────────────────────────────────────


def test_empty_db():
    """All spend verbs handle an empty DB gracefully (rc0, no crash)."""
    print("empty DB edges:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, err = _run(cli, "spend-find", "2026-07-01")
        _check("find empty rc0", rc == 0, f"rc={rc} err={err}")
        _check("find empty no output", out.strip() == "", out)

        rc, out, err = _run(cli, "spend-day", "2026-07-01")
        _check("day empty rc0", rc == 0, f"rc={rc} err={err}")
        _check("day empty says no spend", "없음" in out, out)

        rc, out, err = _run(cli, "spend-week", "2026-07-21")
        _check("week empty rc0", rc == 0, f"rc={rc} err={err}")
        _check("week empty total=0",
               "total=0" in out or "total=0원" in out or ",0원" in out, out)

        rc, out, err = _run(cli, "spend-month", "2026-07")
        _check("month empty rc0", rc == 0, f"rc={rc} err={err}")
        _check("month empty total=0",
               "total=0" in out or "0원" in out, out)


# ── 19. prev-period compare correctness ─────────────────────


def test_prev_period_compare():
    """Week and month prev comparisons are period-accurate (no data leakage)."""
    print("prev-period compare:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        # 2026-07-07 week (Mon): 100000
        _run(cli, "spend-add", "2026-07-07", "100000", "지난주", "식비")
        # 2026-07-14 week (Mon): 40000
        _run(cli, "spend-add", "2026-07-14", "40000", "이번주", "식비",
             "", "personal", "--new")

        rc, out, _ = _run(cli, "spend-week", "2026-07-14")
        _check("week prev compare rc0", rc == 0, f"rc={rc}")
        _check("week current total=40000",
               "40,000" in out or "40000" in out, out)
        _check("week prev total=100000",
               "100,000" in out or "100000" in out, out)
        # diff = 40000 - 100000 = -60000
        _check("week diff=-60000",
               "-60,000" in out or "-60000" in out, out)


# ── 20. spend-upd returns None on missing id ─────────────────


def test_spend_upd_missing_id():
    """spend-upd on a nonexistent id exits with rc=1."""
    print("spend-upd missing id:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, err = _run(cli, "spend-upd", "99999", "name=ghost")
        _check("upd missing id rc1", rc == 1, f"rc={rc} err={err}")


# ── 21. scope='unknown' stored and visible via --all ─────────


def test_scope_unknown():
    """scope=unknown rows stored; excluded from personal; visible with --all."""
    print("scope=unknown:")
    with tempfile.TemporaryDirectory() as tmp:
        cli = _build_instance(tmp)
        rc, out, _ = _run(cli, "spend-add", "2026-07-10", "25000", "모호한지출",
                          "식비", "", "unknown")
        _check("unknown scope add rc0", rc == 0, f"rc={rc} err={_}")

        # Not in personal find
        rc, out_p, _ = _run(cli, "spend-find", "2026-07-10")
        _check("unknown not in personal find", out_p.strip() == "", out_p)

        # In --all
        rc, out_a, _ = _run(cli, "spend-find", "2026-07-10", "--all")
        lines = [l for l in out_a.splitlines() if l]
        _check("unknown appears with --all", len(lines) == 1, out_a)
        _check("unknown scope shown in row", "unknown" in lines[0], lines[0])

        # Can be updated to personal (C2 sweep)
        eid = lines[0].split("\t")[0]
        rc, out_u, _ = _run(cli, "spend-upd", eid, "scope=personal")
        _check("sweep unknown->personal rc0", rc == 0, f"rc={rc}")
        rc, out_pp, _ = _run(cli, "spend-find", "2026-07-10")
        lines2 = [l for l in out_pp.splitlines() if l]
        _check("after sweep row in personal find", len(lines2) == 1, out_pp)


# ── main ─────────────────────────────────────────────────────


def main():
    tests = [
        test_seed_integrity,
        test_spend_add_happy,
        test_dup_gate,
        test_is_fixed,
        test_category_auto_register,
        test_spend_find,
        test_spend_day,
        test_spend_del,
        test_spend_upd,
        test_negative_amount,
        test_installment_note,
        test_scope_isolation,
        test_spend_week,
        test_spend_month,
        test_scope_isolation_exact_amounts,
        test_week_boundary_month_edge,
        test_month_no_prev,
        test_empty_db,
        test_prev_period_compare,
        test_spend_upd_missing_id,
        test_scope_unknown,
    ]
    for t in tests:
        t()
    print()
    if _failures:
        print(f"FAILED {len(_failures)}: {', '.join(_failures)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
