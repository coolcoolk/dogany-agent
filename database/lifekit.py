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
import sys
import json
import argparse
import sqlite3
import datetime
from decimal import Decimal, ROUND_HALF_UP

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


# ── 연결 ───────────────────────────────────────────────────
def get_conn():
    """lifekit.db 연결(단일 진입점). foreign_keys ON."""
    if not os.path.isfile(DB_PATH):
        print(f"lifekit.db 없음 ({DB_PATH})", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


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


def appt_find(date_from, date_to=None, conn=None):
    """기간 내 약속 목록(start_at 날짜 기준). (id, start_at, title, location) 리스트."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        to = date_to or date_from
        return conn.execute(
            "SELECT id, start_at, title, location FROM appointments "
            "WHERE date(start_at) >= ? AND date(start_at) <= ? "
            "ORDER BY start_at;", (date_from, to)).fetchall()
    finally:
        if own:
            conn.close()


def appt_add(title, start_at, end_at=None, location=None,
             purpose=None, summary=None, conn=None):
    """약속 한 건 신규 등록. 새 id 반환. end_at 미지정시 시작+3시간 디폴트."""
    if end_at is None and start_at:
        try:
            end_at = (datetime.datetime.fromisoformat(start_at)
                      + datetime.timedelta(hours=3)).isoformat()
        except ValueError:
            end_at = None
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO appointments "
            "(title, start_at, end_at, location, purpose, summary) "
            "VALUES (?,?,?,?,?,?);",
            (title, _txt(start_at), _txt(end_at), _txt(location),
             _txt(purpose), _txt(summary)))
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


_APPT_UPD_COLS = {
    'title': _txt, 'start_at': _txt, 'end_at': _txt,
    'location': _txt, 'location_url': _txt, 'purpose': _txt, 'summary': _txt,
}


def appt_upd(aid, fields, conn=None):
    """약속 한 건의 지정 필드만 갱신(부분 수정). 갱신 후 행 반환(없으면 None)."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        sets, vals = [], []
        for col, raw in fields.items():
            if col not in _APPT_UPD_COLS:
                raise ValueError(f"수정 불가 컬럼: {col}")
            sets.append(f"{col}=?")
            vals.append(_APPT_UPD_COLS[col](raw))
        if not sets:
            raise ValueError("갱신할 필드가 없음")
        vals.append(int(aid))
        cur = conn.execute(
            f"UPDATE appointments SET {', '.join(sets)} WHERE id=?;", vals)
        conn.commit()
        if cur.rowcount == 0:
            return None
        return conn.execute(
            "SELECT id, title, location, purpose, summary "
            "FROM appointments WHERE id=?;", (int(aid),)).fetchone()
    finally:
        if own:
            conn.close()


def appt_person_add(aid, pid, conn=None):
    """약속에 참가자(사람) 연결. 이미 있으면 무시. 연결 후 참가자 수 반환."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO appointment_persons "
            "(appointment_id, person_id) VALUES (?,?);", (int(aid), int(pid)))
        conn.commit()
        return conn.execute(
            "SELECT COUNT(*) FROM appointment_persons WHERE appointment_id=?;",
            (int(aid),)).fetchone()[0]
    finally:
        if own:
            conn.close()


def appt_persons(aid, conn=None):
    """약속의 참가자 목록. (id, name, aliases, relation) 리스트."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        return conn.execute(
            "SELECT p.id, p.name, p.aliases, p.relation FROM appointment_persons ap "
            "JOIN persons p ON p.id=ap.person_id WHERE ap.appointment_id=? "
            "ORDER BY p.id;", (int(aid),)).fetchall()
    finally:
        if own:
            conn.close()


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

    DGN-071 v2: 진실의 원천을 body_stats.json 직접 읽기에서 _CONFIG_STORE(SQL)
    경유로 옮겼다. card.py/compute_targets 등 모든 호출부가 SQL truth를 읽는다.
    _CONFIG_STORE는 이 함수보다 아래에서 정의되지만 파이썬은 전역을 호출 시점에
    해석하므로 순서 문제 없다(모듈 최초 import 후 첫 호출 시 이미 바인딩됨)."""
    return _CONFIG_STORE.load()


# ── 설정 저장소 추상화 (DGN-059) ────────────────────────────
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
    """config 테이블(key/value TEXT) 백엔드. DGN-071 v2에서 신체 스탯의 canonical.

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


# 기본 설정 저장소(현재 백엔드). DGN-071 v2 클린 컷오버: SQL(config 테이블)이 canonical.
# 교체 시 이 한 줄만 바꾸면 호출부 무변경(load_body_stats/get_config/set_stats 모두 경유).
_CONFIG_STORE = SqliteConfigStore()


# ── 설정값 쓰기/읽기 API (DGN-059) ──────────────────────────
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


# ── 측정값 시계열 API (DGN-059) ─────────────────────────────
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

        # 운동: 있으면 burn_kcal 채움 (detail = "유형 (이름)" 합침)
        burn = float(_f0(a['burn_kcal']))
        if burn > 0:
            labels = []
            for _id, wtype, wname, _min, _kc in workout_find(iso_date, conn=conn):
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


