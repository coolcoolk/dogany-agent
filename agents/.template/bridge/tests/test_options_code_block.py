"""DGN-085: extract_options and has_numbered_list must ignore fenced code blocks.

Root cause: when a response has a code block containing numbered items AND an
actual [[OPTIONS]] list, the regex scanned both sets. The consecutive-numbering
check (e.g. 1,2,1,2) failed, extract_options returned [], and no keyboard was
built. Fix: strip fenced code blocks before scanning (bridge/options.py).
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

from bridge.options import extract_options, has_numbered_list  # noqa: E402

# Response: code block with numbered steps, then an actual options list.
_CODE_THEN_OPTIONS = (
    "Here is code:\n\n"
    "```python\n"
    "1. read input\n"
    "2. transform data\n"
    "```\n\n"
    "Pick one:\n\n"
    "1. Run it\n"
    "2. Skip it\n"
)

# Response: only a code block with numbered items, no prose options list.
_CODE_ONLY_NUMBERED = "```python\n1. step_one()\n2. step_two()\n```\n"

# Response: plain prose with a numbered list, no code blocks.
_PROSE_OPTIONS = "Choose:\n\n1. Option A\n2. Option B\n"

# Response: prose options list preceded by a three-item code block whose items
# start at 1 -- worst-case for the old regex (max interference).
_THREE_CODE_THEN_TWO_OPTIONS = (
    "```\n1. alpha\n2. beta\n3. gamma\n```\n\n"
    "1. Do this\n"
    "2. Do that\n"
)


class TestExtractOptions:
    def test_ignores_numbered_items_in_code_block(self):
        result = extract_options(_CODE_THEN_OPTIONS)
        assert result == ["Run it", "Skip it"], f"got {result}"

    def test_no_false_match_from_code_only(self):
        assert extract_options(_CODE_ONLY_NUMBERED) == []

    def test_pure_prose_still_works(self):
        assert extract_options(_PROSE_OPTIONS) == ["Option A", "Option B"]

    def test_three_code_items_then_two_prose_options(self):
        result = extract_options(_THREE_CODE_THEN_TWO_OPTIONS)
        assert result == ["Do this", "Do that"], f"got {result}"

    def test_returns_empty_for_empty_string(self):
        assert extract_options("") == []

    def test_single_option_now_accepted(self):
        # DGN-325: single-item lists are now valid when [[OPTIONS]] marker is present
        assert extract_options("1. Only one option\n") == ["Only one option"]

    def test_nonconsecutive_prose_numbering_returns_empty(self):
        assert extract_options("1. A\n3. B\n") == []


class TestHasNumberedList:
    def test_code_block_only_numbering_not_detected(self):
        assert not has_numbered_list(_CODE_ONLY_NUMBERED)

    def test_prose_numbering_detected(self):
        assert has_numbered_list(_PROSE_OPTIONS)

    def test_mixed_code_and_prose_uses_prose_count(self):
        # _CODE_THEN_OPTIONS has 2 prose numbered lines -> True
        assert has_numbered_list(_CODE_THEN_OPTIONS)

    def test_single_prose_numbered_line_not_detected(self):
        assert not has_numbered_list("1. Just one\n")
