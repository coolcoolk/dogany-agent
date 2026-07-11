#!/usr/bin/env python3
"""
lifekit.py — lifekit.db(로컬 라이프 OS)의 공식 코어

이 파일이 lifekit.db에 접근하는 모든 진실의 단일 원천이다.
  - 임포트 가능한 모듈: meal_add / workout_add / agg_day / compute_targets … 직접 호출.
  - CLI: lifekit.sh가 그대로 재현하던 서브커맨드(meal-add, agg-day …)를 그대로 제공.

설계 원칙:
  - DB 경로/연결을 한 곳(get_conn)에 둔다. foreign_keys ON.
  - 식단=area '식습관', 운동=area '신체건강' 자동연결(SELECT id FROM areas WHERE name=...).
  - kcal은 meals에서 generated(단백×4+지방×9+(탄-섬유)×4+알코올×7) → INSERT 금지.
  - CLI 표준출력 포맷은 옛 lifekit.sh와 ★한 글자도 다르지 않게★ 재현한다(호출처가 파싱).
  - card.py·retro 등이 sqlite/모델 로직을 중복하지 않도록 모델 함수도 여기로 통일.

표준 라이브러리만 사용(sqlite3/json/math/argparse/datetime). matplotlib 등 무거운 의존 없음.
표준 인터프리터에서 그대로 동작한다(별도 venv 불요).
"""

import os
import re
import sys
import json
import time
import secrets
import argparse
import sqlite3
import datetime
from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta, timezone

# ── 경로 ───────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, 'lifekit.db')
BODY_STATS_PATH = os.path.join(SCRIPT_DIR, 'body_stats.json')

AREA_MEAL = '식습관'
AREA_WORKOUT = '신체건강'

# 한글 요일 (월=0 .. 일=6)
_WDAY_KO = ['월', '화', '수', '목', '금', '토', '일']
# 영문 요일 약어 (월=0 .. 일=6) — agg-week 일별추이 (date +%a 재현)
_WDAY_EN = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


# ── 연결 (F1 하드닝: WAL + busy_timeout + 유계 SQLITE_BUSY 재시도) ──────
# DGN-179: 여러 프로세스가 같은 lifekit.db 파일을 동시에 열 때 쓰기 유실/
# SQLITE_BUSY 를 막는다. 전제 = 동일 호스트 로컬 파일(WAL 는 네트워크 FS 금지).
BUSY_TIMEOUT_MS = 5000
RETRY_BUDGET = 20
RETRY_BASE_SLEEP = 0.005


def _apply_pragmas(conn):
    """Uniform hardening PRAGMAs for every lifekit connection (F1).
    journal_mode=WAL serializes cross-process writers; busy_timeout gives the
    C layer a backstop; foreign_keys stays ON (legacy contract)."""
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = %d;" % BUSY_TIMEOUT_MS)


def get_conn():
    """lifekit.db 연결(단일 진입점). WAL + busy_timeout=5000 + foreign_keys ON.

    시그니처/반환은 기존과 동일(무인자, sqlite3.Connection 반환, 파일 없으면
    stderr 후 exit(1)) -- 기존 호출부 무변경. 하드닝 PRAGMA 만 추가된다."""
    if not os.path.isfile(DB_PATH):
        print(f"lifekit.db 없음 ({DB_PATH})", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    _apply_pragmas(conn)
    return conn


def is_busy(err):
    s = str(err).lower()
    return ("database is locked" in s) or ("database is busy" in s)


def with_retry(fn, budget=RETRY_BUDGET):
    """Bounded SQLITE_BUSY retry wrapper. On budget exhaustion, re-raises the
    original sqlite3.OperationalError verbatim (M6: preserve caller exception
    semantics -- callers that catch OperationalError keep working)."""
    retries = 0
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if is_busy(e) and retries < budget:
                retries += 1
                time.sleep(RETRY_BASE_SLEEP * (1 + (os.getpid() % 7)) * retries * 0.1
                           + RETRY_BASE_SLEEP)
                continue
            raise


def area_id(conn, name):
    """영역명 → id (없으면 None)."""
    row = conn.execute(
        "SELECT id FROM areas WHERE name=? LIMIT 1;", (name,)).fetchone()
    return row[0] if row else None


# ── 숫자 정규화 (lifekit.sh num() 재현: 비었거나 숫자 아니면 기본값) ──
def _num(v, default=0.0):
    if v is None:
        return default
    s = str(v).strip()
    if s == '':
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _opt_num(v):
    """빈 값이면 None(=SQL NULL), 아니면 숫자. lifekit.sh의 grams NULL 처리 재현."""
    if v is None:
        return None
    s = str(v).strip()
    if s == '':
        return None
    try:
        return float(s)
    except ValueError:
        return 0.0


def _txt(v):
    """빈 문자열이면 None(=NULL). lifekit.sh txt() 재현."""
    if v is None:
        return None
    s = str(v)
    return None if s == '' else s


def _f0(x):
    """SQLite/C printf('%.0f') 재현: 0.5 단위는 0에서 멀어지게 반올림(half-away-from-zero).
    Python의 %.0f(half-to-even)와 다르므로 Decimal ROUND_HALF_UP로 맞춘다."""
    q = Decimal(str(float(x))).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return f"{q}"


def _fp0(x):
    """printf('%+.0f') 재현: 부호 강제 + half-away-from-zero. 0은 +0."""
    q = Decimal(str(float(x))).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return f"+{q}" if q >= 0 else f"{q}"


# ── 식단 CRUD ──────────────────────────────────────────────
def meal_add(date, meal, name, carb=0, protein=0, fat=0,
             fiber=0, sugar=0, alt_sugar=0, grams=None, alcohol=0, conn=None):
    """식단 한 건 기록(area=식습관 자동). kcal은 DB generated. 새 행 id 반환.

    alcohol=순수 알코올 그램(선택, 7kcal/g). 술류는 volume_ml×ABV×0.789로 구해 넘긴다.
    미지정이면 0. 탄수는 실제 탄수 그램 그대로(알코올을 탄수에 접지 말 것)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        aid = area_id(conn, AREA_MEAL)
        cur = conn.execute(
            "INSERT INTO meals (date, meal, name, grams, carb, protein, fat, "
            "fiber, sugar, alt_sugar, alcohol, area_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?);",
            (date, _txt(meal), name, _opt_num(grams), _num(carb), _num(protein),
             _num(fat), _num(fiber), _num(sugar), _num(alt_sugar), _num(alcohol), aid))
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def meal_get(conn, mid):
    return conn.execute("SELECT * FROM meals WHERE id=?;", (mid,)).fetchone()


def meal_find(date, conn=None):
    """그날 식단 목록 (id, name, meal, kcal) 튜플 리스트."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        return conn.execute(
            "SELECT id, name, COALESCE(meal,''), kcal "
            "FROM meals WHERE date=? ORDER BY id;", (date,)).fetchall()
    finally:
        if own:
            conn.close()


def meal_day(date, conn=None):
    """그날 식단 (사람용 라인 리스트)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, meal, name, kcal, protein, carb, fat "
            "FROM meals WHERE date=? ORDER BY id;", (date,)).fetchall()
        out = []
        for mid, meal, name, kcal, protein, carb, fat in rows:
            out.append(
                f"[{mid}] {meal or '?'} · {name}"
                f"  ({_f0(kcal)}kcal 단{_f0(protein)} 탄{_f0(carb)} 지{_f0(fat)})")
        return out
    finally:
        if own:
            conn.close()


def meal_del(mid, conn=None):
    own = conn is None
    if own:
        conn = get_conn()
    try:
        conn.execute("DELETE FROM meals WHERE id=?;", (int(mid),))
        conn.commit()
    finally:
        if own:
            conn.close()


# 부분 업데이트 허용 컬럼 → 값 변환기. kcal은 generated라 제외.
_MEAL_UPD_COLS = {
    'date': _txt, 'meal': _txt, 'name': _txt,
    'carb': _num, 'protein': _num, 'fat': _num, 'fiber': _num,
    'sugar': _num, 'alt_sugar': _num, 'alcohol': _num,
    'grams': _opt_num,
}


def meal_upd(mid, fields, conn=None):
    """식단 한 건의 지정 필드만 갱신(부분 업데이트). fields=={컬럼:원본값}.
    kcal은 generated라 갱신 대상에서 제외(자동 재계산). 갱신 후 행 반환(없으면 None)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        sets, vals = [], []
        for col, raw in fields.items():
            if col not in _MEAL_UPD_COLS:
                raise ValueError(f"수정 불가 컬럼: {col}")
            sets.append(f"{col}=?")
            vals.append(_MEAL_UPD_COLS[col](raw))
        if not sets:
            raise ValueError("갱신할 필드가 없음")
        vals.append(int(mid))
        cur = conn.execute(
            f"UPDATE meals SET {', '.join(sets)} WHERE id=?;", vals)
        conn.commit()
        if cur.rowcount == 0:
            return None
        return conn.execute("SELECT id, name, kcal FROM meals WHERE id=?;",
                            (int(mid),)).fetchone()
    finally:
        if own:
            conn.close()


# ── 운동 CRUD ──────────────────────────────────────────────
def workout_type_id(conn, category, subtype):
    """(대분류, 세부) → workout_types.id. 사전에 없으면 None.

    빈 category/subtype이면 매핑 시도 안 하고 None(예: 세부만 모르는 경우)."""
    cat = (category or '').strip()
    sub = (subtype or '').strip()
    if not cat or not sub:
        return None
    row = conn.execute(
        "SELECT id FROM workout_types WHERE category=? AND subtype=? LIMIT 1;",
        (cat, sub)).fetchone()
    return row[0] if row else None


def workout_type_get_or_create(conn, category, subtype):
    """(대분류, 세부) → workout_types.id. 사전에 없으면 자동 등록 후 그 id 반환.

    type/name 캐시 컬럼 제거 후 라벨이 빈칸 되는 걸 막는 핵심 보완책:
    미등록 분류가 들어오면 workout_types에 (category, subtype, active=1) 신규 등록.
    빈 category/subtype이면 등록하지 않고 None(라벨 없는 운동은 type_id=NULL 허용)."""
    cat = (category or '').strip()
    sub = (subtype or '').strip()
    if not cat or not sub:
        return None
    tid = workout_type_id(conn, cat, sub)
    if tid is not None:
        return tid
    # 미등록 분류 → 사전에 자동 추가(sort는 0 기본, active=1).
    conn.execute(
        "INSERT INTO workout_types (category, subtype, active) VALUES (?,?,1);",
        (cat, sub))
    return workout_type_id(conn, cat, sub)


def workout_add_classification(conn, workout_id, category, subtype):
    """운동 한 건에 분류 1개를 연결(N:M junction에 1행).

    (category, subtype)를 workout_type_get_or_create로 type_id 확보 후
    workout_classifications에 (workout_id, type_id)를 INSERT OR IGNORE.
    빈 category/subtype이면 아무것도 안 넣고 None 반환(라벨 없는 분류 허용).
    반환: 연결된 type_id(없으면 None)."""
    tid = workout_type_get_or_create(conn, category, subtype)
    if tid is None:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO workout_classifications (workout_id, type_id) "
        "VALUES (?,?);", (workout_id, tid))
    return tid


def workout_add(date, wtype, name='', minutes=0, kcal=0, note='',
                avg_hr=None, conn=None, types=None):
    """운동 한 건 기록(area=신체건강 자동). 새 행 id 반환.

    운동 분류는 N:M junction(workout_classifications)으로 모델링한다 —
    한 운동이 분류(workout_types) 여러 개를 가질 수 있다(예: "근력+유산소").
      - workouts 행은 분류 없이 INSERT(date, minutes, kcal, note, area_id, avg_hr).
      - 단일 쌍(하위호환): wtype=대분류(category), name=세부(subtype)를
        workout_type_get_or_create로 확보해 junction에 1행 추가.
      - 복수 분류: types=[(category, subtype), ...]로 추가 분류를 넘기면
        junction에 여러 행으로 들어간다(중복은 INSERT OR IGNORE로 무시).
      - (category, subtype)가 사전에 없으면 에러로 막지 않고 stderr 경고 후
        workout_types에 자동 등록(active=1)한다.
        → 라벨은 항상 junction→workout_types 조인으로 복원된다.
      - category/subtype이 모두 비면 junction에 아무 행도 안 만든다(라벨 없는 운동 허용).
    avg_hr=평균 심박수(bpm). 미지정/빈값이면 NULL로 들어감(_opt_num)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        aid = area_id(conn, AREA_WORKOUT)
        cur = conn.execute(
            "INSERT INTO workouts (date, minutes, kcal, note, area_id, avg_hr) "
            "VALUES (?,?,?,?,?,?);",
            (date, _num(minutes), _num(kcal),
             _txt(note), aid, _opt_num(avg_hr)))
        wid = cur.lastrowid

        # 연결할 분류 쌍 모으기: 기본 단일 쌍(wtype/name) + 추가 types.
        pairs = [(wtype, name)]
        if types:
            pairs.extend(types)
        for cat, sub in pairs:
            existing = workout_type_id(conn, cat, sub)
            tid = workout_add_classification(conn, wid, cat, sub)
            if tid is not None and existing is None:
                print(f"[workout_add] 미등록 분류 자동 등록: '{cat}/{sub}' "
                      f"→ workout_types.id={tid}", file=sys.stderr)
        conn.commit()
        return wid
    finally:
        if own:
            conn.close()


def workout_find(date, conn=None):
    """그날 운동 목록 (id, type, name, minutes, kcal) 튜플 리스트.

    ★기존 5컬럼 반환 계약 유지★ (card.py/retro/weekly가 의존). 운동 한 건당 정확히 1행.
    분류는 N:M(workout_classifications)이라 한 운동이 분류 여러 개를 가질 수 있으므로:
      - junction을 LEFT JOIN하고 workout_id로 GROUP BY해 운동당 1행으로 접는다.
      - type 자리 = group_concat(DISTINCT wt.category) (여러 개면 콤마로 합쳐짐, 예 "근력,유산소").
      - name 자리 = group_concat(wt.subtype, ', ') (정렬은 type_id 순으로 안정화).
      - 분류 없으면 type/name은 빈문자열(COALESCE로 NULL→'').
    탭 구분 파싱(retro-2100.sh)이 안 깨지게 group_concat 구분자는 콤마/쉼표만 쓴다."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        return conn.execute(
            "SELECT w.id, "
            "COALESCE(group_concat(DISTINCT wt.category), ''), "
            "COALESCE(group_concat(wt.subtype, ', '), ''), "
            "w.minutes, w.kcal "
            "FROM workouts w "
            "LEFT JOIN workout_classifications wc ON wc.workout_id = w.id "
            "LEFT JOIN workout_types wt ON wt.id = wc.type_id "
            "WHERE w.date=? "
            "GROUP BY w.id ORDER BY w.id;", (date,)).fetchall()
    finally:
        if own:
            conn.close()


def workout_find_full(date, conn=None):
    """그날 운동 목록 (정규화 분류 포함). 운동 한 건당 1행.

    분류는 N:M(workout_classifications)이므로 운동당 1행으로 접고 분류는 group_concat한다.
    반환: (id, type, name, minutes, kcal, type_ids, categories, subtypes) 튜플 리스트.
      - type      = group_concat(DISTINCT wt.category)  (워크아웃당 모든 대분류, 콤마 합침)
      - name      = group_concat(wt.subtype, ', ')       (모든 세부, 쉼표 합침)
      - type_ids  = group_concat(wc.type_id)             (연결된 type_id들, 콤마 합침)
      - categories/subtypes = group_concat 원본(미연결이면 NULL→'')
    분류 없으면 모든 분류 칼럼은 빈문자열. (실호출자 없음 → workout_find와 동일 1:1 형태로 통일.)"""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        return conn.execute(
            "SELECT w.id, "
            "COALESCE(group_concat(DISTINCT wt.category), ''), "
            "COALESCE(group_concat(wt.subtype, ', '), ''), "
            "w.minutes, w.kcal, "
            "COALESCE(group_concat(wc.type_id), ''), "
            "COALESCE(group_concat(DISTINCT wt.category), ''), "
            "COALESCE(group_concat(wt.subtype, ', '), '') "
            "FROM workouts w "
            "LEFT JOIN workout_classifications wc ON wc.workout_id = w.id "
            "LEFT JOIN workout_types wt ON wt.id = wc.type_id "
            "WHERE w.date=? "
            "GROUP BY w.id ORDER BY w.id;", (date,)).fetchall()
    finally:
        if own:
            conn.close()


def workout_del(wid, conn=None):
    own = conn is None
    if own:
        conn = get_conn()
    try:
        conn.execute("DELETE FROM workouts WHERE id=?;", (int(wid),))
        conn.commit()
    finally:
        if own:
            conn.close()


# ── 집계 ───────────────────────────────────────────────────
def agg_day(date, conn=None):
    """그날 섭취/매크로/소모/밸런스 dict (v_daily_energy 기반)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        row = conn.execute(
            "SELECT intake_kcal, protein_g, carb_g, fat_g, burn_kcal, workout_min "
            "FROM v_daily_energy WHERE date=?;", (date,)).fetchone()
        intake, protein, carb, fat, burn, wmin = row if row else (0, 0, 0, 0, 0, 0)
        meal_cnt = conn.execute(
            "SELECT COUNT(*) FROM meals WHERE date=?;", (date,)).fetchone()[0]
        wo_cnt = conn.execute(
            "SELECT COUNT(*) FROM workouts WHERE date=?;", (date,)).fetchone()[0]
        return {
            'date': date,
            'meal_cnt': meal_cnt,
            'intake_kcal': intake or 0,
            'protein_g': protein or 0,
            'carb_g': carb or 0,
            'fat_g': fat or 0,
            'workout_cnt': wo_cnt,
            'workout_min': wmin or 0,
            'burn_kcal': burn or 0,
            'balance': (intake or 0) - (burn or 0),
        }
    finally:
        if own:
            conn.close()


def _shift_date(iso, days):
    d = datetime.date.fromisoformat(iso) + datetime.timedelta(days=days)
    return d.isoformat()


def agg_week(monday, conn=None):
    """그 주(월~일) 집계 + 직전주 대비 dict. monday=월요일 YYYY-MM-DD."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        nxt = _shift_date(monday, 7)     # 다음주 월(배타)
        prev = _shift_date(monday, -7)   # 직전주 월(포함)
        sun = _shift_date(monday, 6)     # 이번주 일

        cur = conn.execute(
            "SELECT COALESCE(SUM(kcal),0), COALESCE(SUM(protein),0), "
            "COALESCE(SUM(carb),0), COALESCE(SUM(fat),0), COUNT(*), "
            "COUNT(DISTINCT date) FROM meals WHERE date >= ? AND date < ?;",
            (monday, nxt)).fetchone()
        c_ik, c_pr, c_cb, c_ft, c_mc, c_md = cur

        pv = conn.execute(
            "SELECT COALESCE(SUM(kcal),0), COUNT(DISTINCT date) "
            "FROM meals WHERE date >= ? AND date < ?;",
            (prev, monday)).fetchone()
        p_ik, p_md = pv

        cw = conn.execute(
            "SELECT COALESCE(SUM(kcal),0), COALESCE(SUM(minutes),0), COUNT(*) "
            "FROM workouts WHERE date >= ? AND date < ?;",
            (monday, nxt)).fetchone()
        cw_bk, cw_wm, cw_wc = cw

        pw = conn.execute(
            "SELECT COALESCE(SUM(kcal),0), COUNT(*) "
            "FROM workouts WHERE date >= ? AND date < ?;",
            (prev, monday)).fetchone()
        pw_bk, pw_wc = pw

        cur_avg = c_ik / c_md if c_md > 0 else 0
        prev_avg = p_ik / p_md if p_md > 0 else 0

        # 일별 추이
        daily = []
        for i in range(7):
            d = _shift_date(monday, i)
            dow = _WDAY_EN[datetime.date.fromisoformat(d).weekday()]
            row = conn.execute(
                "SELECT intake_kcal, burn_kcal FROM v_daily_energy WHERE date=?;",
                (d,)).fetchone()
            ik = row[0] if row else 0
            bk = row[1] if row else 0
            mc = conn.execute(
                "SELECT COUNT(*) FROM meals WHERE date=?;", (d,)).fetchone()[0]
            wc = conn.execute(
                "SELECT COUNT(*) FROM workouts WHERE date=?;", (d,)).fetchone()[0]
            daily.append({'date': d, 'dow': dow, 'intake': ik or 0,
                          'burn': bk or 0, 'meal_cnt': mc, 'workout_cnt': wc})

        return {
            'monday': monday, 'sunday': sun,
            'meal_cnt': c_mc, 'meal_days': c_md,
            'intake_total': c_ik, 'intake_avg_day': cur_avg,
            'protein_total': c_pr, 'carb_total': c_cb, 'fat_total': c_ft,
            'workout_cnt': cw_wc, 'workout_min': cw_wm, 'burn_total': cw_bk,
            'prev_intake_total': p_ik, 'prev_intake_avg_day': prev_avg,
            'prev_workout_cnt': pw_wc, 'prev_burn_total': pw_bk,
            'diff_intake_avg': cur_avg - prev_avg,
            'diff_workout_cnt': cw_wc - pw_wc,
            'diff_burn_total': cw_bk - pw_bk,
            'daily': daily,
        }
    finally:
        if own:
            conn.close()