# ── CLI (lifekit.sh 서브커맨드 100% 동일 재현) ───────────────────
USAGE = ("사용법: lifekit.sh meal-add|meal-find|meal-day|meal-del|meal-upd|"
         "workout-add|workout-find|workout-del|agg-day|agg-week ...\n"
         "  workout-add <date> <category> <subtype> [minutes kcal note avg_hr]\n"
         "    (category=대분류, subtype=세부. 인자 순서는 옛 <type> <name>와 동일 위치.)")


def _err(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def cli_meal_add(argv):
    # meal-add <date> <meal> <name> [carb protein fat fiber sugar alt_sugar grams alcohol]
    if len(argv) < 3 or not argv[0] or not argv[2]:
        _err("사용법: lifekit.sh meal-add <date> <meal> <name> "
             "[carb protein fat fiber sugar alt_sugar grams alcohol]")
    date, meal, name = argv[0], argv[1], argv[2]
    g = lambda i: argv[i] if i < len(argv) else None
    conn = get_conn()
    try:
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
    # workout-add <date> <category> <subtype> [minutes kcal note avg_hr]
    # category/subtype 은 옛 <type>/<name> 위치 그대로 — 인자 순서 호환 보존.
    if len(argv) < 2 or not argv[0] or not argv[1]:
        _err("사용법: lifekit.sh workout-add <date> <category> <subtype> "
             "[minutes kcal note avg_hr]")
    date, wtype = argv[0], argv[1]
    g = lambda i: argv[i] if i < len(argv) else ''
    conn = get_conn()
    try:
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
    # person-add <name> [relation] [aliases]
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh person-add <name> [relation] [aliases]")
    g = lambda i: argv[i] if i < len(argv) else None
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


def cli_appt_find(argv):
    # appt-find <date_from> [date_to]
    if not argv or not argv[0]:
        _err("사용법: lifekit.sh appt-find <date_from> [date_to]")
    g = lambda i: argv[i] if i < len(argv) else None
    for aid, start_at, title, location in appt_find(argv[0], g(1)):
        print(f"{aid}\t{start_at or ''}\t{title}\t{location or ''}")


def cli_appt_add(argv):
    # appt-add <title> <start_at> [end_at location purpose summary]
    if len(argv) < 2 or not argv[0] or not argv[1]:
        _err("사용법: lifekit.sh appt-add <title> <start_at> "
             "[end_at location purpose summary]")
    g = lambda i: argv[i] if i < len(argv) else None
    aid = appt_add(argv[0], argv[1], end_at=g(2), location=g(3),
                   purpose=g(4), summary=g(5))
    print(f"{aid}\t{argv[0]}")


def cli_appt_upd(argv):
    # appt-upd <id> field=value [field=value ...]
    _u = ("사용법: lifekit.sh appt-upd <id> field=value [field=value ...]\n"
          "  field: title start_at end_at location location_url purpose summary")
    if len(argv) < 2 or not str(argv[0]).isdigit():
        _err(_u)
    fields = {}
    for tok in argv[1:]:
        if '=' not in tok:
            _err(f"field=value 형식이 아님: {tok}\n{_u}")
        col, val = tok.split('=', 1)
        col = col.strip()
        if col not in _APPT_UPD_COLS:
            _err(f"수정 불가 컬럼: {col}\n{_u}")
        fields[col] = val
    if not fields:
        _err(_u)
    row = appt_upd(argv[0], fields)
    if row is None:
        _err(f"해당 id 약속 없음: {argv[0]}")
    print(f"{row[0]}\t{row[1]}\t{row[2] or ''}")


def cli_appt_person(argv):
    # appt-person <appt_id> <person_id>
    if len(argv) < 2 or not str(argv[0]).isdigit() or not str(argv[1]).isdigit():
        _err("사용법: lifekit.sh appt-person <appt_id> <person_id>")
    n = appt_person_add(argv[0], argv[1])
    print(f"appt {argv[0]} 참가자 {n}명")


def cli_appt_show(argv):
    # appt-show <id> — 약속 1건 + 참가자
    if not argv or not str(argv[0]).isdigit():
        _err("사용법: lifekit.sh appt-show <id>")
    conn = get_conn()
    try:
        a = conn.execute(
            "SELECT id, title, start_at, end_at, location, purpose, summary "
            "FROM appointments WHERE id=?;", (int(argv[0]),)).fetchone()
        if a is None:
            _err(f"해당 id 약속 없음: {argv[0]}")
        print(f"{a[0]}\t{a[1]}\t{a[2] or ''}\t{a[3] or ''}\t"
              f"{a[4] or ''}\t{a[5] or ''}\t{a[6] or ''}")
        for pid, name, aliases, relation in appt_persons(argv[0], conn=conn):
            print(f"  {pid}\t{name}\t{aliases or ''}\t{relation or ''}")
    finally:
        conn.close()


def cli_migrate_body_stats(argv):
    """body_stats.json 의 모든 키를 config 테이블로 복사(멱등, DGN-071 v2).

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
    """현재 신체/목표 상태를 KEY=VALUE 로 출력(DGN-071 v2 hook/사람용).

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
