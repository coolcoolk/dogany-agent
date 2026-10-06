"""DGN-1242: the SAME choice set must not show three times (body x2 + buttons).

Incident shape: the author writes a short label list, then a described
restatement of the same choices, then a labeled marker (or a bare marker with
trailing labels) matching the SHORT list. Path A (_last_option_run) picks the
LAST run -- the described list -- which mismatches the marker labels, and the
old path B (_strip_verbatim_label_block) compared lines to labels with exact
equality, which a "1. " prefix can never satisfy. Both paths missed -> both
lists stayed in the body on top of the buttons.

Fix (owner-confirmed plan 1, 2026-09-02): path B normalizes the numbered
prefix before comparing -- but position-locked (line number must equal the
label's 1-based position) and full-block only, so the DGN-984 no-hijack
invariant is untouched: a partial, reordered, or mid-list match never strips.
The DESCRIBED list survives in the body; the label-only duplicate is removed;
buttons stay.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import os  # noqa: E402

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge.options import (  # noqa: E402
    extract_marker_labels,
    strip_consumed_options,
)


SHORT = ["라이프킷이 값만 저장", "모듈이 각자 저장", "저장 안 함"]
DESC = [
    "라이프킷이 값을 들고, 뜻은 안 붙임 (추천)",
    "각 모듈이 자기 방식으로 -- 대신 같은 값이 여러 군데 생김",
    "저장 안 하고 필요할 때 다시 물음",
]
LABELED = f"[[OPTIONS: {' | '.join(SHORT)}]]"

BODY_TWO_LISTS = (
    "저장 방식을 정해야 합니다.\n"
    "\n"
    + "".join(f"{i}. {s}\n" for i, s in enumerate(SHORT, 1))
    + "\n"
    + "".join(f"{i}. {s}\n" for i, s in enumerate(DESC, 1))
    + "\n선택해 주세요:\n"
)


def _strip(text):
    return strip_consumed_options(text, marker_labels=extract_marker_labels(text))


class TestIncidentRepro:
    def test_labeled_marker_label_list_removed_desc_kept(self):
        """Screenshot shape (c): label list out, described list stays, 3 buttons."""
        display, options = _strip(BODY_TWO_LISTS + LABELED + "\n")
        assert options == SHORT
        assert f"1. {SHORT[0]}" not in display
        assert SHORT[1] not in display.split(DESC[0])[0]  # short list gone above
        for line in DESC:
            assert line in display  # plan 1: the described list survives
        assert "선택해 주세요:" in display

    def test_bare_marker_trailing_labels_both_duplicates_removed(self):
        """Shape (d): numbered label list AND trailing bare block both removed."""
        text = BODY_TWO_LISTS + "[[OPTIONS]]\n" + "\n".join(SHORT) + "\n"
        display, options = _strip(text)
        assert options == SHORT
        assert f"1. {SHORT[0]}" not in display
        assert not any(ln.strip() == SHORT[0] for ln in display.split("\n"))
        for line in DESC:
            assert line in display

    def test_removed_block_leaves_single_blank_seam(self):
        display, _ = _strip(BODY_TWO_LISTS + LABELED + "\n")
        assert "\n\n\n" not in display

    def test_wrong_direction_never_happens(self):
        """The described list must NEVER be the removed one (plan 1 core)."""
        display, _ = _strip(BODY_TWO_LISTS + LABELED + "\n")
        assert display.count(DESC[0]) == 1
        assert display.count(DESC[2]) == 1


class TestNumberFormatVariants:
    def test_dot_paren_spaced_and_cjk_prefixes_all_dedup(self):
        for fmt in ("{}. {}", "{}) {}", "{} . {}", "{}、{}", "{}）{}"):
            body = (
                "고르세요.\n"
                + "".join(fmt.format(i, s) + "\n" for i, s in enumerate(SHORT, 1))
                + f"\n{LABELED}\n"
            )
            display, options = _strip(body)
            assert options == SHORT
            assert SHORT[0] not in display, f"format {fmt!r} missed dedup"
            assert "고르세요." in display


class TestNoHijackInvariants:
    def test_mid_slice_of_larger_list_never_stripped(self):
        """Grill (i): lines 2..3 of an UNRELATED larger list coincide with the
        labels textually -- the position lock (2 != 1) must refuse the match."""
        text = (
            "작업 순서:\n"
            "1. 준비\n"
            f"2. {SHORT[0]}\n"
            f"3. {SHORT[1]}\n"
            "4. 마무리\n"
            f"\n[[OPTIONS: {SHORT[0]} | {SHORT[1]}]]\n"
        )
        display, options = _strip(text)
        assert options == [SHORT[0], SHORT[1]]
        assert f"2. {SHORT[0]}" in display
        assert "1. 준비" in display and "4. 마무리" in display

    def test_reordered_list_never_stripped(self):
        text = (
            f"1. {SHORT[1]}\n"
            f"2. {SHORT[0]}\n"
            f"\n[[OPTIONS: {SHORT[0]} | {SHORT[1]}]]\n"
        )
        display, options = _strip(text)
        assert f"1. {SHORT[1]}" in display

    def test_partial_restatement_never_stripped(self):
        text = (
            f"1. {SHORT[0]}\n"
            f"\n[[OPTIONS: {SHORT[0]} | {SHORT[1]}]]\n"
        )
        display, options = _strip(text)
        assert f"1. {SHORT[0]}" in display

    def test_fenced_duplicate_never_stripped(self):
        text = (
            "예시:\n```\n"
            + "".join(f"{i}. {s}\n" for i, s in enumerate(SHORT, 1))
            + "```\n"
            + f"{LABELED}\n"
        )
        display, options = _strip(text)
        assert options == SHORT
        assert f"1. {SHORT[0]}" in display


class TestLabelWithNumberText:
    def test_label_starting_with_number_word_not_corrupted(self):
        """Grill (iii): '1번 안' has no list punctuation after the digit, so
        the prefix regex must not shave it -- both bare and numbered lines."""
        labels = ["1번 안", "2번 안"]
        text = (
            "1. 1번 안\n"
            "2. 2번 안\n"
            f"\n[[OPTIONS: {labels[0]} | {labels[1]}]]\n"
        )
        display, options = _strip(text)
        assert options == labels
        assert "1번 안" not in display  # duplicate deduped, labels intact

    def test_bare_number_word_lines_exact_match_still_dedups(self):
        labels = ["1번 안", "2번 안"]
        text = f"1번 안\n2번 안\n[[OPTIONS: {labels[0]} | {labels[1]}]]\n"
        display, options = _strip(text)
        assert options == labels
        assert "1번 안" not in display


class TestThreePlusLists:
    def test_duplicate_label_list_twice_both_removed_desc_kept(self):
        """Grill (iv): label list x2 + described list -> only desc survives."""
        dup = "".join(f"{i}. {s}\n" for i, s in enumerate(SHORT, 1))
        desc = "".join(f"{i}. {s}\n" for i, s in enumerate(DESC, 1))
        text = f"머리말\n\n{dup}\n{desc}\n{dup}\n{LABELED}\n"
        display, options = _strip(text)
        assert options == SHORT
        assert SHORT[0] not in display
        for line in DESC:
            assert line in display