# ── 약속 / 사람 CRUD ───────────────────────────────────────
# 약속(appointments)과 사람(persons)은 appointment_persons로 N:M 연결.
# 사람은 본명(name) 외에 별명(aliases, 콤마조인)으로도 찾는다.
def person_find(query, conn=None):
    """이름 또는 별명에 query가 들어가는 사람 목록. (id, name, relation, aliases) 리스트."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        like = f"%{query}%"
        return conn.execute(
            "SELECT id, name, relation, aliases FROM persons "
            "WHERE name LIKE ? OR (aliases IS NOT NULL AND aliases LIKE ?) "
            "ORDER BY id;", (like, like)).fetchall()
    finally:
        if own:
            conn.close()


def person_add(name, relation=None, aliases=None, conn=None):
    """사람 한 명 신규 등록. 새 id 반환."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO persons (name, relation, aliases) VALUES (?,?,?);",
            (name, _txt(relation), _txt(aliases)))
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def person_alias_add(pid, alias, conn=None):
    """기존 사람의 aliases에 별명 하나 추가(콤마조인, 중복 무시). 갱신 행 반환(없으면 None)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        row = conn.execute(
            "SELECT aliases FROM persons WHERE id=?;", (int(pid),)).fetchone()
        if row is None:
            return None
        cur_aliases = [a.strip() for a in (row[0] or '').split(',') if a.strip()]
        if alias.strip() and alias.strip() not in cur_aliases:
            cur_aliases.append(alias.strip())
        joined = ','.join(cur_aliases) or None
        conn.execute("UPDATE persons SET aliases=? WHERE id=?;",
                     (joined, int(pid)))
        conn.commit()
        return conn.execute(
            "SELECT id, name, aliases FROM persons WHERE id=?;",
            (int(pid),)).fetchone()
    finally:
        if own:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# DGN-179 verb-delta (spec v2): event-backed appt_find / appt_show render.
# The legacy appointments-table appt_* functions are rewritten over the unified
# event table. Forward references (all_day_instants / ZoneInfo / event_conn /
# MutationResult / event_persons) resolve at call time -- they are defined later
# in the event SDK block at module scope.
# ══════════════════════════════════════════════════════════════════════════

def _render_local_col(start_or_end, schedule_kind, display_tz):
    """M-3/D4 rendering rule for a stored canonical-UTC instant:
      timed   -> local ISO w/ offset  '2026-07-10T07:00:00+09:00'
      all_day -> local bare date      '2026-07-10' (no 'T'; awk omits time)
      NULL    -> '' (caller decides padding).
    The awk consumers (morning-brief / daily-retro) extract HH:MM via
    substr($col, index($col,'T')+1, 5); a 'T'-less all_day value therefore
    prints date-only, byte-identical to the legacy behavior."""
    if start_or_end is None:
        return ""
    local = (datetime.datetime.strptime(start_or_end, "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=timezone.utc)
             .astimezone(ZoneInfo(display_tz)))
    if schedule_kind == "all_day":
        return local.strftime("%Y-%m-%d")
    return local.isoformat()


def appt_find(date_from, date_to=None, conn=None):
    """D4: appointments whose START instant falls in the LOCAL date window
    [date_from, date_to] inclusive (KST scope). Returns
    [(id, col2, title, location)]:
      timed   -> col2 = local ISO w/ offset
      all_day -> col2 = local bare date (no 'T')
    Bucketing = start-instant in [win_start, win_end) on canonical UTC (legacy
    date(start_at) semantics incl. multi-day showing only on its start day).
    kind='appointment' only. settled_outcome='abandoned' excluded (a cancelled
    meeting must not reappear in the brief); done/expired stay visible."""
    win_start, win_end = all_day_instants(date_from, date_to or date_from)
    own = conn is None
    if own:
        conn = event_conn()          # asserts user_version==4, actionable error
    try:
        rows = conn.execute(
            "SELECT id, start_at, schedule_kind, display_tz, title, location "
            "FROM event "
            "WHERE kind = 'appointment' "
            "AND start_at >= ? AND start_at < ? "
            "AND (settled_outcome IS NULL OR settled_outcome <> 'abandoned') "
            "ORDER BY start_at;", (win_start, win_end)).fetchall()
        out = []
        for eid, sa, sk, tz, title, loc in rows:
            out.append((eid, _render_local_col(sa, sk, tz), title, loc))
        return out
    finally:
        if own:
            conn.close()


def appt_show_row(conn, eid):
    """M-3: event-backed appt-show. Returns
    (id, title, start_local, end_local, location, purpose, summary) or None.
    start_local/end_local rendered per the M-3 rule (timed -> local ISO offset,
    all_day -> local date, NULL -> '')."""
    row = conn.execute(
        "SELECT id, title, start_at, end_at, schedule_kind, display_tz, "
        "location, purpose, summary FROM event "
        "WHERE id=? AND kind='appointment';", (int(eid),)).fetchone()
    if row is None:
        return None
    eid_, title, sa, ea, sk, tz, loc, purpose, summary = row
    return (eid_, title,
            _render_local_col(sa, sk, tz), _render_local_col(ea, sk, tz),
            loc, purpose, summary)


# ── 신체 스탯 / 목표 칼로리 모델 (card.py에서 이전, 공식 동일) ──
# Generic illustrative defaults only (NOT any real person's measurements).
# The owner's actual stats live in lifekit.db (config table) and override these.
DEFAULT_STATS = {
    'weight_kg': 70, 'height_cm': 170, 'fat_mass_kg': 15, 'lean_mass_kg': 55,
    'avg_steps': 8000, 'deficit_kcal': 300, 'other_neat_kcal': 150,
    'protein_g': 120, 'fat_ratio': 0.25,
}


def load_body_stats():
    """신체 스탯 전체 dict(DEFAULT_STATS 위에 저장값 머지).

    v2: 진실의 원천을 body_stats.json 직접 읽기에서 _CONFIG_STORE(SQL)
    경유로 옮겼다. card.py/compute_targets 등 모든 호출부가 SQL truth를 읽는다.
    _CONFIG_STORE는 이 함수보다 아래에서 정의되지만 파이썬은 전역을 호출 시점에
    해석하므로 순서 문제 없다(모듈 최초 import 후 첫 호출 시 이미 바인딩됨)."""
    return _CONFIG_STORE.load()


# ── 설정 저장소 추상화 ────────────────────────────
# 저장 백엔드(JSON/sqlite)를 호출부로부터 은닉하는 얇은 인터페이스.
# 지금은 JsonConfigStore(body_stats.json 어댑터)가 1차 백엔드.
# 나중에 SqliteConfigStore로 교체해도 set_stats/get_config 호출부는 무변경.
import tempfile  # 원자 쓰기(temp write -> os.replace)용


class ConfigStore:
    """단일 설정값 저장소 인터페이스. body_stats 같은 key/value dict 계약."""

    def load(self):
        """전체 설정 dict 반환(load_body_stats와 같은 계약: DEFAULT_STATS 머지)."""
        raise NotImplementedError

    def get(self, key, default=None):
        """단일 키 조회."""
        raise NotImplementedError

    def update(self, fields):
        """주어진 필드만 원자적으로 병합 갱신. 갱신된 전체 dict 반환."""
        raise NotImplementedError


class JsonConfigStore(ConfigStore):
    """body_stats.json 어댑터. 원자 쓰기(temp write -> os.replace)로 동시쓰기 보호.

    같은 디렉터리에 임시파일을 만들고 os.replace로 교체한다 — 같은 파일시스템
    내 rename은 POSIX에서 원자적이라 부분기록/동시쓰기 깨짐을 막는다.
    load() 계약은 기존 load_body_stats()와 동일(DEFAULT_STATS 위에 파일 머지)."""

    def __init__(self, path=None):
        self.path = path or BODY_STATS_PATH

    def _read_raw(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def load(self):
        return {**DEFAULT_STATS, **self._read_raw()}

    def get(self, key, default=None):
        raw = self._read_raw()
        if key in raw:
            return raw[key]
        return DEFAULT_STATS.get(key, default)

    def update(self, fields):
        """주어진 필드만 병합 후 원자 교체(temp write -> os.replace). 전체 dict 반환.

        Locking: the file SWAP is atomic (os.replace), so no reader ever sees a
        torn file. The read-modify-write is NOT itself locked, so two concurrent
        writers can lost-update (last os.replace wins). This is acceptable here:
        the single agent process is the only writer. If multi-writer access is
        ever needed, move config to SqliteConfigStore (single-writer WAL) instead
        of adding file locks."""
        raw = self._read_raw()
        raw.update(fields)
        raw['updated'] = datetime.date.today().isoformat()
        self._atomic_write(raw)
        return {**DEFAULT_STATS, **raw}

    def _atomic_write(self, data):
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(prefix='.body_stats.', suffix='.tmp', dir=d)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class SqliteConfigStore(ConfigStore):
    """config 테이블(key/value TEXT) 백엔드. v2에서 신체 스탯의 canonical.

    config.value 는 TEXT 이므로 load()는 DEFAULT_STATS 의 숫자 키를 명시적으로
    float 캐스팅해 JsonConfigStore(파일 JSON, 숫자는 그대로 int/float)와 값 계약을
    맞춘다. goal_mode/updated 같은 비숫자 키는 문자열 그대로 둔다.
    load() 계약: {**DEFAULT_STATS, **rows} (파일판과 동일하게 DEFAULT 위에 저장값)."""

    # 숫자로 캐스팅할 키 집합(DEFAULT_STATS 숫자 키 + 파일에만 있던 측정 숫자 키).
    # goal_mode/updated 는 여기 없으니 문자열 유지.
    _NUMERIC_KEYS = {
        'weight_kg', 'height_cm', 'skeletal_muscle_kg', 'fat_mass_kg',
        'lean_mass_kg', 'avg_steps', 'deficit_kcal', 'other_neat_kcal',
        'protein_g', 'fat_ratio',
    }

    def _cast(self, key, raw):
        """저장된 TEXT 값을 계약형으로 복원. 숫자 키면 float, 아니면 원문."""
        if key in self._NUMERIC_KEYS:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return DEFAULT_STATS.get(key, raw)
        return raw

    def _read_rows(self, conn=None):
        own = conn is None
        if own:
            conn = get_conn()
        try:
            rows = conn.execute("SELECT key, value FROM config;").fetchall()
            return {k: self._cast(k, v) for k, v in rows}
        finally:
            if own:
                conn.close()

    def load(self):
        return {**DEFAULT_STATS, **self._read_rows()}

    def get(self, key, default=None):
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM config WHERE key=? LIMIT 1;", (key,)).fetchone()
        finally:
            conn.close()
        if row is not None:
            return self._cast(key, row[0])
        return DEFAULT_STATS.get(key, default)

    def update(self, fields):
        """주어진 필드만 config에 upsert(값은 str로 저장). 'updated' 자동 갱신.
        갱신된 전체 dict 반환(load 계약)."""
        merged = dict(fields)
        merged['updated'] = datetime.date.today().isoformat()
        conn = get_conn()
        try:
            for k, v in merged.items():
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, "
                    "updated_at=datetime('now','localtime');",
                    (k, str(v)))
            conn.commit()
        finally:
            conn.close()
        return self.load()


# 기본 설정 저장소(현재 백엔드). v2 클린 컷오버: SQL(config 테이블)이 canonical.
# 교체 시 이 한 줄만 바꾸면 호출부 무변경(load_body_stats/get_config/set_stats 모두 경유).
_CONFIG_STORE = SqliteConfigStore()


# ── 설정값 쓰기/읽기 API ──────────────────────────
def set_stats(**fields):
    """body_stats 측정/설정 필드를 원자적으로 갱신. 갱신된 전체 dict 반환.

    load_body_stats()와 같은 dict 계약(DEFAULT_STATS 머지)을 유지한다.
    'updated' 필드는 자동 갱신된다(오늘 날짜). 저장 백엔드는 _CONFIG_STORE 경유."""
    if not fields:
        return _CONFIG_STORE.load()
    return _CONFIG_STORE.update(dict(fields))


def set_config(key, value):
    """단일 설정값 하나를 원자적으로 갱신(set_stats의 단일키 편의 래퍼)."""
    return _CONFIG_STORE.update({key: value})


def get_config(key, default=None):
    """설정값 조회. 현재 백엔드(JSON, body_stats) 경유.

    나중 SqliteConfigStore로 교체하면 config 테이블에서 읽도록 바뀌지만
    호출부는 동일하게 get_config(key)만 부른다."""
    return _CONFIG_STORE.get(key, default)


# ── 측정값 시계열 API ─────────────────────────────
def log_metric(date, metric, value, note=None, conn=None):
    """metric_log에 측정값 1건 upsert(하루 한 측정). (date, metric)이 같으면 갱신.

    weight_kg/skeletal_muscle_kg/fat_mass_kg 같은 측정값의 일별 추세 기록용.
    반환: 해당 행 id."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO metric_log (date, metric, value, note) VALUES (?,?,?,?) "
            "ON CONFLICT(date, metric) DO UPDATE SET "
            "value=excluded.value, note=excluded.note, "
            "created_at=datetime('now','localtime');",
            (date, metric, _num(value), _txt(note)))
        conn.commit()
        row = conn.execute(
            "SELECT id FROM metric_log WHERE date=? AND metric=?;",
            (date, metric)).fetchone()
        return row[0] if row else None
    finally:
        if own:
            conn.close()


def get_series(metric, date_from=None, date_to=None, conn=None):
    """한 metric의 시계열 조회. [{date, value}] 리스트(날짜 오름차순).

    date_from/date_to(YYYY-MM-DD)로 기간 한정(둘 다 포함). 미지정이면 전체."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        sql = "SELECT date, value FROM metric_log WHERE metric=?"
        params = [metric]
        if date_from is not None:
            sql += " AND date >= ?"
            params.append(date_from)
        if date_to is not None:
            sql += " AND date <= ?"
            params.append(date_to)
        sql += " ORDER BY date;"
        rows = conn.execute(sql, params).fetchall()
        return [{'date': d, 'value': v} for d, v in rows]
    finally:
        if own:
            conn.close()


def latest_metric(metric, conn=None):
    """한 metric의 최신 측정값. {date, value} (없으면 None).

    최신 기준은 date 내림차순(가장 최근 날짜), 동일 날짜는 id 내림차순."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        row = conn.execute(
            "SELECT date, value FROM metric_log WHERE metric=? "
            "ORDER BY date DESC, id DESC LIMIT 1;", (metric,)).fetchone()
        return {'date': row[0], 'value': row[1]} if row else None
    finally:
        if own:
            conn.close()


def compute_targets(stats, exercise_kcal=0):
    """리컴프/다이어트 목표 칼로리(사용자 모델). card.py와 값 동일해야 함."""
    lean = stats.get('lean_mass_kg') or (stats['weight_kg'] - stats.get('fat_mass_kg', 0))
    bmr = 370 + 21.6 * lean                                  # Katch-McArdle
    stride_m = stats['height_cm'] / 100 * 0.415              # 보폭 ≈ 키×0.415
    walk_km = stats['avg_steps'] * stride_m / 1000
    walk_kcal = 0.5 * stats['weight_kg'] * walk_km           # 순 보행소모
    neat = walk_kcal + stats.get('other_neat_kcal', 150)     # 활동소모(운동 제외)
    base_goal = bmr - stats['deficit_kcal'] + neat           # 운동 0일 때 기준
    eff_goal = base_goal + exercise_kcal
    return {
        'bmr': round(bmr), 'neat': round(neat),
        'deficit': round(stats['deficit_kcal']),
        'base_goal': round(base_goal), 'eff_goal': round(eff_goal),
    }


def compute_macro_goals(total_kcal, stats):
    protein_g = stats.get('protein_g', 120)
    fat_ratio = stats.get('fat_ratio', 0.25)
    fat_g = round(total_kcal * fat_ratio / 9)
    carb_g = round((total_kcal - protein_g * 4 - total_kcal * fat_ratio) / 4)
    return {'protein': protein_g, 'carb': carb_g, 'fat': fat_g}


# ── 카드용: 그날 식단·운동을 읽어 card.py가 쓰는 dict 조각 만들기 ──
# (옛 card.py load_from_life_db를 lifekit.py로 이전. subprocess 대신 직접 DB.)
def load_card_data(iso_date, conn=None):
    """card.py용: 해당 날짜 식단·운동을 읽어 카드 dict 조각 반환."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        a = agg_day(iso_date, conn=conn)
        # 상단 합계(섭취/매크로)는 옛 card가 agg-day의 %.0f 출력을 받아 float()한 값을
        # 그대로 썼다. 카드 표시·바 길이가 한 픽셀도 안 바뀌게 동일 반올림을 재현한다.
        # 당 합계는 agg_day(v_daily_energy 뷰)에 없으므로 meals에서 직접 합산.
        sugar_total = conn.execute(
            "SELECT COALESCE(SUM(sugar),0) FROM meals WHERE date=?;",
            (iso_date,)).fetchone()[0] or 0
        res = {
            'intake_kcal': {'current': float(_f0(a['intake_kcal']))},
            'protein': {'current': float(_f0(a['protein_g']))},
            'carbs': {'current': float(_f0(a['carb_g'])),
                      'sugar': float(_f0(sugar_total))},
            'fat': {'current': float(_f0(a['fat_g']))},
        }

        # 운동: 항상 workout_find 실행 → workouts 배열 + burn_kcal detail 공용
        wo_rows = workout_find(iso_date, conn=conn)
        res['workouts'] = [
            {'id': r[0], 'type': r[1], 'name': r[2], 'minutes': r[3], 'kcal': r[4]}
            for r in wo_rows
        ]

        burn = float(_f0(a['burn_kcal']))
        if burn > 0:
            labels = []
            for _id, wtype, wname, _min, _kc in wo_rows:
                if wtype and wname:
                    labels.append(f"{wtype} ({wname})")
                elif wtype:
                    labels.append(wtype)
            detail = ', '.join(labels) if labels else '운동'
            res['burn_kcal'] = {'current': burn, 'detail': detail}

        # 식사 목록 + 매크로
        meals = []
        rows = conn.execute(
            "SELECT meal, name, protein, carb, fat, sugar "
            "FROM meals WHERE date=? ORDER BY id;", (iso_date,)).fetchall()
        for meal, name, protein, carb, fat, sugar in rows:
            meals.append({
                'type': meal or '', 'name': name,
                'protein': float(protein), 'carbs': float(carb),
                'fat': float(fat), 'sugar': float(sugar or 0),
            })
        res['meals'] = meals
        return res
    finally:
        if own:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# DGN-179 event SDK core -- unified L1 event model (spec v5 LOCK 2026-07-07).
# Ported verbatim from the verified sandbox event_core.py. Three tables
# (event / sub_event / reschedule_requests). English/ASCII only in this block.
#
# Key invariants enforced here:
#   - canonical UTC 'YYYY-MM-DDThh:mm:ssZ' (20 chars) for every instant; a
#     non-canonical write is refused by the app validator AND by table CHECKs.
#   - occupancy predicate targets slot_exclusive=1 AND settled_at IS NULL
#     AND start_at IS NOT NULL (v5.1: physical liveness bit; status cache NOT
#     consulted). schedule_kind='timed' was dropped from the filter so an
#     exclusive all_day day-block physically repels timed events that day;
#     untimed rows fall out via start_at IS NOT NULL.
#   - every slot-touching mutation (INSERT + all UPDATE variants) passes the
#     SAME atomic half-open overlap predicate in one write txn (FG-1). UPDATEs
#     exclude self. changes()==0 -> re-query to tell CAS-fail from overlap-reject.
#   - recompute derives status but NEVER derives 'abandoned'; a settled event
#     returns its stored settled_outcome verbatim (F-A seal).
#   - cancel = force-settle with outcome 'abandoned'; force_settle = outcome 'done'.
#
# NOTE ON user_version: the event schema is framework migration 003 (DGN-180
# D180-0 renumber -- 002_tasks_archived_at already owns user_version=2, so the
# event schema is 3). The DGN-179 verb-delta adds migration 004 (event_persons
# junction, D2), so the SDK now asserts 4.
# ═══════════════════════════════════════════════════════════════════════════

# MIN-7a (spec v2): ZoneInfo imported at module scope (used by appt_find + the
# facade time parser). datetime/timezone are already imported at file top.
from zoneinfo import ZoneInfo

EXPECTED_USER_VERSION = 6  # 006_routine_recurrence (DGN-240 T7)

# INF sentinel string: lexicographically greater than any canonical "...Z"
# instant. '~' (0x7E) sorts after digits/'Z'/'T'. Used only at compute time;
# open-ended end stays SQL NULL on disk.
INF_STR = "~~~~~~~~~~~~~~~~~~~~"

CANON_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# effective (half-open) end for a stored event row `e`, matching _cand_eff_end.
# NULL end -> INF ; zero-length (start==end) -> start + 1 second ; else end.
EFF_END_SQL = (
    "(CASE WHEN e.end_at IS NULL THEN '%s' "
    "WHEN e.end_at = e.start_at "
    "  THEN strftime('%%Y-%%m-%%dT%%H:%%M:%%SZ', e.start_at, '+1 second') "
    "ELSE e.end_at END)" % INF_STR
)

# liveness filter for the occupancy predicate (physical bit, not status cache).
# v5.1 (spec correction): drop schedule_kind='timed'. An exclusive
# all_day day-block (exam day, etc.) must physically block timed events that day
# via the atomic predicate. all_day rows are stored as UTC instant ranges so they
# join the uniform half-open formula directly. Untimed rows are excluded by the
# explicit start_at IS NOT NULL guard (they have no instant to occupy).
LIVE_FILTER = ("e.slot_exclusive = 1 AND e.settled_at IS NULL "
               "AND e.start_at IS NOT NULL")


class MigrationRequired(Exception):
    """Raised when user_version does not match. Actionable, not a hard exit."""


def event_conn(db_path=None, assert_version=True):
    """Open a hardened connection for the event SDK. WAL + busy_timeout=5000.
    Asserts user_version==4 unless told otherwise (migration tooling opens with
    assert_version=False). Defaults to the module DB_PATH; a path override lets
    tests / migration point at a copy.

    This is distinct from get_conn() (the legacy no-arg entrypoint) so the
    version assertion never fires on legacy lifekit callers -- only the event
    SDK opts into it."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    _apply_pragmas(conn)
    if assert_version:
        v = conn.execute("PRAGMA user_version;").fetchone()[0]
        if v != EXPECTED_USER_VERSION:
            conn.close()
            raise MigrationRequired(
                "event schema user_version=%d, expected %d. "
                "run: update.sh (applies pending migrations under "
                "database/migrations/)"
                % (v, EXPECTED_USER_VERSION))
    return conn


