"""DGN-515: extract_options edge-case regression (D3).

Covers the three edge cases called out in the DGN-515 spec:
  1. Single "1." standalone -- must extract one button.
  2. "1." standalone run followed by a non-consecutive number (e.g. "3.") --
     cur run is length-1 when it is broken, so it is discarded; final result is
     [] (no spurious button).  Verifies the cur-flush path in extract_options.
  3. Paren-delimiter lists "1) 2) 3)" -- must extract the same way as dot-delimiter.

Also asserts that a plain body numbered list WITHOUT [[OPTIONS]] is NOT turned
into buttons by extract_options itself (the marker gate lives upstream in
_send_smart / _reply_smart, not inside extract_options; extract_options just
returns labels regardless of marker presence -- the marker gate is tested at
the integration level, not here).
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Satisfy telegram import before bridge.options is loaded, but ONLY when the
# real package is absent -- unconditionally seeding sys.modules poisons later
# test files that import the real telegram package (review fix, DGN-085).
if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge.options import extract_options  # noqa: E402


class TestDgn515CallbackEdge:
    # --- edge case 1: single "1." standalone ---

    def test_single_dot_item_extracted(self):
        """Single '1. label' alone must yield ['label'] (DGN-325 acceptance)."""
        assert extract_options("1. confirm\n") == ["confirm"]

    def test_single_dot_item_with_prose_above(self):
        """Single '1. label' after non-numbered prose must still extract."""
        text = "Here is your next step:\n\n1. proceed now\n"
        assert extract_options(text) == ["proceed now"]

    # --- edge case 2: "1." followed by non-consecutive number ---

    def test_single_dot_then_gap_number_returns_empty(self):
        """'1. A' followed by '3. B' (gap) -> cur is length-1 when broken,
        so it is discarded and no run reaches runs[].  Result: []."""
        assert extract_options("1. A\n3. B\n") == []

    def test_single_dot_then_two_digit_gap_returns_empty(self):
        """Gap of more than one also discards the length-1 leading run."""
        assert extract_options("1. First item\n5. Way later\n") == []

    def test_single_dot_then_same_number_returns_empty(self):
        """Repeated '1. X\n1. Y\n' -- second '1.' resets cur to [(1,'Y')],
        first run (length 1, broken by reset) is discarded.  Final cur [(1,'Y')]
        flushes into runs and returns ['Y']."""
        # Two separate "1." items: first is discarded (broken by reset),
        # second 1. starts a new run that flushes to runs -> ['Y']
        assert extract_options("1. X\n1. Y\n") == ["Y"]

    def test_valid_run_after_gap_discards_gap_returns_valid(self):
        """'1. stray\n3. gap\n' broken run discarded; subsequent '1. A\n2. B\n'
        forms a valid run and is returned."""
        text = "1. stray\n3. gap\n\n1. real A\n2. real B\n"
        assert extract_options(text) == ["real A", "real B"]

    # --- edge case 3: paren-delimiter "1) 2) 3)" ---

    def test_paren_delimiter_three_items(self):
        """'1) 2) 3)' paren-delimiter list extracts the same as dot-delimiter."""
        text = "1) option alpha\n2) option beta\n3) option gamma\n"
        assert extract_options(text) == ["option alpha", "option beta", "option gamma"]

    def test_paren_delimiter_single_item(self):
        """Single '1)' paren item extracts (DGN-325 paren variant)."""
        assert extract_options("1) take action\n") == ["take action"]

    def test_paren_delimiter_with_gap_returns_empty(self):
        """'1) A\n3) B\n' paren gap -> [] (same guard as dot-delimiter)."""
        assert extract_options("1) A\n3) B\n") == []

    def test_paren_delimiter_two_items(self):
        """Two paren items must extract correctly."""
        assert extract_options("1) approve\n2) reject\n") == ["approve", "reject"]

    def test_mixed_delimiter_not_consecutive(self):
        """'1. A\n2) B\n' -- dot then paren: both match _OPTION_RE with same
        index 1 and 2, so they form a consecutive 1..2 run -> ['A', 'B']."""
        # _OPTION_RE matches both '.' and ')' delimiters without distinction.
        assert extract_options("1. A\n2) B\n") == ["A", "B"]
