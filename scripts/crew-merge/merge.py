#!/usr/bin/env python3
"""lifekit crew merge engine -- DGN-791 spec-v3 pipeline (steps 0-14).

Reusable crew-merge primitive.  All table/FK/dedup/tag/name-map values are
configuration arguments -- no hardcoding of instance-specific data.

Pipeline:
  0  PRE-SCAN         schema drift check vs migrate.py coverage
  1  LADDER           VACUUM INTO copies + migrate.py v19 upgrade
  2  TARGET DDL-ONLY  fresh build from schema.sql (no seeds inserted)
  3  NATURAL-KEY MAPS pre-compute type_id, area, dedup, dup->ag-id maps
  4  LOAD Ag (base)   explicit column-mapped INSERT (no SELECT *)
  5  ID-REMAP Warg    offset + area constant-map + type natural-key map
  6  REBUILD children workout_classifications, session_type dedup,
                      session_muscle rebuild
  7  WARG-ONLY        workout_sets, ledger_*, exercise ref tables (FK remap)
  8  LOAD Warg        surviving rows with owning_agent domain-owner
  9  CONFIG MERGE     key union, conflict = max(updated_at), tie-break health->Warg
 10  SPEND CATEGORY   Ag 17 canonical; Warg rows excluded
 11  DROP ag_source_id post-merge column removal
 12  VERIFY           row counts / fk_check / integrity / natural-key exhaustive check
 13  WAL CHECKPOINT   close + wal_checkpoint(TRUNCATE) + -wal 0-byte gate
 14  ATOMIC PROMOTE   rename temp -> crew path + write marker

Usage:
    python3 merge.py [OPTIONS]

    --ag-db PATH          Ag live DB (read-only; VACUUM INTO copy made)
    --warg-db PATH        Warg live DB (read-only; VACUUM INTO copy made)
    --schema-sql PATH     Skull v19 schema.sql (DDL source of truth)
    --migrate-py PATH     Skull migrate.py runner
    --output PATH         Target crew DB path (atomic rename destination)
    --temp-dir PATH       Scratch directory (default: /tmp)
    --dry-run             Run through step 12 VERIFY, skip steps 13-14 promote
    --force               Overwrite existing output (ignore marker guard)
    --config PATH         JSON config overrides (area map, health-tie-break keys)

Exit codes:
    0  success (or dry-run VERIFY green)
    1  pre-scan mismatch / VERIFY failure
    2  migrate ladder failure
    3  internal error
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# defaults (spec-v3 locked values for the Ag/Warg crew merge)
# ---------------------------------------------------------------------------

# area constant-map: Warg area_id -> Ag area_id (spec rule 1)
DEFAULT_AREA_MAP = {1: 5, 2: 15}

# tie-break: on config key conflict with equal updated_at, prefer Warg
# for these health-domain keys (spec rule addendum #3)
DEFAULT_HEALTH_TIE_BREAK_KEYS = {
    'weight_kg', 'deficit_kcal', 'fat_mass_kg', 'fat_ratio',
    'lean_mass_kg', 'skeletal_muscle_kg', 'avg_steps', 'height_cm',
    'other_neat_kcal', 'protein_g', 'goal_mode', 'updated',
}

# owning_agent domain owner by table (spec rule 9 + addendum #2)
AG_OWNER_TABLES = {'tasks', 'appointments', 'persons'}
WARG_OWNER_TABLES = {'workouts', 'meals', 'metric_log'}
# routine_def is per-row by created_by (handled inline)
AG_CREATED_BY_VALUES = {'ag', 'notion-import'}

# spend_category: Ag canonical, Warg excluded (spec rule addendum #4)
SPEND_CATEGORY_OWNER = 'ag'

# tables that carry type_id FK (workout_types natural key) in Warg
TYPE_ID_FK_TABLES = {'session_muscle', 'workout_classifications'}

# Warg-only tables to include verbatim (spec rule 7)
WARG_ONLY_TABLES = [
    'workout_sets',
    'ledger_goal',
    'ledger_preference',
    'ledger_resource',
    'ledger_constraint',
    'ledger_audit',
    'exercise_movement_grip',
    'movement_muscle_base',
    'exercise_muscle',
    'exercise_subregion',
    'grip_muscle_delta',
    'grip_vocab',
    'exercise_fatigue_cost',
    'exercise_pattern',
]

# marker sentinel stored as a PRAGMA application_id value
MERGE_MARKER_APP_ID = 0x44474E31  # 'DGN1' as int

log = logging.getLogger('lifekit_merge')


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def open_rw(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.execute('PRAGMA foreign_keys = ON')
    c.execute('PRAGMA journal_mode = WAL')
    c.execute('PRAGMA busy_timeout = 5000')
    return c


def open_ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    return c


def vacuum_into(src: str, dst: str) -> None:
    """Safe offline copy via VACUUM INTO (handles WAL mode).

    Source is opened read-only (mode=ro) to prevent any write to the original.
    VACUUM INTO requires only a read lock on the source; the destination is
    written by the SQLite VFS directly, so mode=ro on the source connection is
    safe and guarantees the original file is never modified.
    """
    if os.path.exists(dst):
        os.unlink(dst)
    c = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
    try:
        c.execute('VACUUM INTO ?', (dst,))
    finally:
        c.close()


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def column_names(conn: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def table_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]


def get_user_version(conn: sqlite3.Connection) -> int:
    return conn.execute('PRAGMA user_version').fetchone()[0]


def _run_migrate(migrate_py: str, db_path: str) -> None:
    """Run Skull migrate.py on db_path in-place."""
    result = subprocess.run(
        [sys.executable, migrate_py, db_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            f'migrate.py failed on {db_path}:\n{result.stdout}\n{result.stderr}'
        )
    log.debug('migrate output: %s', result.stdout.strip())


def _schema_table_columns(schema_sql: str) -> dict:
    """Build a temp in-memory DB from schema.sql and return {table: [cols]}
    via PRAGMA table_info -- accurate, no regex guesswork."""
    schema_text = Path(schema_sql).read_text(encoding='utf-8')
    # Strip INSERT statements so only DDL remains
    lines = []
    skip = False
    for line in schema_text.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith('INSERT'):
            skip = True
        if not skip:
            lines.append(line)
        if skip and stripped.endswith(';'):
            skip = False
    ddl_only = '\n'.join(lines)

    c = sqlite3.connect(':memory:')
    c.execute('PRAGMA foreign_keys = OFF')
    c.executescript(ddl_only)
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    result = {}
    for t in tables:
        result[t] = [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
    c.close()
    return result


# ---------------------------------------------------------------------------
# STEP 0: PRE-SCAN
# ---------------------------------------------------------------------------

def step_pre_scan(ag_copy: str, warg_copy: str, migrate_py: str,
                  schema_sql: str) -> None:
    """Verify schema drift is covered by migrate.py.

    Un-versioned extra columns that are NOT in schema.sql and NOT already
    absorbed by the known-safe exclusion list (ag_source_id, kcal) indicate
    drift that migrate.py has not handled.  This is a hard abort: continuing
    on drifted schema risks silent data loss or constraint violations.

    Spec-v3 step 0: un-versioned drift -> ABORT (exit nonzero).
    """
    log.info('[0] PRE-SCAN: checking schema drift vs migrate.py coverage')
    schema_cols = _schema_table_columns(schema_sql)

    # Known-safe columns that legitimately exist in live DBs but not in
    # schema.sql (ag_source_id dropped post-merge; kcal is GENERATED).
    SAFE_EXTRA = {'ag_source_id', 'kcal'}

    drift_errors = []

    for db_path, label in [(ag_copy, 'Ag'), (warg_copy, 'Warg')]:
        c = sqlite3.connect(db_path)
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        for t in tables:
            if t not in schema_cols:
                # Table absent from schema: flag if it has data (unexpected)
                cnt = table_count(c, t)
                if cnt > 0:
                    msg = (f'[0] PRE-SCAN: table {t!r} in {label} not in '
                           f'schema.sql ({cnt} rows) -- un-versioned drift')
                    log.error(msg)
                    drift_errors.append(msg)
            else:
                live_cols = set(column_names(c, t))
                schema_set = set(schema_cols[t])
                extra = live_cols - schema_set - SAFE_EXTRA
                if extra:
                    msg = (f'[0] PRE-SCAN: {label}.{t} has extra columns '
                           f'not in schema.sql: {extra} -- un-versioned drift')
                    log.error(msg)
                    drift_errors.append(msg)
        c.close()

    if drift_errors:
        raise RuntimeError(
            f'[0] PRE-SCAN ABORT: {len(drift_errors)} un-versioned drift(s) '
            f'detected; migrate.py must absorb all column/table additions '
            f'before merge can proceed.\n' + '\n'.join(drift_errors)
        )

    log.info('[0] PRE-SCAN: complete, no un-versioned drift detected')


# ---------------------------------------------------------------------------
# STEP 1: LADDER  (migrate to v19)
# ---------------------------------------------------------------------------

def step_ladder(ag_copy: str, warg_copy: str, migrate_py: str) -> None:
    log.info('[1] LADDER: running migrate.py on both copies')
    for db_path, label in [(ag_copy, 'Ag'), (warg_copy, 'Warg')]:
        before_counts = {}
        c = sqlite3.connect(db_path)
        for t in [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]:
            try:
                before_counts[t] = table_count(c, t)
            except Exception:
                pass
        c.close()

        _run_migrate(migrate_py, db_path)

        c = sqlite3.connect(db_path)
        uv = get_user_version(c)
        if uv != 19:
            raise RuntimeError(f'[1] {label} migrate result user_version={uv}, expected 19')
        ic = c.execute('PRAGMA integrity_check').fetchone()[0]
        if ic != 'ok':
            raise RuntimeError(f'[1] {label} integrity_check after migrate: {ic}')
        # row-count preservation -- abort on unexpected loss
        row_errors = []
        for t, cnt_before in before_counts.items():
            if table_exists(c, t):
                cnt_after = table_count(c, t)
                if cnt_after < cnt_before:
                    msg = (f'[1] {label}.{t} row count DECREASED after migrate: '
                           f'{cnt_before} -> {cnt_after}')
                    log.error(msg)
                    row_errors.append(msg)
                elif cnt_after != cnt_before:
                    # Increase is acceptable (migrate may add rows e.g. backfill)
                    log.info('[1] %s.%s row count changed: %d -> %d',
                             label, t, cnt_before, cnt_after)
        if row_errors:
            raise RuntimeError(
                f'[1] LADDER ABORT: migrate.py caused row loss in {label}.\n'
                + '\n'.join(row_errors)
            )
        c.close()
        log.info('[1] %s migrated to v19, integrity ok', label)


# ---------------------------------------------------------------------------
# STEP 2: TARGET DDL-ONLY BUILD
# ---------------------------------------------------------------------------

def step_target_ddl(schema_sql: str, temp_dir: str) -> str:
    """Build a fresh target DB from schema.sql (DDL only, no seed INSERTs).

    Returns path to the new temp DB.
    """
    log.info('[2] TARGET DDL-ONLY BUILD')
    target_path = os.path.join(temp_dir, 'lifekit_merge_target.db')
    if os.path.exists(target_path):
        os.unlink(target_path)

    schema_text = Path(schema_sql).read_text(encoding='utf-8')
    # Strip INSERT statements (seed data) -- only CREATE TABLE / INDEX / VIEW / PRAGMA
    lines = []
    skip = False
    for line in schema_text.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith('INSERT'):
            skip = True
        if not skip:
            lines.append(line)
        if skip and stripped.endswith(';'):
            skip = False

    ddl_only = '\n'.join(lines)

    c = sqlite3.connect(target_path)
    c.execute('PRAGMA foreign_keys = OFF')
    c.executescript(ddl_only)
    c.execute('PRAGMA foreign_keys = ON')
    uv = get_user_version(c)
    ic = c.execute('PRAGMA integrity_check').fetchone()[0]
    c.close()

    if ic != 'ok':
        raise RuntimeError(f'[2] fresh DDL build integrity_check: {ic}')
    log.info('[2] target DB built at %s (user_version=%d)', target_path, uv)
    return target_path


# ---------------------------------------------------------------------------
# STEP 3: NATURAL-KEY MAPS
# ---------------------------------------------------------------------------

def step_natural_key_maps(ag_copy: str, warg_copy: str,
                          area_map: dict, target_path: str,
                          temp_dir: str) -> dict:
    """Pre-compute all remap tables needed before FK rewriting.

    Returns a config dict consumed by later steps:
      type_id_map:      {warg_type_id -> merged_type_id}  (int -> int)
      warg_type_new:    {warg_type_id -> (category, subtype)} for warg-only
      area_map:         {warg_area_id -> ag_area_id}
      dedup_workout:    set of Warg workout ids to DROP (ag_source_id mirrors)
      dedup_meal:       set of Warg meal ids to DROP
      dup_wc_ag_id:     {warg_workout_id -> ag_workout_id} for mirror WC remap
      ag_max_ids:       {table -> current max id in Ag copy (= offset base)}
    """
    log.info('[3] NATURAL-KEY MAPS')
    ag = sqlite3.connect(ag_copy)
    warg = sqlite3.connect(warg_copy)
    tgt = sqlite3.connect(target_path)

    # --- type_id map (workout_types natural key = (category, subtype)) ---
    ag_type_map = {(r[1], r[2]): r[0]
                   for r in ag.execute('SELECT id,category,subtype FROM workout_types')}
    warg_types = warg.execute(
        'SELECT id,category,subtype FROM workout_types ORDER BY id'
    ).fetchall()

    # New warg-only types get ids starting after Ag's max(id) + some headroom
    ag_max_type_id = ag.execute('SELECT max(id) FROM workout_types').fetchone()[0] or 0
    new_type_offset = ag_max_type_id + 100  # generous headroom
    type_id_map = {}
    warg_type_new = {}
    new_type_counter = new_type_offset
    for wid, cat, sub in warg_types:
        if (cat, sub) in ag_type_map:
            type_id_map[wid] = ag_type_map[(cat, sub)]
        else:
            type_id_map[wid] = new_type_counter
            warg_type_new[wid] = (cat, sub)
            new_type_counter += 1

    log.info('[3] type_id_map: %d shared, %d warg-only-new',
             len(warg_types) - len(warg_type_new), len(warg_type_new))

    # --- dedup sets (ag_source_id mirrors) ---
    dedup_workout = set()
    dup_wc_ag_id = {}  # warg workout_id -> ag workout_id (for WC remap)
    for r in warg.execute('SELECT id, ag_source_id FROM workouts WHERE ag_source_id IS NOT NULL'):
        dedup_workout.add(r[0])
        dup_wc_ag_id[r[0]] = r[1]

    dedup_meal = set()
    for r in warg.execute('SELECT id FROM meals WHERE ag_source_id IS NOT NULL'):
        dedup_meal.add(r[0])

    # routine_def: spec says 1 row dedup (ag_source_id) but column absent in Warg.
    # From live data, no routine_def dedup needed (no ag_source_id col in Warg).
    dedup_routine_def = set()
    if 'ag_source_id' in column_names(warg, 'routine_def'):
        for r in warg.execute('SELECT id FROM routine_def WHERE ag_source_id IS NOT NULL'):
            dedup_routine_def.add(r[0])

    # place dedup: Warg place 1 ('집') = same physical place as Ag place 2
    # Strategy: map Warg place id 1 -> Ag place id 2
    # (verified: same name, Warg has no FK refs on timeblock.place_id or routine_def.place_id)
    warg_place_map = {}  # warg_place_id -> ag_place_id
    ag_place_by_name = {r[1]: r[0]
                        for r in ag.execute('SELECT id, name FROM place')}
    for r in warg.execute('SELECT id, name FROM place'):
        wid, name = r[0], r[1]
        if name in ag_place_by_name:
            warg_place_map[wid] = ag_place_by_name[name]
        # else: warg-only place (will get offset)

    log.info('[3] dedup_workout=%d dedup_meal=%d dedup_routine_def=%d',
             len(dedup_workout), len(dedup_meal), len(dedup_routine_def))
    log.info('[3] place_map=%s', warg_place_map)

    # --- max id per table in Ag (offset base) ---
    shared_tables = ['workouts', 'meals', 'metric_log', 'timeblock', 'sub_event',
                     'routine', 'routine_def', 'areas', 'place', 'projects',
                     'session_type', 'workout_types', 'tasks', 'appointments',
                     'persons', 'spend_category', 'config',
                     'ledger_goal', 'ledger_preference', 'ledger_resource',
                     'ledger_constraint', 'ledger_audit', 'workout_sets',
                     'roller_log']
    ag_max_ids = {}
    for t in shared_tables:
        if table_exists(ag, t):
            try:
                v = ag.execute(f'SELECT max(id) FROM "{t}"').fetchone()[0]
                ag_max_ids[t] = v or 0
            except Exception:
                ag_max_ids[t] = 0
        else:
            ag_max_ids[t] = 0

    ag.close()
    warg.close()
    tgt.close()

    return {
        'type_id_map': type_id_map,
        'warg_type_new': warg_type_new,
        'area_map': area_map,
        'dedup_workout': dedup_workout,
        'dedup_meal': dedup_meal,
        'dedup_routine_def': dedup_routine_def,
        'dup_wc_ag_id': dup_wc_ag_id,
        'warg_place_map': warg_place_map,
        'ag_max_ids': ag_max_ids,
    }


# ---------------------------------------------------------------------------
# column helpers for explicit INSERT
# ---------------------------------------------------------------------------

def _intersect_cols(src_conn, src_table, dst_cols):
    """Return column list that exists in both src table and dst_cols list."""
    src_cols = set(column_names(src_conn, src_table))
    return [c for c in dst_cols if c in src_cols]


def _insert_rows(dst: sqlite3.Connection, table: str, cols: list,
                 rows: list) -> None:
    """Bulk INSERT rows (list of tuples) into dst.table with explicit cols."""
    if not rows:
        return
    placeholders = ', '.join(['?'] * len(cols))
    col_str = ', '.join(f'"{c}"' for c in cols)
    dst.executemany(
        f'INSERT INTO "{table}" ({col_str}) VALUES ({placeholders})',
        rows
    )


# ---------------------------------------------------------------------------
# STEP 4: LOAD Ag (base)
# ---------------------------------------------------------------------------

def step_load_ag(ag_copy: str, target_path: str, maps: dict) -> None:
    """Load all Ag rows into target with explicit column mapping."""
    log.info('[4] LOAD Ag (base)')
    ag = sqlite3.connect(ag_copy)
    tgt = open_rw(target_path)
    tgt.execute('PRAGMA foreign_keys = OFF')

    # Tables to copy from Ag verbatim (owning_agent already set by migrate or spec)
    # These are all tables except the ones we handle specially or that are Warg-only
    ag_tables = [r[0] for r in ag.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]

    skip_tables = set(WARG_ONLY_TABLES) | {
        # spend_category: Ag canonical, loaded below with explicit log
        'spend_category',
        # config: merged in step 9 (key-union, max updated_at)
        'config',
        # workout_classifications: loaded explicitly below (B4 fix)
        'workout_classifications',
        # session_muscle: loaded explicitly below (B4 fix)
        'session_muscle',
    }

    for table in ag_tables:
        if table in skip_tables:
            continue
        if not table_exists(tgt, table):
            log.warning('[4] table %r in Ag but not in target -- skip', table)
            continue

        dst_cols = column_names(tgt, table)
        # ag_source_id is absent from target schema (dropped post-merge, step 11)
        dst_cols = [c for c in dst_cols if c != 'ag_source_id']

        src_cols = _intersect_cols(ag, table, dst_cols)
        rows = ag.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in src_cols)} FROM "{table}"'
        ).fetchall()

        # If src_cols != dst_cols, fill missing dst cols with None
        if src_cols == dst_cols:
            _insert_rows(tgt, table, dst_cols, rows)
        else:
            # Build full row with None for missing cols
            src_idx = {c: i for i, c in enumerate(src_cols)}
            full_rows = []
            for row in rows:
                full_row = tuple(
                    row[src_idx[c]] if c in src_idx else None
                    for c in dst_cols
                )
                full_rows.append(full_row)
            _insert_rows(tgt, table, dst_cols, full_rows)

        if rows:
            log.debug('[4] Ag.%s: %d rows', table, len(rows))

    # spend_category: Ag canonical
    sc_dst_cols = [c for c in column_names(tgt, 'spend_category') if c != 'ag_source_id']
    sc_src_cols = _intersect_cols(ag, 'spend_category', sc_dst_cols)
    sc_rows = ag.execute(
        f'SELECT {", ".join(chr(34)+c+chr(34) for c in sc_src_cols)} FROM spend_category'
    ).fetchall()
    _insert_rows(tgt, 'spend_category', sc_src_cols, sc_rows)
    log.info('[4] spend_category: %d rows (Ag canonical)', len(sc_rows))

    # workout_classifications: Ag-native rows -- B4 fix.
    # Previously in skip_tables with a false comment "already loaded in step4".
    # Ag-native WC rows must be explicitly loaded here; Warg WC rows (including
    # mirror remaps) are added in step 6c.  INSERT OR IGNORE ensures step 6c's
    # Warg rows do not collide.
    if table_exists(tgt, 'workout_classifications') and table_exists(ag, 'workout_classifications'):
        wc_dst_cols = column_names(tgt, 'workout_classifications')
        wc_src_cols = _intersect_cols(ag, 'workout_classifications', wc_dst_cols)
        wc_rows = ag.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in wc_src_cols)} FROM workout_classifications'
        ).fetchall()
        for row in wc_rows:
            tgt.execute(
                f'INSERT OR IGNORE INTO workout_classifications '
                f'({", ".join(chr(34)+c+chr(34) for c in wc_src_cols)}) '
                f'VALUES ({", ".join(["?"]*len(wc_src_cols))})',
                row
            )
        log.info('[4] workout_classifications (Ag): %d rows', len(wc_rows))

    # session_muscle: Ag-native rows -- B4 fix.
    # session_muscle has PK(session_type_id, type_id); Ag rows loaded here.
    # Warg rows are rebuilt in step 6b with remapped ids.
    if table_exists(tgt, 'session_muscle') and table_exists(ag, 'session_muscle'):
        sm_dst_cols = column_names(tgt, 'session_muscle')
        sm_src_cols = _intersect_cols(ag, 'session_muscle', sm_dst_cols)
        sm_rows = ag.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in sm_src_cols)} FROM session_muscle'
        ).fetchall()
        for row in sm_rows:
            tgt.execute(
                f'INSERT OR IGNORE INTO session_muscle '
                f'({", ".join(chr(34)+c+chr(34) for c in sm_src_cols)}) '
                f'VALUES ({", ".join(["?"]*len(sm_src_cols))})',
                row
            )
        log.info('[4] session_muscle (Ag): %d rows', len(sm_rows))

    tgt.commit()
    ag.close()
    tgt.close()
    log.info('[4] Ag base load complete')


# ---------------------------------------------------------------------------
# STEP 5: ID-REMAP Warg survivors
# ---------------------------------------------------------------------------

def _compute_warg_id_remap(warg_copy: str, maps: dict) -> dict:
    """Compute {table -> {warg_id -> new_merged_id}} for all Warg tables
    that need integer id remapping.

    Also returns compound remap for type_id, area_id, place_id FKs.
    """
    ag_max = maps['ag_max_ids']
    type_id_map = maps['type_id_map']
    area_map = maps['area_map']
    warg_place_map = maps['warg_place_map']
    dedup_workout = maps['dedup_workout']
    dedup_meal = maps['dedup_meal']
    dedup_routine_def = maps['dedup_routine_def']

    warg = sqlite3.connect(warg_copy)

    # Offset values: start at Ag max + generous gap to avoid collision
    # We use per-table offsets based on ag_max_ids
    OFFSET_GAP = 10000

    id_remap = {}  # {table: {old_id: new_id}}

    def _make_offset(table):
        base = ag_max.get(table, 0)
        return base + OFFSET_GAP

    # workout_types: shared -> ag id, warg-only -> pre-computed new ids
    wt_remap = {}
    for wid in [r[0] for r in warg.execute('SELECT id FROM workout_types ORDER BY id')]:
        wt_remap[wid] = type_id_map[wid]
    id_remap['workout_types'] = wt_remap

    # areas: constant map (Warg 1->5, 2->15 per spec)
    id_remap['areas'] = dict(area_map)

    # workouts: surviving = those NOT in dedup_workout; offset
    wo_offset = _make_offset('workouts')
    wo_remap = {}
    for wid, in warg.execute('SELECT id FROM workouts ORDER BY id'):
        if wid not in dedup_workout:
            wo_remap[wid] = wid + wo_offset
    id_remap['workouts'] = wo_remap

    # meals: surviving = those NOT in dedup_meal; offset
    ml_offset = _make_offset('meals')
    ml_remap = {}
    for wid, in warg.execute('SELECT id FROM meals ORDER BY id'):
        if wid not in dedup_meal:
            ml_remap[wid] = wid + ml_offset
    id_remap['meals'] = ml_remap

    # metric_log: Warg wins entirely (all Ag rows are dupes per spec rule 6)
    # Warg rows offset to avoid collision with Ag ids
    mlog_offset = _make_offset('metric_log')
    mlog_remap = {}
    for wid, in warg.execute('SELECT id FROM metric_log ORDER BY id'):
        mlog_remap[wid] = wid + mlog_offset
    id_remap['metric_log'] = mlog_remap

    # routine: Ag and Warg both have id=1 with same name -> dedup (Ag wins)
    # Warg routine id 1 -> Ag routine id 1
    # If Warg had other routines, offset them; for now it's just id=1
    ag_routine_by_name = {}
    ag_conn = sqlite3.connect(warg_copy)  # temp workaround; use ag below
    ag_conn.close()
    # Load ag routines from ag_copy via maps? We need ag conn here.
    # Simple: for the known case (same name), map Warg id 1 -> Ag id 1.
    # Generic: compare names; if matched, use Ag id; else offset.
    # We store this in maps from a separate pass -- done below.
    id_remap['routine'] = {}  # filled after ag load

    # routine_def: Warg rows offset
    rd_offset = _make_offset('routine_def')
    rd_remap = {}
    for wid, in warg.execute('SELECT id FROM routine_def ORDER BY id'):
        if wid not in dedup_routine_def:
            rd_remap[wid] = wid + rd_offset
    id_remap['routine_def'] = rd_remap

    # timeblock: Warg events offset
    ev_offset = _make_offset('timeblock')
    ev_remap = {}
    for wid, in warg.execute('SELECT id FROM timeblock ORDER BY id'):
        ev_remap[wid] = wid + ev_offset
    id_remap['timeblock'] = ev_remap

    # sub_event: Warg sub_events offset
    sev_offset = _make_offset('sub_event')
    sev_remap = {}
    for wid, in warg.execute('SELECT id FROM sub_event ORDER BY id'):
        sev_remap[wid] = wid + sev_offset
    id_remap['sub_event'] = sev_remap

    # session_type: dedup by (remapped routine_id, code)
    # Warg session_type ids offset; dedup handled in step 6
    st_offset = _make_offset('session_type')
    st_remap = {}
    for wid, in warg.execute('SELECT id FROM session_type ORDER BY id'):
        st_remap[wid] = wid + st_offset
    id_remap['session_type'] = st_remap

    # place: matched -> ag id; warg-only -> offset
    place_offset = _make_offset('place')
    pl_remap = {}
    for wid, in warg.execute('SELECT id FROM place ORDER BY id'):
        if wid in warg_place_map:
            pl_remap[wid] = warg_place_map[wid]
        else:
            pl_remap[wid] = wid + place_offset
    id_remap['place'] = pl_remap

    # projects: Warg has 0 rows -> no remap needed; offset for safety
    proj_offset = _make_offset('projects')
    proj_remap = {}
    for wid, in warg.execute('SELECT id FROM projects ORDER BY id'):
        proj_remap[wid] = wid + proj_offset
    id_remap['projects'] = proj_remap

    # Warg-only tables that have integer PKs: offset them
    for table in ['ledger_goal', 'ledger_preference', 'ledger_resource',
                  'ledger_constraint', 'ledger_audit', 'workout_sets']:
        if not table_exists(warg, table):
            id_remap[table] = {}
            continue
        offset = _make_offset(table)
        remap = {}
        try:
            for wid, in warg.execute(f'SELECT id FROM "{table}" ORDER BY id'):
                remap[wid] = wid + offset
        except Exception:
            pass
        id_remap[table] = remap

    warg.close()
    return id_remap


# ---------------------------------------------------------------------------
# STEP 6: REBUILD children (workout_classifications, session_type, session_muscle)
# ---------------------------------------------------------------------------

def _remap_type_id(type_id, type_id_map):
    """Map Warg type_id to merged type_id.  Raise if missing."""
    if type_id not in type_id_map:
        raise ValueError(f'type_id {type_id} not in type_id_map')
    return type_id_map[type_id]


# ---------------------------------------------------------------------------
# STEP 7+8: Warg survivors load  (combined for efficiency)
# ---------------------------------------------------------------------------

def _owning_agent_for_row(table: str, created_by: str = None) -> str:
    """Determine owning_agent for a Warg survivor row."""
    if table in WARG_OWNER_TABLES:
        return 'warg'
    if table in AG_OWNER_TABLES:
        return 'ag'
    if table == 'routine_def':
        if created_by in AG_CREATED_BY_VALUES:
            return 'ag'
        return 'warg'
    # timeblock/sub_event: preserve verbatim (handled in caller)
    return 'warg'


# ---------------------------------------------------------------------------
# MAIN PIPELINE EXECUTOR
# ---------------------------------------------------------------------------

class MergeEngine:

    def __init__(self, ag_db: str, warg_db: str, schema_sql: str,
                 migrate_py: str, output: str, temp_dir: str,
                 area_map: dict = None,
                 health_tie_break_keys: set = None,
                 dry_run: bool = False,
                 force: bool = False):
        self.ag_db = ag_db
        self.warg_db = warg_db
        self.schema_sql = schema_sql
        self.migrate_py = migrate_py
        self.output = output
        self.temp_dir = temp_dir
        self.area_map = area_map or DEFAULT_AREA_MAP
        self.health_tie_break_keys = health_tie_break_keys or DEFAULT_HEALTH_TIE_BREAK_KEYS
        self.dry_run = dry_run
        self.force = force

        self.ag_copy = os.path.join(temp_dir, 'lifekit_merge_ag.db')
        self.warg_copy = os.path.join(temp_dir, 'lifekit_merge_warg.db')
        self.target_path = None  # set by step 2
        self.maps = None         # set by step 3
        self.id_remap = None     # set by step 5

    def run(self):
        log.info('=== lifekit merge engine start ===')

        # guard: check marker to prevent re-merge overwrite
        if not self.force and os.path.exists(self.output):
            c = sqlite3.connect(self.output)
            app_id = c.execute('PRAGMA application_id').fetchone()[0]
            c.close()
            if app_id == MERGE_MARKER_APP_ID:
                raise RuntimeError(
                    f'Output {self.output} already has merge marker. '
                    'Use --force to overwrite.'
                )

        # step 0: pre-scan (on originals, read-only)
        log.info('[0] Making VACUUM INTO copies before pre-scan')
        vacuum_into(self.ag_db, self.ag_copy)
        vacuum_into(self.warg_db, self.warg_copy)

        step_pre_scan(self.ag_copy, self.warg_copy, self.migrate_py,
                      self.schema_sql)

        # step 1: ladder
        step_ladder(self.ag_copy, self.warg_copy, self.migrate_py)

        # step 2: target DDL-only
        self.target_path = step_target_ddl(self.schema_sql, self.temp_dir)

        # step 3: natural-key maps
        self.maps = step_natural_key_maps(
            self.ag_copy, self.warg_copy, self.area_map,
            self.target_path, self.temp_dir
        )

        # step 4: load Ag
        step_load_ag(self.ag_copy, self.target_path, self.maps)

        # step 5: compute id remap
        log.info('[5] ID-REMAP: computing Warg id remaps')
        self.id_remap = _compute_warg_id_remap(self.warg_copy, self.maps)
        # Fill routine dedup: Warg routine id -> Ag routine id (same name)
        self._fill_routine_remap()

        # step 6+: load warg-only tables, rebuild children, load survivors
        self._step_load_warg()

        # step 9: config merge
        self._step_config_merge()

        # step 10: spend_category already done in step 4 (Ag canonical)
        log.info('[10] spend_category: Ag canonical already loaded in step 4')

        # step 11: drop ag_source_id
        self._step_drop_ag_source_id()

        # step 12: VERIFY
        verify_result = self._step_verify()
        if not verify_result['ok']:
            raise RuntimeError(f'[12] VERIFY FAILED: {verify_result}')
        log.info('[12] VERIFY GREEN: %s', verify_result)

        if self.dry_run:
            log.info('[DRY-RUN] Skipping steps 13-14 (promote)')
            return verify_result

        # step 13: WAL checkpoint
        self._step_wal_checkpoint()

        # step 14: atomic promote
        self._step_atomic_promote()

        log.info('=== lifekit merge engine complete ===')
        return verify_result

    # -----------------------------------------------------------------------
    # internal helpers
    # -----------------------------------------------------------------------

    def _fill_routine_remap(self):
        """Resolve Warg routine ids: same-name -> Ag id; novel -> offset."""
        ag = sqlite3.connect(self.ag_copy)
        warg = sqlite3.connect(self.warg_copy)
        ag_routine_by_name = {r[1]: r[0]
                              for r in ag.execute('SELECT id, name FROM routine')}
        remap = {}
        offset = self.maps['ag_max_ids'].get('routine', 0) + 10000
        for wid, name in warg.execute('SELECT id, name FROM routine ORDER BY id'):
            if name in ag_routine_by_name:
                remap[wid] = ag_routine_by_name[name]
            else:
                remap[wid] = wid + offset
        self.id_remap['routine'] = remap
        ag.close()
        warg.close()
        log.info('[5] routine remap: %s', remap)

    def _remap_val(self, table, old_id):
        """Return remapped id for a given table's old_id.  None -> None."""
        if old_id is None:
            return None
        tbl_remap = self.id_remap.get(table, {})
        return tbl_remap.get(old_id, old_id)

    def _step_load_warg(self):
        """Steps 5 (apply remap), 6 (rebuild children), 7 (Warg-only tables),
        8 (Warg survivors with owning_agent)."""
        log.info('[5-8] Loading Warg data into target')
        warg = sqlite3.connect(self.warg_copy)
        tgt = open_rw(self.target_path)
        tgt.execute('PRAGMA foreign_keys = OFF')

        type_id_map = self.maps['type_id_map']
        area_map = self.maps['area_map']
        dedup_workout = self.maps['dedup_workout']
        dedup_meal = self.maps['dedup_meal']
        dedup_routine_def = self.maps['dedup_routine_def']
        dup_wc_ag_id = self.maps['dup_wc_ag_id']
        id_remap = self.id_remap

        # --- workout_types: insert warg-only new types ---
        warg_type_new = self.maps['warg_type_new']
        existing_ag_types = set(tgt.execute(
            'SELECT id FROM workout_types'
        ).fetchall())
        for wid, (cat, sub) in warg_type_new.items():
            new_id = type_id_map[wid]
            warg_row = warg.execute(
                'SELECT sort, active FROM workout_types WHERE id=?', (wid,)
            ).fetchone()
            sort_val = warg_row[0] if warg_row else 0
            active_val = warg_row[1] if warg_row else 1
            tgt.execute(
                'INSERT OR IGNORE INTO workout_types (id, category, subtype, sort, active) '
                'VALUES (?,?,?,?,?)',
                (new_id, cat, sub, sort_val, active_val)
            )
            log.debug('[5] new workout_type id=%d %s/%s', new_id, cat, sub)

        # --- routine: insert Warg-novel routines (those not dedup'd to Ag) ---
        routine_cols = column_names(warg, 'routine')
        routine_dst_cols = column_names(tgt, 'routine')
        routine_common = [c for c in routine_dst_cols if c in set(routine_cols)]
        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in routine_common)} FROM routine ORDER BY id'
        ):
            row_dict = dict(zip(routine_common, row))
            wid = row_dict['id']
            new_id = id_remap['routine'].get(wid, wid)
            row_dict['id'] = new_id
            # only insert if this is a novel routine (not dedup'd to Ag)
            existing = tgt.execute('SELECT id FROM routine WHERE id=?', (new_id,)).fetchone()
            if existing is None:
                final_cols = [c for c in routine_dst_cols if c in row_dict]
                tgt.execute(
                    f'INSERT OR IGNORE INTO routine '
                    f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                    f'VALUES ({", ".join(["?"]*len(final_cols))})',
                    tuple(row_dict[c] for c in final_cols)
                )

        # --- routine_def: surviving Warg rows ---
        rd_dst_cols = [c for c in column_names(tgt, 'routine_def')
                       if c not in ('ag_source_id',)]
        rd_src_cols = [c for c in column_names(warg, 'routine_def')
                       if c not in ('ag_source_id',)]
        rd_common = [c for c in rd_dst_cols if c in set(rd_src_cols)]
        rd_common_set = set(rd_common)

        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in rd_common)} FROM routine_def ORDER BY id'
        ):
            row_dict = dict(zip(rd_common, row))
            wid = row_dict['id']
            if wid in dedup_routine_def:
                continue
            new_id = id_remap['routine_def'].get(wid, wid)
            row_dict['id'] = new_id
            # remap FKs
            if 'area_id' in row_dict and row_dict['area_id'] is not None:
                row_dict['area_id'] = area_map.get(row_dict['area_id'], row_dict['area_id'])
            if 'routine_id' in row_dict and row_dict['routine_id'] is not None:
                row_dict['routine_id'] = id_remap['routine'].get(
                    row_dict['routine_id'], row_dict['routine_id'])
            if 'place_id' in row_dict and row_dict['place_id'] is not None:
                row_dict['place_id'] = id_remap['place'].get(
                    row_dict['place_id'], row_dict['place_id'])
            if 'project_id' in row_dict and row_dict['project_id'] is not None:
                row_dict['project_id'] = id_remap['projects'].get(
                    row_dict['project_id'], row_dict['project_id'])
            # owning_agent per-row
            created_by = row_dict.get('created_by', '')
            row_dict['owning_agent'] = _owning_agent_for_row('routine_def', created_by)
            # insert into dst cols that exist
            final_cols = [c for c in rd_dst_cols if c in row_dict]
            vals = tuple(row_dict[c] for c in final_cols)
            tgt.execute(
                f'INSERT OR IGNORE INTO routine_def ({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                f'VALUES ({", ".join(["?"]*len(final_cols))})',
                vals
            )

        # --- workouts: surviving Warg ---
        wo_dst_cols = [c for c in column_names(tgt, 'workouts')
                       if c not in ('ag_source_id', 'kcal')]
        # kcal is GENERATED, omit from INSERT
        wo_src_raw = column_names(warg, 'workouts')
        wo_src_cols = [c for c in wo_src_raw if c not in ('ag_source_id', 'kcal')]
        wo_common = [c for c in wo_dst_cols if c in set(wo_src_cols)]

        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in wo_common)} FROM workouts ORDER BY id'
        ):
            row_dict = dict(zip(wo_common, row))
            wid = row_dict['id']
            if wid in dedup_workout:
                continue
            new_id = id_remap['workouts'].get(wid, wid)
            row_dict['id'] = new_id
            row_dict['owning_agent'] = 'warg'
            if 'area_id' in row_dict and row_dict['area_id'] is not None:
                row_dict['area_id'] = area_map.get(row_dict['area_id'], row_dict['area_id'])
            if 'session_type_id' in row_dict and row_dict['session_type_id'] is not None:
                # session_type dedup: after session_type is loaded, map old->new
                # For now store warg session_type_id; will fix in step 6 post-dedup
                row_dict['session_type_id'] = id_remap['session_type'].get(
                    row_dict['session_type_id'], row_dict['session_type_id'])
            final_cols = [c for c in wo_dst_cols if c in row_dict]
            tgt.execute(
                f'INSERT OR IGNORE INTO workouts '
                f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                f'VALUES ({", ".join(["?"]*len(final_cols))})',
                tuple(row_dict[c] for c in final_cols)
            )

        # --- meals: surviving Warg ---
        ml_dst_cols = [c for c in column_names(tgt, 'meals')
                       if c not in ('ag_source_id', 'kcal')]
        ml_src_cols = [c for c in column_names(warg, 'meals')
                       if c not in ('ag_source_id', 'kcal')]
        ml_common = [c for c in ml_dst_cols if c in set(ml_src_cols)]

        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in ml_common)} FROM meals ORDER BY id'
        ):
            row_dict = dict(zip(ml_common, row))
            wid = row_dict['id']
            if wid in dedup_meal:
                continue
            new_id = id_remap['meals'].get(wid, wid)
            row_dict['id'] = new_id
            row_dict['owning_agent'] = 'warg'
            if 'area_id' in row_dict and row_dict['area_id'] is not None:
                row_dict['area_id'] = area_map.get(row_dict['area_id'], row_dict['area_id'])
            final_cols = [c for c in ml_dst_cols if c in row_dict]
            tgt.execute(
                f'INSERT OR IGNORE INTO meals '
                f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                f'VALUES ({", ".join(["?"]*len(final_cols))})',
                tuple(row_dict[c] for c in final_cols)
            )

        # --- metric_log: (date, metric) dedup, Warg wins on conflict (spec rule 6).
        # Non-conflicting Ag rows are PRESERVED; conflicting Ag rows are REPLACED
        # by Warg values (B1 re-fix).
        #
        # NOTE: migrate step_019 backfills owning_agent='warg' on ALL metric_log
        # rows (Ag copy included), so an owning_agent predicate is USELESS for
        # distinguishing Ag from Warg rows.  We must delete by natural key alone.
        #
        # Strategy:
        #   1. Step 4 already loaded all Ag metric_log rows into target.
        #   2. For each Warg (date, metric) natural key, delete the matching
        #      target row (the Ag row) BY NATURAL KEY -- no owning_agent filter.
        #   3. Insert every Warg row with an offset id.
        #   Result: conflict -> Warg value, non-conflict Ag preserved,
        #   non-conflict Warg added.
        ml_log_dst_cols = [c for c in column_names(tgt, 'metric_log')
                           if c not in ('ag_source_id',)]
        ml_log_src_cols = [c for c in column_names(warg, 'metric_log')]
        ml_log_common = [c for c in ml_log_dst_cols if c in set(ml_log_src_cols)]

        # Collect Warg (date, metric) natural keys
        warg_ml_nk = {(r[0], r[1])
                      for r in warg.execute('SELECT date, metric FROM metric_log')}

        # Delete target (Ag-loaded) rows colliding with Warg keys -- by natural
        # key only (Warg wins; spec rule 6).  UNIQUE(date, metric) guarantees at
        # most one matching row per key.
        ml_deleted = 0
        for date_val, metric_val in warg_ml_nk:
            cur = tgt.execute(
                'DELETE FROM metric_log WHERE date=? AND metric=?',
                (date_val, metric_val)
            )
            ml_deleted += cur.rowcount

        # Insert all Warg rows with offset ids
        warg_ml_inserted = 0
        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in ml_log_common)} FROM metric_log ORDER BY id'
        ):
            row_dict = dict(zip(ml_log_common, row))
            new_id = id_remap['metric_log'].get(row_dict['id'], row_dict['id'])
            row_dict['id'] = new_id
            row_dict['owning_agent'] = 'warg'
            final_cols = [c for c in ml_log_dst_cols if c in row_dict]
            # INSERT (not OR IGNORE): after natural-key delete, no collision can
            # remain; a silent IGNORE here would mask a bug, so let it raise.
            tgt.execute(
                f'INSERT INTO metric_log '
                f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                f'VALUES ({", ".join(["?"]*len(final_cols))})',
                tuple(row_dict[c] for c in final_cols)
            )
            warg_ml_inserted += 1

        total_ml = tgt.execute('SELECT count(*) FROM metric_log').fetchone()[0]
        log.info('[8] metric_log: %d Ag-collision rows deleted, %d Warg inserted, '
                 'total merged=%d (Ag non-colliding rows preserved)',
                 ml_deleted, warg_ml_inserted, total_ml)

        # --- timeblock: Warg events ---
        ev_dst_cols = [c for c in column_names(tgt, 'timeblock')
                       if c not in ('ag_source_id',)]
        ev_src_cols = [c for c in column_names(warg, 'timeblock')
                       if c not in ('ag_source_id',)]
        ev_common = [c for c in ev_dst_cols if c in set(ev_src_cols)]

        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in ev_common)} FROM timeblock ORDER BY id'
        ):
            row_dict = dict(zip(ev_common, row))
            wid = row_dict['id']
            new_id = id_remap['timeblock'].get(wid, wid)
            row_dict['id'] = new_id
            # owning_agent: preserve verbatim (spec rule 9)
            if 'area_id' in row_dict and row_dict['area_id'] is not None:
                row_dict['area_id'] = area_map.get(row_dict['area_id'], row_dict['area_id'])
            if 'place_id' in row_dict and row_dict['place_id'] is not None:
                row_dict['place_id'] = id_remap['place'].get(
                    row_dict['place_id'], row_dict['place_id'])
            final_cols = [c for c in ev_dst_cols if c in row_dict]
            tgt.execute(
                f'INSERT OR IGNORE INTO timeblock '
                f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                f'VALUES ({", ".join(["?"]*len(final_cols))})',
                tuple(row_dict[c] for c in final_cols)
            )

        # --- sub_event: Warg ---
        se_dst_cols = [c for c in column_names(tgt, 'sub_event')]
        se_src_cols = [c for c in column_names(warg, 'sub_event')]
        se_common = [c for c in se_dst_cols if c in set(se_src_cols)]
        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in se_common)} FROM sub_event ORDER BY id'
        ):
            row_dict = dict(zip(se_common, row))
            wid = row_dict['id']
            new_id = id_remap['sub_event'].get(wid, wid)
            row_dict['id'] = new_id
            if 'event_id' in row_dict and row_dict['event_id'] is not None:
                row_dict['event_id'] = id_remap['timeblock'].get(
                    row_dict['event_id'], row_dict['event_id'])
            final_cols = [c for c in se_dst_cols if c in row_dict]
            tgt.execute(
                f'INSERT OR IGNORE INTO sub_event '
                f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                f'VALUES ({", ".join(["?"]*len(final_cols))})',
                tuple(row_dict[c] for c in final_cols)
            )

        # --- place: Warg-only places (those not mapped to Ag) ---
        pl_dst_cols = [c for c in column_names(tgt, 'place')]
        pl_src_cols = [c for c in column_names(warg, 'place')]
        pl_common = [c for c in pl_dst_cols if c in set(pl_src_cols)]
        warg_place_map = self.maps['warg_place_map']
        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in pl_common)} FROM place ORDER BY id'
        ):
            row_dict = dict(zip(pl_common, row))
            wid = row_dict['id']
            # If mapped to Ag, skip (Ag already has this place)
            if wid in warg_place_map:
                continue
            new_id = id_remap['place'].get(wid, wid)
            row_dict['id'] = new_id
            final_cols = [c for c in pl_dst_cols if c in row_dict]
            tgt.execute(
                f'INSERT OR IGNORE INTO place '
                f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                f'VALUES ({", ".join(["?"]*len(final_cols))})',
                tuple(row_dict[c] for c in final_cols)
            )

        # --- step 6: REBUILD session_type (dedup by remapped routine_id, code) ---
        log.info('[6] REBUILD session_type dedup')
        # Load Warg session_type rows (only novel ones after routine dedup)
        # Key: (remapped_routine_id, code) -> if exists in Ag-loaded target, skip
        ag_st_set = {(r[0], r[1]) for r in tgt.execute(
            'SELECT routine_id, code FROM session_type'
        ).fetchall()}
        # Will store final session_type remap: warg_st_id -> merged_st_id
        st_final_remap = {}
        st_dst_cols = [c for c in column_names(tgt, 'session_type')]
        st_src_cols = [c for c in column_names(warg, 'session_type')]
        st_common = [c for c in st_dst_cols if c in set(st_src_cols)]

        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in st_common)} FROM session_type ORDER BY id'
        ):
            row_dict = dict(zip(st_common, row))
            wid = row_dict['id']
            remapped_routine_id = id_remap['routine'].get(
                row_dict['routine_id'], row_dict['routine_id'])
            code = row_dict['code']
            # Check if this (remapped_routine_id, code) pair already in target
            existing_st = tgt.execute(
                'SELECT id FROM session_type WHERE routine_id=? AND code=?',
                (remapped_routine_id, code)
            ).fetchone()
            if existing_st:
                # dedup: map warg id to existing Ag id
                st_final_remap[wid] = existing_st[0]
            else:
                new_id = id_remap['session_type'].get(wid, wid)
                row_dict['id'] = new_id
                row_dict['routine_id'] = remapped_routine_id
                final_cols = [c for c in st_dst_cols if c in row_dict]
                tgt.execute(
                    f'INSERT OR IGNORE INTO session_type '
                    f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                    f'VALUES ({", ".join(["?"]*len(final_cols))})',
                    tuple(row_dict[c] for c in final_cols)
                )
                st_final_remap[wid] = new_id

        self.id_remap['session_type_final'] = st_final_remap
        log.info('[6] session_type: %d warg rows processed, st_final_remap size=%d',
                 warg.execute('SELECT count(*) FROM session_type').fetchone()[0],
                 len(st_final_remap))

        # Fix workouts.session_type_id for Warg surviving workouts
        # (They were stored with offset st ids; now update to st_final_remap)
        # session_type offsets were stored in id_remap['session_type']
        # but st_final_remap gives the real final ids
        for wid, new_st_id in id_remap['session_type'].items():
            final_st_id = st_final_remap.get(wid, new_st_id)
            if final_st_id != new_st_id:
                # update any workouts that got the offset st id
                tgt.execute(
                    'UPDATE workouts SET session_type_id=? WHERE session_type_id=?',
                    (final_st_id, new_st_id)
                )

        # --- step 6b: REBUILD session_muscle ---
        log.info('[6] REBUILD session_muscle')
        sm_dst_cols = [c for c in column_names(tgt, 'session_muscle')]
        sm_src_cols = [c for c in column_names(warg, 'session_muscle')]
        sm_common = [c for c in sm_dst_cols if c in set(sm_src_cols)]

        for row in warg.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in sm_common)} FROM session_muscle ORDER BY session_type_id'
        ):
            row_dict = dict(zip(sm_common, row))
            new_st_id = st_final_remap.get(
                row_dict['session_type_id'], row_dict['session_type_id'])
            new_type_id = type_id_map.get(
                row_dict['type_id'], row_dict['type_id'])
            row_dict['session_type_id'] = new_st_id
            row_dict['type_id'] = new_type_id
            final_cols = [c for c in sm_dst_cols if c in row_dict]
            tgt.execute(
                f'INSERT OR IGNORE INTO session_muscle '
                f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                f'VALUES ({", ".join(["?"]*len(final_cols))})',
                tuple(row_dict[c] for c in final_cols)
            )

        # --- step 6c: REBUILD workout_classifications ---
        log.info('[6] REBUILD workout_classifications')
        # Three cases:
        # (a) Surviving Warg workouts: remap workout_id (offset) + type_id (natural key map)
        # (b) Mirror Warg workouts: remap workout_id -> ag_source_id; type_id natural key map
        #     Insert only if (ag_workout_id, mapped_type_id) not already in target
        # (c) Ag WC: already loaded in step 4

        warg_wc_rows = warg.execute(
            'SELECT workout_id, type_id FROM workout_classifications ORDER BY workout_id'
        ).fetchall()

        for wc_woid, wc_tid in warg_wc_rows:
            new_tid = type_id_map.get(wc_tid, wc_tid)

            if wc_woid in dedup_workout:
                # (b) mirror: remap to ag_source_id
                new_woid = dup_wc_ag_id[wc_woid]
            else:
                # (a) surviving: offset remap
                new_woid = id_remap['workouts'].get(wc_woid, wc_woid)

            tgt.execute(
                'INSERT OR IGNORE INTO workout_classifications (workout_id, type_id) '
                'VALUES (?,?)',
                (new_woid, new_tid)
            )

        # --- step 7: WARG-ONLY tables ---
        log.info('[7] WARG-ONLY tables')

        # ledger_goal (has self-referential parent_id and resume_to_id)
        if table_exists(warg, 'ledger_goal'):
            lg_dst_cols = [c for c in column_names(tgt, 'ledger_goal')]
            lg_src_cols = [c for c in column_names(warg, 'ledger_goal')]
            lg_common = [c for c in lg_dst_cols if c in set(lg_src_cols)]
            lg_offset = self.maps['ag_max_ids'].get('ledger_goal', 0) + 10000
            lg_remap = {r[0]: r[0] + lg_offset
                        for r in warg.execute('SELECT id FROM ledger_goal')}
            for row in warg.execute(
                f'SELECT {", ".join(chr(34)+c+chr(34) for c in lg_common)} FROM ledger_goal ORDER BY id'
            ):
                row_dict = dict(zip(lg_common, row))
                row_dict['id'] = lg_remap.get(row_dict['id'], row_dict['id'])
                if row_dict.get('parent_id') is not None:
                    row_dict['parent_id'] = lg_remap.get(row_dict['parent_id'],
                                                          row_dict['parent_id'])
                if row_dict.get('resume_to_id') is not None:
                    row_dict['resume_to_id'] = lg_remap.get(row_dict['resume_to_id'],
                                                             row_dict['resume_to_id'])
                final_cols = [c for c in lg_dst_cols if c in row_dict]
                tgt.execute(
                    f'INSERT OR IGNORE INTO ledger_goal '
                    f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                    f'VALUES ({", ".join(["?"]*len(final_cols))})',
                    tuple(row_dict[c] for c in final_cols)
                )
            self.id_remap['ledger_goal'] = lg_remap

        # ledger_constraint (has goal_id FK)
        if table_exists(warg, 'ledger_constraint'):
            lc_dst_cols = [c for c in column_names(tgt, 'ledger_constraint')]
            lc_src_cols = [c for c in column_names(warg, 'ledger_constraint')]
            lc_common = [c for c in lc_dst_cols if c in set(lc_src_cols)]
            lc_offset = self.maps['ag_max_ids'].get('ledger_constraint', 0) + 10000
            lg_remap = self.id_remap.get('ledger_goal', {})
            for row in warg.execute(
                f'SELECT {", ".join(chr(34)+c+chr(34) for c in lc_common)} FROM ledger_constraint ORDER BY id'
            ):
                row_dict = dict(zip(lc_common, row))
                row_dict['id'] = row_dict['id'] + lc_offset
                if row_dict.get('goal_id') is not None:
                    row_dict['goal_id'] = lg_remap.get(row_dict['goal_id'],
                                                        row_dict['goal_id'])
                final_cols = [c for c in lc_dst_cols if c in row_dict]
                tgt.execute(
                    f'INSERT OR IGNORE INTO ledger_constraint '
                    f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                    f'VALUES ({", ".join(["?"]*len(final_cols))})',
                    tuple(row_dict[c] for c in final_cols)
                )

        # Simple warg-only tables (no FK remap needed)
        simple_warg_only = [
            'ledger_preference', 'ledger_resource', 'ledger_audit',
            'exercise_movement_grip', 'movement_muscle_base', 'exercise_muscle',
            'exercise_subregion', 'grip_muscle_delta', 'grip_vocab',
            'exercise_fatigue_cost', 'exercise_pattern',
        ]
        for table in simple_warg_only:
            if not table_exists(warg, table):
                continue
            if not table_exists(tgt, table):
                continue
            dst_cols = column_names(tgt, table)
            src_cols = column_names(warg, table)
            common = [c for c in dst_cols if c in set(src_cols)]
            rows = warg.execute(
                f'SELECT {", ".join(chr(34)+c+chr(34) for c in common)} FROM "{table}"'
            ).fetchall()
            for row in rows:
                tgt.execute(
                    f'INSERT OR IGNORE INTO "{table}" '
                    f'({", ".join(chr(34)+c+chr(34) for c in common)}) '
                    f'VALUES ({", ".join(["?"]*len(common))})',
                    row
                )
            log.debug('[7] %s: %d rows', table, len(rows))

        # workout_sets: remap session_id (workout offset) + straight offset for set id
        if table_exists(warg, 'workout_sets'):
            ws_dst_cols = [c for c in column_names(tgt, 'workout_sets')]
            ws_src_cols = [c for c in column_names(warg, 'workout_sets')]
            ws_common = [c for c in ws_dst_cols if c in set(ws_src_cols)]
            ws_offset = self.maps['ag_max_ids'].get('workout_sets', 0) + 10000
            for row in warg.execute(
                f'SELECT {", ".join(chr(34)+c+chr(34) for c in ws_common)} FROM workout_sets ORDER BY id'
            ):
                row_dict = dict(zip(ws_common, row))
                row_dict['id'] = row_dict['id'] + ws_offset
                # session_id = Warg workout id -> remapped surviving workout id
                if row_dict['session_id'] in dedup_workout:
                    # workout_sets on mirror workouts should be 0 (verified); skip
                    log.warning('[7] workout_set refs dedup workout %d -- skipping',
                                row_dict['session_id'])
                    continue
                row_dict['session_id'] = id_remap['workouts'].get(
                    row_dict['session_id'], row_dict['session_id'])
                final_cols = [c for c in ws_dst_cols if c in row_dict]
                tgt.execute(
                    f'INSERT OR IGNORE INTO workout_sets '
                    f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                    f'VALUES ({", ".join(["?"]*len(final_cols))})',
                    tuple(row_dict[c] for c in final_cols)
                )
            log.info('[7] workout_sets: %d rows',
                     warg.execute('SELECT count(*) FROM workout_sets').fetchone()[0])

        # roller_log: SHARED table (Ag rows already in target from step 4).
        # Warg's 173 rows are NON-mirror (no ag_source_id pattern) -- include via
        # offset id-remap.  recurrence_id is a ULID string, naturally disjoint
        # across agents, no remap needed.  No other FKs.  (B2 fix)
        if table_exists(warg, 'roller_log') and table_exists(tgt, 'roller_log'):
            rl_dst_cols = [c for c in column_names(tgt, 'roller_log')]
            rl_src_cols = [c for c in column_names(warg, 'roller_log')]
            rl_common = [c for c in rl_dst_cols if c in set(rl_src_cols)]
            rl_offset = self.maps['ag_max_ids'].get('roller_log', 0) + 10000
            rl_inserted = 0
            for row in warg.execute(
                f'SELECT {", ".join(chr(34)+c+chr(34) for c in rl_common)} FROM roller_log ORDER BY id'
            ):
                row_dict = dict(zip(rl_common, row))
                row_dict['id'] = row_dict['id'] + rl_offset
                final_cols = [c for c in rl_dst_cols if c in row_dict]
                tgt.execute(
                    f'INSERT OR IGNORE INTO roller_log '
                    f'({", ".join(chr(34)+c+chr(34) for c in final_cols)}) '
                    f'VALUES ({", ".join(["?"]*len(final_cols))})',
                    tuple(row_dict[c] for c in final_cols)
                )
                rl_inserted += 1
            log.info('[7] roller_log: %d Warg rows inserted (offset=%d)',
                     rl_inserted, rl_offset)

        tgt.commit()
        tgt.execute('PRAGMA foreign_keys = ON')
        warg.close()
        tgt.close()
        log.info('[5-8] Warg data load complete')

    def _step_config_merge(self):
        """Step 9: merge config (key union, max updated_at wins, tie-break health->Warg)."""
        log.info('[9] CONFIG MERGE')
        ag = sqlite3.connect(self.ag_copy)
        warg = sqlite3.connect(self.warg_copy)
        tgt = open_rw(self.target_path)

        # Collect all keys from both
        ag_cfg = {r[0]: (r[1], r[2])
                  for r in ag.execute('SELECT key, value, updated_at FROM config')}
        warg_cfg = {r[0]: (r[1], r[2])
                    for r in warg.execute('SELECT key, value, updated_at FROM config')}

        # Delete what step 4 loaded (Ag config)
        tgt.execute('DELETE FROM config')

        all_keys = set(ag_cfg) | set(warg_cfg)
        for key in all_keys:
            if key in ag_cfg and key not in warg_cfg:
                val, uat = ag_cfg[key]
            elif key in warg_cfg and key not in ag_cfg:
                val, uat = warg_cfg[key]
            else:
                # conflict: max updated_at wins
                av, aat = ag_cfg[key]
                wv, wat = warg_cfg[key]
                if (wat or '') > (aat or ''):
                    val, uat = wv, wat
                elif (aat or '') > (wat or ''):
                    val, uat = av, aat
                else:
                    # tie: health keys -> Warg, else Ag
                    if key in self.health_tie_break_keys:
                        val, uat = wv, wat
                    else:
                        val, uat = av, aat

            tgt.execute(
                'INSERT INTO config (key, value, updated_at) VALUES (?,?,?)',
                (key, val, uat)
            )

        tgt.commit()
        n = tgt.execute('SELECT count(*) FROM config').fetchone()[0]
        log.info('[9] config: %d keys merged', n)
        ag.close()
        warg.close()
        tgt.close()

    def _step_drop_ag_source_id(self):
        """Step 11: drop ag_source_id column from workouts, meals, routine_def
        if present in target (should not be -- schema.sql v19 excludes it,
        but guard for safety)."""
        log.info('[11] DROP ag_source_id (schema.sql v19 already excludes it)')
        tgt = sqlite3.connect(self.target_path)
        for table in ('workouts', 'meals', 'routine_def'):
            if table_exists(tgt, table):
                cols = column_names(tgt, table)
                if 'ag_source_id' in cols:
                    log.warning('[11] ag_source_id found in target.%s -- dropping', table)
                    # SQLite 3.35+ supports DROP COLUMN
                    try:
                        tgt.execute(f'ALTER TABLE "{table}" DROP COLUMN ag_source_id')
                        tgt.commit()
                        log.info('[11] dropped ag_source_id from %s', table)
                    except Exception as e:
                        log.error('[11] could not drop ag_source_id from %s: %s', table, e)
        tgt.close()
        log.info('[11] ag_source_id check complete')

    def _step_verify(self) -> dict:
        """Step 12: comprehensive VERIFY.

        Returns dict with ok=True/False and details.
        """
        log.info('[12] VERIFY')
        ag = sqlite3.connect(self.ag_copy)
        warg = sqlite3.connect(self.warg_copy)
        tgt = sqlite3.connect(self.target_path)
        tgt.execute('PRAGMA foreign_keys = ON')

        failures = []
        counts = {}

        # (a) table-level row count checks -- all DYNAMIC, no hardcoded instance literals.
        # B5 fix: expected counts are derived from live source measurements at
        # verify-time, not hardcoded.  This ensures new records added between
        # the measurement snapshot and cutover do not cause false VERIFY failures.

        def check_count(table, ag_total, warg_survivor, label=None):
            if table_exists(tgt, table):
                actual = table_count(tgt, table)
                counts[table] = actual
                expected_n = ag_total + warg_survivor
                if actual != expected_n:
                    failures.append(
                        f'{table}: expected {expected_n} (ag={ag_total}+'
                        f'warg={warg_survivor}), got {actual}'
                    )
                    log.error('[12] FAIL: %s = %d != %d', table, actual, expected_n)
                else:
                    log.info('[12] OK: %s = %d (ag=%d + warg=%d)',
                             table, actual, ag_total, warg_survivor)

        # --- dynamic single-source counts (Ag canonical, Warg excluded/zero) ---

        # areas: Ag canonical (Warg areas mapped to Ag ids, no new area rows)
        ag_areas_n = table_count(ag, 'areas') if table_exists(ag, 'areas') else 0
        check_count('areas', ag_areas_n, 0, 'areas')

        # spend_category: Ag 17 canonical, Warg excluded
        ag_sc_n = table_count(ag, 'spend_category') if table_exists(ag, 'spend_category') else 0
        check_count('spend_category', ag_sc_n, 0, 'spend_category')

        # workout_types: Ag all + Warg-only new types (not shared by natural key)
        ag_wt_n = table_count(ag, 'workout_types') if table_exists(ag, 'workout_types') else 0
        warg_wt_new_n = len(self.maps['warg_type_new'])
        check_count('workout_types', ag_wt_n, warg_wt_new_n, 'workout_types')

        # workout_sets: Warg-only (Ag has 0)
        ag_ws_n = table_count(ag, 'workout_sets') if table_exists(ag, 'workout_sets') else 0
        warg_ws_n = table_count(warg, 'workout_sets') if table_exists(warg, 'workout_sets') else 0
        check_count('workout_sets', ag_ws_n, warg_ws_n, 'workout_sets')

        # roller_log: shared table -- Ag all + Warg all (no dedup, ULID-keyed)
        ag_rl_n = table_count(ag, 'roller_log') if table_exists(ag, 'roller_log') else 0
        warg_rl_n = table_count(warg, 'roller_log') if table_exists(warg, 'roller_log') else 0
        check_count('roller_log', ag_rl_n, warg_rl_n, 'roller_log')

        # metric_log: Ag non-colliding + Warg all.
        # Collision set = Warg natural keys (date, metric).
        # Ag survivors = Ag rows whose (date,metric) NOT in Warg nk set.
        if table_exists(ag, 'metric_log') and table_exists(warg, 'metric_log'):
            warg_ml_nk_count = warg.execute('SELECT count(*) FROM metric_log').fetchone()[0]
            ag_ml_all = table_count(ag, 'metric_log')
            # Count Ag rows that collide with ANY Warg (date, metric) key.
            warg_nk_rows = warg.execute(
                'SELECT date, metric FROM metric_log'
            ).fetchall()
            warg_nk_set = set(warg_nk_rows)
            if warg_nk_rows:
                placeholders = ','.join(['(?,?)'] * len(warg_nk_rows))
                flat_vals = [v for pair in warg_nk_rows for v in pair]
                ag_ml_collide = ag.execute(
                    f'SELECT count(*) FROM metric_log WHERE (date, metric) IN ({placeholders})',
                    flat_vals
                ).fetchone()[0]
            else:
                ag_ml_collide = 0
            ag_ml_survivors = ag_ml_all - ag_ml_collide
            expected_ml = ag_ml_survivors + warg_ml_nk_count
            if table_exists(tgt, 'metric_log'):
                actual_ml = table_count(tgt, 'metric_log')
                counts['metric_log'] = actual_ml
                if actual_ml != expected_ml:
                    failures.append(
                        f'metric_log: expected {expected_ml} '
                        f'(ag_survivors={ag_ml_survivors}+warg={warg_ml_nk_count}), '
                        f'got {actual_ml}'
                    )
                    log.error('[12] FAIL: metric_log = %d != %d', actual_ml, expected_ml)
                else:
                    log.info('[12] OK: metric_log = %d (ag_survivors=%d + warg=%d)',
                             actual_ml, ag_ml_survivors, warg_ml_nk_count)

                # VALUE-LEVEL gate (B1): for every conflicting (date, metric),
                # the merged value MUST equal the Warg value (Warg wins rule 6).
                # This catches the failure mode where Ag's stale value survives.
                warg_ml_values = {
                    (r[0], r[1]): r[2]
                    for r in warg.execute('SELECT date, metric, value FROM metric_log')
                }
                tgt_ml_values = {
                    (r[0], r[1]): r[2]
                    for r in tgt.execute('SELECT date, metric, value FROM metric_log')
                }
                ml_value_failures = 0
                for nk in warg_nk_set:
                    warg_val = warg_ml_values.get(nk)
                    tgt_val = tgt_ml_values.get(nk)
                    if tgt_val is None:
                        failures.append(
                            f'metric_log value gate: conflict key {nk} '
                            f'missing from target'
                        )
                        ml_value_failures += 1
                    elif tgt_val != warg_val:
                        failures.append(
                            f'metric_log value gate: key {nk} merged value '
                            f'{tgt_val} != Warg value {warg_val} (Warg must win)'
                        )
                        ml_value_failures += 1
                if ml_value_failures == 0:
                    log.info('[12] OK: metric_log value gate (%d conflict keys, '
                             'all = Warg value)', len(warg_nk_set))
                else:
                    log.error('[12] FAIL: %d metric_log value mismatches',
                              ml_value_failures)

        # meals: Ag all + Warg survivors (non-mirror)
        check_count('meals', table_count(ag, 'meals'),
                    table_count(warg, 'meals') - len(self.maps['dedup_meal']))
        # workouts: Ag all + Warg survivors
        check_count('workouts', table_count(ag, 'workouts'),
                    table_count(warg, 'workouts') - len(self.maps['dedup_workout']))
        # routine_def: Ag 91 + Warg 8
        check_count('routine_def', table_count(ag, 'routine_def'),
                    table_count(warg, 'routine_def') - len(self.maps['dedup_routine_def']))
        # timeblock
        check_count('timeblock', table_count(ag, 'timeblock'), table_count(warg, 'timeblock'))
        # config
        ag_cfg_keys = {r[0] for r in ag.execute('SELECT key FROM config')}
        warg_cfg_keys = {r[0] for r in warg.execute('SELECT key FROM config')}
        expected_cfg = len(ag_cfg_keys | warg_cfg_keys)
        actual_cfg = table_count(tgt, 'config')
        counts['config'] = actual_cfg
        if actual_cfg != expected_cfg:
            failures.append(f'config: expected {expected_cfg}, got {actual_cfg}')
            log.error('[12] FAIL: config = %d != %d', actual_cfg, expected_cfg)
        else:
            log.info('[12] OK: config = %d', actual_cfg)

        # (a2) N2: EXHAUSTIVE per-table count gate.
        # Every remaining shared/warg-only table gets a dynamic count check so
        # that INSERT OR IGNORE silent drops (schema/FK collisions swallowed) are
        # caught.  expected = source measurement - dedup set size.
        _ac = lambda t: table_count(ag, t) if table_exists(ag, t) else 0
        _wc = lambda t: table_count(warg, t) if table_exists(warg, t) else 0

        # place: Ag all + Warg-only (those NOT mapped to an Ag place)
        warg_place_mapped = len(self.maps['warg_place_map'])
        check_count('place', _ac('place'), _wc('place') - warg_place_mapped)

        # sub_event: Ag all + Warg all (no dedup)
        check_count('sub_event', _ac('sub_event'), _wc('sub_event'))

        # routine: Ag all + Warg-novel (same-name deduped to Ag).
        # Warg-novel = Warg routine ids whose remap target is NOT an existing Ag id.
        ag_routine_ids = {r[0] for r in ag.execute('SELECT id FROM routine')} \
            if table_exists(ag, 'routine') else set()
        warg_routine_novel = sum(
            1 for new_id in self.id_remap.get('routine', {}).values()
            if new_id not in ag_routine_ids
        )
        check_count('routine', _ac('routine'), warg_routine_novel)

        # session_type: Ag all + Warg-novel (deduped by (routine_id, code)).
        # st_final_remap maps every Warg st id -> merged id; novel = those NOT
        # collapsed onto an existing Ag session_type id.
        st_final = self.id_remap.get('session_type_final', {})
        ag_st_ids = {r[0] for r in ag.execute('SELECT id FROM session_type')} \
            if table_exists(ag, 'session_type') else set()
        warg_st_novel = sum(1 for new_id in st_final.values() if new_id not in ag_st_ids)
        check_count('session_type', _ac('session_type'), warg_st_novel)

        # workout_classifications: join table (no id).  Expected = distinct union
        # of Ag WC rows + Warg WC rows after workout/type remap and dedup.
        # Compute expected as size of the deduped set built from both sources.
        if table_exists(tgt, 'workout_classifications'):
            expected_wc = set()
            for woid, tid in ag.execute(
                'SELECT workout_id, type_id FROM workout_classifications'
            ) if table_exists(ag, 'workout_classifications') else []:
                expected_wc.add((woid, tid))
            dedup_workout = self.maps['dedup_workout']
            dup_wc_ag_id = self.maps['dup_wc_ag_id']
            type_id_map_local = self.maps['type_id_map']
            for woid, tid in warg.execute(
                'SELECT workout_id, type_id FROM workout_classifications'
            ) if table_exists(warg, 'workout_classifications') else []:
                new_tid = type_id_map_local.get(tid, tid)
                if woid in dedup_workout:
                    new_woid = dup_wc_ag_id.get(woid)
                else:
                    new_woid = self.id_remap.get('workouts', {}).get(woid, woid)
                expected_wc.add((new_woid, new_tid))
            actual_wc = table_count(tgt, 'workout_classifications')
            counts['workout_classifications'] = actual_wc
            if actual_wc != len(expected_wc):
                failures.append(
                    f'workout_classifications: expected {len(expected_wc)} '
                    f'(deduped union), got {actual_wc}'
                )
                log.error('[12] FAIL: workout_classifications = %d != %d',
                          actual_wc, len(expected_wc))
            else:
                log.info('[12] OK: workout_classifications = %d', actual_wc)

        # session_muscle: join table PK(session_type_id, type_id).  Expected =
        # deduped union of Ag rows + Warg rows after st/type remap.
        if table_exists(tgt, 'session_muscle'):
            expected_sm = set()
            for stid, tid in ag.execute(
                'SELECT session_type_id, type_id FROM session_muscle'
            ) if table_exists(ag, 'session_muscle') else []:
                expected_sm.add((stid, tid))
            type_id_map_local = self.maps['type_id_map']
            for stid, tid in warg.execute(
                'SELECT session_type_id, type_id FROM session_muscle'
            ) if table_exists(warg, 'session_muscle') else []:
                new_st = st_final.get(stid, stid)
                new_tid = type_id_map_local.get(tid, tid)
                expected_sm.add((new_st, new_tid))
            actual_sm = table_count(tgt, 'session_muscle')
            counts['session_muscle'] = actual_sm
            if actual_sm != len(expected_sm):
                failures.append(
                    f'session_muscle: expected {len(expected_sm)} '
                    f'(deduped union), got {actual_sm}'
                )
                log.error('[12] FAIL: session_muscle = %d != %d',
                          actual_sm, len(expected_sm))
            else:
                log.info('[12] OK: session_muscle = %d', actual_sm)

        # Ag-domain single-source tables (Warg has 0 rows -- verified in handoff)
        for t in ('tasks', 'appointments', 'persons'):
            check_count(t, _ac(t), _wc(t))

        # Warg-origin reference/ledger tables.  Two distinct cases:
        #  - id-PK tables (ledger_*): offset-remapped, so merged = ag + warg.
        #  - natural-key-PK tables (exercise_*, movement_*, grip_*): migrate
        #    step_019 SEEDS these on BOTH copies with identical rows, and the
        #    engine loads Ag first then Warg via INSERT OR IGNORE.  The correct
        #    expected is the DISTINCT UNION over the natural-key PK, not ag+warg.
        def _pk_cols(conn, table):
            return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')
                    if r[5] > 0]

        def _pk_union_size(table):
            """distinct union of natural-key PK tuples across Ag + Warg copies."""
            keys = set()
            for src in (ag, warg):
                if not table_exists(src, table):
                    continue
                pk = _pk_cols(src, table)
                if not pk:
                    return None  # cannot compute; fall back to caller
                col_sql = ', '.join(f'"{c}"' for c in pk)
                for row in src.execute(f'SELECT {col_sql} FROM "{table}"'):
                    keys.add(tuple(row))
            return len(keys)

        for t in WARG_ONLY_TABLES:
            if t in ('workout_sets',):
                continue  # already checked above
            if not table_exists(tgt, t):
                continue
            pk = _pk_cols(warg, t) if table_exists(warg, t) else _pk_cols(ag, t)
            is_int_id_pk = (pk == ['id'])
            if is_int_id_pk:
                # offset-remapped -> straight ag + warg
                check_count(t, _ac(t), _wc(t))
            else:
                # natural-key seed table -> distinct union
                expected_u = _pk_union_size(t)
                actual_u = table_count(tgt, t)
                counts[t] = actual_u
                if expected_u is not None and actual_u != expected_u:
                    failures.append(
                        f'{t}: expected {expected_u} (distinct pk union), got {actual_u}'
                    )
                    log.error('[12] FAIL: %s = %d != %d (pk union)',
                              t, actual_u, expected_u)
                else:
                    log.info('[12] OK: %s = %d (pk union)', t, actual_u)

        # (a3) UMBRELLA count gate (grill option 2, N4): fail-closed on any
        # target table that is NEITHER explicitly loaded/gated above NOR a seed
        # table, yet has Warg source rows.  Such a table would silently lose its
        # Warg data (the pipeline never carries it), so abort.
        #
        # Covered set is derived DYNAMICALLY from `counts` keys (every explicit
        # gate above records its table there) -- new tables auto-fall under the
        # umbrella without editing a hardcoded list.
        #
        # Seed exception: natural-key-PK tables that migrate seeds IDENTICALLY on
        # both copies (Ag rows == Warg rows == the seed set) are not "Warg data";
        # excluding them prevents false positives.  The exception is explicit and
        # only applies when the seed criterion actually holds.
        covered = {k for k in counts.keys()
                   if k not in ('fk_violations', 'integrity')}

        def _is_identical_seed(table):
            """True iff table has a natural-key PK and Ag/Warg hold the identical
            row set (i.e. migrate-seeded reference data, not per-agent data)."""
            if not (table_exists(ag, table) and table_exists(warg, table)):
                return False
            pk = [r[1] for r in warg.execute(f'PRAGMA table_info("{table}")') if r[5] > 0]
            if not pk or pk == ['id']:
                return False  # id-PK is per-agent data, never a shared seed
            col_sql = ', '.join(f'"{c}"' for c in pk)
            ag_keys = {tuple(r) for r in ag.execute(f'SELECT {col_sql} FROM "{table}"')}
            warg_keys = {tuple(r) for r in warg.execute(f'SELECT {col_sql} FROM "{table}"')}
            return len(warg_keys) > 0 and ag_keys == warg_keys

        tgt_tables = {r[0] for r in tgt.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in sorted(tgt_tables):
            if t.startswith('sqlite_'):
                continue
            if t in covered:
                continue
            warg_rows = _wc(t)
            if warg_rows == 0:
                continue  # nothing to lose from Warg
            if _is_identical_seed(t):
                log.info('[12] umbrella: %s ungated but identical seed (ag==warg) '
                         '-- exempt', t)
                continue
            # Ungated table with real Warg data -> fail-closed
            failures.append(
                f'umbrella gate: table {t!r} has {warg_rows} Warg source rows but '
                f'is neither explicitly merged nor count-gated -- potential silent loss'
            )
            log.error('[12] FAIL umbrella: %s has %d ungated Warg rows', t, warg_rows)

        # (b) foreign_key_check
        fk_result = tgt.execute('PRAGMA foreign_key_check').fetchall()
        counts['fk_violations'] = len(fk_result)
        if fk_result:
            failures.append(f'foreign_key_check: {len(fk_result)} violations: {fk_result[:5]}')
            log.error('[12] FAIL: foreign_key_check %d violations', len(fk_result))
        else:
            log.info('[12] OK: foreign_key_check = 0')

        # (c) integrity_check
        ic = tgt.execute('PRAGMA integrity_check').fetchone()[0]
        counts['integrity'] = ic
        if ic != 'ok':
            failures.append(f'integrity_check: {ic}')
            log.error('[12] FAIL: integrity_check = %s', ic)
        else:
            log.info('[12] OK: integrity_check = ok')

        # (d) natural-key exhaustive check -- B3 re-fix.
        #
        # Correct method: build INVERTED id_remap (target_id -> Warg source_id)
        # per table, JOIN each Warg-origin surviving row back to its ORIGINAL Warg
        # row, and compare the target's FK against the deterministic expected value
        # computed FROM THE SOURCE.  Never re-derive the expected value from the
        # (possibly corrupted) target FK itself -- that is the circular bug that
        # let injected cross-references pass.
        #
        # area_id gate: target.area_id MUST equal area_map[warg_source.area_id]
        #               (unmapped source area_id -> that is itself a FAIL, not skip).
        # type_id gate: target FK's (category, subtype) MUST equal the SOURCE
        #               Warg type's (category, subtype).
        # Any mismatch -> VERIFY RED.
        log.info('[12] natural-key exhaustive check (B3: inverted-remap per-row)')

        type_id_map = self.maps['type_id_map']
        area_map = self.maps['area_map']
        id_remap = self.id_remap

        # target workout_types id -> (category, subtype)
        tgt_type_by_id = {r[0]: (r[1], r[2])
                          for r in tgt.execute(
                              'SELECT id, category, subtype FROM workout_types'
                          ).fetchall()}
        # Warg source area_id -> Warg source area name
        warg_area_names = {r[0]: r[1] for r in warg.execute('SELECT id, name FROM areas')}
        # target area_id -> name (for logging only, NOT for deriving expectations)
        tgt_area_names  = {r[0]: r[1] for r in tgt.execute('SELECT id, name FROM areas')}

        def _inverted(table):
            """target_id -> warg_source_id for a remapped table."""
            fwd = id_remap.get(table, {})
            return {new: old for old, new in fwd.items()}

        # ---- area_id gate (workouts, meals, timeblock, routine_def) ----
        area_row_failures = 0
        for tbl in ('workouts', 'meals', 'timeblock', 'routine_def'):
            if not table_exists(tgt, tbl) or not table_exists(warg, tbl):
                continue
            if 'area_id' not in column_names(tgt, tbl):
                continue
            inv = _inverted(tbl)
            if not inv:
                continue
            # Warg source area_id per source row id
            warg_src_area = {
                r[0]: r[1]
                for r in warg.execute(f'SELECT id, area_id FROM "{tbl}"')
            }
            for (tgt_id, tgt_area_id) in tgt.execute(
                f'SELECT id, area_id FROM "{tbl}"'
            ).fetchall():
                src_id = inv.get(tgt_id)
                if src_id is None:
                    continue  # Ag-origin row (not in Warg remap); Ag area_id authoritative
                src_area_id = warg_src_area.get(src_id)
                if src_area_id is None:
                    # source had NULL area_id -> target must also be NULL
                    if tgt_area_id is not None:
                        failures.append(
                            f'area nk mismatch {tbl}.id={tgt_id} (warg src {src_id}): '
                            f'target area_id={tgt_area_id}, source area_id=NULL'
                        )
                        area_row_failures += 1
                    continue
                # deterministic expected target area_id from SOURCE via area_map
                if src_area_id not in area_map:
                    # unmapped source area -> spec requires an explicit mapping;
                    # its absence is a FAIL, not a skip (per re-grill instruction)
                    failures.append(
                        f'area nk {tbl}.id={tgt_id} (warg src {src_id}): source '
                        f'area_id={src_area_id} ({warg_area_names.get(src_area_id)!r}) '
                        f'has no area_map entry -- cannot verify'
                    )
                    area_row_failures += 1
                    continue
                expected_area_id = area_map[src_area_id]
                if tgt_area_id != expected_area_id:
                    failures.append(
                        f'area nk mismatch {tbl}.id={tgt_id} (warg src {src_id}): '
                        f'target area_id={tgt_area_id} '
                        f'(name={tgt_area_names.get(tgt_area_id)!r}), '
                        f'expected {expected_area_id} '
                        f'(source area {src_area_id}={warg_area_names.get(src_area_id)!r})'
                    )
                    log.error('[12] FAIL area nk: %s.id=%d src=%d area_id=%d != %d',
                              tbl, tgt_id, src_id, tgt_area_id, expected_area_id)
                    area_row_failures += 1

        if area_row_failures == 0:
            log.info('[12] OK: area natural-key inverted-remap check passed')
        else:
            log.error('[12] FAIL: %d area natural-key row failures', area_row_failures)

        # ---- type_id gate ----
        type_nk_failures = 0

        # Check 1: every Warg type maps to a target row with identical (cat, sub)
        warg_type_rows = warg.execute(
            'SELECT id, category, subtype FROM workout_types'
        ).fetchall()
        warg_type_nk = {wid: (cat, sub) for wid, cat, sub in warg_type_rows}
        for wid, cat, sub in warg_type_rows:
            new_id = type_id_map.get(wid)
            if new_id is None:
                failures.append(f'workout_type warg id {wid} not in type_id_map')
                type_nk_failures += 1
                continue
            tgt_row = tgt_type_by_id.get(new_id)
            if tgt_row is None:
                failures.append(
                    f'workout_type warg id {wid} -> tgt id {new_id} missing in target'
                )
                type_nk_failures += 1
            elif tgt_row != (cat, sub):
                failures.append(
                    f'workout_type nk mismatch: warg {wid} ({cat},{sub}) '
                    f'-> tgt {new_id} {tgt_row}'
                )
                type_nk_failures += 1

        # Check 2: workout_classifications -- inverted-remap to source row, then
        # verify target type_id's (cat, sub) equals the SOURCE Warg type's (cat, sub).
        inv_wo = _inverted('workouts')
        dedup_workout = self.maps['dedup_workout']
        dup_wc_ag_id = self.maps['dup_wc_ag_id']
        # Warg source WC rows: (workout_id, type_id) with source type nk
        warg_wc_src = warg.execute(
            'SELECT workout_id, type_id FROM workout_classifications'
        ).fetchall()
        # For each Warg source WC row, compute expected (merged_woid, merged_tid)
        # and confirm the target row exists with correct (cat, sub).
        for src_woid, src_tid in warg_wc_src:
            # source type natural key
            src_type_nk = warg_type_nk.get(src_tid)
            if src_type_nk is None:
                failures.append(
                    f'WC warg row (workout_id={src_woid}, type_id={src_tid}): '
                    f'source type_id absent from Warg workout_types'
                )
                type_nk_failures += 1
                continue
            # expected merged workout_id
            if src_woid in dedup_workout:
                merged_woid = dup_wc_ag_id.get(src_woid)
            else:
                merged_woid = id_remap.get('workouts', {}).get(src_woid, src_woid)
            expected_tid = type_id_map.get(src_tid)
            # target row must exist with this (workout_id, type_id)
            tgt_wc = tgt.execute(
                'SELECT type_id FROM workout_classifications WHERE workout_id=? AND type_id=?',
                (merged_woid, expected_tid)
            ).fetchone()
            if tgt_wc is None:
                failures.append(
                    f'WC warg row (src workout={src_woid}, src type={src_tid}): '
                    f'expected target (workout_id={merged_woid}, type_id={expected_tid}) '
                    f'not found'
                )
                type_nk_failures += 1
                continue
            # verify the target type_id resolves to the SAME (cat, sub) as source
            tgt_type_nk = tgt_type_by_id.get(tgt_wc[0])
            if tgt_type_nk != src_type_nk:
                failures.append(
                    f'WC type nk mismatch (src workout={src_woid}): target type '
                    f'{tgt_wc[0]}={tgt_type_nk} != source {src_type_nk}'
                )
                type_nk_failures += 1

        # Check 3: session_muscle -- same inverted approach via session_type_final.
        st_final = self.id_remap.get('session_type_final', {})
        inv_st = {new: old for old, new in st_final.items()}
        warg_sm_src = warg.execute(
            'SELECT session_type_id, type_id FROM session_muscle'
        ).fetchall()
        for src_stid, src_tid in warg_sm_src:
            src_type_nk = warg_type_nk.get(src_tid)
            if src_type_nk is None:
                failures.append(
                    f'SM warg row (st_id={src_stid}, type_id={src_tid}): '
                    f'source type_id absent from Warg workout_types'
                )
                type_nk_failures += 1
                continue
            merged_stid = st_final.get(src_stid, src_stid)
            expected_tid = type_id_map.get(src_tid)
            tgt_sm = tgt.execute(
                'SELECT type_id FROM session_muscle WHERE session_type_id=? AND type_id=?',
                (merged_stid, expected_tid)
            ).fetchone()
            if tgt_sm is None:
                failures.append(
                    f'SM warg row (src st={src_stid}, src type={src_tid}): '
                    f'expected target (st_id={merged_stid}, type_id={expected_tid}) '
                    f'not found'
                )
                type_nk_failures += 1
                continue
            tgt_type_nk = tgt_type_by_id.get(tgt_sm[0])
            if tgt_type_nk != src_type_nk:
                failures.append(
                    f'SM type nk mismatch (src st={src_stid}): target type '
                    f'{tgt_sm[0]}={tgt_type_nk} != source {src_type_nk}'
                )
                type_nk_failures += 1

        if type_nk_failures == 0:
            log.info('[12] OK: type natural-key inverted-remap check '
                     '(%d warg types + WC + session_muscle verified)',
                     len(warg_type_rows))
        else:
            log.error('[12] FAIL: %d type natural-key failures', type_nk_failures)

        ag.close()
        warg.close()
        tgt.close()

        ok = len(failures) == 0
        result = {
            'ok': ok,
            'counts': counts,
            'failures': failures,
        }
        if ok:
            log.info('[12] VERIFY GREEN -- all checks passed')
        else:
            log.error('[12] VERIFY RED -- %d failures: %s', len(failures), failures)
        return result

    def _step_wal_checkpoint(self):
        """Step 13: WAL checkpoint gate before promote."""
        log.info('[13] WAL CHECKPOINT GATE')
        c = sqlite3.connect(self.target_path)
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        c.close()

        wal_path = self.target_path + '-wal'
        if os.path.exists(wal_path):
            wal_size = os.path.getsize(wal_path)
            if wal_size > 0:
                raise RuntimeError(
                    f'[13] WAL file {wal_path} not empty after checkpoint: {wal_size} bytes'
                )
        log.info('[13] WAL checkpoint complete, -wal = 0 bytes')

    def _step_atomic_promote(self):
        """Step 14: atomic rename temp DB -> output path + write marker."""
        log.info('[14] ATOMIC PROMOTE: %s -> %s', self.target_path, self.output)

        # Ensure output directory exists
        os.makedirs(os.path.dirname(self.output) if os.path.dirname(self.output) else '.',
                    exist_ok=True)

        # Write merge marker into target before rename
        c = sqlite3.connect(self.target_path)
        c.execute(f'PRAGMA application_id = {MERGE_MARKER_APP_ID}')
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        c.close()

        # Atomic rename (same FS)
        os.rename(self.target_path, self.output)
        log.info('[14] Promoted to %s', self.output)

        # Clean up copies
        for p in [self.ag_copy, self.warg_copy]:
            if os.path.exists(p):
                os.unlink(p)
                log.debug('[14] cleaned up %s', p)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='lifekit crew merge engine (DGN-791 spec-v3)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__ or '')
    )
    parser.add_argument('--ag-db', required=True,
                        help='Ag live DB path (read-only)')
    parser.add_argument('--warg-db', required=True,
                        help='Warg live DB path (read-only)')
    parser.add_argument('--schema-sql', required=True,
                        help='Skull v19 schema.sql')
    parser.add_argument('--migrate-py', required=True,
                        help='Skull migrate.py runner')
    parser.add_argument('--output', required=True,
                        help='Target crew DB path')
    parser.add_argument('--temp-dir', default='/tmp',
                        help='Scratch directory (default: /tmp)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Run through VERIFY, skip promote steps 13-14')
    parser.add_argument('--force', action='store_true',
                        help='Overwrite existing output ignoring marker guard')
    parser.add_argument('--config', default=None,
                        help='JSON config overrides file')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Debug-level logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='[%(levelname)s] %(message)s'
    )

    area_map = DEFAULT_AREA_MAP
    health_tie_break_keys = DEFAULT_HEALTH_TIE_BREAK_KEYS

    if args.config:
        cfg = json.loads(Path(args.config).read_text())
        if 'area_map' in cfg:
            area_map = {int(k): v for k, v in cfg['area_map'].items()}
        if 'health_tie_break_keys' in cfg:
            health_tie_break_keys = set(cfg['health_tie_break_keys'])

    engine = MergeEngine(
        ag_db=args.ag_db,
        warg_db=args.warg_db,
        schema_sql=args.schema_sql,
        migrate_py=args.migrate_py,
        output=args.output,
        temp_dir=args.temp_dir,
        area_map=area_map,
        health_tie_break_keys=health_tie_break_keys,
        dry_run=args.dry_run,
        force=args.force,
    )

    try:
        result = engine.run()
        if args.dry_run:
            print('DRY-RUN VERIFY result:')
            print(f'  ok: {result["ok"]}')
            print(f'  counts: {result["counts"]}')
            if result['failures']:
                print(f'  failures: {result["failures"]}')
                sys.exit(1)
        sys.exit(0)
    except RuntimeError as e:
        log.error('MERGE FAILED: %s', e)
        sys.exit(1)
    except Exception as e:
        log.exception('UNEXPECTED ERROR: %s', e)
        sys.exit(3)


if __name__ == '__main__':
    main()