# ── time / canonical validator ────────────────────────────────────────────
def canonical(dt_str):
    """App-side validator (refusal path). Returns dt_str if canonical UTC,
    else raises ValueError. Mirrors the table CHECKs (belt-and-suspenders)."""
    if dt_str is None:
        return None
    if not CANON_RE.match(dt_str):
        raise ValueError("non-canonical time refused: %r" % dt_str)
    return dt_str


def validate_interval(start_at, end_at):
    """grill-5 (MINOR-1) app-level validator: reject a reversed interval
    (end < start). Zero-length (end == start) is LEGAL; only strictly-reversed
    is refused. Both args must already be canonical (fixed-width UTC sorts
    chronologically, so a lexical compare is exact). Mirrors the table CHECK
    (end_at IS NULL OR start_at IS NULL OR end_at >= start_at). No-op if either
    endpoint is NULL. Returns None; raises ValueError on a reversed interval."""
    if start_at is None or end_at is None:
        return None
    if end_at < start_at:
        raise ValueError(
            "reversed interval refused: end_at %r < start_at %r" % (end_at, start_at))
    return None


def validate_all_day_instants(schedule_kind, start_at, end_at):
    """spec v2 MIN-5 app-side belt: an all_day row must carry BOTH instants.
    A fresh DB enforces this with a schema CHECK; a MIGRATED DB cannot (SQLite
    has no ALTER ADD CONSTRAINT), so this validator closes the live SDK path on
    every DB regardless of when it was minted. The SDK never emits a
    NULL-instant all_day (all_day always flows through all_day_instants), so this
    is belt-and-suspenders. Returns None; raises ValueError on violation."""
    if schedule_kind == "all_day" and (start_at is None or end_at is None):
        raise ValueError(
            "all_day row must carry both start_at and end_at "
            "(got start=%r end=%r)" % (start_at, end_at))
    return None


def now_utc():
    return datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plus_one_second(ts):
    """Canonical ts + 1 second, computed by sqlite so it is byte-identical to
    the in-DB EFF_END_SQL promotion (same strftime, same rollover)."""
    c = sqlite3.connect(":memory:")
    r = c.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', ?, '+1 second');",
                  (ts,)).fetchone()[0]
    c.close()
    return r


def _cand_eff_end(start, end, open_ended):
    """Half-open effective end for a candidate (mirror of EFF_END_SQL)."""
    if open_ended:
        return INF_STR
    if end == start:
        return _plus_one_second(start)
    return end


def all_day_instants(local_date_start, local_date_end=None, tz_name="Asia/Seoul"):
    """Derive the canonical UTC instant pair for an all_day event via zoneinfo
    (fixed offset forbidden -- N3). Single day: local_date_end None.
    Returns (start_at_utc, end_at_utc):
        [start_day 00:00 local, end_day+1 00:00 local)
    local_date_* are 'YYYY-MM-DD' strings.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    sd = datetime.datetime.strptime(local_date_start, "%Y-%m-%d").date()
    ed = (datetime.datetime.strptime(local_date_end, "%Y-%m-%d").date()
          if local_date_end else sd)
    start_local = datetime.datetime(sd.year, sd.month, sd.day, 0, 0, 0, tzinfo=tz)
    end_local = (datetime.datetime(ed.year, ed.month, ed.day, 0, 0, 0, tzinfo=tz)
                 + timedelta(days=1))
    su = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    eu = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return su, eu


# ── ULID generator (stdlib only, Crockford base32, monotonic-ish) ──────────
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value, length):
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_ulid(ts_ms=None):
    """26-char Crockford base32 ULID: 48-bit ms timestamp + 80 random bits."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    ts_part = _encode(ts_ms & ((1 << 48) - 1), 10)
    rand = secrets.randbits(80)
    rand_part = _encode(rand, 16)
    return ts_part + rand_part


# ── kind policy: slot_exclusive is decided by SDK, never per-write. ────────
# skills that declare a time-occupying task kind register here (born exclusive).
TIME_OCCUPYING_TASK_KINDS = set()  # e.g. {'workout_session', 'focus_block'}


def resolve_slot_exclusive(kind, schedule_kind, task_kind=None, day_block=False):
    """Decide slot_exclusive per kind-policy (N5/D8 locus).

    F-1 (grill-1 FATAL, spec v2): the all_day branch is evaluated BEFORE the
    appointment-kind branch. all_day is a CONTAINING context (trip/travel):
    default 0, exclusive 1 ONLY on an explicit full-day block (exam day, etc.),
    regardless of kind. Putting the appointment branch first minted a "day
    brick" -- every date-only appointment born exclusive, physically blocking
    its whole day under the v5.1 occupancy predicate. v5 kind-policy cells:
      - all_day  -> 0 default, 1 iff day_block (containing context; kind-blind).
      - appointment (timed)          -> 1 (a point-in-time meeting occupies).
      - task with declared time-occupying kind -> 1 (born exclusive).
      - general task (timed/untimed) -> 0.
    """
    if schedule_kind == "all_day":
        return 1 if day_block else 0
    if kind == "appointment":
        return 1
    if task_kind is not None and task_kind in TIME_OCCUPYING_TASK_KINDS:
        return 1
    return 0


# ── derived status recompute (F-A seal: never derives abandoned) ───────────
def derive_status(schedule_kind, start_at, end_at, open_ended,
                  settled_at, settled_outcome, live_subs, completion_rule,
                  now=None):
    """Pure recompute. live_subs = list of done(0/1) for non-tombstoned subs.
    Priority: settled(outcome verbatim) > expired > vacuous-open > all-done > open.
    Derivation NEVER produces 'abandoned'.

    completion_rule is 'all' or 'manual' ONLY (grill-5 enum shrink; 'any'/'n_of_m'
    removed -- undefined derivation). 'all' auto-completes when every live sub is
    done; 'manual' never auto-completes (only force_settle can complete it), so a
    manual event with all subs done stays 'open' until settled -- this is
    intentional (manual = human-confirmed completion).
    """
    # 1. settled: return stored outcome verbatim (F-A seal, immune to sub-writes).
    if settled_at is not None:
        return settled_outcome  # 'done' or 'abandoned'

    if now is None:
        now = now_utc()

    # only 'all' auto-completes; 'manual' waits for force_settle (enum shrink).
    all_done = (completion_rule == "all" and len(live_subs) > 0
                and all(x == 1 for x in live_subs))

    # 2. expired: past deadline with not-all-done. Only timed/all_day have a
    #    deadline instant; untimed has none so it can never expire.
    deadline = (_cand_eff_end(start_at, end_at, open_ended)
                if start_at is not None else None)
    if deadline is not None and deadline != INF_STR and now >= deadline and not all_done:
        return "expired"

    # 3. vacuous-open: zero live subs stays open (vacuous-AND fix).
    if len(live_subs) == 0:
        return "open"
    # 4. all-done.
    if all_done:
        return "done"
    # 5. default open.
    return "open"


def _live_subs(conn, eid):
    return [r[0] for r in conn.execute(
        "SELECT done FROM sub_event WHERE event_id=? AND tombstone=0;",
        (eid,)).fetchall()]


def _recompute_and_store(conn, eid, now=None):
    """Recompute derived status from live sub rows + settle state and store it.
    Caller holds an open write txn."""
    row = conn.execute(
        "SELECT schedule_kind, start_at, end_at, open_ended, settled_at, "
        "settled_outcome, completion_rule FROM event WHERE id=?;", (eid,)).fetchone()
    sk, sa, ea, oe, set_at, set_out, rule = row
    live = _live_subs(conn, eid)
    st = derive_status(sk, sa, ea, oe, set_at, set_out, live, rule, now=now)
    conn.execute("UPDATE event SET status=? WHERE id=?;", (st, eid))
    return st


# ── overlap predicate helpers (uniform half-open; slot-touching mutations) ─
def is_blocker(slot_exclusive, start_at):
    """v5.1 liveness predicate in Python: a row participates in the occupancy
    predicate iff it is slot_exclusive AND has a start instant (settled_at NULL
    is guaranteed for the just-touched row -- an unsettled write). Mirrors
    LIVE_FILTER for the mutation-side 'should I guard the slot?' decision.
    schedule_kind is NOT consulted (timed AND exclusive-all_day both block;
    untimed has NULL start and falls out)."""
    return slot_exclusive == 1 and start_at is not None


def _overlap_not_exists(exclude_id=False):
    """WHERE NOT EXISTS(...) overlap probe against live exclusive rows.
    Params (in order): cand_start, cand_eff_end [, exclude_id].
    Uniform half-open: e.start < cand_eff_end AND cand_start < eff_end(e).
    """
    sql = ("NOT EXISTS (SELECT 1 FROM event e WHERE " + LIVE_FILTER +
           " AND ? < " + EFF_END_SQL + " AND e.start_at < ?")
    if exclude_id:
        sql += " AND e.id != ?"
    sql += ")"
    return sql


_REC_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # DGN-240 rec_date shape


# ── event_add: atomic conditional insert for slot_exclusive events. ────────
# non-exclusive events insert unconditionally (no slot to guard).
def event_add(conn, kind, title, schedule_kind, start_at=None, end_at=None,
              open_ended=0, owning_agent=None, created_by=None,
              completion_rule="all", note=None, area_id=None,
              display_tz="Asia/Seoul", task_kind=None, day_block=False,
              notion_id=None, meta=None,
              recurrence_id=None, rec_date=None, is_routine=0):
    """Insert a new event. For slot_exclusive events with a start instant, the
    insert is atomic INSERT..WHERE NOT EXISTS(overlap): returns event id on win,
    None on slot loss. For non-exclusive/no-start, always inserts (no slot guard).

    Time validation is enforced here (canonical) and by table CHECKs.

    meta (spec v2 DEV-1 verdict): optional {col: value} of KIND-SPECIFIC
    metadata columns carried in the SAME INSERT (single txn -- no crash window
    between insert and a follow-up event_set_meta, and no version bump at
    birth). Validated against META_COLS_BY_KIND[kind] with the same rule as
    event_set_meta: a kind-illegal column raises ValueError. title/note have
    first-class params and are NOT accepted through meta.

    DGN-240 (spec v3 1.6, T5) recurrence gate -- the SDK promotion of the
    DGN-180 N1 gate (anchor-less routine writes stay impossible):
      - recurrence_id given -> the anchor MUST resolve to a routine_def row
        AND rec_date (YYYY-MM-DD) is required; violations raise ValueError.
      - is_routine=1 without recurrence_id -> ValueError.
      - a recurrence instance must be timed|all_day (1.4 invariant; untimed
        entry is refused at the verb level).
    sqlite ALTER cannot add CHECKs on the migrated columns, so this verb IS
    the coherence enforcement point (spec 1.3).
    """
    # DGN-240 recurrence gate (order: routine flag first, then anchor checks).
    if is_routine and not recurrence_id:
        raise ValueError(
            "event_add: is_routine=1 requires recurrence_id (N1 gate -- "
            "anchor-less routine writes are refused)")
    if recurrence_id is not None:
        anchor = conn.execute(
            "SELECT 1 FROM routine_def WHERE recurrence_id=?;",
            (recurrence_id,)).fetchone()
        if anchor is None:
            raise ValueError(
                "event_add: recurrence_id %r does not resolve to a "
                "routine_def anchor" % recurrence_id)
        if rec_date is None or not _REC_DATE_RE.match(str(rec_date)):
            raise ValueError(
                "event_add: recurrence instance requires rec_date "
                "YYYY-MM-DD, got %r" % rec_date)
        if schedule_kind not in ("timed", "all_day"):
            raise ValueError(
                "event_add: recurrence instance schedule_kind must be "
                "timed|all_day, got %r" % schedule_kind)
    elif rec_date is not None:
        raise ValueError("event_add: rec_date requires recurrence_id")
    canonical(start_at)
    canonical(end_at)
    validate_interval(start_at, end_at)   # grill-5: reject reversed interval
    validate_all_day_instants(schedule_kind, start_at, end_at)  # v2 MIN-5 belt
    if owning_agent is None or created_by is None:
        raise ValueError("owning_agent and created_by are required")
    meta = dict(meta) if meta else {}
    allowed = set(META_COLS_BY_KIND.get(kind, ()))
    for col in meta:
        if col not in allowed:
            raise ValueError(
                "event_add: meta column %r not allowed for kind %r "
                "(allowed: %s; title/note are first-class params)"
                % (col, kind, sorted(allowed)))
    slot_exclusive = resolve_slot_exclusive(kind, schedule_kind, task_kind, day_block)
    now = now_utc()
    ulid = new_ulid()

    meta_cols = sorted(meta.keys())       # deterministic column order
    cols = ("ulid, kind, title, note, area_id, schedule_kind, start_at, end_at, "
            "display_tz, open_ended, slot_exclusive, completion_rule, "
            "owning_agent, created_by, notion_id, "
            "recurrence_id, rec_date, is_routine, created_at, updated_at")
    if meta_cols:
        cols += ", " + ", ".join(meta_cols)
    base_vals = [ulid, kind, title, note, area_id, schedule_kind, start_at, end_at,
                 display_tz, open_ended, slot_exclusive, completion_rule,
                 owning_agent, created_by, notion_id,
                 recurrence_id, rec_date, 1 if is_routine else 0, now, now]
    base_vals += [meta[c] for c in meta_cols]

    conn.execute("BEGIN IMMEDIATE;")
    try:
        # v5.1: guard the slot for any exclusive row that has a start instant
        # (timed OR exclusive all_day day-block). untimed has NULL start -> no slot.
        if is_blocker(slot_exclusive, start_at):
            cee = _cand_eff_end(start_at, end_at, open_ended)
            sql = ("INSERT INTO event (" + cols + ") SELECT " +
                   ",".join("?" for _ in base_vals) + " WHERE " +
                   _overlap_not_exists(exclude_id=False))
            cur = conn.execute(sql, base_vals + [start_at, cee])
            if cur.rowcount != 1:
                conn.commit()
                return None
        else:
            sql = ("INSERT INTO event (" + cols + ") VALUES (" +
                   ",".join("?" for _ in base_vals) + ")")
            conn.execute(sql, base_vals)
        eid = conn.execute("SELECT id FROM event WHERE ulid=?;", (ulid,)).fetchone()[0]
        _recompute_and_store(conn, eid, now=now)
        conn.commit()
        return eid
    except Exception:
        conn.rollback()
        raise


# ── slot-touching UPDATE family (FG-1): move / kind transition / promotion. ─
# All share UPDATE..WHERE id=? AND version=? AND NOT EXISTS(overlap, self excl).
# changes()==0 -> re-query to distinguish CAS-fail from overlap-reject.
class MutationResult(object):
    APPLIED = "applied"
    CAS_FAIL = "cas_fail"
    OVERLAP_REJECT = "overlap_reject"
    NOT_FOUND = "not_found"


def _cas_slot_update(conn, eid, version, set_clause, set_params,
                     new_start, new_eff_end, will_be_exclusive_timed):
    """Run a slot-touching UPDATE under version CAS + self-excluded overlap.
    Returns a MutationResult. Caller supplies an OPEN write txn.

    will_be_exclusive_timed: whether the row AFTER the update is exclusive+timed
    (so we must guard the slot). If not, no overlap guard is applied.
    """
    where_overlap = (" AND " + _overlap_not_exists(exclude_id=True)
                     if will_be_exclusive_timed else "")
    sql = ("UPDATE event SET " + set_clause + ", version=version+1, updated_at=? "
           "WHERE id=? AND version=?" + where_overlap)
    now = now_utc()
    params = set_params + [now, eid, version]
    if will_be_exclusive_timed:
        params += [new_start, new_eff_end, eid]
    cur = conn.execute(sql, params)
    if cur.rowcount == 1:
        _recompute_and_store(conn, eid, now=now)
        return MutationResult.APPLIED
    # changes()==0: distinguish CAS-fail vs overlap-reject vs not-found.
    row = conn.execute("SELECT version FROM event WHERE id=?;", (eid,)).fetchone()
    if row is None:
        return MutationResult.NOT_FOUND
    if row[0] != version:
        return MutationResult.CAS_FAIL
    # version still matches -> the overlap guard was what blocked it.
    return MutationResult.OVERLAP_REJECT


def event_move(conn, eid, version, new_start, new_end, open_ended=0):
    """Move a timed event to a new [start,end). Passes the atomic slot predicate
    (self excluded). Returns MutationResult.

    grill-final M-2: the all_day validator runs against the row's CURRENT
    schedule_kind, so moving an all_day row to NULL instants raises ValueError
    on EVERY DB (fresh AND migrated -- consistent error class; migrated DBs lack
    the schema CHECK)."""
    canonical(new_start)
    canonical(new_end)
    validate_interval(new_start, new_end)   # grill-5: reject reversed interval
    conn.execute("BEGIN IMMEDIATE;")
    try:
        row = conn.execute("SELECT slot_exclusive, schedule_kind FROM event WHERE id=?;",
                           (eid,)).fetchone()
        if row is None:
            conn.commit()
            return MutationResult.NOT_FOUND
        slot_ex, sk = row
        # grill-final M-2: all_day rows must keep BOTH instants through a move.
        validate_all_day_instants(sk, new_start, new_end)
        # v5.1: guard if the moved row will be an exclusive blocker with a start.
        guard = is_blocker(slot_ex, new_start)
        cee = _cand_eff_end(new_start, new_end, open_ended)
        res = _cas_slot_update(
            conn, eid, version,
            "start_at=?, end_at=?, open_ended=?",
            [new_start, new_end, open_ended],
            new_start, cee, guard)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise


def event_transition_schedule_kind(conn, eid, version, new_schedule_kind,
                                   new_start=None, new_end=None, open_ended=0):
    """Transition schedule_kind (e.g. untimed->timed). owning_agent gate is the
    caller's responsibility; here we enforce version CAS + slot predicate when
    the target is an exclusive blocker.

    grill-5 (MAJOR-1) untimed guard: transitioning TO 'untimed' must leave NO
    time instants behind. A stale start_at would keep the row in the occupancy
    predicate (start_at IS NOT NULL) and phantom-block its slot. We REJECT (loud,
    not silent-clear) any non-NULL time argument on an untimed target -- the
    caller must not pass start/end/open_ended for an untimed transition. This
    pairs with the schema belt CHECK (untimed -> start/end NULL). To CLEAR a
    timed row's instants, call this with new_schedule_kind='untimed' and all time
    args left at their defaults (NULL / 0); this forces start_at/end_at to NULL
    and open_ended to 0 in the UPDATE below.

    grill-final M-3: slot_exclusive is RE-RESOLVED from the kind-policy against
    the NEW schedule_kind inside the same CAS UPDATE (resolve_slot_exclusive
    with day_block=False). Without this, a timed appointment carried its
    exclusive=1 into all_day (minting a day brick), and an all_day appointment
    carried its 0 into timed (a non-occupying meeting, double-bookable). An
    EXPLICIT day-block is NOT expressible through this verb: transition first,
    then re-acquire exclusivity via event_promote_exclusive (which passes the
    slot predicate).

    grill-final M-2: the all_day validator runs against the NEW schedule_kind,
    so a transition to all_day with a missing instant raises ValueError on
    every DB (fresh AND migrated).
    """
    if new_schedule_kind == "untimed" and (
            new_start is not None or new_end is not None or open_ended):
        raise ValueError(
            "untimed transition must not carry time args "
            "(new_start=%r new_end=%r open_ended=%r); untimed rows hold no instant"
            % (new_start, new_end, open_ended))
    canonical(new_start)
    canonical(new_end)
    validate_interval(new_start, new_end)   # grill-5: reject reversed interval
    # grill-final M-2: validate against the TARGET schedule_kind.
    validate_all_day_instants(new_schedule_kind, new_start, new_end)
    conn.execute("BEGIN IMMEDIATE;")
    try:
        row = conn.execute("SELECT kind FROM event WHERE id=?;",
                           (eid,)).fetchone()
        if row is None:
            conn.commit()
            return MutationResult.NOT_FOUND
        kind = row[0]
        # grill-final M-3: exclusivity follows the kind-policy of the TARGET
        # schedule_kind (day_block=False -- explicit day-blocks re-acquire via
        # event_promote_exclusive after the transition).
        new_exclusive = resolve_slot_exclusive(kind, new_schedule_kind,
                                               task_kind=None, day_block=False)
        # v5.1: guard for any exclusive transition target that has a start
        # instant (timed OR all_day). untimed target has NULL start -> no slot.
        guard = is_blocker(new_exclusive, new_start)
        cee = _cand_eff_end(new_start, new_end, open_ended) if new_start else None
        res = _cas_slot_update(
            conn, eid, version,
            "schedule_kind=?, start_at=?, end_at=?, open_ended=?, slot_exclusive=?",
            [new_schedule_kind, new_start, new_end, open_ended, new_exclusive],
            new_start, cee, guard)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise


