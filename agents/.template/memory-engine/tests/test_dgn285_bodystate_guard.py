# DGN-285: fresh-instance body-state fabrication guard (direction-A, locked
# 2026-07-23). _hook_body_state_line must inject a body-state line ONLY when
# goal_mode + weight_kg + height_cm all exist as RAW config rows AND goal_mode
# is non-blank. Everything else (fresh mint, partial setup, porous unrelated
# rows, blank goal_mode) must be a silent no-op -- injecting lifekit's
# DEFAULT_STATS (70kg/170cm placeholders) as if they were user facts poisons
# the fresh-user consult interview ("darkwarg" incident, 2026-07-14).
#
# Sandboxed integration tests: the REAL database/lifekit.py + REAL
# database/schema.sql are staged into a tmp dir and wired via LIFEKIT_DIR.
# Zero LLM, zero network, no repo-level lifekit.db touched.
#
# Run: /usr/bin/python3 -m pytest memory-engine/tests/test_dgn285_bodystate_guard.py -v

import os
import shutil
import sqlite3
import sys

import pytest

ENGINE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402

# Repo root = agents/.template/memory-engine/tests -> four levels up.
REPO_ROOT = os.path.normpath(os.path.join(ENGINE_DIR, "..", "..", ".."))
LIFEKIT_SRC = os.path.join(REPO_ROOT, "database", "lifekit.py")
SCHEMA_SRC = os.path.join(REPO_ROOT, "database", "schema.sql")


@pytest.fixture
def lifekit_dir(tmp_path, monkeypatch):
    """Stage the real lifekit module + a fresh schema DB in a tmp dir.

    LIFEKIT_DIR points the hook at the sandbox. The lifekit module caches
    SCRIPT_DIR/DB_PATH at import time, so any previously imported copy is
    evicted before AND after each test to keep imports isolated.
    """
    d = tmp_path / "lifekit"
    d.mkdir()
    shutil.copy(LIFEKIT_SRC, d / "lifekit.py")
    conn = sqlite3.connect(d / "lifekit.db")
    with open(SCHEMA_SRC, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    monkeypatch.setenv("LIFEKIT_DIR", str(d))
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
