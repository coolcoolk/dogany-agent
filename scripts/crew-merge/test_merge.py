#!/usr/bin/env python3
"""DGN-791 lifekit merge engine tests.

Three test tiers:
  1. Unit tests on synthetic fixture DBs (fast, no live data dependency)
  2. Integration tests using synthetic fixture DBs (blockers B1/B3/B4/B5/B7)
  3. Dry-run VERIFY against real DB copies in /tmp (run via run_live_dryrun())
     + Idempotency + crash-resume against live DB copies

Usage:
    python3 test_merge.py              # unit + integration tests
    python3 test_merge.py --live       # unit + integration + live dry-run + idempotency
    python3 test_merge.py --live --ag-db PATH --warg-db PATH ...

Live mode requires:
    --ag-db, --warg-db, --schema-sql, --migrate-py
"""

import argparse
import logging
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from merge import (
    MergeEngine,
    DEFAULT_AREA_MAP,
    DEFAULT_HEALTH_TIE_BREAK_KEYS,
    _schema_table_columns,
    vacuum_into,
    step_pre_scan,
)

log = logging.getLogger('test_merge')
logging.basicConfig(level=logging.WARNING, format='[%(levelname)s] %(message)s')

# ---------------------------------------------------------------------------
# fixture builder helpers
# ---------------------------------------------------------------------------

MINI_SCHEMA = """
PRAGMA user_version = 11;
CREATE TABLE areas (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, domain TEXT NOT NULL, description TEXT, notion_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE workout_types (id INTEGER PRIMARY KEY, category TEXT NOT NULL, subtype TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, UNIQUE(category, subtype));
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE metric_log (id INTEGER PRIMARY KEY, date TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, note TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), UNIQUE(date, metric));
CREATE TABLE meals (id INTEGER PRIMARY KEY, date TEXT NOT NULL, meal TEXT, name TEXT NOT NULL, grams REAL, carb REAL NOT NULL DEFAULT 0, protein REAL NOT NULL DEFAULT 0, fat REAL NOT NULL DEFAULT 0, fiber REAL NOT NULL DEFAULT 0, sugar REAL NOT NULL DEFAULT 0, alt_sugar REAL NOT NULL DEFAULT 0, alcohol REAL NOT NULL DEFAULT 0, kcal REAL GENERATED ALWAYS AS (protein*4 + fat*9 + (carb-fiber)*4 + alcohol*7) STORED, area_id INTEGER REFERENCES areas(id), notion_id TEXT, ag_source_id INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE workouts (id INTEGER PRIMARY KEY, date TEXT NOT NULL, minutes REAL NOT NULL DEFAULT 0, kcal REAL NOT NULL DEFAULT 0, note TEXT, area_id INTEGER REFERENCES areas(id), notion_id TEXT, ag_source_id INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
"""

MINI_SCHEMA_NO_AGSRC = """
PRAGMA user_version = 11;
CREATE TABLE areas (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, domain TEXT NOT NULL, description TEXT, notion_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE workout_types (id INTEGER PRIMARY KEY, category TEXT NOT NULL, subtype TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, UNIQUE(category, subtype));
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE metric_log (id INTEGER PRIMARY KEY, date TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, note TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), UNIQUE(date, metric));
CREATE TABLE meals (id INTEGER PRIMARY KEY, date TEXT NOT NULL, meal TEXT, name TEXT NOT NULL, grams REAL, carb REAL NOT NULL DEFAULT 0, protein REAL NOT NULL DEFAULT 0, fat REAL NOT NULL DEFAULT 0, fiber REAL NOT NULL DEFAULT 0, sugar REAL NOT NULL DEFAULT 0, alt_sugar REAL NOT NULL DEFAULT 0, alcohol REAL NOT NULL DEFAULT 0, kcal REAL GENERATED ALWAYS AS (protein*4 + fat*9 + (carb-fiber)*4 + alcohol*7) STORED, area_id INTEGER REFERENCES areas(id), notion_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE workouts (id INTEGER PRIMARY KEY, date TEXT NOT NULL, minutes REAL NOT NULL DEFAULT 0, kcal REAL NOT NULL DEFAULT 0, note TEXT, area_id INTEGER REFERENCES areas(id), notion_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
"""


def _make_fixture(path: str, ddl: str, rows: dict) -> None:
    """Create a fixture DB at path with given DDL and row data."""
    if os.path.exists(path):
        os.unlink(path)
    c = sqlite3.connect(path)
    c.execute('PRAGMA foreign_keys = OFF')
    c.executescript(ddl)
    for table, table_rows in rows.items():
        if not table_rows:
            continue
        cols = [r for r in table_rows[0].keys()]
        col_str = ', '.join(f'"{c_}"' for c_ in cols)
        placeholders = ', '.join(['?'] * len(cols))
        c.executemany(
            f'INSERT INTO "{table}" ({col_str}) VALUES ({placeholders})',
            [tuple(r[c_] for c_ in cols) for r in table_rows]
        )
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# unit tests
# ---------------------------------------------------------------------------

PASS = 0
FAIL = 0