def event_promote_exclusive(conn, eid, version):
    """Promote slot_exclusive 0->1 on a timed event. Must pass the slot predicate
    (against currently-live exclusive rows), self excluded. Returns MutationResult.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        row = conn.execute(
            "SELECT schedule_kind, start_at, end_at, open_ended, slot_exclusive "
            "FROM event WHERE id=?;", (eid,)).fetchone()
        if row is None:
            conn.commit()
            return MutationResult.NOT_FOUND
        sk, sa, ea, oe, cur_ex = row
        # v5.1: after promotion the row is exclusive; guard the slot if it has a
        # start instant (timed OR all_day). untimed has NULL start -> no slot.
        guard = is_blocker(1, sa)
        cee = _cand_eff_end(sa, ea, oe) if sa else None
        res = _cas_slot_update(
            conn, eid, version,
            "slot_exclusive=1",
            [],
            sa, cee, guard)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise


# ══════════════════════════════════════════════════════════════════════════
# DGN-179 verb-delta (spec v2): D1 metadata verb + D2 participant junction.
# ══════════════════════════════════════════════════════════════════════════

# D1 -- metadata columns per kind. Structurally excludes every slot field
# (start_at/end_at/open_ended/slot_exclusive/schedule_kind) and every derivation
# input -> event_set_meta needs no overlap guard and no recompute dependency.
META_COLS_COMMON = ("title", "note")
META_COLS_BY_KIND = {
    "appointment": ("location", "location_url", "purpose", "summary"),
    "task": (),  # extended when the task CLI is built (no caller today)
}


def _meta_allowed_cols(kind):
    return set(META_COLS_COMMON) | set(META_COLS_BY_KIND.get(kind, ()))


def event_set_meta(conn, eid, version, fields):
    """D1: metadata-only update under version CAS. fields: {col: value}.
    Allowed cols = META_COLS_COMMON + META_COLS_BY_KIND[row.kind]. No slot field
    is reachable (allowlist excludes them structurally) -> no overlap guard, no
    recompute dependency. Legal on settled rows (summary is written post-meeting;
    safe under the F-A seal -- meta columns feed no derivation).

    Bumps version + updated_at. Returns MutationResult:
      APPLIED / CAS_FAIL / NOT_FOUND. OVERLAP_REJECT unreachable (no guard).
    Raises ValueError on: empty fields; unknown/kind-illegal column; title
    present but empty/None (NOT NULL contract).

    MIN-7b (spec v2): the NOT_FOUND path commits the open txn before returning
    (mirrors event_move's NOT_FOUND commit) -- no dangling BEGIN IMMEDIATE.
    """
    if not fields:
        raise ValueError("event_set_meta: empty fields")
    if "title" in fields and (fields["title"] is None or fields["title"] == ""):
        raise ValueError("event_set_meta: title is NOT NULL, cannot be empty")
    conn.execute("BEGIN IMMEDIATE;")
    try:
        row = conn.execute("SELECT kind FROM event WHERE id=?;", (eid,)).fetchone()
        if row is None:
            conn.commit()                       # MIN-7b: commit before NOT_FOUND
            return MutationResult.NOT_FOUND
        kind = row[0]
        allowed = _meta_allowed_cols(kind)
        for col in fields:
            if col not in allowed:
                # roll back the open txn before raising (no partial state).
                conn.rollback()
                raise ValueError(
                    "event_set_meta: column %r not allowed for kind %r "
                    "(allowed: %s)" % (col, kind, sorted(allowed)))
        cols = sorted(fields.keys())            # deterministic SET order
        set_clause = ", ".join("%s=?" % c for c in cols)
        params = [fields[c] for c in cols]
        # reuse the CAS update machinery with NO slot guard.
        res = _cas_slot_update(
            conn, eid, version, set_clause, params,
            None, None, will_be_exclusive_timed=False)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise


def event_person_add(conn, eid, person_id):
    """D2: link a person to an event. INSERT OR IGNORE INTO event_persons
    (idempotent via PK). NO parent version bump (self-atomic, no slot, no
    derivation input; legal on settled rows). FK violations raise
    sqlite3.IntegrityError (loud; foreign_keys=ON on every connection). Returns
    the participant count for eid."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO event_persons (event_id, person_id) "
            "VALUES (?,?);", (int(eid), int(person_id)))
        n = conn.execute(
            "SELECT COUNT(*) FROM event_persons WHERE event_id=?;",
            (int(eid),)).fetchone()[0]
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise


def event_persons(conn, eid):
    """D2: [(person_id, name, aliases, relation)] via JOIN persons, ORDER BY
    p.id -- same shape as legacy appt_persons."""
    return conn.execute(
        "SELECT p.id, p.name, p.aliases, p.relation FROM event_persons ep "
        "JOIN persons p ON p.id = ep.person_id WHERE ep.event_id=? "
        "ORDER BY p.id;", (int(eid),)).fetchall()


# ── settle family: cancel (abandoned) / force_settle (done). owner + CAS. ──
def _settle(conn, eid, version, outcome, settled_by, settled_at_ts=None):
    """Shared settle path. Sets settled_at/by/outcome + recompute (returns the
    stored outcome verbatim). CAS guarded. Returns MutationResult.

    settled_at_ts (DGN-180, grill-4 finding 6(v) promotion): the surface
    completion instant (canonical UTC) -- e.g. the GTasks 'completed'
    timestamp -- lands verbatim in settled_at instead of the poll time.
    None keeps the legacy behavior byte-for-byte (settled_at = now).
    updated_at is ALWAYS now (audit truth of when we wrote)."""
    now = now_utc()
    ts = canonical(settled_at_ts) if settled_at_ts else now
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.execute(
            "UPDATE event SET settled_at=?, settled_by=?, settled_outcome=?, "
            "version=version+1, updated_at=? WHERE id=? AND version=? "
            "AND settled_at IS NULL;",
            (ts, settled_by, outcome, now, eid, version))
        if cur.rowcount == 1:
            _recompute_and_store(conn, eid, now=now)
            conn.commit()
            return MutationResult.APPLIED
        row = conn.execute("SELECT version, settled_at FROM event WHERE id=?;",
                          (eid,)).fetchone()
        conn.commit()
        if row is None:
            return MutationResult.NOT_FOUND
        # already settled or version moved -> CAS fail (idempotent-ish).
        return MutationResult.CAS_FAIL
    except Exception:
        conn.rollback()
        raise


def cancel(conn, eid, version, settled_by):
    """Cancel verb = force-settle with outcome 'abandoned'. Frees the slot
    (settled_at set -> drops out of the liveness filter)."""
    return _settle(conn, eid, version, "abandoned", settled_by)


def force_settle(conn, eid, version, settled_by, settled_at_ts=None):
    """Force-settle with outcome 'done' (the only legal path to complete a
    zero-sub / manual event). settled_at_ts: optional surface completion
    instant (DGN-180 mirror lane)."""
    return _settle(conn, eid, version, "done", settled_by, settled_at_ts)


# ── DGN-180 mirror-lane verbs (ulid-addressed; sandbox sdk_bridge port) ────
# Ground truth: Metal sandbox dgn180-adapter/sdk_bridge.py, harness 108/108.
# These are the inbound-mirror verbs: string results (not MutationResult) by
# design -- the adapter's retry classifier consumes them.

def _ulid_lookup(conn, ulid):
    """(eid, version) for a ulid, or None."""
    row = conn.execute(
        "SELECT id, version FROM event WHERE ulid=?;", (ulid,)).fetchone()
    return None if row is None else (row[0], row[1])


def _mirror_overlap_post_check(conn, eid):
    """After a bypass-lane apply, detect whether the row now overlaps a live
    exclusive blocker (v3 MAJOR-5: apply anyway, notify once). Returns a
    warning string or None."""
    row = conn.execute(
        "SELECT slot_exclusive, start_at, end_at, open_ended "
        "FROM event WHERE id=?;", (eid,)).fetchone()
    if row is None:
        return None
    slot_ex, sa, ea, oe = row
    if not is_blocker(slot_ex, sa):
        return None
    cee = _cand_eff_end(sa, ea, oe)
    sql = ("SELECT e.ulid FROM event e WHERE " + LIVE_FILTER +
           " AND ? < " + EFF_END_SQL + " AND e.start_at < ? AND e.id != ? "
           "LIMIT 1;")
    hit = conn.execute(sql, (sa, cee, eid)).fetchone()
    if hit:
        return "overlap with event %s after bypass apply" % hit[0]
    return None


def unsettle(conn, ulid, by):
    """DGN-180 verb (v3 N180-4): reopen done->open ONLY.
    - settled_outcome must be 'done' (abandoned/expired revival refused).
    - CAS on version; clears settled_at/by/outcome; version+1; updated_at;
      recompute (past deadline re-derives 'expired' -- OQ-2 ruling).
    - Inbound lane = predicate bypass + overlap post-check (v3 MAJOR-5).
    Returns (result, overlap_warning) with result in
    'applied' | 'refused' | 'cas_fail' | 'not_found'."""
    found = _ulid_lookup(conn, ulid)
    if found is None:
        return "not_found", None
    eid, version = found
    row = conn.execute(
        "SELECT settled_outcome FROM event WHERE id=?;", (eid,)).fetchone()
    if row is None:
        return "not_found", None
    if row[0] != "done":
        return "refused", None  # abandoned/expired/never-settled: no revival
    now = now_utc()

    def run():
        conn.execute("BEGIN IMMEDIATE;")
        try:
            cur = conn.execute(
                "UPDATE event SET settled_at=NULL, settled_by=NULL, "
                "settled_outcome=NULL, version=version+1, updated_at=? "
                "WHERE id=? AND version=? AND settled_outcome='done';",
                (now, eid, version))
            if cur.rowcount == 1:
                _recompute_and_store(conn, eid, now=now)
                conn.commit()
                return "applied"
            conn.commit()
            return "cas_fail"
        except Exception:
            conn.rollback()
            raise

    res = with_retry(run)
    warning = _mirror_overlap_post_check(conn, eid) if res == "applied" else None
    return res, warning


def bypass_schedule_apply(conn, ulid, new_schedule_kind, new_start, new_end):
    """v3 MAJOR-5 inbound ground-truth lane: the owner's surface schedule
    edit is applied WITHOUT the FG-1 predicate (bypass), then overlap
    post-check -> caller notifies. Invariants kept: canonical validation,
    interval validation, schedule_kind re-derivation, untimed instants
    cleared, version+1, updated_at, recompute.
    open_ended coherence (v4 ruling 1 / grill-4 finding 10): a materialized
    end clears open_ended; untimed clears it; a timed apply that PRESERVES
    end NULL (open-ended placeholder rule, caller-side guard) preserves the
    existing flag. Returns (result, overlap_warning).

    DGN-240 (spec v3 1.6, T3):
      - untimed gate (M-C): a recurrence instance may NEVER transition to
        untimed (identity/lapse would break). Classified terminal result
        ('rejected_untimed', None), row unchanged -- not an exception, not
        a retryable failure (adapter terminal handling = T12/S8).
      - exception stamping (M-B: this verb is the SOLE stamping owner): a
        schedule-shape edit on a recurrence row sets rec_exception=1 in the
        SAME txn -- the row detaches from rule governance (GCal drag /
        GTasks due edit / CLI move all pass through here). content_update
        does NOT stamp (version-bump guard chain already protects it)."""
    if new_schedule_kind == "untimed":
        new_start = None
        new_end = None
    canonical(new_start)
    canonical(new_end)
    validate_interval(new_start, new_end)
    found = _ulid_lookup(conn, ulid)
    if found is None:
        return "not_found", None
    eid, version = found
    rec_row = conn.execute(
        "SELECT recurrence_id FROM event WHERE id=?;", (eid,)).fetchone()
    is_recurrence = rec_row is not None and rec_row[0] is not None
    if is_recurrence and new_schedule_kind == "untimed":
        return "rejected_untimed", None          # M-C: no change, terminal
    stamp_sql = ", rec_exception=1" if is_recurrence else ""
    now = now_utc()

    def run():
        conn.execute("BEGIN IMMEDIATE;")
        try:
            cur = conn.execute(
                "UPDATE event SET schedule_kind=?, start_at=?, end_at=?, "
                "open_ended=CASE WHEN ? IS NOT NULL THEN 0 "
                "WHEN ?='untimed' THEN 0 ELSE open_ended END"
                + stamp_sql +
                ", version=version+1, updated_at=? WHERE id=? AND version=?;",
                (new_schedule_kind, new_start, new_end,
                 new_end, new_schedule_kind, now, eid, version))
            if cur.rowcount == 1:
                _recompute_and_store(conn, eid, now=now)
                conn.commit()
                return "applied"
            conn.commit()
            return "cas_fail"
        except Exception:
            conn.rollback()
            raise

    res = with_retry(run)
    warning = _mirror_overlap_post_check(conn, eid) if res == "applied" else None
    return res, warning


def bypass_event_add(conn, kind, title, schedule_kind, start_at, end_at,
                     owning_agent, created_by, note=None):
    """dec-009 force-adopt lane (grill-6 F5): the owner's hand-made calendar
    entry is reality -- insert WITHOUT the FG-1 overlap guard (canonical
    validation, kind-policy slot_exclusive, recompute, stamps all intact;
    only the overlap predicate is bypassed). Returns (eid, overlap_warning)."""
    canonical(start_at)
    canonical(end_at)
    validate_interval(start_at, end_at)
    slot_exclusive = resolve_slot_exclusive(kind, schedule_kind)
    now = now_utc()
    ulid = new_ulid()
    cols = ("ulid, kind, title, note, schedule_kind, start_at, end_at, "
            "display_tz, open_ended, slot_exclusive, completion_rule, "
            "owning_agent, created_by, created_at, updated_at")
    vals = [ulid, kind, title, note, schedule_kind, start_at, end_at,
            "Asia/Seoul", 0, slot_exclusive, "all",
            owning_agent, created_by, now, now]

    def run():
        conn.execute("BEGIN IMMEDIATE;")
        try:
            conn.execute(
                "INSERT INTO event (" + cols + ") VALUES (" +
                ",".join("?" for _ in vals) + ")", vals)
            eid = conn.execute("SELECT id FROM event WHERE ulid=?;",
                               (ulid,)).fetchone()[0]
            _recompute_and_store(conn, eid, now=now)
            conn.commit()
            return eid
        except Exception:
            conn.rollback()
            raise

    eid = with_retry(run)
    warning = _mirror_overlap_post_check(conn, eid)
    return eid, warning


def recompute(conn, ulid):
    """Sweep lane (DGN-180): refresh the derived status cache for one row
    (status is a derived cache -- no version bump, no payload mutation).
    Returns the stored status or None if the row is missing."""
    found = _ulid_lookup(conn, ulid)
    if found is None:
        return None
    eid, _version = found

    def run():
        conn.execute("BEGIN IMMEDIATE;")
        try:
            st = _recompute_and_store(conn, eid)
            conn.commit()
            return st
        except Exception:
            conn.rollback()
            raise

    return with_retry(run)


# ── sub_event lifecycle: add / done / reopen / tombstone. ──────────────────
# Each = sub write + in-txn bubble-up recompute under parent version CAS.
def _cas_sub_txn(conn, eid, mutate, now=None):
    """Parent version CAS + sub mutation + in-txn recompute. Caller supplies an
    OPEN write txn. mutate(conn, eid) does the sub-row change. Returns
    MutationResult (APPLIED / CAS_FAIL / NOT_FOUND)."""
    row = conn.execute("SELECT version FROM event WHERE id=?;", (eid,)).fetchone()
    if row is None:
        return MutationResult.NOT_FOUND
    version = row[0]
    cur = conn.execute("UPDATE event SET version=version+1 WHERE id=? AND version=?;",
                      (eid, version))
    if cur.rowcount != 1:
        return MutationResult.CAS_FAIL
    mutate(conn, eid)
    _recompute_and_store(conn, eid, now=now)
    return MutationResult.APPLIED


def sub_add(conn, eid, owning_agent, kind=None, ref=None):
    """Add a sub_event under a parent, bubble-up recompute in same txn."""
    ulid = new_ulid()
    now = now_utc()
    def mut(c, e):
        c.execute("INSERT INTO sub_event (ulid, event_id, owning_agent, kind, ref, "
                  "done, tombstone, created_at) VALUES (?,?,?,?,?,0,0,?);",
                  (ulid, e, owning_agent, kind, ref, now))
    conn.execute("BEGIN IMMEDIATE;")
    try:
        res = _cas_sub_txn(conn, eid, mut, now=now)
        conn.commit()
        return res, ulid
    except Exception:
        conn.rollback()
        raise


def sub_done(conn, eid, sub_ulid):
    """Mark a sub done=1. Bubble-up recompute."""
    now = now_utc()
    def mut(c, e):
        c.execute("UPDATE sub_event SET done=1, settled_at=? WHERE ulid=? AND event_id=?;",
                  (now, sub_ulid, e))
    conn.execute("BEGIN IMMEDIATE;")
    try:
        res = _cas_sub_txn(conn, eid, mut, now=now)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise


def sub_reopen(conn, eid, sub_ulid):
    """Reopen a sub (done 1->0). NO direct parent flip: the parent status is
    re-derived by recompute in the same txn (settled parent stays settled --
    outcome preserved)."""
    def mut(c, e):
        c.execute("UPDATE sub_event SET done=0, settled_at=NULL WHERE ulid=? AND event_id=?;",
                  (sub_ulid, e))
    conn.execute("BEGIN IMMEDIATE;")
    try:
        res = _cas_sub_txn(conn, eid, mut)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise


def sub_tombstone(conn, eid, sub_ulid):
    """Tombstone a sub (delete != complete). Invisible to derivation. Recompute."""
    now = now_utc()
    def mut(c, e):
        c.execute("UPDATE sub_event SET tombstone=1, settled_at=? WHERE ulid=? AND event_id=?;",
                  (now, sub_ulid, e))
    conn.execute("BEGIN IMMEDIATE;")
    try:
        res = _cas_sub_txn(conn, eid, mut, now=now)
        conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise


def uninstall(conn, agent):
    """Owning-agent uninstall: tombstone ALL subs owned by `agent` across ALL
    events, recompute EVERY affected parent, in ONE txn (m1 orphan-free)."""
    now = now_utc()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        affected = [r[0] for r in conn.execute(
            "SELECT DISTINCT event_id FROM sub_event "
            "WHERE owning_agent=? AND tombstone=0;", (agent,)).fetchall()]
        conn.execute("UPDATE sub_event SET tombstone=1, settled_at=? "
                    "WHERE owning_agent=? AND tombstone=0;", (now, agent))
        for eid in affected:
            conn.execute("UPDATE event SET version=version+1 WHERE id=?;", (eid,))
            _recompute_and_store(conn, eid, now=now)
        conn.commit()
        return len(affected)
    except Exception:
        conn.rollback()
        raise


# ── reschedule queue primitives (M1): enqueue / claim / apply. ─────────────
# apply on CAS-fail re-queries -> applied if target already at proposed, else rejected.
def reschedule_enqueue(conn, event_ulid, requester_agent, proposed_start,
                       proposed_end, reason=None):
    """Enqueue a durable reschedule request."""
    canonical(proposed_start)
    canonical(proposed_end)
    validate_interval(proposed_start, proposed_end)   # grill-5: reject reversed
    ulid = new_ulid()
    now = now_utc()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute(
            "INSERT INTO reschedule_requests (ulid, event_ulid, requester_agent, "
            "proposed_start, proposed_end, reason, status, created_at) "
            "VALUES (?,?,?,?,?,?, 'queued', ?);",
            (ulid, event_ulid, requester_agent, proposed_start, proposed_end, reason, now))
        conn.commit()
        return ulid
    except Exception:
        conn.rollback()
        raise


