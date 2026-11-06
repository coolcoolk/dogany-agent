"""DGN-1177: /btw fork turn budget must match the main-turn budget.

Background: BTW_TURN_TIMEOUT was min(PROCESS_TIMEOUT, 120) on the assumption
that side questions are lightweight. Live evidence (bot.log 2026-08-20,
plus the DGN-953 fork-leak test's own background note) showed the FIRST fork
turn -- cold CLI spawn + full fork of a multi-MB main session history + the
model call on that context -- kept exceeding 120s, so every /btw on a heavy
session died as BTW_FORK_FAILED ("Btw does not work", owner report 2026-08-31).

A fork turn does strictly MORE work than a main turn (which gets the full
PROCESS_TIMEOUT), so it must get the same budget. Fork tasks live in the
separate _btw_fork_tasks set (DGN-922 FIX 4), so a long fork turn never
blocks the main conversation -- raising the budget is latency-safe.
"""

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge.btw import BTW_TURN_TIMEOUT
from bridge.config import PROCESS_TIMEOUT


def test_fork_turn_budget_matches_main_turn_budget():
    # The fork turn does at least as much work as a main turn; its timeout
    # must track PROCESS_TIMEOUT exactly (including env overrides).
    assert BTW_TURN_TIMEOUT == PROCESS_TIMEOUT


def test_fork_turn_budget_not_capped_below_main_turn():
    # Regression pin against reintroducing a min(PROCESS_TIMEOUT, <cap>)
    # style hard cap smaller than the main-turn budget.
    assert BTW_TURN_TIMEOUT >= PROCESS_TIMEOUT
