# DGN-285: fresh-instance body-state fabrication guard (direction-A, locked
# 2026-07-23). _hook_body_state_line must inject a body-state line ONLY when
# goal_mode + weight_kg + height_cm all exist as RAW config rows AND goal_mode
# is non-blank. Everything else (fresh mint, partial setup, porous unrelated
# rows, blank goal_mode) must be a silent no-op -- injecting lifekit's
# DEFAULT_STATS (70kg/170cm placeholders) as if they were user facts poisons
# the fresh-user consult interview ("darkwarg" incident, 2026-07-14).
#
# Sandboxed integration tests: a minimal life-engine STUB is synthesized
# into a tmp dir and wired via LIFEKIT_DIR. The real engine left this repo
# with the DGN-803 LS-5 pack extraction, so the old fixture -- which copied
# database/lifekit.py from the repo root, a hardcoded stem AND a hardcoded
# location (the DGN-1238 defect class) -- sat 9/9 red on every
# framework-only clone. The stub is faithful to the contracts the guard
# depends on (see _LIFEKIT_STUB). Zero LLM, zero network.
#
# Run: /usr/bin/python3 -m pytest memory-engine/tests/test_dgn285_bodystate_guard.py -v

import os
import sqlite3
import sys

import pytest

ENGINE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402

# Contracts the guard depends on, reproduced from the packed kit_life.py:
# SCRIPT_DIR/DB_PATH cached at import, get_conn() single entry point, and
# load_body_stats() == {**DEFAULT_STATS, **config rows} -- a fresh instance
# still looks like a complete 70kg/170cm body, which is exactly what the
# raw-row guard exists to see through. Staged under the "lifekit" stem (the
# second KNOWN_STEMS entry), so the resolver's probe is exercised instead of
# any repo path.
_LIFEKIT_STUB = '''\
import os
import sqlite3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "lifekit.db")

DEFAULT_STATS = {
    "weight_kg": 70, "height_cm": 170, "fat_mass_kg": 15, "lean_mass_kg": 55,
    "avg_steps": 8000, "deficit_kcal": 300, "other_neat_kcal": 150,
    "protein_g": 120, "fat_ratio": 0.25,
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def load_body_stats():
    conn = get_conn()
    try:
        rows = dict(conn.execute("SELECT key, value FROM config;").fetchall())
    finally:
        conn.close()
    merged = dict(DEFAULT_STATS)
    for k, v in rows.items():
        try:
            merged[k] = float(v)
        except (TypeError, ValueError):
            merged[k] = v
    return merged


def agg_day(date):
    return {"burn_kcal": 0}


def compute_targets(stats, exercise_kcal=0):
    lean = stats.get("lean_mass_kg") or (
        stats["weight_kg"] - stats.get("fat_mass_kg", 0))
    bmr = 370 + 21.6 * lean
    stride_m = stats["height_cm"] / 100 * 0.415
    walk_km = stats["avg_steps"] * stride_m / 1000
    walk_kcal = 0.5 * stats["weight_kg"] * walk_km
    neat = walk_kcal + stats.get("other_neat_kcal", 150)
    base_goal = bmr - stats["deficit_kcal"] + neat
    eff_goal = base_goal + exercise_kcal
    return {"bmr": round(bmr), "neat": round(neat),
            "deficit": round(stats["deficit_kcal"]),
            "base_goal": round(base_goal), "eff_goal": round(eff_goal)}


def compute_macro_goals(total_kcal, stats):
    protein_g = stats.get("protein_g", 120)
    fat_ratio = stats.get("fat_ratio", 0.25)
    fat_g = round(total_kcal * fat_ratio / 9)
    carb_g = round((total_kcal - protein_g * 4 - total_kcal * fat_ratio) / 4)
    return {"protein": protein_g, "carb": carb_g, "fat": fat_g}
'''

_SCHEMA_STUB = "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);\n"