def reschedule_claim(conn, req_ulid):
    """Claim a queued request (queued->claimed) under CAS. Returns True iff
    THIS claim landed (lease). Prevents double-apply on drainer crash."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.execute(
            "UPDATE reschedule_requests SET status='claimed' "
            "WHERE ulid=? AND status='queued';", (req_ulid,))
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        conn.rollback()
        raise


def reschedule_apply(conn, req_ulid):
    """Apply a claimed request: move the target event via the slot predicate.
    On event CAS-fail, re-query the event: if it is already at the proposed
    slot, mark applied (idempotent completion); else mark rejected.
    Returns final request status string.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        req = conn.execute(
            "SELECT event_ulid, proposed_start, proposed_end FROM reschedule_requests "
            "WHERE ulid=? AND status='claimed';", (req_ulid,)).fetchone()
        if req is None:
            conn.commit()
            return None  # not claimed / not found
        ev_ulid, ps, pe = req
        ev = conn.execute(
            "SELECT id, version, slot_exclusive, schedule_kind FROM event WHERE ulid=?;",
            (ev_ulid,)).fetchone()
        if ev is None:
            conn.execute("UPDATE reschedule_requests SET status='rejected', resolved_at=? "
                        "WHERE ulid=?;", (now_utc(), req_ulid))
            conn.commit()
            return "rejected"
        eid, version, slot_ex, sk = ev
        # v5.1: guard if the target is an exclusive blocker with a start instant.
        guard = is_blocker(slot_ex, ps)
        cee = _cand_eff_end(ps, pe, 0)
        res = _cas_slot_update(
            conn, eid, version, "start_at=?, end_at=?", [ps, pe], ps, cee, guard)
        if res == MutationResult.APPLIED:
            final = "applied"
        else:
            # re-query: idempotent completion if already at proposed slot.
            cur_row = conn.execute(
                "SELECT start_at, end_at FROM event WHERE id=?;", (eid,)).fetchone()
            if cur_row is not None and cur_row[0] == ps and cur_row[1] == pe:
                final = "applied"
            else:
                final = "rejected"
        conn.execute("UPDATE reschedule_requests SET status=?, resolved_at=? WHERE ulid=?;",
                    (final, now_utc(), req_ulid))
        conn.commit()
        return final
    except Exception:
        conn.rollback()
        raise


# ══════════════════════════════════════════════════════════════════════════
# DGN-240 routine verbs (spec v3 T6): routine_def lifecycle + health.
# Instance machinery (materialize / regen / group-cancel / occupied) lives in
# database/routine_roller.py (single implementation shared with the nightly
# roller); these verbs lazily import it (roller imports lifekit at top level,
# so the import must be deferred here to avoid a cycle).
# English/ASCII only in this block.
# ══════════════════════════════════════════════════════════════════════════

ROUTINE_VALID_DAYS = 56          # autonomous renewal window (spec 1.3/2.5)
_TOD_RE = re.compile(r"^\d{2}:\d{2}$")


def _roller_mod():
    """Deferred import of the shared instance engine (cycle guard)."""
    if SCRIPT_DIR not in sys.path:
        sys.path.insert(0, SCRIPT_DIR)
    import routine_roller
    return routine_roller


def _projection_mod():
    if SCRIPT_DIR not in sys.path:
        sys.path.insert(0, SCRIPT_DIR)
    import routine_projection
    return routine_projection


def routine_today(tz_name="Asia/Seoul"):
    """Local calendar date (ISO string) in the routine display tz."""
    return datetime.datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def routine_def_get(conn, token):
    """routine_def row (sqlite3.Row-like dict) by def ulid, recurrence_id or
    integer id. None if absent."""
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM routine_def WHERE ulid=? OR recurrence_id=?;",
            (token, token)).fetchone()
        if row is None and str(token).isdigit():
            row = conn.execute(
                "SELECT * FROM routine_def WHERE id=?;", (int(token),)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.row_factory = old_factory


def _routine_validate(cadence, schedule_kind, time_of_day, kind):
    """Shared registration/update validation (spec 1.3/1.4/1.6)."""
    if kind != "task":
        # v1 scope: appointment routines (hard-blocker materialization) are a
        # follow-up ticket (OQ-12). Schema keeps the column; the verb gates.
        raise ValueError("routine_add: kind='appointment' is not supported "
                         "in v1 (OQ-12); only kind='task'")
    _projection_mod().parse_cadence(cadence)     # closed-grammar validation
    if schedule_kind not in ("timed", "all_day"):
        raise ValueError("routine schedule_kind must be timed|all_day, got %r"
                         % schedule_kind)
    if schedule_kind == "timed" and (
            time_of_day is None or not _TOD_RE.match(time_of_day)):
        raise ValueError("timed routine requires time_of_day HH:MM, got %r"
                         % time_of_day)


def _valid_until_for(end_date, today):
    """Spec 1.3 initialization: autonomous -> today+56d; end_date defs carry
    valid_until=end_date for shape consistency (it never binds upper())."""
    if end_date is not None:
        return end_date
    d = datetime.date.fromisoformat(today) + timedelta(days=ROUTINE_VALID_DAYS)
    return d.isoformat()


def routine_add(conn, title, cadence, schedule_kind, time_of_day=None,
                duration_min=None, area_id=None, project_id=None,
                purpose=None, start_date=None, end_date=None, kind="task",
                display_tz="Asia/Seoul", created_by=None, today=None,
                materialize=True, state_conn=None, now=None):
    """Register a routine_def + inline first materialization (spec 8.2).
    Returns dict(def_id, ulid, recurrence_id, materialized_dates)."""
    _routine_validate(cadence, schedule_kind, time_of_day, kind)
    if not title or not str(title).strip():
        raise ValueError("routine_add: title required")
    if created_by is None:
        created_by = _facade_agent()
    today = today or routine_today(display_tz)
    start_date = start_date or today
    valid_until = _valid_until_for(end_date, today)
    ulid = new_ulid()
    rid = "rd:%s" % ulid
    ts = now_utc()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute(
            "INSERT INTO routine_def (ulid, recurrence_id, title, kind, "
            "cadence, schedule_kind, time_of_day, duration_min, display_tz, "
            "area_id, project_id, purpose, status, start_date, end_date, "
            "valid_until, rule_effective_from, created_by, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?);",
            (ulid, rid, title, kind, cadence, schedule_kind, time_of_day,
             duration_min, display_tz, area_id, project_id, purpose,
             start_date, end_date, valid_until, today, created_by, ts, ts))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    dates = []
    if materialize:
        rr = _roller_mod()
        defn = routine_def_get(conn, ulid)
        dates = rr.materialize_def(conn, state_conn, defn,
                                   today=today, now=now)
    return {"def_id": routine_def_get(conn, ulid)["id"], "ulid": ulid,
            "recurrence_id": rid, "materialized_dates": dates}


_ROUTINE_UPD_COLS = ("title", "cadence", "schedule_kind", "time_of_day",
                     "duration_min", "area_id", "project_id", "purpose",
                     "end_date")


def routine_update(conn, def_token, changes, effective_from=None,
                   today=None, state_conn=None, now=None):
    """Mode B rule change (spec 4.2) -- convergent, not atomic:
      1. single txn: CAS bump def (version, updated_at) + apply changes +
         persist rule_effective_from (F-A).
      2. cancel-set regen (row-per-txn, roller regen path).
      3. materialize per new rule over [max(today, effective_from), upper].
    Returns dict(result, cancelled, deleted, materialized_dates)."""
    defn = routine_def_get(conn, def_token)
    if defn is None:
        return {"result": "not_found"}
    today = today or routine_today(defn["display_tz"])
    effective_from = effective_from or today
    merged = dict(defn)
    for col in changes:
        if col not in _ROUTINE_UPD_COLS:
            raise ValueError("routine_update: column %r not updatable "
                             "(allowed: %s)" % (col, list(_ROUTINE_UPD_COLS)))
        merged[col] = changes[col]
    _routine_validate(merged["cadence"], merged["schedule_kind"],
                      merged["time_of_day"], merged["kind"])
    # end_date change re-derives valid_until per the 1.3 initialization rule.
    new_valid_until = (_valid_until_for(merged["end_date"], today)
                       if "end_date" in changes else defn["valid_until"])
    ts = now_utc()
    sets = ", ".join("%s=?" % c for c in sorted(changes.keys()))
    params = [changes[c] for c in sorted(changes.keys())]
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.execute(
            "UPDATE routine_def SET " + (sets + ", " if sets else "") +
            "valid_until=?, rule_effective_from=?, version=version+1, "
            "updated_at=? WHERE id=? AND version=?;",
            params + [new_valid_until, effective_from, ts,
                      defn["id"], defn["version"]])
        if cur.rowcount != 1:
            conn.rollback()
            return {"result": "cas_fail"}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # steps 2+3: best-effort catch-up; nightly conformance converges (2.4).
    rr = _roller_mod()
    fresh = routine_def_get(conn, def_token)
    cancelled, deleted = rr.cancel_set_regen(conn, state_conn, fresh,
                                             effective_from, now=now)
    dates = rr.materialize_def(conn, state_conn, fresh, today=today, now=now,
                               floor_date=max(today, effective_from))
    return {"result": "applied", "cancelled": cancelled, "deleted": deleted,
            "materialized_dates": dates}


def routine_pause(conn, def_token, today=None, state_conn=None, now=None):
    """pause = cancel-set treatment + status='paused' (spec 4.2)."""
    defn = routine_def_get(conn, def_token)
    if defn is None:
        return {"result": "not_found"}
    today = today or routine_today(defn["display_tz"])
    ts = now_utc()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.execute(
            "UPDATE routine_def SET status='paused', version=version+1, "
            "updated_at=? WHERE id=? AND version=? AND status='active';",
            (ts, defn["id"], defn["version"]))
        if cur.rowcount != 1:
            conn.rollback()
            return {"result": "cas_fail"}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    rr = _roller_mod()
    cancelled, deleted = rr.cancel_set_regen(conn, state_conn, defn,
                                             today, now=now)
    return {"result": "applied", "cancelled": cancelled, "deleted": deleted}


def routine_resume(conn, def_token, today=None, state_conn=None, now=None):
    """resume: status='active' (+ autonomous valid_until reset, M7) + inline
    materialize. end_date defs skip the valid_until reset (unused, 2.2).
    pause/resume never touch rule_effective_from (spec 4.2)."""
    defn = routine_def_get(conn, def_token)
    if defn is None:
        return {"result": "not_found"}
    today = today or routine_today(defn["display_tz"])
    ts = now_utc()
    new_valid = (_valid_until_for(None, today)
                 if defn["end_date"] is None else defn["valid_until"])
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.execute(
            "UPDATE routine_def SET status='active', valid_until=?, "
            "version=version+1, updated_at=? "
            "WHERE id=? AND version=? AND status='paused';",
            (new_valid, ts, defn["id"], defn["version"]))
        if cur.rowcount != 1:
            conn.rollback()
            return {"result": "cas_fail"}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    rr = _roller_mod()
    fresh = routine_def_get(conn, def_token)
    dates = rr.materialize_def(conn, state_conn, fresh, today=today, now=now)
    return {"result": "applied", "materialized_dates": dates}


def routine_retire(conn, def_token, today=None, state_conn=None, now=None,
                   by="routine-retire"):
    """Group cleanup (spec 5.4): cancel ALL future unsettled instances
    (exceptions INCLUDED -- retire means stop) + def.status='retired'
    (row preserved). Past/settled rows = lifelog, untouchable."""
    defn = routine_def_get(conn, def_token)
    if defn is None:
        return {"result": "not_found"}
    today = today or routine_today(defn["display_tz"])
    ts = now_utc()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.execute(
            "UPDATE routine_def SET status='retired', version=version+1, "
            "updated_at=? WHERE id=? AND version=? AND status != 'retired';",
            (ts, defn["id"], defn["version"]))
        if cur.rowcount != 1:
            conn.rollback()
            return {"result": "cas_fail"}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    rr = _roller_mod()
    n = rr.retire_group_cancel(conn, state_conn, defn, today, now=now, by=by)
    return {"result": "applied", "cancelled": n}


def routine_exception(conn, event_ulid, schedule=None, content=None):
    """Mode A this-instance-only edit (spec 4.1) -- thin wrapper.
    schedule = (new_schedule_kind, new_start, new_end) -> delegates to
    bypass_schedule_apply (which owns the rec_exception stamp, M-B).
    content = {title/note} -> event_set_meta path (NO stamp; version bump
    already makes the row regen-untouchable). Returns result string."""
    if schedule is None and not content:
        raise ValueError("routine_exception: nothing to change")
    res = "noop"
    if schedule is not None:
        sk, ns, ne = schedule
        res, _warn = bypass_schedule_apply(conn, event_ulid, sk, ns, ne)
        if res != "applied":
            return res
    if content:
        found = _ulid_lookup(conn, event_ulid)
        if found is None:
            return "not_found"
        eid, version = found
        mres = event_set_meta(conn, eid, version, dict(content))
        res = mres if isinstance(mres, str) else "applied"
    return res


# ── health aggregation (spec 6.1 -- structural tombstones in NO bucket) ────
ANOMALY_RATE_MIN_ELAPSED = 4     # last-14d elapsed occurrences (OQ-1)
ANOMALY_RATE_THRESHOLD = 0.30
ANOMALY_STREAK_D = 5
ANOMALY_STREAK_WI = 3

STRUCTURAL_SETTLERS = ("rule-regen", "routine-retire")


def _def_streak_threshold(cadence):
    return ANOMALY_STREAK_D if cadence == "D" else ANOMALY_STREAK_WI


def routine_health(conn, window=28, as_of=None, include_retired=False):
    """Per-def health JSON rows (spec 6.1). Pure read.
    scheduled = COUNT(DISTINCT rec_date) of elapsed instances in the window,
    structural tombstones (settled_by rule-regen/routine-retire) excluded
    from EVERY bucket (M-E). rate = done/scheduled."""
    as_of = as_of or routine_today()
    lo = (datetime.date.fromisoformat(as_of)
          - timedelta(days=int(window))).isoformat()
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        q = "SELECT * FROM routine_def"
        if not include_retired:
            q += " WHERE status != 'retired'"
        defs = [dict(r) for r in conn.execute(q + " ORDER BY id;")]
        out = []
        for defn in defs:
            rid = defn["recurrence_id"]
            rows = [dict(r) for r in conn.execute(
                "SELECT rec_date, status, settled_at, settled_by, "
                "settled_outcome FROM event WHERE recurrence_id=? "
                "AND rec_date IS NOT NULL AND rec_date <= ? "
                "AND NOT (settled_outcome IS 'abandoned' AND settled_by IN "
                "('rule-regen','routine-retire')) ORDER BY rec_date;",
                (rid, as_of))]
            def _bucket(r):
                if r["settled_outcome"] == "done":
                    return "done"
                if (r["settled_outcome"] == "abandoned"
                        and r["settled_by"] == "roller-lapse"):
                    return "missed"
                if r["settled_outcome"] == "abandoned":
                    return "skipped"
                if r["status"] == "expired":
                    return "missed"
                return "open"

            per_date = {}
            for r in rows:
                # one bucket per (rid, rec_date): done wins over miss/skip
                b = _bucket(r)
                cur = per_date.get(r["rec_date"])
                order = {"done": 3, "skipped": 2, "missed": 1, "open": 0}
                if cur is None or order[b] > order[cur]:
                    per_date[r["rec_date"]] = b

            # m-3 (grill-final): an as_of-day occurrence counts as elapsed
            # only once resolved -- a still-open same-day row would
            # transiently understate the morning rate via the denominator.
            def _elapsed(d):
                return d < as_of or per_date.get(d, "open") != "open"

            in_win = [r for r in rows if r["rec_date"] > lo]
            sched_dates = sorted(
                d for d in {r["rec_date"] for r in in_win} if _elapsed(d))
            scheduled = len(sched_dates)
            done = sum(1 for d in sched_dates if per_date.get(d) == "done")
            missed = sum(1 for d in sched_dates if per_date.get(d) == "missed")
            skipped = sum(1 for d in sched_dates if per_date.get(d) == "skipped")
            displaced = conn.execute(
                "SELECT COUNT(DISTINCT detail) FROM roller_log "
                "WHERE category='displaced' AND recurrence_id=? "
                "AND detail > ? AND detail <= ?;", (rid, lo, as_of)).fetchone()[0]
            rate = round(done / scheduled, 2) if scheduled else None
            # consec_miss over the most recent elapsed occurrences (all time)
            consec = 0
            for d in sorted(per_date.keys(), reverse=True):
                b = per_date[d]
                if b == "missed":
                    consec += 1
                elif b == "open":
                    continue                     # not yet resolved: neutral
                else:
                    break                        # done/skipped breaks streak
            # anomaly (2.5 constants; conformance_frozen from roller_log)
            frozen = conn.execute(
                "SELECT 1 FROM roller_log WHERE category='conformance_frozen' "
                "AND recurrence_id=? AND ts > ? LIMIT 1;",
                (rid, defn["updated_at"])).fetchone() is not None
            lo14 = (datetime.date.fromisoformat(as_of)
                    - timedelta(days=14)).isoformat()
            d14 = sorted(d for d in {r["rec_date"] for r in rows
                                     if r["rec_date"] > lo14} if _elapsed(d))
            done14 = sum(1 for d in d14 if per_date.get(d) == "done")
            anomaly = None
            if frozen:
                anomaly = "conformance_frozen"
            elif (len(d14) >= ANOMALY_RATE_MIN_ELAPSED
                    and (done14 / len(d14)) < ANOMALY_RATE_THRESHOLD):
                anomaly = "rate"
            elif (defn["cadence"] is not None
                    and consec >= _def_streak_threshold(defn["cadence"])):
                anomaly = "streak"
            area = conn.execute("SELECT name FROM areas WHERE id=?;",
                                (defn["area_id"],)).fetchone() \
                if defn["area_id"] else None
            project = None
            if defn["project_id"]:
                pr = conn.execute("SELECT title FROM projects WHERE id=?;",
                                  (defn["project_id"],)).fetchone()
                project = pr[0] if pr else None
            out.append({
                "rid": rid, "title": defn["title"],
                "purpose": defn["purpose"],
                "area": area[0] if area else None, "project": project,
                "cadence": defn["cadence"], "status": defn["status"],
                "scheduled": scheduled, "done": done, "missed": missed,
                "skipped": skipped, "displaced": displaced,
                "rate": rate, "consec_miss": consec, "anomaly": anomaly,
                "valid_until": defn["valid_until"],
            })
        return out
    finally:
        conn.row_factory = old_factory


def routine_ack_anomaly(conn, def_token, snapshot):
    """Retro choice 4 (spec 6.3): record anomaly_ack snapshot JSON
    ({type, rate, streak, ts}) under CAS."""
    defn = routine_def_get(conn, def_token)
    if defn is None:
        return "not_found"
    ts = now_utc()
    payload = dict(snapshot)
    payload.setdefault("ts", ts)
    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.execute(
            "UPDATE routine_def SET anomaly_ack=?, version=version+1, "
            "updated_at=? WHERE id=? AND version=?;",
            (json.dumps(payload), ts, defn["id"], defn["version"]))
        if cur.rowcount != 1:
            conn.rollback()
            return "cas_fail"
        conn.commit()
        return "applied"
    except Exception:
        conn.rollback()
        raise


# ── mirror outbox enqueue hook (DGN-258) ─────────────────────────────────────
# Self-locating path: SCRIPT_DIR = database/ -> ../mirror/mirror_state.db.
# Idempotent insert: unique-pending index (idx_outbox_ulid_pending) on
# (event_ulid) WHERE status IN ('queued','claimed') collapses duplicates.
# Pattern is byte-identical to routine_roller.outbox_enqueue (single home kept
# there; this copy avoids importing routine_roller into the interactive path
# which would create a circular import: routine_roller -> lifekit -> .).
#
# Flag gate (DGN-259): when the mirror module is disabled (MIRROR_MODULE=off
# in config/lifekit.conf, or the module directory is absent), this hook is a
# SILENT no-op. A warning is emitted to stderr only if the module is enabled
# but the state DB is unreachable (broken module, not disabled module).
# The gate is cheap: one stat check per process start, cached in _MIRROR_ENABLED.

_STATE_DB_PATH = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "mirror", "mirror_state.db"))

# Module-presence sentinel: ../mirror/ directory (adjacent to database/).
_MIRROR_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "mirror"))

# Config file: ../config/lifekit.conf (standard agent config location).
_LIFEKIT_CONF_PATH = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "config", "lifekit.conf"))

_MIRROR_ENABLED = None          # None = not yet resolved; True/False = cached


