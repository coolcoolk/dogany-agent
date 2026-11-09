"""Rework A: the bridge writes the decision ledger row when it sends a menu.

No Stop hook and no agent obligation: one sent [[OPTIONS]] menu yields exactly
one pending ledger entry, written by the bridge at send time, and the live menu
record links to it. Sending the same reply again does not add a second row.
"""
import shutil
from pathlib import Path

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge.tests.test_dgn1997_duplicate_menu import _Harness

TEMPLATE = Path(__file__).resolve().parents[2]
MENU = "Ship order?\n\n1. alpha\n2. beta\n[[OPTIONS]]\n"


def _instance(tmp_path):
    shutil.copytree(TEMPLATE / "routines", tmp_path / "routines",
                    ignore=shutil.ignore_patterns("tests", "__pycache__"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/agent.conf").write_text(
        "DECISION_LEDGER_PATH=files/decisions/_DECISIONS.md\n"
        "DECISION_LEDGER_DIR=files/decisions\n")
    return tmp_path


def _rows(root):
    ledger = root / "files/decisions/_DECISIONS.md"
    return ledger.read_text().count("state=pending") if ledger.exists() else 0


def test_sent_menu_gets_exactly_one_bridge_written_ledger_row(monkeypatch, tmp_path):
    root = _instance(tmp_path)
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", root)
    h = _Harness()
    h.menu(MENU, 100)
    assert _rows(root) == 1
    ledger = (root / "files/decisions/_DECISIONS.md").read_text()
    assert "Ship order?" in ledger and "1. alpha" in ledger
    menu = h.bot._live_menu_registry()
    assert menu.live(42) == [100]
    # Same reply again: still one row (no duplicate entry).
    h.menu(MENU, 200)
    assert _rows(root) == 1


def test_no_ledger_configured_writes_nothing(monkeypatch, tmp_path):
    root = _instance(tmp_path)
    (root / "config/agent.conf").write_text("")
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", root)
    _Harness().menu(MENU, 100)
    # ESTATE:BEGIN the legacy ledger directory belongs to the canonical estate
    assert not (root / "worklog").exists()
    # ESTATE:ELSE
    # assert not (root / "files/decisions/_DECISIONS.md").exists()
    # ESTATE:END