@pytest.fixture
def lifekit_dir(tmp_path, monkeypatch):
    """Synthesize the stub engine + a fresh config DB in a tmp dir.

    LIFEKIT_DIR points the hook at the sandbox. The engine module caches
    SCRIPT_DIR/DB_PATH at import time, so any previously imported copy is
    evicted before AND after each test to keep imports isolated.
    """
    d = tmp_path / "lifekit"
    d.mkdir()
    (d / "lifekit.py").write_text(_LIFEKIT_STUB, encoding="utf-8")
    conn = sqlite3.connect(d / "lifekit.db")
    conn.executescript(_SCHEMA_STUB)
    conn.commit()
    conn.close()
    monkeypatch.setenv("LIFEKIT_DIR", str(d))
    # _kit_dir_candidates() also falls back to PROJECT_ROOT/database and
    # HERE/../database (memory.py:3715-3729) when the LIFEKIT_DIR candidate
    # doesn't match. LIFEKIT_DIR alone isolates candidate 1 but not those
    # two -- on a live instance memory-engine/ always sits next to a REAL
    # database/ with real kit_life.py+db, so a deleted-db test run there
    # silently falls through the ladder and reads real user data instead of
    # observing "missing" (measured: test_missing_db_file_is_silent read a
    # live goal_mode/weight row through this exact path, 2026-09-17). Clear
    # PROJECT_ROOT and point HERE at a directory with no database/ sibling
    # so every candidate but the sandboxed one is a guaranteed miss.
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setattr(memory, "HERE", str(tmp_path / "no-sibling-database"))
    sys.modules.pop("lifekit", None)
    yield d
    sys.modules.pop("lifekit", None)
    # Drop the sandbox dir the hook inserted, so the next test re-imports fresh.
    try:
        sys.path.remove(str(d))
    except ValueError:
        pass


def _set_config(d, **rows):
    conn = sqlite3.connect(d / "lifekit.db")
    for k, v in rows.items():
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
            (k, str(v)))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# no-op cases: guard must stay silent
# ---------------------------------------------------------------------------

def test_fresh_empty_config_is_silent(lifekit_dir):
    """Fresh mint: db exists, config empty -> no injection at all."""
    assert memory._hook_body_state_line() is None


def test_unrelated_config_rows_are_silent(lifekit_dir):
    """Regression for the porous COUNT(*) guard: unrelated keys must NOT
    unlock injection (the original poison path -- one non-body key made the
    old guard pass and DEFAULT_STATS got injected as user facts)."""
    _set_config(lifekit_dir, updated="2026-08-01", consult_state="pending")
    assert memory._hook_body_state_line() is None


def test_goal_mode_without_body_rows_is_silent(lifekit_dir):
    """goal_mode present but weight_kg/height_cm raw rows absent -> silent.
    This is the gap the old goal_mode-only hotfix could not close: the merged
    load_body_stats() dict always carries default 70/170, so only a raw-row
    check can detect the missing body."""
    _set_config(lifekit_dir, goal_mode="recomp")
    assert memory._hook_body_state_line() is None


def test_body_rows_without_goal_mode_is_silent(lifekit_dir):
    """weight_kg+height_cm present but no goal_mode row -> silent."""
    _set_config(lifekit_dir, weight_kg=82.5, height_cm=178)
    assert memory._hook_body_state_line() is None


def test_partial_body_rows_are_silent(lifekit_dir):
    """goal_mode + weight_kg but no height_cm -> silent (all three required)."""
    _set_config(lifekit_dir, goal_mode="recomp", weight_kg=82.5)
    assert memory._hook_body_state_line() is None


def test_blank_goal_mode_is_silent(lifekit_dir):
    """All three rows exist but goal_mode is blank/whitespace -> silent."""
    _set_config(lifekit_dir, goal_mode="   ", weight_kg=82.5, height_cm=178)
    assert memory._hook_body_state_line() is None


def test_missing_db_file_is_silent(lifekit_dir):
    """lifekit.py present but lifekit.db absent -> silent no-op."""
    os.unlink(lifekit_dir / "lifekit.db")
    assert memory._hook_body_state_line() is None


# ---------------------------------------------------------------------------
# inject case: real user setup
# ---------------------------------------------------------------------------

def test_real_setup_injects_user_values_not_defaults(lifekit_dir):
    """Full real setup -> body-state line with the USER's values; the line
    must never carry the code-default weight (70)."""
    _set_config(lifekit_dir, goal_mode="recomp", weight_kg=82.5, height_cm=178)
    line = memory._hook_body_state_line()
    assert line is not None
    assert line.startswith("[현재 신체/목표]")
    assert "goal_mode=recomp" in line
    assert "weight=82.5" in line
    assert "weight=70 " not in line  # fabricated default must never appear
    assert "eff_goal=" in line and "protein=" in line


def test_compose_omits_body_state_when_fresh(lifekit_dir):
    """_hook_compose on a fresh instance must not contain the body marker."""
    out = memory._hook_compose()
    assert "[현재 신체/목표]" not in out
    assert "[현재 시각]" in out