def _mirror_module_enabled():
    """Return True iff the mirror module is present AND not explicitly disabled.
    Result is cached per process (one stat + one file read, then pinned).
    Disabled = MIRROR_MODULE=off in lifekit.conf OR mirror/ dir absent."""
    global _MIRROR_ENABLED
    if _MIRROR_ENABLED is not None:
        return _MIRROR_ENABLED
    # Module presence check (fast path: no dir = definitely disabled).
    if not os.path.isdir(_MIRROR_DIR):
        _MIRROR_ENABLED = False
        return False
    # Config flag check: read MIRROR_MODULE line from lifekit.conf if present.
    try:
        with open(_LIFEKIT_CONF_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("MIRROR_MODULE="):
                    val = line.split("=", 1)[1].strip().lower()
                    if val == "off":
                        _MIRROR_ENABLED = False
                        return False
    except OSError:
        pass  # conf absent or unreadable: fall through (module dir present = enabled)
    _MIRROR_ENABLED = True
    return True


# Minimal DDL subset -- only used when the state db does not exist yet
# (e.g. a fresh test sandbox). Byte-identical schema subset to routine_roller.
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


def _outbox_enqueue_best_effort(event_ulid, state_db_path=None):
    """Best-effort enqueue of event_ulid into mirror_outbox.
    Called after every interactive lifekit write that touches the event table.
    NEVER raises: on any failure, prints a one-line warning to stderr and
    returns False. The lifekit write is already committed at this point.
    Silent no-op when mirror module is disabled or absent (flag gate)."""
    if not _mirror_module_enabled():
        return False   # silent: module disabled/absent
    path = state_db_path or _STATE_DB_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sc = sqlite3.connect(path)
        sc.execute("PRAGMA journal_mode=WAL")
        sc.execute("PRAGMA busy_timeout=5000")
        sc.executescript(_STATE_MIN_DDL)
        sc.commit()
        now = now_utc()
        try:
            sc.execute(
                "INSERT INTO mirror_outbox(event_ulid, op, status, "
                "created_at, updated_at) VALUES(?, 'sync', 'queued', ?, ?)",
                (event_ulid, now, now))
            sc.commit()
        except sqlite3.IntegrityError:
            sc.rollback()   # already queued/claimed -- idempotent, not an error
        sc.close()
        return True
    except Exception as exc:
        print("mirror outbox enqueue warning: %s (ulid=%s)" % (exc, event_ulid),
              file=sys.stderr)
        return False


def _ulid_for_event_id(conn, eid):
    """Return the ulid for an event integer id, or None."""
    row = conn.execute("SELECT ulid FROM event WHERE id=?;", (eid,)).fetchone()
    return row[0] if row else None


# ── routine CLI (lifekit.sh routine <verb> ..., task-CLI 동형) ─────────────
def _routine_line(defn):
    return "%s\t%s\t%s\t%s\t%s\t%s" % (
        defn["id"], defn["ulid"], defn["title"], defn["cadence"] or "",
        defn["schedule_kind"] + (":" + defn["time_of_day"]
                                 if defn["time_of_day"] else ""),
        defn["status"])


def cli_routine(argv):
    _u = ("사용법: lifekit.sh routine "
          "add|list|show|update|pause|resume|retire|exception|health|"
          "materialize ...\n"
          "  add <title> <cadence> [field=value ...] [--new]\n"
          "      cadence: D | W:MON,WED,FRI | I:2@YYYY-MM-DD\n"
          "      field: time(HH:MM) duration area project purpose start end\n"
          "  update <def_ulid> [effective_from=YYYY-MM-DD] field=value ...\n"
          "  exception <event_ulid> field=value ... "
          "(field: start_at end_at date title note)\n"
          "  health [window=N] [as_of=YYYY-MM-DD]")
    if not argv:
        _err(_u)
    verb, rest = argv[0], argv[1:]
    conn = event_conn()
    try:
        if verb == "add":
            rest, force_new = _pop_flag(rest, "--new")   # DGN-231 pattern
            if len(rest) < 2 or not rest[0] or not rest[1]:
                _err(_u)
            title, cadence = rest[0], rest[1]
            fields = {}
            for tok in rest[2:]:
                if "=" not in tok:
                    _err("field=value 형식이 아님: %s\n%s" % (tok, _u))
                k, v = tok.split("=", 1)
                fields[k.strip()] = v
            if not force_new:
                # duplicate gate: same-title active def (spec 8.3 / DGN-231)
                dups = conn.execute(
                    "SELECT id, ulid, title, cadence, schedule_kind, "
                    "time_of_day, status FROM routine_def "
                    "WHERE title=? AND status='active';", (title,)).fetchall()
                if dups:
                    print("EXISTS %d" % len(dups))
                    for d in dups:
                        print(_routine_line({
                            "id": d[0], "ulid": d[1], "title": d[2],
                            "cadence": d[3], "schedule_kind": d[4],
                            "time_of_day": d[5], "status": d[6]}))
                    sys.exit(EXISTS_CODE)
            aid = None
            if fields.get("area"):
                aid = area_id(conn, fields["area"])
                if aid is None:
                    _err("영역 없음: %s" % fields["area"])
            pid = None
            if fields.get("project"):
                pr = conn.execute(
                    "SELECT id FROM projects WHERE title=? LIMIT 1;",
                    (fields["project"],)).fetchone()
                if pr is None:
                    _err("프로젝트 없음: %s" % fields["project"])
                pid = pr[0]
            tod = fields.get("time")
            sk = "timed" if tod else "all_day"
            try:
                res = routine_add(
                    conn, title, cadence, sk, time_of_day=tod,
                    duration_min=(int(fields["duration"])
                                  if fields.get("duration") else None),
                    area_id=aid, project_id=pid,
                    purpose=fields.get("purpose"),
                    start_date=fields.get("start"),
                    end_date=fields.get("end"))
            except ValueError as e:
                _err(str(e))
            print("%s\t%s\t%s" % (res["def_id"], res["ulid"],
                                  ",".join(res["materialized_dates"])))
        elif verb == "list":
            status = rest[0] if rest else None
            q = ("SELECT id, ulid, title, cadence, schedule_kind, "
                 "time_of_day, status FROM routine_def ")
            if status:
                rows = conn.execute(q + "WHERE status=? ORDER BY id;",
                                    (status,)).fetchall()
            else:
                rows = conn.execute(q + "ORDER BY id;").fetchall()
            for d in rows:
                print(_routine_line({
                    "id": d[0], "ulid": d[1], "title": d[2], "cadence": d[3],
                    "schedule_kind": d[4], "time_of_day": d[5],
                    "status": d[6]}))
        elif verb == "show":
            if not rest:
                _err(_u)
            defn = routine_def_get(conn, rest[0])
            if defn is None:
                _err("routine 없음: %s" % rest[0])
            for k in ("id", "ulid", "recurrence_id", "title", "kind",
                      "cadence", "schedule_kind", "time_of_day",
                      "duration_min", "area_id", "project_id", "purpose",
                      "status", "start_date", "end_date", "valid_until",
                      "rule_effective_from", "version"):
                print("%s=%s" % (k, "" if defn[k] is None else defn[k]))
        elif verb in ("update", "pause", "resume", "retire"):
            if not rest:
                _err(_u)
            token = rest[0]
            if verb == "update":
                fields = {}
                eff = None
                for tok in rest[1:]:
                    if "=" not in tok:
                        _err("field=value 형식이 아님: %s\n%s" % (tok, _u))
                    k, v = tok.split("=", 1)
                    k = k.strip()
                    if k == "effective_from":
                        eff = v
                    elif k == "time":
                        fields["time_of_day"] = v or None
                        fields.setdefault("schedule_kind",
                                          "timed" if v else "all_day")
                    elif k == "duration":
                        fields["duration_min"] = int(v)
                    elif k == "end":
                        fields["end_date"] = v or None
                    elif k in _ROUTINE_UPD_COLS:
                        fields[k] = v or None
                    else:
                        _err("수정 불가 컬럼: %s\n%s" % (k, _u))
                if not fields:
                    _err(_u)
                try:
                    res = routine_update(conn, token, fields,
                                         effective_from=eff)
                except ValueError as e:
                    _err(str(e))
            elif verb == "pause":
                res = routine_pause(conn, token)
            elif verb == "resume":
                res = routine_resume(conn, token)
            else:
                res = routine_retire(conn, token)
            if res["result"] != "applied":
                _err("routine %s 실패 (%s)" % (verb, res["result"]))
            extra = ""
            if "materialized_dates" in res:
                extra = "\t" + ",".join(res["materialized_dates"])
            if "cancelled" in res:
                extra += "\tcancelled=%s" % res["cancelled"]
            print("%s\t%s%s" % (token, res["result"], extra))
        elif verb == "exception":
            if len(rest) < 2:
                _err(_u)
            ulid_t = rest[0]
            fields = {}
            for tok in rest[1:]:
                if "=" not in tok:
                    _err("field=value 형식이 아님: %s\n%s" % (tok, _u))
                k, v = tok.split("=", 1)
                fields[k.strip()] = v
            schedule = None
            if "date" in fields:                       # all_day move
                sa, ea = all_day_instants(fields["date"])
                schedule = ("all_day", sa, ea)
            elif "start_at" in fields or "end_at" in fields:
                try:
                    ns = (_to_canonical_utc(fields["start_at"])
                          if fields.get("start_at") else None)
                    ne = (_to_canonical_utc(fields["end_at"])
                          if fields.get("end_at") else None)
                except ValueError as e:
                    _err(str(e))
                if ns and not ne:
                    # m-2 (grill-final): start-only move preserves the
                    # row's current duration instead of collapsing to a
                    # zero-length instance (end_at=start_at).
                    _row = conn.execute(
                        "SELECT start_at, end_at FROM event WHERE ulid=?;",
                        (ulid_t,)).fetchone()
                    if _row and _row[0] and _row[1]:
                        _fmt = "%Y-%m-%dT%H:%M:%SZ"
                        _dur = (datetime.datetime.strptime(_row[1], _fmt)
                                - datetime.datetime.strptime(_row[0], _fmt))
                        ne = (datetime.datetime.strptime(ns, _fmt)
                              + _dur).strftime(_fmt)
                    else:
                        ne = ns
                schedule = ("timed", ns, ne)
            content = {k: v for k, v in fields.items()
                       if k in ("title", "note")}
            try:
                res = routine_exception(conn, ulid_t, schedule=schedule,
                                        content=content or None)
            except ValueError as e:
                _err(str(e))
            if res not in ("applied", "noop"):
                _err("routine exception 실패 (%s)" % res)
            print("%s\t%s" % (ulid_t, res))
        elif verb == "health":
            window, as_of = 28, None
            i = 0
            while i < len(rest):
                tok = rest[i]
                if tok.startswith("window="):
                    window = int(tok.split("=", 1)[1])
                elif tok.startswith("as_of="):
                    as_of = tok.split("=", 1)[1]
                elif tok == "--window" and i + 1 < len(rest):
                    i += 1
                    window = int(rest[i])
                elif tok == "--as-of" and i + 1 < len(rest):
                    i += 1
                    as_of = rest[i]
                i += 1
            for row in routine_health(conn, window=window, as_of=as_of):
                print(json.dumps(row, ensure_ascii=False))
        elif verb == "materialize":
            rr = _roller_mod()
            defs = ([routine_def_get(conn, rest[0])] if rest
                    else rr.active_defs(conn))
            total = []
            for defn in defs:
                if defn is None:
                    _err("routine 없음: %s" % rest[0])
                if defn["status"] == "active" and defn["cadence"]:
                    total += rr.materialize_def(conn, None, defn)
            print("materialized\t%d" % len(total))
        else:
            _err(_u)
    finally:
        conn.close()


# ── CLI (lifekit.sh 서브커맨드 100% 동일 재현) ───────────────────
USAGE = ("사용법: lifekit.sh meal-add|meal-find|meal-day|meal-del|meal-upd|"
         "workout-add|workout-find|workout-del|agg-day|agg-week|"
         "task-add|task-find|task-done|task-undone|task-reschedule|"
         "task-archive|task-overdue|task-done-between|event-window|"
         "project-list|project-add|project-upd ...\n"
         "  workout-add <date> <category> <subtype> [minutes kcal note avg_hr]\n"
         "    (category=대분류, subtype=세부. 인자 순서는 옛 <type> <name>와 동일 위치.)")


def _err(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


# DGN-231: reconcile-before-write (doctrine D3). add verbs self-check for an
# existing match before insert; on match they print "EXISTS n" + the matching
# rows (reusing the verb's find/list format) and exit 3, registering nothing.
# The --new flag forces the insert past a match. A --new-less 0-match call keeps
# the pre-DGN-231 behavior byte-for-byte (existing callers unchanged).
EXISTS_CODE = 3


def _pop_flag(argv, flag):
    """Return (argv_without_flag, present_bool), removing every occurrence of
    flag. These CLIs are positional, so the flag may sit anywhere; stripping it
    leaves the remaining positional args index-identical to the legacy call."""
    if flag in argv:
        return [a for a in argv if a != flag], True
    return list(argv), False


def cli_meal_add(argv):
    # meal-add <date> <meal> <name> [carb protein fat fiber sugar alt_sugar grams alcohol] [--new]
    argv, force_new = _pop_flag(argv, '--new')      # DGN-231
    if len(argv) < 3 or not argv[0] or not argv[2]:
        _err("사용법: lifekit.sh meal-add <date> <meal> <name> "
             "[carb protein fat fiber sugar alt_sugar grams alcohol]")
    date, meal, name = argv[0], argv[1], argv[2]
    g = lambda i: argv[i] if i < len(argv) else None
    conn = get_conn()
    try:
        if not force_new:
            # DGN-231 match key = (date, meal slot). meal_find -> (id, name,
            # meal, kcal); its meal col is COALESCE(meal,'') so '' <-> _txt None.
            slot = _txt(meal)
            matches = [r for r in meal_find(date, conn=conn)
                       if (r[2] or None) == slot]
            if matches:
                print(f"EXISTS {len(matches)}")
                for mid, nm, ml, kcal in matches:
                    print(f"{mid}\t{nm}\t{ml}\t{_f0(kcal)}")
                sys.exit(EXISTS_CODE)
        nid = meal_add(date, meal, name,
                       carb=g(3), protein=g(4), fat=g(5), fiber=g(6),
                       sugar=g(7), alt_sugar=g(8), grams=g(9), alcohol=g(10),
                       conn=conn)
        row = conn.execute("SELECT id, name, kcal FROM meals WHERE id=?;",
                           (nid,)).fetchone()
        print(f"{row[0]}\t{row[1]}\t{_f0(row[2])}")
    finally:
        conn.close()


def cli_meal_find(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh meal-find <date>")
    for mid, name, meal, kcal in meal_find(argv[0]):
        print(f"{mid}\t{name}\t{meal}\t{_f0(kcal)}")


def cli_meal_day(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh meal-day <date>")
    for line in meal_day(argv[0]):
        print(line)


def cli_meal_del(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh meal-del <id>")
    if not str(argv[0]).isdigit():
        _err(f"id는 숫자여야 함: {argv[0]}")
    meal_del(argv[0])
    print(f"deleted meal {argv[0]}")


def cli_meal_upd(argv):
    # meal-upd <id> field=value [field=value ...]
    # field: date meal name carb protein fat fiber sugar alt_sugar grams alcohol
    _u = ("사용법: lifekit.sh meal-upd <id> field=value [field=value ...]\n"
          "  field: date meal name carb protein fat fiber sugar alt_sugar grams alcohol\n"
          "  지정한 필드만 바뀐다(부분 수정). kcal은 자동 재계산.")
    if len(argv) < 2 or not str(argv[0]).isdigit():
        _err(_u)
    fields = {}
    for tok in argv[1:]:
        if '=' not in tok:
            _err(f"field=value 형식이 아님: {tok}\n{_u}")
        col, val = tok.split('=', 1)
        col = col.strip()
        if col not in _MEAL_UPD_COLS:
            _err(f"수정 불가 컬럼: {col}\n{_u}")
        fields[col] = val
    if not fields:
        _err(_u)
    row = meal_upd(argv[0], fields)
    if row is None:
        _err(f"해당 id 식단 없음: {argv[0]}")
    print(f"{row[0]}\t{row[1]}\t{_f0(row[2])}")


def cli_workout_add(argv):
    # workout-add <date> <category> <subtype> [minutes kcal note avg_hr] [--new]
    # category/subtype 은 옛 <type>/<name> 위치 그대로 — 인자 순서 호환 보존.
    argv, force_new = _pop_flag(argv, '--new')      # DGN-231
    if len(argv) < 2 or not argv[0] or not argv[1]:
        _err("사용법: lifekit.sh workout-add <date> <category> <subtype> "
             "[minutes kcal note avg_hr]")
    date, wtype = argv[0], argv[1]
    g = lambda i: argv[i] if i < len(argv) else ''
    conn = get_conn()
    try:
        if not force_new:
            # DGN-231 match key = (date, category). workout_find -> (id,
            # category_concat, subtype_concat, minutes, kcal); category_concat
            # is group_concat(DISTINCT category), so split on comma and test
            # membership (a workout may carry several categories).
            cat = (wtype or '').strip()
            matches = [r for r in workout_find(date, conn=conn)
                       if cat and cat in
                       [c.strip() for c in (r[1] or '').split(',') if c.strip()]]
            if matches:
                print(f"EXISTS {len(matches)}")
                for wid, wt, nm, minutes, kcal in matches:
                    print(f"{wid}\t{wt}\t{nm}\t{_f0(minutes)}\t{_f0(kcal)}")
                sys.exit(EXISTS_CODE)
        nid = workout_add(date, wtype, name=g(2), minutes=g(3),
                          kcal=g(4), note=g(5), avg_hr=g(6), conn=conn)
        # 라벨(category/subtype)은 junction(workout_classifications) 경유로 복원.
        # 운동당 1행이 되도록 GROUP BY로 분류를 group_concat한다.
        row = conn.execute(
            "SELECT w.id, "
            "COALESCE(group_concat(DISTINCT wt.category), ''), "
            "COALESCE(group_concat(wt.subtype, ', '), ''), "
            "w.kcal, w.avg_hr "
            "FROM workouts w "
            "LEFT JOIN workout_classifications wc ON wc.workout_id = w.id "
            "LEFT JOIN workout_types wt ON wt.id = wc.type_id "
            "WHERE w.id=? GROUP BY w.id;", (nid,)).fetchone()
        # avg_hr는 NULL이면 빈 칸으로 출력(하위호환: 기존 4컬럼 뒤에 1컬럼 추가).
        hr = '' if row[4] is None else _f0(row[4])
        print(f"{row[0]}\t{row[1]}\t{row[2]}\t{_f0(row[3])}\t{hr}")
    finally:
        conn.close()


def cli_workout_classify(argv):
    # workout-classify <workout_id> <category> <subtype>
    # 운동 한 건에 분류 1개를 N:M junction(workout_classifications)에 추가 연결.
    if len(argv) < 3 or not argv[0] or not argv[1] or not argv[2]:
        _err("사용법: lifekit.sh workout-classify <workout_id> <category> <subtype>")
    if not str(argv[0]).isdigit():
        _err(f"workout_id는 숫자여야 함: {argv[0]}")
    wid = int(argv[0])
    category, subtype = argv[1], argv[2]
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM workouts WHERE id=? LIMIT 1;", (wid,)).fetchone()
        if exists is None:
            _err(f"운동 id={wid} 없음 (workout-find로 확인하세요)")
        # 중복이면 INSERT OR IGNORE라 조용히 무시됨.
        workout_add_classification(conn, wid, category, subtype)
        conn.commit()
        # 연결 후 그 운동의 현재 분류 전체를 한 줄로(cli_workout_add 출력과 호환):
        #   id<TAB>category들 group_concat<TAB>subtype들 group_concat
        row = conn.execute(
            "SELECT w.id, "
            "COALESCE(group_concat(DISTINCT wt.category), ''), "
            "COALESCE(group_concat(wt.subtype, ', '), '') "
            "FROM workouts w "
            "LEFT JOIN workout_classifications wc ON wc.workout_id = w.id "
            "LEFT JOIN workout_types wt ON wt.id = wc.type_id "
            "WHERE w.id=? GROUP BY w.id;", (wid,)).fetchone()
        print(f"{row[0]}\t{row[1]}\t{row[2]}")
    finally:
        conn.close()


def cli_workout_find(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh workout-find <date>")
    for wid, wtype, name, minutes, kcal in workout_find(argv[0]):
        print(f"{wid}\t{wtype}\t{name}\t{_f0(minutes)}\t{_f0(kcal)}")


def cli_workout_del(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh workout-del <id>")
    if not str(argv[0]).isdigit():
        _err(f"id는 숫자여야 함: {argv[0]}")
    workout_del(argv[0])
    print(f"deleted workout {argv[0]}")


def cli_agg_day(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh agg-day <date>")
    a = agg_day(argv[0])
    print(f"date={a['date']}")
    print(f"meal_cnt={a['meal_cnt']}")
    print(f"intake_kcal={_f0(a['intake_kcal'])}")
    print(f"protein_g={_f0(a['protein_g'])}")
    print(f"carb_g={_f0(a['carb_g'])}")
    print(f"fat_g={_f0(a['fat_g'])}")
    print(f"workout_cnt={a['workout_cnt']}")
    print(f"workout_min={_f0(a['workout_min'])}")
    print(f"burn_kcal={_f0(a['burn_kcal'])}")
    print(f"balance={_fp0(a['balance'])}")


def cli_agg_week(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh agg-week <월요일date>")
    w = agg_week(argv[0])
    print(f"week={w['monday']}~{w['sunday']}")
    print(f"meal_cnt={w['meal_cnt']}")
    print(f"meal_days={w['meal_days']}")
    print(f"intake_total={_f0(w['intake_total'])}")
    print(f"intake_avg_day={_f0(w['intake_avg_day'])}")
    print(f"protein_total={_f0(w['protein_total'])}")
    print(f"carb_total={_f0(w['carb_total'])}")
    print(f"fat_total={_f0(w['fat_total'])}")
    print(f"workout_cnt={w['workout_cnt']}")
    print(f"workout_min={_f0(w['workout_min'])}")
    print(f"burn_total={_f0(w['burn_total'])}")
    print(f"prev_intake_total={_f0(w['prev_intake_total'])}")
    print(f"prev_intake_avg_day={_f0(w['prev_intake_avg_day'])}")
    print(f"prev_workout_cnt={w['prev_workout_cnt']}")
    print(f"prev_burn_total={_f0(w['prev_burn_total'])}")
    print(f"diff_intake_avg={_fp0(w['diff_intake_avg'])}")
    print(f"diff_workout_cnt={w['diff_workout_cnt']:+d}")
    print(f"diff_burn_total={_fp0(w['diff_burn_total'])}")
    print("--- daily ---")
    for d in w['daily']:
        print(f"{d['date']} ({d['dow']})  섭취 {_f0(d['intake'])}kcal"
              f"  소모 {_f0(d['burn'])}kcal"
              f"  (식단 {d['meal_cnt']}건, 운동 {d['workout_cnt']}회)")


def cli_targets(argv):
    """targets --burn N → eff_goal bmr neat deficit protein_goal (공백구분 한 줄)."""
    p = argparse.ArgumentParser(prog='lifekit.sh targets', add_help=False)
    p.add_argument('--burn', type=float, default=0)
    ns, _ = p.parse_known_args(argv)
    stats = load_body_stats()
    t = compute_targets(stats, exercise_kcal=ns.burn)
    print(f"{t['eff_goal']} {t['bmr']} {t['neat']} {t['deficit']} "
          f"{stats.get('protein_g', 120)}")


def cli_dump(argv):
    """sqlite3 .dump 상당. backup-data.sh가 쓴다.
    백업 .sql 포맷을 기존과 ★바이트 동일★하게 유지하려고 표준 sqlite3 CLI에 위임한다
    (Python iterdump은 테이블명 따옴표·부동소수 정밀도가 달라 diff를 더럽힘)."""
    import subprocess
    sqlite_bin = '/usr/bin/sqlite3'
    if not os.path.isfile(DB_PATH):
        print(f"lifekit.db 없음 ({DB_PATH})", file=sys.stderr)
        sys.exit(1)
    r = subprocess.run([sqlite_bin, DB_PATH, '.dump'])
    sys.exit(r.returncode)


# ── 약속 / 사람 CLI ────────────────────────────────────────
def cli_person_find(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh person-find <이름또는별명>")
    rows = person_find(argv[0])
    for pid, name, relation, aliases in rows:
        print(f"{pid}\t{name}\t{relation or ''}\t{aliases or ''}")


def cli_person_add(argv):
    # person-add <name> [relation] [aliases] [--new]
    argv, force_new = _pop_flag(argv, '--new')      # DGN-231
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh person-add <name> [relation] [aliases]")
    g = lambda i: argv[i] if i < len(argv) else None
    name = argv[0]
    if not force_new:
        # DGN-231 match key = the new name EXACTLY equals an existing person's
        # name or one of that person's comma-split aliases (person_find's LIKE
        # substring match is too broad for a dedup gate). Output reuses the
        # person-find format (id, name, relation, aliases).
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, relation, aliases FROM persons "
                "ORDER BY id;").fetchall()
        finally:
            conn.close()
        matches = []
        for pid, pname, relation, aliases in rows:
            alias_set = [a.strip() for a in (aliases or '').split(',') if a.strip()]
            if name == pname or name in alias_set:
                matches.append((pid, pname, relation, aliases))
        if matches:
            print(f"EXISTS {len(matches)}")
            for pid, pname, relation, aliases in matches:
                print(f"{pid}\t{pname}\t{relation or ''}\t{aliases or ''}")
            sys.exit(EXISTS_CODE)
    pid = person_add(argv[0], relation=g(1), aliases=g(2))
    print(f"{pid}\t{argv[0]}")


def cli_person_alias(argv):
    # person-alias <id> <alias>
    if len(argv) < 2 or not str(argv[0]).isdigit():
        _err("사용법: lifekit.sh person-alias <id> <alias>")
    row = person_alias_add(argv[0], argv[1])
    if row is None:
        _err(f"해당 id 사람 없음: {argv[0]}")
    print(f"{row[0]}\t{row[1]}\t{row[2] or ''}")


# ══════════════════════════════════════════════════════════════════════════
# DGN-179 verb-delta (spec v2): appt CLI facade -- kind gate (M-1), shape-regex
# parsing (MIN-1), mixed-shape loud error (MIN-2), retry loop with full-row
# re-read (M-2), +3h deletion, zero-length default. Signatures preserved.
# ══════════════════════════════════════════════════════════════════════════

# MIN-1: shape detection is regex-FIRST (py3.9 fromisoformat("YYYY-MM-DD")
# succeeds as midnight, so a naive-datetime-first order would silently turn an
# all_day intent into a timed-midnight appt).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# grill-final M-5: (\.\d+)? accepts fractional seconds (live Ag Notion shape
# '...07:00:00.000+09:00'); the fraction is stripped during normalization so
# canonical output stays 20-char GLOB-legal. grill-final M-4: this regex and
# _to_canonical_utc (after normalization) accept exactly the same set --
# normalization handles space sep, trailing Z, colon-less offset, fraction.
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")

_APPT_UPD_FIELDS = ("title", "start_at", "end_at",
                    "location", "location_url", "purpose", "summary")
_TIME_FIELDS = ("start_at", "end_at")
_META_FIELDS = ("title", "location", "location_url", "purpose", "summary")
_UPD_RETRY_MAX = 3


def _facade_agent():
    """owning_agent/created_by for the facade: LIFEKIT_AGENT env if set, else the
    lowercased basename of the instance root (the dir containing database/)."""
    env = os.environ.get("LIFEKIT_AGENT")
    if env:
        return env.strip().lower()
    # SCRIPT_DIR is <root>/database; the root basename is the agent id.
    return os.path.basename(os.path.dirname(SCRIPT_DIR)).lower()


def _shape_of(val):
    """Classify a time token: 'date' / 'datetime' / 'bad' (MIN-1 regex-first)."""
    if val is None:
        return None
    if _DATE_RE.match(val):
        return "date"
    if _DATETIME_RE.match(val):
        return "datetime"
    return "bad"


def _to_canonical_utc(val):
    """Facade datetime -> canonical UTC 'YYYY-MM-DDThh:mm:ssZ'.
      - trailing-'Z' 20-char canonical -> passthrough (validated by canonical()).
      - ISO with offset -> convert to UTC.
      - naive datetime -> interpret in Asia/Seoul (default display_tz) -> UTC.
    grill-final M-4: NORMALIZE BEFORE PARSING so py3.9 fromisoformat accepts
    exactly what _DATETIME_RE accepts: space sep -> 'T'; fractional seconds
    stripped (M-5: output is whole-second canonical anyway); trailing 'Z' ->
    '+00:00' (py3.9 fromisoformat rejects 'Z'); colon inserted into a 4-digit
    offset ('+0900' -> '+09:00'). A calendar-impossible date (e.g. 02-30) still
    raises ValueError from fromisoformat -- callers wrap it into a loud one-line
    error (facade error contract). Raises ValueError with a clean message."""
    if CANON_RE.match(val):
        return val
    s = val.replace(" ", "T")
    s = re.sub(r"\.\d+", "", s)                    # strip fraction (M-5)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"                       # Z -> explicit offset
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)  # +0900 -> +09:00
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        raise ValueError("bad time format: %s" % val)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_appt_kind(conn, eid):
    """Return the event kind for eid, or None if the row is absent."""
    row = conn.execute("SELECT kind FROM event WHERE id=?;", (int(eid),)).fetchone()
    return row[0] if row else None


def cli_appt_find(argv):
    # appt-find <date_from> [date_to]
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh appt-find <date_from> [date_to]")
    g = lambda i: argv[i] if i < len(argv) else None
    # grill-final M-4: a malformed date (e.g. '2026-7-8') must be a loud
    # one-liner, not a strptime traceback. Regex-first, same as appt-add.
    for d in (argv[0], g(1)):
        if d is not None and not _DATE_RE.match(d):
            _err(f"bad date format (want YYYY-MM-DD): {d}")   # E-find-badfmt
    for aid, col2, title, location in appt_find(argv[0], g(1)):
        print(f"{aid}\t{col2 or ''}\t{title}\t{location or ''}")


def cli_appt_add(argv):
    # appt-add <title> <start_at> [end_at location purpose summary] [--new]
    argv, force_new = _pop_flag(argv, '--new')      # DGN-231
    if len(argv) < 2 or not argv[0] or not argv[1]:
        _err("사용법: lifekit.sh appt-add <title> <start_at> "
             "[end_at location purpose summary]")
    g = lambda i: argv[i] if i < len(argv) else None
    title, start_raw = argv[0], argv[1]
    end_raw = g(2)
    location, purpose, summary = g(3), g(4), g(5)

    s_shape = _shape_of(start_raw)
    if s_shape == "bad":
        _err(f"bad time format: {start_raw}")          # E-add-badfmt (A8)
    e_shape = _shape_of(end_raw) if end_raw else None
    if e_shape == "bad":
        _err(f"bad time format: {end_raw}")            # E-add-badfmt (A8)
    # MIN-2: mixed shape (datetime start + date-only end, or reverse) -> loud.
    if e_shape is not None and e_shape != s_shape:
        _err("start and end must be the same shape "
             "(both datetime or both date)")           # E-add-mixed (A5/A6)

    agent = _facade_agent()
    # grill-final M-4: every SDK ValueError becomes a loud one-liner (no
    # traceback): impossible calendar dates from _to_canonical_utc, reversed
    # intervals from validate_interval, etc.
    try:
        if s_shape == "date":
            # all_day (A3 single / A4 multi-day). E-add-reversed: date-only
            # reversed range is refused before instant derivation.
            if end_raw and end_raw < start_raw:
                raise ValueError(
                    "reversed interval refused: end %s < start %s"
                    % (end_raw, start_raw))
            schedule_kind = "all_day"
            start_at, end_at = all_day_instants(start_raw, end_raw)
        else:
            # timed (A1 zero-length / A2 explicit end).
            schedule_kind = "timed"
            start_at = _to_canonical_utc(start_raw)
            end_at = _to_canonical_utc(end_raw) if end_raw else start_at  # A1
    except ValueError as e:
        _err(str(e))                                   # E-add-badfmt / reversed

    # DEV-1 verdict: metadata rides the event_add INSERT itself (single txn,
    # no crash window, no version bump at birth).
    meta = {}
    if location is not None and location != "":
        meta["location"] = location
    if purpose is not None and purpose != "":
        meta["purpose"] = purpose
    if summary is not None and summary != "":
        meta["summary"] = summary

    conn = event_conn()
    _appt_add_ulid = None
    try:
        if not force_new:
            # DGN-231 match key = same LOCAL calendar date (+/-0 day). start_at
            # is canonical UTC; convert to the display tz date and reuse
            # appt_find (id, col2, title, location).
            local_date = _local_date_of(start_at, "Asia/Seoul").isoformat()
            matches = appt_find(local_date, conn=conn)
            if matches:
                print(f"EXISTS {len(matches)}")
                for aid, col2, mtitle, mloc in matches:
                    print(f"{aid}\t{col2 or ''}\t{mtitle}\t{mloc or ''}")
                sys.exit(EXISTS_CODE)
        try:
            eid = event_add(conn, kind="appointment", title=title,
                            schedule_kind=schedule_kind, start_at=start_at,
                            end_at=end_at, open_ended=0,
                            owning_agent=agent, created_by=agent,
                            completion_rule="manual", meta=meta)
        except ValueError as e:
            _err(str(e))                               # M-4 loud, no traceback
        if eid is None:
            _err("slot occupied")                      # E-add-slot (A7)
        _appt_add_ulid = _ulid_for_event_id(conn, eid)
    finally:
        conn.close()
    # DGN-258: best-effort mirror outbox enqueue after successful commit.
    if _appt_add_ulid:
        _outbox_enqueue_best_effort(_appt_add_ulid)
    print(f"{eid}\t{title}")


def _local_date_of(instant, tz_name):
    """Local calendar date of a stored canonical-UTC instant."""
    return (datetime.datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .astimezone(ZoneInfo(tz_name)).date())


def _upd_overlay_time(cur_row, fields):
    """Overlay the given time endpoints onto the CURRENT row (M-2: called with
    a FRESHLY re-read row on every retry). cur_row = (schedule_kind, start_at,
    end_at, open_ended, display_tz). Returns (new_start, new_end, open_ended)
    canonical, or raises ValueError (shape mismatch E-upd-shape / bad format /
    reversed range). schedule_kind transitions are NOT reachable here.

    grill-final F-1 (Metal verdict) all_day semantics -- an all_day row is a
    [start_day 00:00 local, end_day+1 00:00 local) instant RANGE, so a
    single-endpoint date update must never overlay ONE instant (that minted
    zero-length / reversed all_day rows and collapsed day-block occupancy):
      - start_at only  = DURATION-PRESERVING SHIFT: the whole range moves so it
        starts on the given date; the length in whole days is preserved
        (multi-day trips stay multi-day).
      - end_at only    = keep start; end re-derived from the given INCLUSIVE
        end date (next-midnight instant). end date < start date -> ValueError.
      - both given     = each endpoint derived from its own date;
        end < start -> ValueError.
    """
    sk, cur_start, cur_end, oe, tz = cur_row
    if sk == "all_day":
        for f in _TIME_FIELDS:
            if f in fields:
                shape = _shape_of(fields[f])
                if shape == "bad":
                    raise ValueError("bad time format: %s" % fields[f])
                if shape != "date":
                    raise ValueError(
                        "shape mismatch: use the SDK schedule_kind transition "
                        "verb to change timed<->all_day")
        # current inclusive local date range
        sd = _local_date_of(cur_start, tz)
        ed_incl = _local_date_of(cur_end, tz) - timedelta(days=1)
        duration_days = (ed_incl - sd).days + 1          # >= 1
        s_raw = fields.get("start_at")
        e_raw = fields.get("end_at")
        if s_raw is not None and e_raw is not None:
            new_sd = datetime.date.fromisoformat(s_raw)
            new_ed = datetime.date.fromisoformat(e_raw)
        elif s_raw is not None:
            # duration-preserving shift
            new_sd = datetime.date.fromisoformat(s_raw)
            new_ed = new_sd + timedelta(days=duration_days - 1)
        else:
            # end-only: keep start, shrink/extend to the given inclusive end
            new_sd = sd
            new_ed = datetime.date.fromisoformat(e_raw)
        if new_ed < new_sd:
            raise ValueError(
                "reversed all_day range refused: end %s < start %s"
                % (new_ed.isoformat(), new_sd.isoformat()))
        ns, ne = all_day_instants(new_sd.isoformat(), new_ed.isoformat(),
                                  tz_name=tz)
        return ns, ne, oe

    # timed row
    new_start, new_end = cur_start, cur_end
    for f in _TIME_FIELDS:
        if f not in fields:
            continue
        raw = fields[f]
        shape = _shape_of(raw)
        if shape == "bad":
            raise ValueError("bad time format: %s" % raw)
        if shape != "datetime":
            raise ValueError(
                "shape mismatch: use the SDK schedule_kind transition verb "
                "to change timed<->all_day")
        val = _to_canonical_utc(raw)
        if f == "start_at":
            new_start = val
        else:
            new_end = val
    return new_start, new_end, oe


def cli_appt_upd(argv):
    # appt-upd <id> field=value [field=value ...]
    _u = ("사용법: lifekit.sh appt-upd <id> field=value [field=value ...]\n"
          "  field: title start_at end_at location location_url purpose summary")
    if len(argv) < 2 or not str(argv[0]).isdigit():
        _err(_u)
    eid = int(argv[0])
    fields = {}
    for tok in argv[1:]:
        if '=' not in tok:
            _err(f"field=value 형식이 아님: {tok}\n{_u}")
        col, val = tok.split('=', 1)
        col = col.strip()
        if col not in _APPT_UPD_FIELDS:
            _err(f"수정 불가 컬럼: {col}\n{_u}")
        fields[col] = val
    if not fields:
        _err(_u)

    conn = event_conn()
    _appt_upd_ulid = None
    try:
        # M-1 kind gate: read kind ONCE; task / not-found -> legacy loud reject.
        kind = _read_appt_kind(conn, eid)
        if kind != "appointment":
            _err(f"해당 id 약속 없음: {eid}")            # E-upd-kind / E-upd-notfound

        time_fields = {k: v for k, v in fields.items() if k in _TIME_FIELDS}
        # grill-final m-4 (Metal verdict): an empty field value (field=) maps to
        # NULL, matching legacy _txt semantics. title= stays a loud error (the
        # NOT NULL contract lives in event_set_meta).
        meta_fields = {k: (None if (v == "" and k != "title") else v)
                       for k, v in fields.items() if k in _META_FIELDS}

        time_applied = False
        # ---- time edit (event_move) with M-2 full-row re-read retry loop ----
        if time_fields:
            for attempt in range(_UPD_RETRY_MAX):
                cur = conn.execute(
                    "SELECT version, schedule_kind, start_at, end_at, "
                    "open_ended, display_tz FROM event WHERE id=?;",
                    (eid,)).fetchone()
                if cur is None:
                    _err(f"해당 id 약속 없음: {eid}")
                version = cur[0]
                try:
                    ns, ne, oe = _upd_overlay_time(cur[1:], time_fields)
                    res = event_move(conn, eid, version, ns, ne, open_ended=oe)
                except ValueError as e:
                    # M-4: overlay errors AND event_move validator errors
                    # (reversed interval, all_day belt) -> loud one-liner.
                    _err(str(e))            # E-upd-shape / badfmt / reversed
                if res == MutationResult.APPLIED:
                    time_applied = True
                    break
                if res == MutationResult.OVERLAP_REJECT:
                    _err("slot occupied")                 # E-upd-slot (U7)
                if res == MutationResult.NOT_FOUND:
                    _err(f"해당 id 약속 없음: {eid}")
                # CAS_FAIL -> re-read full row and retry.
            else:
                _err("conflict, please retry")            # E-upd-cas (U8)

        # ---- meta edit (event_set_meta) retry loop -- m-3: uniform full-row
        # re-read (same discipline as the time loop; meta values are absolute
        # so the re-read is for uniformity, the version is what matters). ----
        if meta_fields:
            meta_ok = False
            fail_reason = "conflict"
            for attempt in range(_UPD_RETRY_MAX):
                cur = conn.execute(
                    "SELECT version, schedule_kind, start_at, end_at, "
                    "open_ended, display_tz FROM event WHERE id=?;",
                    (eid,)).fetchone()
                if cur is None:
                    fail_reason = f"해당 id 약속 없음: {eid}"
                    break
                version = cur[0]
                try:
                    res = event_set_meta(conn, eid, version, dict(meta_fields))
                except ValueError as e:
                    # M-4: kind-illegal column / empty title -> loud one-liner
                    # (or partial report if time already landed).
                    fail_reason = str(e)
                    break
                if res == MutationResult.APPLIED:
                    meta_ok = True
                    break
                if res == MutationResult.NOT_FOUND:
                    fail_reason = f"해당 id 약속 없음: {eid}"
                    break
                # CAS_FAIL -> re-read full row and retry.
            if not meta_ok:
                if time_applied:
                    # U4 partial (spec 4.4 E-upd-partial wording): time landed,
                    # metadata did not, with the concrete reason.
                    _err(f"time applied, metadata NOT applied: {fail_reason}")
                _err(fail_reason if fail_reason != "conflict"
                     else "conflict, please retry")       # E-upd-cas / loud
        # success line (legacy shape: id, title, location).
        row = conn.execute(
            "SELECT id, title, location FROM event WHERE id=?;", (eid,)).fetchone()
        # DGN-258: fetch ulid for outbox enqueue before conn closes.
        _appt_upd_ulid = _ulid_for_event_id(conn, eid)
        print(f"{row[0]}\t{row[1]}\t{row[2] or ''}")
    finally:
        conn.close()
    # DGN-258: best-effort mirror outbox enqueue after successful commit.
    if _appt_upd_ulid:
        _outbox_enqueue_best_effort(_appt_upd_ulid)


def cli_appt_person(argv):
    # appt-person <appt_id> <person_id>
    if len(argv) < 2 or not str(argv[0]).isdigit() or not str(argv[1]).isdigit():
        _err("사용법: lifekit.sh appt-person <appt_id> <person_id>")
    eid, pid = int(argv[0]), int(argv[1])
    conn = event_conn()
    try:
        # M-1 kind gate.
        kind = _read_appt_kind(conn, eid)
        if kind != "appointment":
            _err(f"해당 id 약속 없음: {eid}")            # E-person-kind
        try:
            n = event_person_add(conn, eid, pid)
        except sqlite3.IntegrityError:
            _err(f"no such person {pid}")                 # E-person-fk
        print(f"appt {eid} 참가자 {n}명")
    finally:
        conn.close()


def cli_appt_show(argv):
    # appt-show <id> — 약속 1건 + 참가자
    if not argv or not str(argv[0]).isdigit():
        _err("사용법: lifekit.sh appt-show <id>")
    eid = int(argv[0])
    conn = event_conn()
    try:
        a = appt_show_row(conn, eid)                      # M-1: None if not appt
        if a is None:
            _err(f"해당 id 약속 없음: {eid}")            # E-show-kind
        # a = (id, title, start_local, end_local, location, purpose, summary)
        print(f"{a[0]}\t{a[1]}\t{a[2] or ''}\t{a[3] or ''}\t"
              f"{a[4] or ''}\t{a[5] or ''}\t{a[6] or ''}")
        for pid, name, aliases, relation in event_persons(conn, eid):
            print(f"  {pid}\t{name}\t{aliases or ''}\t{relation or ''}")
    finally:
        conn.close()


def cli_migrate_body_stats(argv):
    """body_stats.json 의 모든 키를 config 테이블로 복사(멱등, v2).

    DEFAULT_STATS 에 없는 키(goal_mode, skeletal_muscle_kg, updated 등)도 전부 복사한다.
    _CONFIG_STORE(=SqliteConfigStore) 경유로 upsert 하므로 몇 번 돌려도 안전(멱등).
    파일이 없으면 아무것도 안 하고 알린다(이미 클린 컷오버 후일 수 있음)."""
    if not os.path.isfile(BODY_STATS_PATH):
        print(f"migrate-body-stats: {BODY_STATS_PATH} 없음 (스킵)")
        return
    try:
        with open(BODY_STATS_PATH, encoding='utf-8') as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _err(f"migrate-body-stats: body_stats.json 읽기 실패: {e}")
    if not isinstance(raw, dict) or not raw:
        print("migrate-body-stats: 복사할 키 없음 (빈 파일)")
        return
    # update()가 'updated'를 오늘로 덮으니, 파일의 updated 를 보존하려면 마지막에
    # 파일 값으로 다시 씀. 여기선 전 키를 그대로 넘기고 update의 updated 자동갱신을 허용하되,
    # 파일에 updated 가 있으면 그 값으로 최종 덮어써 원본 이력을 보존한다.
    _CONFIG_STORE.update(dict(raw))
    if 'updated' in raw:
        # update()가 'updated'=오늘로 덮었으므로 파일의 원본 updated로 되돌린다(멱등 이력 보존).
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO config (key, value) VALUES ('updated', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=datetime('now','localtime');", (str(raw['updated']),))
            conn.commit()
        finally:
            conn.close()
    keys = sorted(raw.keys())
    print(f"migrate-body-stats: {len(keys)}개 키 복사 완료 -> config")
    for k in keys:
        print(f"  {k}={raw[k]}")


def cli_body_state(argv):
    """현재 신체/목표 상태를 KEY=VALUE 로 출력(v2 hook/사람용).

    goal_mode + weight + 계산된 목표(compute_targets/compute_macro_goals)를 함께 낸다.
    hook 주입도 이 값을 재사용(1 SQLite read). 값은 SQL canonical(_CONFIG_STORE)에서 온다."""
    stats = load_body_stats()
    t = compute_targets(stats, exercise_kcal=0)
    g = compute_macro_goals(t['eff_goal'], stats)
    print(f"goal_mode={stats.get('goal_mode', '')}")
    print(f"weight_kg={stats.get('weight_kg', '')}")
    print(f"bmr={t['bmr']}")
    print(f"neat={t['neat']}")
    print(f"deficit={t['deficit']}")
    print(f"eff_goal={t['eff_goal']}")
    print(f"protein_g={g['protein']}")
    print(f"carb_g={g['carb']}")
    print(f"fat_g={g['fat']}")


def cli_config_set(argv):
    # config-set key=value [key=value ...]
    # Upsert one or more config rows via _CONFIG_STORE (set_stats). 'updated' auto.
    # This is the CLI path the diet-log skill uses to persist body stats without
    # touching the config table directly (set_stats/SqliteConfigStore mirror).
    _u = ("Usage: lifekit.sh config-set key=value [key=value ...]\n"
          "  Upserts config rows (body stats). 'updated' auto-set to today.")
    if not argv:
        _err(_u)
    fields = {}
    for tok in argv:
        if '=' not in tok:
            _err(f"not key=value: {tok}\n{_u}")
        k, v = tok.split('=', 1)
        k = k.strip()
        if not k:
            _err(f"empty key: {tok}\n{_u}")
        fields[k] = v
    if not fields:
        _err(_u)
    merged = set_stats(**fields)
    for k in sorted(fields.keys()):
        print(f"{k}={merged.get(k, '')}")


def cli_log_metric(argv):
    # log-metric <date> <metric> <value> [note]
    # Upsert a single time-series measurement (one row per (date, metric)).
    if len(argv) < 3 or not argv[0] or not argv[1]:
        _err("Usage: lifekit.sh log-metric <date> <metric> <value> [note]")
    date, metric, value = argv[0], argv[1], argv[2]
    note = argv[3] if len(argv) > 3 else None
    mid = log_metric(date, metric, value, note=note)
    print(f"{mid}\t{date}\t{metric}\t{_f0(_num(value))}")


# ── DGN-180 task CLI (event core, kind='task'; dec-001) ───────────────────
# Output contract (task.sh 1:1): id<TAB>title<TAB>due<TAB>done|todo.
# id = event integer id (same id space as the appt-* CLI).

def _task_fetch(conn, eid):
    return conn.execute(
        "SELECT id, ulid, title, start_at, end_at, schedule_kind, display_tz, "
        "settled_outcome, settled_at, version FROM event "
        "WHERE id=? AND kind='task';", (eid,)).fetchone()


def _resolve_task(conn, token):
    """Accept integer id or ulid; must be a task row."""
    row = None
    if token.isdigit():
        row = _task_fetch(conn, int(token))
    if row is None:
        r = conn.execute("SELECT id FROM event WHERE ulid=? AND kind='task';",
                         (token,)).fetchone()
        if r is not None:
            row = _task_fetch(conn, r[0])
    if row is None:
        _err(f"task 없음: {token}")
    return row


def _task_line(row):
    eid, _ulid, title, sa, _ea, _sk, tz, outcome, _sat, _v = row
    due = str(_local_date_of(sa, tz or "Asia/Seoul")) if sa else ""
    state = "done" if outcome == "done" else "todo"
    return f"{eid}\t{title}\t{due}\t{state}"


def cli_task_add(argv):
    # task-add <title> [due_date] [note]  (dec-001: event_add kind='task')
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh task-add <title> [due_date] [note]")
    title = argv[0]
    due = argv[1] if len(argv) > 1 and argv[1] else None
    note = argv[2] if len(argv) > 2 and argv[2] else None
    if due is not None and not _DATE_RE.match(due):
        _err(f"bad date format (want YYYY-MM-DD): {due}")
    conn = event_conn()
    _task_add_ulid = None
    try:
        if due:
            sa, ea = all_day_instants(due)
            eid = event_add(conn, "task", title, "all_day", start_at=sa,
                            end_at=ea, owning_agent=_facade_agent(),
                            created_by=_facade_agent(), note=note)
        else:
            eid = event_add(conn, "task", title, "untimed",
                            owning_agent=_facade_agent(),
                            created_by=_facade_agent(), note=note)
        _task_add_ulid = _ulid_for_event_id(conn, eid)
        print(_task_line(_task_fetch(conn, eid)))
    finally:
        conn.close()
    # DGN-258: best-effort mirror outbox enqueue after successful commit.
    if _task_add_ulid:
        _outbox_enqueue_best_effort(_task_add_ulid)


def cli_task_find(argv):
    # task-find [date|all|keyword] -- archived(=abandoned, OQ-C1) excluded.
    arg = argv[0] if argv and argv[0] else "all"
    conn = event_conn()
    try:
        base = ("SELECT id, ulid, title, start_at, end_at, schedule_kind, "
                "display_tz, settled_outcome, settled_at, version FROM event "
                "WHERE kind='task' "
                "AND (settled_outcome IS NULL OR settled_outcome <> 'abandoned') ")
        if arg == "all":
            rows = conn.execute(
                base + "ORDER BY (start_at IS NULL), start_at DESC;").fetchall()
        elif _DATE_RE.match(arg):
            ws, we = all_day_instants(arg)
            rows = conn.execute(
                base + "AND start_at >= ? AND start_at < ? "
                "ORDER BY start_at DESC;", (ws, we)).fetchall()
        else:
            rows = conn.execute(
                base + "AND title LIKE ? "
                "ORDER BY (start_at IS NULL), start_at DESC;",
                (f"%{arg}%",)).fetchall()
        for row in rows:
            print(_task_line(row))
    finally:
        conn.close()


def cli_task_done(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh task-done <id>")
    conn = event_conn()
    _task_done_ulid = None
    try:
        row = _resolve_task(conn, argv[0])
        res = force_settle(conn, row[0], row[9], _facade_agent())
        if res != MutationResult.APPLIED:
            _err(f"task-done 실패 ({res}): 이미 완료/취소되었거나 동시 변경")
        _task_done_ulid = row[1]
        print(_task_line(_task_fetch(conn, row[0])))
    finally:
        conn.close()
    # DGN-258: best-effort mirror outbox enqueue after successful commit.
    if _task_done_ulid:
        _outbox_enqueue_best_effort(_task_done_ulid)


def cli_task_undone(argv):
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh task-undone <id>")
    conn = event_conn()
    _task_undone_ulid = None
    try:
        row = _resolve_task(conn, argv[0])
        res, _warn = unsettle(conn, row[1], _facade_agent())
        if res != "applied":
            _err(f"task-undone 실패 ({res}): done 상태가 아니면 재개 불가")
        _task_undone_ulid = row[1]
        print(_task_line(_task_fetch(conn, row[0])))
    finally:
        conn.close()
    # DGN-258: best-effort mirror outbox enqueue after successful commit.
    if _task_undone_ulid:
        _outbox_enqueue_best_effort(_task_undone_ulid)


def cli_task_reschedule(argv):
    if len(argv) < 2 or not argv[0] or not argv[1]:
        _err("사용법: lifekit.sh task-reschedule <id> <YYYY-MM-DD>")
    if not _DATE_RE.match(argv[1]):
        _err(f"bad date format (want YYYY-MM-DD): {argv[1]}")
    conn = event_conn()
    _task_resched_ulid = None
    try:
        row = _resolve_task(conn, argv[0])
        sa, ea = all_day_instants(argv[1])
        res, warn = bypass_schedule_apply(conn, row[1], "all_day", sa, ea)
        if res != "applied":
            _err(f"task-reschedule 실패 ({res})")
        if warn:
            print(warn, file=sys.stderr)
        _task_resched_ulid = row[1]
        print(_task_line(_task_fetch(conn, row[0])))
    finally:
        conn.close()
    # DGN-258: best-effort mirror outbox enqueue after successful commit.
    if _task_resched_ulid:
        _outbox_enqueue_best_effort(_task_resched_ulid)


def cli_task_archive(argv):
    # OQ-C1 (LOUD, TEMPORARY): 179 task-archive semantics = archived_at
    # visibility soft-delete, but the event table has NO archive home
    # (archived_at lives on the legacy tasks table only). Per the OQ-C1
    # ruling fallback we do NOT invent schema here: archive maps to
    # cancel(abandoned) -- hidden from task-find/overdue and deleted from
    # the mirror surface (X8). Divergence: an archived task shows as
    # "cancelled" semantics, not neutral archive. Revisit when an event
    # archive column is specced (OPEN QUESTION OQ-C1 in DGN-180).
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh task-archive <id>")
    conn = event_conn()
    _task_archive_ulid = None
    try:
        row = _resolve_task(conn, argv[0])
        res = cancel(conn, row[0], row[9], _facade_agent())
        if res != MutationResult.APPLIED:
            _err(f"task-archive 실패 ({res}): 이미 종결되었거나 동시 변경")
        _task_archive_ulid = row[1]
        print(f"{row[0]}\t{row[2]}\tarchived")
    finally:
        conn.close()
    # DGN-258: best-effort mirror outbox enqueue after successful commit.
    if _task_archive_ulid:
        _outbox_enqueue_best_effort(_task_archive_ulid)


def cli_task_overdue(argv):
    # open tasks whose effective end passed before today's local midnight.
    today = datetime.datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    today_start, _ = all_day_instants(today)
    conn = event_conn()
    try:
        rows = conn.execute(
            "SELECT id, ulid, title, start_at, end_at, schedule_kind, "
            "display_tz, settled_outcome, settled_at, version FROM event "
            "WHERE kind='task' AND settled_at IS NULL "
            "AND start_at IS NOT NULL "
            "AND COALESCE(end_at, start_at) <= ? "
            "ORDER BY start_at;", (today_start,)).fetchall()
        for row in rows:
            print(_task_line(row))
    finally:
        conn.close()


def cli_task_done_between(argv):
    # task-done-between <from> <to>  (to = EXCLUSIVE; weekly-review window)
    if len(argv) < 2 or not _DATE_RE.match(argv[0]) or not _DATE_RE.match(argv[1]):
        _err("사용법: lifekit.sh task-done-between <YYYY-MM-DD> <YYYY-MM-DD>")
    ws, _ = all_day_instants(argv[0])
    we, _ = all_day_instants(argv[1])
    conn = event_conn()
    try:
        rows = conn.execute(
            "SELECT id, ulid, title, start_at, end_at, schedule_kind, "
            "display_tz, settled_outcome, settled_at, version FROM event "
            "WHERE kind='task' AND settled_outcome='done' "
            "AND settled_at >= ? AND settled_at < ? "
            "ORDER BY settled_at;", (ws, we)).fetchall()
        for row in rows:
            done_d = str(_local_date_of(row[8], row[6] or "Asia/Seoul"))
            print(f"{row[0]}\t{row[2]}\t{done_d}\tdone")
    finally:
        conn.close()


def cli_event_window(argv):
    # event-window <from_utc> <to_utc> [kind] -- remind.sh read lane (04b).
    if len(argv) < 2:
        _err("사용법: lifekit.sh event-window <from_utc> <to_utc> [kind]")
    try:
        canonical(argv[0])
        canonical(argv[1])
    except ValueError as e:
        _err(str(e))
    kind = argv[2] if len(argv) > 2 and argv[2] else None
    conn = event_conn()
    try:
        q = ("SELECT ulid, kind, title, start_at, end_at, status FROM event "
             "WHERE settled_at IS NULL AND schedule_kind='timed' "
             "AND start_at >= ? AND start_at < ? ")
        params = [argv[0], argv[1]]
        if kind:
            q += "AND kind=? "
            params.append(kind)
        for u, k, t, sa, ea, st in conn.execute(
                q + "ORDER BY start_at;", params).fetchall():
            print(f"{u}\t{k}\t{t}\t{sa}\t{ea or ''}\t{st}")
    finally:
        conn.close()


# ── project CRUD (DGN-256) ────────────────────────────────────────────────
# projects table: id, ulid, title, status, start_date, end_date, note,
#                 area_id, notion_id, created_at
# Verbs: project-list, project-add, project-upd.
# notion_id is NULL for locally-created rows.

_DEFAULT_PROJECT_STATUS = "진행 중"


def project_list(status=None, conn=None):
    """Return list of projects as tuples (id, title, status, start_date, end_date).
    status=None returns all rows; otherwise filtered by exact match."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT id, title, status, start_date, end_date "
                "FROM projects WHERE status=? "
                "ORDER BY COALESCE(start_date,'9999') DESC, id;",
                (status,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, status, start_date, end_date "
                "FROM projects "
                "ORDER BY COALESCE(start_date,'9999') DESC, id;").fetchall()
        return rows
    finally:
        if own:
            conn.close()


def project_add(title, status=None, start=None, end=None, note=None, conn=None):
    """Insert a new project row with a fresh ulid. notion_id is NULL.
    Returns the new row id."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        st = status if status else _DEFAULT_PROJECT_STATUS
        now_str = datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = conn.execute(
            "INSERT INTO projects (ulid, title, status, start_date, end_date, "
            "note, area_id, notion_id, created_at) VALUES (?,?,?,?,?,?,NULL,NULL,?)",
            (new_ulid(), title, st, _txt(start), _txt(end), _txt(note), now_str))
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def project_upd(pid, title=None, status=None, start=None, end=None, note=None,
                conn=None):
    """Partial update of a project row. Returns True if updated, False if not found.
    At least one field must be given (caller checks)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        sets, vals = [], []
        if title is not None:
            sets.append("title=?")
            vals.append(title)
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        if start is not None:
            sets.append("start_date=?")
            vals.append(_txt(start))
        if end is not None:
            sets.append("end_date=?")
            vals.append(_txt(end))
        if note is not None:
            sets.append("note=?")
            vals.append(_txt(note))
        if not sets:
            raise ValueError("no fields to update")
        vals.append(int(pid))
        cur = conn.execute(
            "UPDATE projects SET %s WHERE id=?" % ", ".join(sets), vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def _project_line(row):
    """Format a project row tuple as TAB-separated output line."""
    pid, title, status, start_date, end_date = row
    return "%s\t%s\t%s\t%s\t%s" % (
        pid, title, status or "",
        start_date or "", end_date or "")


def cli_project_list(argv):
    # project-list [--status <S>] [--json]
    status = None
    as_json = False
    i = 0
    while i < len(argv):
        if argv[i] == "--status" and i + 1 < len(argv):
            status = argv[i + 1]
            i += 2
        elif argv[i] == "--json":
            as_json = True
            i += 1
        else:
            _err("사용법: lifekit.sh project-list [--status <S>] [--json]")
    rows = project_list(status=status)
    if as_json:
        out = [{"id": r[0], "title": r[1], "status": r[2],
                "start_date": r[3], "end_date": r[4]} for r in rows]
        print(json.dumps(out, ensure_ascii=False))
    else:
        for row in rows:
            print(_project_line(row))


def cli_project_add(argv):
    # project-add --title <T> [--status <S>] [--start <d>] [--end <d>] [--note <N>]
    title = None
    status = None
    start = None
    end = None
    note = None
    i = 0
    while i < len(argv):
        if argv[i] == "--title" and i + 1 < len(argv):
            title = argv[i + 1]; i += 2
        elif argv[i] == "--status" and i + 1 < len(argv):
            status = argv[i + 1]; i += 2
        elif argv[i] == "--start" and i + 1 < len(argv):
            start = argv[i + 1]; i += 2
        elif argv[i] == "--end" and i + 1 < len(argv):
            end = argv[i + 1]; i += 2
        elif argv[i] == "--note" and i + 1 < len(argv):
            note = argv[i + 1]; i += 2
        else:
            _err("사용법: lifekit.sh project-add --title <T> "
                 "[--status <S>] [--start <d>] [--end <d>] [--note <N>]")
    if not title:
        _err("project-add: --title is required")
    if start and not _DATE_RE.match(start):
        _err("bad date format (want YYYY-MM-DD): " + start)
    if end and not _DATE_RE.match(end):
        _err("bad date format (want YYYY-MM-DD): " + end)
    pid = project_add(title, status=status, start=start, end=end, note=note)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, title, status, start_date, end_date FROM projects WHERE id=?;",
            (pid,)).fetchone()
        print(_project_line(row))
    finally:
        conn.close()


def cli_project_upd(argv):
    # project-upd --id <id> [--title T] [--status S] [--start d] [--end d] [--note N]
    pid = None
    title = None
    status = None
    start = None
    end = None
    note = None
    i = 0
    while i < len(argv):
        if argv[i] == "--id" and i + 1 < len(argv):
            pid = argv[i + 1]; i += 2
        elif argv[i] == "--title" and i + 1 < len(argv):
            title = argv[i + 1]; i += 2
        elif argv[i] == "--status" and i + 1 < len(argv):
            status = argv[i + 1]; i += 2
        elif argv[i] == "--start" and i + 1 < len(argv):
            start = argv[i + 1]; i += 2
        elif argv[i] == "--end" and i + 1 < len(argv):
            end = argv[i + 1]; i += 2
        elif argv[i] == "--note" and i + 1 < len(argv):
            note = argv[i + 1]; i += 2
        else:
            _err("사용법: lifekit.sh project-upd --id <id> "
                 "[--title T] [--status S] [--start d] [--end d] [--note N]")
    if pid is None:
        _err("project-upd: --id is required")
    if not str(pid).isdigit():
        _err("project-upd: --id must be a numeric id, got: " + str(pid))
    if start and not _DATE_RE.match(start):
        _err("bad date format (want YYYY-MM-DD): " + start)
    if end and not _DATE_RE.match(end):
        _err("bad date format (want YYYY-MM-DD): " + end)
    # Check at least one field given
    if all(v is None for v in (title, status, start, end, note)):
        _err("project-upd: at least one of --title/--status/--start/--end/--note required")
    found = project_upd(int(pid), title=title, status=status,
                        start=start, end=end, note=note)
    if not found:
        _err("project 없음: " + str(pid))
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, title, status, start_date, end_date FROM projects WHERE id=?;",
            (int(pid),)).fetchone()
        print(_project_line(row))
    finally:
        conn.close()


_DISPATCH = {
    'meal-add': cli_meal_add,
    'meal-find': cli_meal_find,
    'meal-day': cli_meal_day,
    'meal-del': cli_meal_del,
    'meal-upd': cli_meal_upd,
    'workout-add': cli_workout_add,
    'workout-classify': cli_workout_classify,
    'workout-find': cli_workout_find,
    'workout-del': cli_workout_del,
    'agg-day': cli_agg_day,
    'agg-week': cli_agg_week,
    'targets': cli_targets,
    'migrate-body-stats': cli_migrate_body_stats,
    'body-state': cli_body_state,
    'config-set': cli_config_set,
    'log-metric': cli_log_metric,
    'dump': cli_dump,
    'person-find': cli_person_find,
    'person-add': cli_person_add,
    'person-alias': cli_person_alias,
    'appt-find': cli_appt_find,
    'appt-add': cli_appt_add,
    'appt-upd': cli_appt_upd,
    'appt-person': cli_appt_person,
    'appt-show': cli_appt_show,
    'task-add': cli_task_add,
    'task-find': cli_task_find,
    'task-done': cli_task_done,
    'task-undone': cli_task_undone,
    'task-reschedule': cli_task_reschedule,
    'task-archive': cli_task_archive,
    'task-overdue': cli_task_overdue,
    'task-done-between': cli_task_done_between,
    'event-window': cli_event_window,
    'project-list': cli_project_list,
    'project-add': cli_project_add,
    'project-upd': cli_project_upd,
    'routine': cli_routine,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        _err(USAGE)
    cmd, rest = argv[0], argv[1:]
    fn = _DISPATCH.get(cmd)
    if fn is None:
        print(f"알 수 없는 명령: {cmd}", file=sys.stderr)
        print("(meal-add|meal-find|meal-day|meal-del|meal-upd|workout-add|"
              "workout-find|workout-del|agg-day|agg-week)", file=sys.stderr)
        sys.exit(1)
    fn(rest)


if __name__ == '__main__':
    main()
