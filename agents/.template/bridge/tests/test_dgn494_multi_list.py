"""DGN-494: extract_options must survive a reply with more than one numbered list.

Root cause: extract_options scanned ALL numbered lines and returned [] unless
they formed ONE contiguous run 1..N. A reply containing both a prose numbered
list ("1. 2. 3.") AND the [[OPTIONS]] block numbered list ("1. 2. 3.") produced
matches [1,2,3,1,2,3]; the consecutive check failed and NO buttons rendered.

Fix: return the LAST contiguous 1..N run of numbered lines -- the options list
sits right before the [[OPTIONS]] marker, so it is always the last run. Keeps
the DGN-085 code-block stripping and the DGN-325 single-item acceptance.

This test covers extract_options only.
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


class TestExtractOptionsMultiList:
    def test_duplicate_numbered_lists_returns_last_run(self):
        """Prose numbered list + [[OPTIONS]] numbered list -> last run's labels."""
        text = (
            "Here are your choices:\n"
            "1. proceed with the plan\n"
            "2. hold and review\n"
            "3. cancel entirely\n"
            "\n"
            "Which do you want?\n"
            "\n"
            "1. proceed\n"
            "2. hold\n"
            "3. cancel\n"
            "[[OPTIONS]]\n"
        )
        result = extract_options(text)
        assert result == ["proceed", "hold", "cancel"], f"got {result}"

    def test_single_list_plus_options_marker_still_works(self):
        """One numbered list followed by [[OPTIONS]] still extracts."""
        text = "Pick one:\n\n1. Option A\n2. Option B\n[[OPTIONS]]\n"
        result = extract_options(text)
        assert result == ["Option A", "Option B"], f"got {result}"

    def test_list_then_dashed_status_footer_still_works(self):
        """A numbered options list followed by a dashed status footer still works.

        The footer's dashed lines are not numbered items, so they neither form a
        run nor break the preceding options run.
        """
        text = (
            "1. approve\n"
            "2. reject\n"
            "[[OPTIONS]]\n"
            "\n"
            "----\n"
            "- 1 live task\n"
            "- 0 pending decisions\n"
        )
        result = extract_options(text)
        assert result == ["approve", "reject"], f"got {result}"

    def test_three_numbered_lists_returns_final_run(self):
        """Three separate numbered lists -> only the final contiguous run."""
        text = (
            "1. context item one\n"
            "2. context item two\n"
            "\n"
            "some prose\n"
            "1. background alpha\n"
            "2. background beta\n"
            "\n"
            "1. do this\n"
            "2. do that\n"
            "[[OPTIONS]]\n"
        )
        result = extract_options(text)
        assert result == ["do this", "do that"], f"got {result}"

    def test_single_item_final_run_after_earlier_list(self):
        """Earlier multi-item list + single-item options run -> single label (DGN-325)."""
        text = (
            "1. first background point\n"
            "2. second background point\n"
            "\n"
            "1. the only action\n"
            "[[OPTIONS]]\n"
        )
        result = extract_options(text)
        assert result == ["the only action"], f"got {result}"

    def test_earlier_list_broken_run_ignored(self):
        """A non-1..N earlier fragment does not shadow a valid final run."""
        text = "2. stray non-start item\n\n1. real A\n2. real B\n[[OPTIONS]]\n"
        result = extract_options(text)
        assert result == ["real A", "real B"], f"got {result}"

    def test_length1_run_broken_by_gap_returns_empty(self):
        """A lone "1." immediately followed by a gap number is discarded -> [].

        This preserves the stray-prose guard: "1. A\n3. B\n" is incidental prose
        numbering, not an options list, so no spurious button is emitted. The
        length-1 run is dropped because it is broken by a non-consecutive line
        (mirrors the frozen DGN-085 / DGN-325 assertions).
        """
        assert extract_options("1. A\n3. B\n") == []
        assert extract_options("1. First\n3. Skip two\n") == []

    def test_length1_broken_then_valid_final_run(self):
        """A broken lone "1." earlier does not shadow a real final run."""
        text = "1. stray\n3. gap\n\n1. real A\n2. real B\n[[OPTIONS]]\n"
        result = extract_options(text)
        assert result == ["real A", "real B"], f"got {result}"
