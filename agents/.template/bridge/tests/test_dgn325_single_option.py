"""DGN-325: extract_options must accept SINGLE-item numbered lists.

When [[OPTIONS]] marker is explicit, a reply like "1. 다음 동작\n[[OPTIONS]]" should
render a single tappable button. This requires extract_options() to accept >=1 item
(not just >=2) and validate consecutive-from-1 numbering.

Classifier gate (has_numbered_list) remains conservative at >=2 to avoid false
positives on incidental single-item lists. This test covers extract_options only.
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


class TestExtractOptionsSingleItem:
    def test_single_numbered_item_accepted(self):
        """Single item "1. label" should extract as ["label"]."""
        result = extract_options("1. 다음 동작\n")
        assert result == ["다음 동작"], f"got {result}"

    def test_single_item_with_prose(self):
        """Single item with surrounding prose should extract."""
        text = "What do you want to do next?\n\n1. Continue the process\n"
        result = extract_options(text)
        assert result == ["Continue the process"], f"got {result}"

    def test_single_item_with_punctuation_variants(self):
        """Single item with alternative punctuation (e.g. "1) label")."""
        result = extract_options("1) Take action\n")
        assert result == ["Take action"], f"got {result}"

    def test_zero_items_returns_empty(self):
        """No numbered items returns []."""
        result = extract_options("Just some prose, no options.\n")
        assert result == [], f"got {result}"

    def test_single_item_in_code_block_only_returns_empty(self):
        """Single numbered item inside code block only should return []."""
        text = "```python\n1. code_step()\n```\n"
        result = extract_options(text)
        assert result == [], f"got {result}"

    def test_single_item_plus_code_block_returns_single_item(self):
        """Code block with single numbered item + prose single item -> extract prose one."""
        text = "```\n1. code snippet\n```\n\n1. Prose action\n"
        result = extract_options(text)
        assert result == ["Prose action"], f"got {result}"

    def test_nonconsecutive_starting_at_two(self):
        """List starting at 2 (not 1) returns []."""
        result = extract_options("2. Missing the first item\n")
        assert result == [], f"got {result}"

    def test_nonconsecutive_one_then_three(self):
        """List with gap (1 then 3) returns []."""
        result = extract_options("1. First\n3. Skip two\n")
        assert result == [], f"got {result}"

    def test_two_items_still_works(self):
        """Two items should still extract (backward compat)."""
        result = extract_options("1. Option A\n2. Option B\n")
        assert result == ["Option A", "Option B"], f"got {result}"

    def test_three_items_still_works(self):
        """Three items should still extract (backward compat)."""
        result = extract_options("1. A\n2. B\n3. C\n")
        assert result == ["A", "B", "C"], f"got {result}"

    def test_empty_string_returns_empty(self):
        """Empty string returns []."""
        result = extract_options("")
        assert result == [], f"got {result}"

    def test_single_item_with_whitespace_normalization(self):
        """Single item with leading/trailing spaces should be stripped."""
        result = extract_options("1.   Padded label   \n")
        assert result == ["Padded label"], f"got {result}"