def ok(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  PASS  {label}')
    else:
        FAIL += 1
        print(f'  FAIL  {label}')


def test_area_map():
    """area constant-map: Warg area 1 -> 5, 2 -> 15."""
    area_map = DEFAULT_AREA_MAP
    ok('area_map[1] == 5', area_map[1] == 5)
    ok('area_map[2] == 15', area_map[2] == 15)


def test_schema_table_columns(schema_sql: str):
    """_schema_table_columns returns correct column sets from schema.sql."""
    cols = _schema_table_columns(schema_sql)
    ok('schema has workout_types', 'workout_types' in cols)
    ok('workout_types has category', 'category' in cols['workout_types'])
    ok('schema has meals', 'meals' in cols)
    ok('meals has owning_agent', 'owning_agent' in cols.get('meals', []))
    ok('meals has aa_protein', 'aa_protein' in cols.get('meals', []))
    ok('schema has metric_log', 'metric_log' in cols)
    ok('metric_log has owning_agent', 'owning_agent' in cols.get('metric_log', []))


def test_config_merge_logic():
    """Config merge: max(updated_at) wins; tie -> health key -> Warg."""
    # Simulated config rows
    ag_cfg = {
        'weight_kg': ('82.5', '2026-07-09 05:34:36'),
        'ag_only_key': ('val', '2026-07-01'),
    }
    warg_cfg = {
        'weight_kg': ('83.6', '2026-07-28 05:34:36'),
        'warg_only_key': ('wval', '2026-07-01'),
        'protein_g': ('160', '2026-07-14'),
    }
    health_keys = DEFAULT_HEALTH_TIE_BREAK_KEYS

    results = {}
    all_keys = set(ag_cfg) | set(warg_cfg)
    for key in all_keys:
        if key in ag_cfg and key not in warg_cfg:
            val, uat = ag_cfg[key]
        elif key in warg_cfg and key not in ag_cfg:
            val, uat = warg_cfg[key]
        else:
            av, aat = ag_cfg[key]
            wv, wat = warg_cfg[key]
            if (wat or '') > (aat or ''):
                val, uat = wv, wat
            elif (aat or '') > (wat or ''):
                val, uat = av, aat
            else:
                val, uat = (wv, wat) if key in health_keys else (av, aat)
        results[key] = val

    ok('config: ag_only_key preserved', results.get('ag_only_key') == 'val')
    ok('config: warg_only_key preserved', results.get('warg_only_key') == 'wval')
    ok('config: weight_kg -> Warg (newer)', results.get('weight_kg') == '83.6')
    ok('config: protein_g -> Warg (only Warg has it)', results.get('protein_g') == '160')


def test_dedup_logic():
    """ag_source_id dedup: Warg rows with ag_source_id are mirrors, dropped."""
    # Simulate Warg meals with 3 having ag_source_id
    warg_meals = [
        {'id': 1, 'ag_source_id': 10, 'name': 'mirror1'},
        {'id': 2, 'ag_source_id': 11, 'name': 'mirror2'},
        {'id': 3, 'ag_source_id': None, 'name': 'novel'},
    ]
    dedup_set = {r['id'] for r in warg_meals if r['ag_source_id'] is not None}
    surviving = [r for r in warg_meals if r['id'] not in dedup_set]
    ok('dedup removes 2 mirrors', len(dedup_set) == 2)
    ok('dedup keeps 1 novel', len(surviving) == 1)
    ok('surviving is novel', surviving[0]['name'] == 'novel')


def test_natural_key_type_map():
    """workout_types natural-key map: shared -> Ag id; warg-only -> new offset."""
    ag_types = [(1, '근력', '가슴'), (2, '근력', '등'), (3, '유산소', '러닝')]
    warg_types = [(1, '근력', '가슴'), (2, '근력', '등'), (5, '유연·회복', '필라테스')]

    ag_map = {(cat, sub): aid for aid, cat, sub in ag_types}
    ag_max_id = max(aid for aid, _, _ in ag_types)
    new_counter = ag_max_id + 100
    type_id_map = {}
    for wid, cat, sub in warg_types:
        if (cat, sub) in ag_map:
            type_id_map[wid] = ag_map[(cat, sub)]
        else:
            type_id_map[wid] = new_counter
            new_counter += 1

    ok('shared type maps to Ag id', type_id_map[1] == 1)
    ok('shared type 2 maps to Ag id 2', type_id_map[2] == 2)
    ok('warg-only gets new id >= offset', type_id_map[5] >= ag_max_id + 100)
    ok('warg-only different from shared', type_id_map[5] not in {1, 2, 3})


# ---------------------------------------------------------------------------
# Blocker integration tests (synthetic fixture DBs, no live data required)
# ---------------------------------------------------------------------------

# Shared minimal schema for integration tests: v19 equivalent (no migrations needed).
# Covers metric_log, workout_classifications, session_muscle, workout_types,
# areas, meals, workouts, config, and a stub schema + migrate path.

_INT_SCHEMA = """
PRAGMA user_version = 19;
CREATE TABLE areas (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL DEFAULT 'shared'
);
CREATE TABLE place (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE workout_types (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    subtype TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(category, subtype)
);
CREATE TABLE routine (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE routine_def (
    id INTEGER PRIMARY KEY,
    routine_id INTEGER REFERENCES routine(id),
    name TEXT,
    created_by TEXT,
    owning_agent TEXT NOT NULL DEFAULT 'ag'
);
CREATE TABLE session_type (
    id INTEGER PRIMARY KEY,
    routine_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    UNIQUE(routine_id, code)
);
CREATE TABLE session_muscle (
    session_type_id INTEGER NOT NULL REFERENCES session_type(id),
    type_id INTEGER NOT NULL REFERENCES workout_types(id),
    PRIMARY KEY (session_type_id, type_id)
);
-- owning_agent DEFAULT 'warg' on metric_log/meals/workouts MIRRORS the real
-- migrate step_019 backfill: step_019 sets owning_agent='warg' on ALL rows of
-- these tables (Ag copy included).  Fixtures must reproduce this so tests
-- exercise the real merge path -- an 'ag' default hid the B1 predicate bug.
CREATE TABLE workouts (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    minutes REAL NOT NULL DEFAULT 0,
    area_id INTEGER REFERENCES areas(id),
    session_type_id INTEGER REFERENCES session_type(id),
    owning_agent TEXT NOT NULL DEFAULT 'warg',
    ag_source_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE workout_classifications (
    workout_id INTEGER NOT NULL REFERENCES workouts(id),
    type_id INTEGER NOT NULL REFERENCES workout_types(id),
    PRIMARY KEY (workout_id, type_id)
);
CREATE TABLE meals (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    carb REAL NOT NULL DEFAULT 0,
    protein REAL NOT NULL DEFAULT 0,
    fat REAL NOT NULL DEFAULT 0,
    fiber REAL NOT NULL DEFAULT 0,
    area_id INTEGER REFERENCES areas(id),
    owning_agent TEXT NOT NULL DEFAULT 'warg',
    ag_source_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE metric_log (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    owning_agent TEXT NOT NULL DEFAULT 'warg',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(date, metric)
);
CREATE TABLE event (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    area_id INTEGER REFERENCES areas(id),
    owning_agent TEXT NOT NULL DEFAULT 'ag'
);
CREATE TABLE sub_event (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES event(id)
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    owning_agent TEXT NOT NULL DEFAULT 'ag'
);
CREATE TABLE appointments (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    owning_agent TEXT NOT NULL DEFAULT 'ag'
);
CREATE TABLE persons (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    owning_agent TEXT NOT NULL DEFAULT 'ag'
);
CREATE TABLE spend_category (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE roller_log (
    id INTEGER PRIMARY KEY,
    recurrence_id TEXT NOT NULL,
    event_type TEXT
);
-- intimacy_levels: an UNGATED table (the engine neither loads nor count-gates
-- it) used to exercise the umbrella gate.  Natural-key PK so the seed-exception
-- path is also reachable.
CREATE TABLE intimacy_levels (
    level TEXT PRIMARY KEY,
    label TEXT
);
"""


def _make_int_fixture(path: str, ddl: str = _INT_SCHEMA, rows: dict = None) -> None:
    """Create an integration test fixture DB."""
    if os.path.exists(path):
        os.unlink(path)
    c = sqlite3.connect(path)
    c.execute('PRAGMA foreign_keys = OFF')
    c.executescript(ddl)
    if rows:
        for table, table_rows in rows.items():
            if not table_rows:
                continue
            cols = list(table_rows[0].keys())
            col_str = ', '.join(f'"{c_}"' for c_ in cols)
            placeholders = ', '.join(['?'] * len(cols))
            c.executemany(
                f'INSERT INTO "{table}" ({col_str}) VALUES ({placeholders})',
                [tuple(r[c_] for c_ in cols) for r in table_rows]
            )
    c.commit()
    c.close()


def _no_migrate(db_path: str) -> None:
    """No-op migrate stub: already at v19."""
    pass


class _NoMigrateEngine(MergeEngine):
    """MergeEngine subclass that skips migration (fixture DBs are already v19).

    Uses _INT_SCHEMA directly instead of reading schema_sql from disk.
    """

    def run(self):
        # Replicate the pipeline, skipping actual migrate.py calls.
        from merge import (
            step_natural_key_maps as _nkm,
            step_load_ag as _load_ag,
            _compute_warg_id_remap,
        )
        # Step 1 (skip migration): copies already at v19
        vacuum_into(self.ag_db, self.ag_copy)
        vacuum_into(self.warg_db, self.warg_copy)

        # Step 2: build target from _INT_SCHEMA directly (no file needed)
        target_path = os.path.join(self.temp_dir, 'lifekit_merge_target.db')
        if os.path.exists(target_path):
            os.unlink(target_path)
        c = sqlite3.connect(target_path)
        c.execute('PRAGMA foreign_keys = OFF')
        c.executescript(_INT_SCHEMA)
        c.execute('PRAGMA foreign_keys = ON')
        c.close()
        self.target_path = target_path

        # Step 3: natural-key maps
        self.maps = _nkm(self.ag_copy, self.warg_copy, self.area_map,
                         self.target_path, self.temp_dir)

        # Step 4: load Ag
        _load_ag(self.ag_copy, self.target_path, self.maps)

        # Step 5: id remap
        self.id_remap = _compute_warg_id_remap(self.warg_copy, self.maps)
        self._fill_routine_remap()

        # Steps 6-8: load Warg
        self._step_load_warg()

        # Step 9: config merge
        self._step_config_merge()

        # Step 11: drop ag_source_id (no-op if not present)
        self._step_drop_ag_source_id()

        # Step 12: verify
        verify_result = self._step_verify()
        return verify_result


def _run_int_engine(ag_rows: dict, warg_rows: dict, temp_dir: str,
                    area_map: dict = None) -> dict:
    """Build fixture DBs and run _NoMigrateEngine, return verify result."""
    ag_path = os.path.join(temp_dir, 'int_ag.db')
    warg_path = os.path.join(temp_dir, 'int_warg.db')
    out_path = os.path.join(temp_dir, 'int_out.db')
    _make_int_fixture(ag_path, rows=ag_rows)
    _make_int_fixture(warg_path, rows=warg_rows)
    engine = _NoMigrateEngine(
        ag_db=ag_path, warg_db=warg_path,
        schema_sql='', migrate_py='',
        output=out_path, temp_dir=temp_dir,
        area_map=area_map or {1: 5, 2: 15},
        dry_run=True, force=True,
    )
    return engine.run()


# --- B1: non-conflicting Ag metric_log rows must survive ---

def test_b1_metric_log_noncolliding_ag_rows():
    """B1: Ag metric_log rows that do NOT collide with Warg must be preserved."""
    print()
    print('-- B1: metric_log non-colliding Ag row preservation --')
    with tempfile.TemporaryDirectory() as tmp:
        # Ag has 2 metric rows; Warg collides on 1 and has 1 unique.
        # owning_agent='warg' on ALL rows mirrors real migrate step_019 backfill
        # (this is exactly what hid the original B1 predicate bug).
        ag_rows = {
            'areas': [{'id': 5, 'name': 'test-area', 'domain': 'shared'}],
            'metric_log': [
                {'id': 1, 'date': '2026-07-01', 'metric': 'weight_kg',
                 'value': 80.0, 'owning_agent': 'warg'},  # collides -> Warg wins
                {'id': 2, 'date': '2026-07-01', 'metric': 'steps',
                 'value': 8000.0, 'owning_agent': 'warg'},  # no Warg collision -> keep
            ],
        }
        warg_rows = {
            'areas': [],
            'metric_log': [
                {'id': 1, 'date': '2026-07-01', 'metric': 'weight_kg',
                 'value': 83.6, 'owning_agent': 'warg'},  # collides with Ag id=1
                {'id': 2, 'date': '2026-07-28', 'metric': 'weight_kg',
                 'value': 83.0, 'owning_agent': 'warg'},  # unique date
            ],
        }
        result = _run_int_engine(ag_rows, warg_rows, tmp)
        # Expected: 1 Ag survivor (steps) + 2 Warg rows = 3
        ml_count = result['counts'].get('metric_log')
        ok('B1: metric_log Ag non-colliding row preserved (count=3)',
           ml_count == 3)
        ok('B1: VERIFY ok (no false failure)', result['ok'])

        tgt_path = os.path.join(tmp, 'lifekit_merge_target.db')
        if os.path.exists(tgt_path):
            c = sqlite3.connect(tgt_path)
            steps_row = c.execute(
                "SELECT value FROM metric_log WHERE metric='steps'"
            ).fetchone()
            # Conflict row must carry the WARG value (83.6), not Ag's stale 80.0
            weight_row = c.execute(
                "SELECT value FROM metric_log WHERE metric='weight_kg' "
                "AND date='2026-07-01'"
            ).fetchone()
            c.close()
            ok('B1: steps metric_log row survives with correct value',
               steps_row is not None and steps_row[0] == 8000.0)
            ok('B1: conflict row = Warg value 83.6 (not Ag stale 80.0)',
               weight_row is not None and weight_row[0] == 83.6)


def test_b1_metric_log_value_gate_catches_ag_stale():
    """B1 value gate: if the merge wrongly keeps the Ag value on a conflict,
    the VERIFY value-level gate must go RED."""
    print()
    print('-- B1 value gate: stale-Ag-value on conflict caught by VERIFY --')
    with tempfile.TemporaryDirectory() as tmp:
        ag_rows = {
            'areas': [{'id': 5, 'name': 'a', 'domain': 'shared'}],
            'metric_log': [
                {'id': 1, 'date': '2026-07-01', 'metric': 'weight_kg',
                 'value': 80.0, 'owning_agent': 'warg'},
            ],
        }
        warg_rows = {
            'areas': [],
            'metric_log': [
                {'id': 1, 'date': '2026-07-01', 'metric': 'weight_kg',
                 'value': 83.6, 'owning_agent': 'warg'},
            ],
        }
        # Run normally, then corrupt the merged conflict row to the Ag value and
        # re-run VERIFY -- it must go RED.
        ag_path = os.path.join(tmp, 'int_ag.db')
        warg_path = os.path.join(tmp, 'int_warg.db')
        _make_int_fixture(ag_path, rows=ag_rows)
        _make_int_fixture(warg_path, rows=warg_rows)
        engine = _NoMigrateEngine(
            ag_db=ag_path, warg_db=warg_path, schema_sql='', migrate_py='',
            output=os.path.join(tmp, 'out.db'), temp_dir=tmp,
            area_map={1: 5, 2: 15}, dry_run=True, force=True,
        )
        orig_verify = engine._step_verify

        def corrupt_then_verify():
            t = sqlite3.connect(engine.target_path)
            t.execute("UPDATE metric_log SET value=80.0 WHERE metric='weight_kg'")
            t.commit()
            t.close()
            return orig_verify()

        engine._step_verify = corrupt_then_verify
        result = engine.run()
        ok('B1 value gate: RED when conflict row holds stale Ag value',
           not result['ok'])


# --- B3: VERIFY must catch injected cross-references (real engine + injection) ---

def _b3_build_and_corrupt(tmp, attack):
    """Run the engine on a synthetic fixture, corrupt the target per `attack`
    just before VERIFY, and return the verify result.

    attack in {'area_flip', 'area_unmapped', 'wc_type', 'sm_type'}.
    Mirrors the grill harness /tmp/regrill791/ib3_harness.py.
    """
    # Fixture: 2 areas (mapped 1->5, 2->15), 2 workout types, Warg workouts that
    # survive with area/type FKs, plus WC and session_muscle rows.
    ag_rows = {
        'areas': [
            {'id': 5, 'name': 'fitness', 'domain': 'shared'},
            {'id': 15, 'name': 'diet', 'domain': 'shared'},
        ],
        'workout_types': [
            {'id': 1, 'category': 'strength', 'subtype': 'chest', 'sort': 0, 'active': 1},
            {'id': 2, 'category': 'cardio', 'subtype': 'run', 'sort': 0, 'active': 1},
        ],
        'routine': [{'id': 1, 'name': 'r1'}],
    }
    warg_rows = {
        'areas': [
            {'id': 1, 'name': 'fitness', 'domain': 'shared'},
            {'id': 2, 'name': 'diet', 'domain': 'shared'},
        ],
        'workout_types': [
            {'id': 1, 'category': 'strength', 'subtype': 'chest', 'sort': 0, 'active': 1},
            {'id': 2, 'category': 'cardio', 'subtype': 'run', 'sort': 0, 'active': 1},
        ],
        'routine': [{'id': 1, 'name': 'r1'}],
        'session_type': [{'id': 1, 'routine_id': 1, 'code': 'A'}],
        'workouts': [
            {'id': 1, 'date': '2026-07-01', 'minutes': 40.0, 'area_id': 1,
             'session_type_id': 1, 'owning_agent': 'warg', 'ag_source_id': None},
            {'id': 2, 'date': '2026-07-02', 'minutes': 30.0, 'area_id': 2,
             'session_type_id': 1, 'owning_agent': 'warg', 'ag_source_id': None},
        ],
        'workout_classifications': [
            {'workout_id': 1, 'type_id': 1},
            {'workout_id': 2, 'type_id': 2},
        ],
        'session_muscle': [
            {'session_type_id': 1, 'type_id': 1},
        ],
    }
    ag_path = os.path.join(tmp, 'int_ag.db')
    warg_path = os.path.join(tmp, 'int_warg.db')
    _make_int_fixture(ag_path, rows=ag_rows)
    _make_int_fixture(warg_path, rows=warg_rows)
    engine = _NoMigrateEngine(
        ag_db=ag_path, warg_db=warg_path, schema_sql='', migrate_py='',
        output=os.path.join(tmp, 'out.db'), temp_dir=tmp,
        area_map={1: 5, 2: 15}, dry_run=True, force=True,
    )
    orig_verify = engine._step_verify

    def corrupt_then_verify():
        t = sqlite3.connect(engine.target_path)
        warg_wo = sorted(engine.id_remap['workouts'].values())
        wo_in = ','.join(map(str, warg_wo)) or '0'
        if attack == 'area_flip':
            # Warg-origin workout: flip area_id 5 <-> 15 (both valid mapped ids)
            row = t.execute(
                f'SELECT id, area_id FROM workouts WHERE id IN ({wo_in}) '
                f'AND area_id IS NOT NULL LIMIT 1').fetchone()
            new_area = 15 if row[1] != 15 else 5
            t.execute('UPDATE workouts SET area_id=? WHERE id=?', (new_area, row[0]))
        elif attack == 'area_unmapped':
            row = t.execute(
                f'SELECT id, area_id FROM workouts WHERE id IN ({wo_in}) '
                f'AND area_id IS NOT NULL LIMIT 1').fetchone()
            # point to a real Ag area that is NOT the correct mapping
            t.execute("INSERT OR IGNORE INTO areas (id, name, domain) "
                      "VALUES (3, 'other', 'shared')")
            t.execute('UPDATE workouts SET area_id=3 WHERE id=?', (row[0],))
        elif attack == 'wc_type':
            row = t.execute(
                f'SELECT workout_id, type_id FROM workout_classifications '
                f'WHERE workout_id IN ({wo_in}) LIMIT 1').fetchone()
            other = t.execute(
                'SELECT id FROM workout_types WHERE id != ? LIMIT 1',
                (row[1],)).fetchone()[0]
            t.execute('UPDATE workout_classifications SET type_id=? '
                      'WHERE workout_id=? AND type_id=?', (other, row[0], row[1]))
        elif attack == 'sm_type':
            warg_st = sorted(engine.id_remap.get('session_type_final', {}).values())
            st_in = ','.join(map(str, warg_st)) or '0'
            row = t.execute(
                f'SELECT session_type_id, type_id FROM session_muscle '
                f'WHERE session_type_id IN ({st_in}) LIMIT 1').fetchone()
            other = t.execute(
                'SELECT id FROM workout_types WHERE id != ? LIMIT 1',
                (row[1],)).fetchone()[0]
            t.execute('UPDATE session_muscle SET type_id=? '
                      'WHERE session_type_id=? AND type_id=?',
                      (other, row[0], row[1]))
        t.commit()
        t.close()
        return orig_verify()

    engine._step_verify = corrupt_then_verify
    return engine.run()


def test_b3_verify_catches_injected_crossrefs():
    """B3: all 4 injected cross-reference attacks must cause VERIFY RED."""
    print()
    print('-- B3: injected cross-refs (4 attacks) -> VERIFY RED --')
    for attack in ('area_flip', 'area_unmapped', 'wc_type', 'sm_type'):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result = _b3_build_and_corrupt(tmp, attack)
                caught = not result['ok']
            except RuntimeError:
                caught = True  # abort also counts as caught
            ok(f'B3: attack {attack!r} caught (VERIFY RED)', caught)


def test_b3_golden_passes():
    """B3: a clean (non-corrupted) merge on the same fixture must be GREEN."""
    print()
    print('-- B3: clean fixture (no injection) -> VERIFY GREEN --')
    with tempfile.TemporaryDirectory() as tmp:
        ag_rows = {
            'areas': [
                {'id': 5, 'name': 'fitness', 'domain': 'shared'},
                {'id': 15, 'name': 'diet', 'domain': 'shared'},
            ],
            'workout_types': [
                {'id': 1, 'category': 'strength', 'subtype': 'chest', 'sort': 0, 'active': 1},
                {'id': 2, 'category': 'cardio', 'subtype': 'run', 'sort': 0, 'active': 1},
            ],
            'routine': [{'id': 1, 'name': 'r1'}],
        }
        warg_rows = {
            'areas': [
                {'id': 1, 'name': 'fitness', 'domain': 'shared'},
                {'id': 2, 'name': 'diet', 'domain': 'shared'},
            ],
            'workout_types': [
                {'id': 1, 'category': 'strength', 'subtype': 'chest', 'sort': 0, 'active': 1},
                {'id': 2, 'category': 'cardio', 'subtype': 'run', 'sort': 0, 'active': 1},
            ],
            'routine': [{'id': 1, 'name': 'r1'}],
            'session_type': [{'id': 1, 'routine_id': 1, 'code': 'A'}],
            'workouts': [
                {'id': 1, 'date': '2026-07-01', 'minutes': 40.0, 'area_id': 1,
                 'session_type_id': 1, 'owning_agent': 'warg', 'ag_source_id': None},
                {'id': 2, 'date': '2026-07-02', 'minutes': 30.0, 'area_id': 2,
                 'session_type_id': 1, 'owning_agent': 'warg', 'ag_source_id': None},
            ],
            'workout_classifications': [
                {'workout_id': 1, 'type_id': 1},
                {'workout_id': 2, 'type_id': 2},
            ],
            'session_muscle': [
                {'session_type_id': 1, 'type_id': 1},
            ],
        }
        result = _run_int_engine(ag_rows, warg_rows, tmp)
        if not result['ok']:
            print(f'    golden failures: {result["failures"]}')
        ok('B3: clean fixture VERIFY GREEN', result['ok'])


# --- B4: Ag-native workout_classifications rows must not be lost ---

def test_b4_ag_wc_rows_preserved():
    """B4: Ag workout_classifications rows must be explicitly loaded and preserved."""
    print()
    print('-- B4: Ag workout_classifications rows preserved --')
    with tempfile.TemporaryDirectory() as tmp:
        ag_rows = {
            'areas': [{'id': 5, 'name': 'test-area', 'domain': 'shared'}],
            'workout_types': [
                {'id': 1, 'category': 'strength', 'subtype': 'chest', 'sort': 0, 'active': 1},
            ],
            'workouts': [
                {'id': 1, 'date': '2026-07-01', 'minutes': 45.0,
                 'area_id': 5, 'owning_agent': 'ag', 'ag_source_id': None},
            ],
            'workout_classifications': [
                {'workout_id': 1, 'type_id': 1},
            ],
        }
        warg_rows = {
            'areas': [],
            'workout_types': [
                {'id': 1, 'category': 'strength', 'subtype': 'chest', 'sort': 0, 'active': 1},
            ],
            'workouts': [],
            'workout_classifications': [],
        }
        result = _run_int_engine(ag_rows, warg_rows, tmp)
        ok('B4: VERIFY passes with Ag WC rows', result['ok'])

        tgt_path = os.path.join(tmp, 'lifekit_merge_target.db')
        if os.path.exists(tgt_path):
            c = sqlite3.connect(tgt_path)
            wc_count = c.execute('SELECT count(*) FROM workout_classifications').fetchone()[0]
            c.close()
            ok('B4: Ag-native WC row present in merged target (count=1)', wc_count == 1)


# --- B5: adding a new Ag record between measurement and cutover must not fail VERIFY ---

def test_b5_dynamic_counts_tolerate_new_records():
    """B5: dynamic expected counts allow new Ag records without VERIFY failure."""
    print()
    print('-- B5: dynamic counts do not reject new records added after measurement --')
    # This test verifies that the VERIFY formula uses live source counts, not
    # hardcoded literals.  We run with 3 meals (not 129) and verify no failure.
    with tempfile.TemporaryDirectory() as tmp:
        ag_rows = {
            'areas': [{'id': 5, 'name': 'home', 'domain': 'shared'}],
            'meals': [
                {'id': 1, 'date': '2026-07-01', 'name': 'meal1',
                 'carb': 50.0, 'protein': 30.0, 'fat': 10.0, 'fiber': 5.0,
                 'area_id': None, 'owning_agent': 'ag', 'ag_source_id': None},
                {'id': 2, 'date': '2026-07-02', 'name': 'meal2',
                 'carb': 60.0, 'protein': 25.0, 'fat': 15.0, 'fiber': 3.0,
                 'area_id': None, 'owning_agent': 'ag', 'ag_source_id': None},
                # This is the "new" record added after measurement snapshot
                {'id': 3, 'date': '2026-07-03', 'name': 'meal3-new',
                 'carb': 40.0, 'protein': 20.0, 'fat': 8.0, 'fiber': 4.0,
                 'area_id': None, 'owning_agent': 'ag', 'ag_source_id': None},
            ],
        }
        warg_rows = {
            'areas': [],
            'meals': [
                {'id': 1, 'date': '2026-07-01', 'name': 'warg-meal',
                 'carb': 30.0, 'protein': 20.0, 'fat': 5.0, 'fiber': 2.0,
                 'area_id': None, 'owning_agent': 'warg', 'ag_source_id': None},
            ],
        }
        result = _run_int_engine(ag_rows, warg_rows, tmp)
        ok('B5: VERIFY passes with 3 Ag meals + 1 Warg meal (no hardcoded 129)',
           result['ok'])
        meals_count = result['counts'].get('meals')
        ok('B5: meals count = 4 (3 Ag + 1 Warg)', meals_count == 4)


# --- B7: un-versioned schema drift must cause hard abort in step 0 ---

def test_b7_pre_scan_aborts_on_drift():
    """B7: un-versioned extra column in live DB must cause step_pre_scan abort."""
    print()
    print('-- B7: pre-scan aborts on un-versioned drift --')
    with tempfile.TemporaryDirectory() as tmp:
        # Create a DB with an extra column not in schema.sql
        drifted_path = os.path.join(tmp, 'drifted.db')
        c = sqlite3.connect(drifted_path)
        c.executescript("""
            PRAGMA user_version = 19;
            CREATE TABLE areas (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                domain TEXT NOT NULL DEFAULT 'shared',
                extra_column_not_in_schema TEXT);
        """)
        c.execute("INSERT INTO areas (id, name, domain) VALUES (1, 'test', 'shared')")
        c.commit()
        c.close()

        # Build a schema SQL that does NOT have extra_column_not_in_schema
        schema_path = os.path.join(tmp, 'test_schema.sql')
        with open(schema_path, 'w') as f:
            f.write("""
PRAGMA user_version = 19;
CREATE TABLE areas (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL DEFAULT 'shared');
""")

        raised = False
        try:
            step_pre_scan(drifted_path, drifted_path, '', schema_path)
        except RuntimeError as e:
            raised = 'ABORT' in str(e) or 'drift' in str(e).lower()
        ok('B7: step_pre_scan raises RuntimeError on un-versioned drift', raised)


# --- Umbrella gate: ungated table with Warg data -> RED; seed table -> exempt ---

def test_umbrella_ungated_warg_data_red():
    """Umbrella gate: Warg rows in an ungated (non-loaded, non-seed) table
    must cause VERIFY RED."""
    print()
    print('-- Umbrella: ungated table with Warg data -> VERIFY RED --')
    with tempfile.TemporaryDirectory() as tmp:
        ag_rows = {
            'areas': [{'id': 5, 'name': 'a', 'domain': 'shared'}],
        }
        # intimacy_levels is ungated by the pipeline; give it a WARG-ONLY row
        # (Ag has none) so it is per-agent data, not a shared seed.
        warg_rows = {
            'areas': [],
            'intimacy_levels': [{'level': 'L1', 'label': 'warg-only'}],
        }
        result = _run_int_engine(ag_rows, warg_rows, tmp)
        ok('Umbrella: ungated Warg data -> VERIFY RED', not result['ok'])
        hit = any('intimacy_levels' in f and 'umbrella' in f
                  for f in result['failures'])
        ok('Umbrella: failure names the offending table', hit)


def test_umbrella_identical_seed_exempt():
    """Umbrella gate: an ungated table whose rows are IDENTICAL on both copies
    (migrate seed) must NOT trip the gate (no false positive)."""
    print()
    print('-- Umbrella: identical seed table exempt (no false positive) --')
    with tempfile.TemporaryDirectory() as tmp:
        seed = [{'level': 'L1', 'label': 'x'}, {'level': 'L2', 'label': 'y'}]
        ag_rows = {
            'areas': [{'id': 5, 'name': 'a', 'domain': 'shared'}],
            'intimacy_levels': [dict(r) for r in seed],
        }
        warg_rows = {
            'areas': [],
            'intimacy_levels': [dict(r) for r in seed],  # identical seed
        }
        result = _run_int_engine(ag_rows, warg_rows, tmp)
        if not result['ok']:
            print(f'    unexpected failures: {result["failures"]}')
        ok('Umbrella: identical-seed table does NOT trip gate', result['ok'])


# ---------------------------------------------------------------------------
# live tests
# ---------------------------------------------------------------------------

def run_live_dryrun(ag_db: str, warg_db: str, schema_sql: str,
                    migrate_py: str, temp_dir: str) -> dict:
    """Full dry-run against real DB copies.  Returns verify result."""
    log.setLevel(logging.INFO)
    output = os.path.join(temp_dir, 'test_merge_output.db')
    engine = MergeEngine(
        ag_db=ag_db,
        warg_db=warg_db,
        schema_sql=schema_sql,
        migrate_py=migrate_py,
        output=output,
        temp_dir=temp_dir,
        dry_run=True,
        force=True,
    )
    return engine.run()


def run_idempotency(ag_db: str, warg_db: str, schema_sql: str,
                    migrate_py: str, temp_dir: str) -> dict:
    """Second dry-run reusing existing copies -- counts must be identical."""
    output = os.path.join(temp_dir, 'test_merge_output.db')
    engine = MergeEngine(
        ag_db=ag_db,
        warg_db=warg_db,
        schema_sql=schema_sql,
        migrate_py=migrate_py,
        output=output,
        temp_dir=temp_dir,
        dry_run=True,
        force=True,
    )
    return engine.run()


def run_crash_resume(ag_db: str, warg_db: str, schema_sql: str,
                     migrate_py: str, temp_dir: str) -> dict:
    """Poison the temp target with garbage, re-run, verify it recovers."""
    # Poison the target
    target_path = os.path.join(temp_dir, 'lifekit_merge_target.db')
    if os.path.exists(target_path):
        os.unlink(target_path)
    c = sqlite3.connect(target_path)
    c.execute('CREATE TABLE poison (x TEXT)')
    c.execute("INSERT INTO poison VALUES ('corrupted')")
    c.commit()
    c.close()

    output = os.path.join(temp_dir, 'test_merge_output.db')
    engine = MergeEngine(
        ag_db=ag_db,
        warg_db=warg_db,
        schema_sql=schema_sql,
        migrate_py=migrate_py,
        output=output,
        temp_dir=temp_dir,
        dry_run=True,
        force=True,
    )
    return engine.run()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='DGN-791 merge engine tests')
    parser.add_argument('--live', action='store_true',
                        help='Also run live dry-run + idempotency + crash-resume')
    parser.add_argument('--ag-db', default=None,
                        help='Ag live DB path (required for --live)')
    parser.add_argument('--warg-db', default=None,
                        help='Warg live DB path (required for --live)')
    parser.add_argument('--schema-sql', default=None,
                        help='schema.sql path (required for --live; '
                             'enables schema-parsing unit test)')
    parser.add_argument('--migrate-py', default=None,
                        help='migrate.py runner path (required for --live)')
    parser.add_argument('--temp-dir', default='/tmp')
    args = parser.parse_args()

    if args.live:
        missing = [name for name, val in (
            ('--ag-db', args.ag_db), ('--warg-db', args.warg_db),
            ('--schema-sql', args.schema_sql), ('--migrate-py', args.migrate_py),
        ) if not val]
        if missing:
            parser.error('--live requires: ' + ', '.join(missing))

    print()
    print('=== UNIT TESTS ===')

    # Area map
    print()
    print('-- area map --')
    test_area_map()

    # Schema column parsing (requires schema.sql)
    if args.schema_sql and os.path.exists(args.schema_sql):
        print()
        print('-- schema column parsing --')
        test_schema_table_columns(args.schema_sql)

    # Config merge logic
    print()
    print('-- config merge logic --')
    test_config_merge_logic()

    # Dedup logic
    print()
    print('-- dedup logic --')
    test_dedup_logic()

    # Natural key type map
    print()
    print('-- natural key type map --')
    test_natural_key_type_map()

    print()
    print(f'Unit tests: {PASS} passed, {FAIL} failed')

    print()
    print('=== BLOCKER INTEGRATION TESTS (synthetic fixtures) ===')

    test_b1_metric_log_noncolliding_ag_rows()
    test_b1_metric_log_value_gate_catches_ag_stale()
    test_b3_verify_catches_injected_crossrefs()
    test_b3_golden_passes()
    test_b4_ag_wc_rows_preserved()
    test_b5_dynamic_counts_tolerate_new_records()
    test_b7_pre_scan_aborts_on_drift()
    test_umbrella_ungated_warg_data_red()
    test_umbrella_identical_seed_exempt()

    print()
    print(f'All tests so far: {PASS} passed, {FAIL} failed')

    if args.live:
        print()
        print('=== LIVE TESTS (real DB copies) ===')

        print()
        print('-- dry-run VERIFY --')
        result = run_live_dryrun(
            args.ag_db, args.warg_db, args.schema_sql, args.migrate_py, args.temp_dir
        )
        print(f'  dry-run ok: {result["ok"]}')
        print(f'  counts: {result["counts"]}')
        if result['failures']:
            print(f'  FAILURES: {result["failures"]}')

        print()
        print('-- idempotency (2nd run) --')
        result2 = run_idempotency(
            args.ag_db, args.warg_db, args.schema_sql, args.migrate_py, args.temp_dir
        )
        same_counts = result['counts'] == result2['counts']
        print(f'  idempotency ok: {result2["ok"]}, counts identical: {same_counts}')
        if not same_counts:
            print(f'  run1: {result["counts"]}')
            print(f'  run2: {result2["counts"]}')

        print()
        print('-- crash-resume (poisoned temp target) --')
        result3 = run_crash_resume(
            args.ag_db, args.warg_db, args.schema_sql, args.migrate_py, args.temp_dir
        )
        print(f'  crash-resume ok: {result3["ok"]}, counts: {result3["counts"]}')

        live_ok = result['ok'] and result2['ok'] and result3['ok'] and same_counts
        print()
        print(f'Live tests: {"ALL PASS" if live_ok else "FAILURES DETECTED"}')
        if not live_ok:
            sys.exit(1)

    if FAIL > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
