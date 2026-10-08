"""DGN-879 (as collapsed by DGN-881): overflow -> number handle contract.

When a button label exceeds _BUTTON_LABEL_MAX_WIDTH (31.0 weighted units),
the button degrades to a localized number handle (i18n key
option_number_handle: ko "{n}번" / en "No.{n}") instead of a raw glyph cut.

DGN-881 collapsed the old sep-strip pipeline: the width check applies to the
FULL label verbatim (no separator parsing), and the number prefix always
exists because build_option_keyboard prepends "{i}. ". The old sep-strip
combination cases and the no-prefix branch are dead paths and are gone.

Rules:
- width <= 31.0 -> label unchanged (separators and colons are ordinary chars).
- width > 31.0 -> localized handle from the prefix digit.
- strip_consumed_options: body KEPT when any option degrades to a handle
  (the handle alone carries no context). All-short bare runs still drop.
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

import os
os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

import pytest  # noqa: E402

from bridge.config import config  # noqa: E402
from bridge.options import (  # noqa: E402
    _shorten_button_label,
    _label_width,
    _overflows_to_handle,
    strip_consumed_options,
    _BUTTON_LABEL_MAX_WIDTH,
)


@pytest.fixture
def ko_locale(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")


# ---------------------------------------------------------------------------
# 1. Core overflow -> handle contract
# ---------------------------------------------------------------------------


class TestOverflowToHandle:
    def test_long_label_becomes_handle(self, ko_locale):
        """Long label with '1.' prefix -> '1번' (ko)."""
        label = "1. 875 최소배선 마무리 + telegram/bridge.md는 818 위임"
        width = _label_width(label)
        assert width > _BUTTON_LABEL_MAX_WIDTH, f"precondition width={width}"
        result = _shorten_button_label(label)
        assert result == "1번", f"expected '1번', got {result!r}"

    def test_long_label_no_leak_of_action_text_onto_button(self, ko_locale):
        """Button text must be the handle only -- no label fragment leaks."""
        label = "1. 875 최소배선 마무리 + telegram/bridge.md는 818 위임"
        result = _shorten_button_label(label)
        assert "최소배선" not in result, f"action text leaked onto button: {result!r}"

    def test_2paren_prefix_overflow_becomes_handle(self, ko_locale):
        """'2) ...' prefix long label -> '2번' (digit parse, not dot-form only)."""
        label = "2) 875 최소배선 마무리 + telegram/bridge.md는 818 위임"
        width = _label_width(label)
        assert width > _BUTTON_LABEL_MAX_WIDTH, f"precondition width={width}"
        result = _shorten_button_label(label)
        assert result == "2번", f"expected '2번', got {result!r}"

    def test_short_label_unchanged(self):
        """Short label within 31.0 stays unchanged -- handle not applied."""
        label = "1. 875 마무리"
        width = _label_width(label)
        assert width <= _BUTTON_LABEL_MAX_WIDTH, f"precondition width={width}"
        result = _shorten_button_label(label)
        assert result == label, f"short label should be unchanged, got {result!r}"

    def test_no_ellipsis_no_raw_cut(self, ko_locale):
        """Overflow produces the handle -- never an ellipsis or a glyph cut."""
        label = "1. " + "가" * 22 + " -- 이 부분은 설명"
        result = _shorten_button_label(label)
        assert result == "1번", f"expected '1번', got {result!r}"
        assert "…" not in result
        assert "..." not in result

    def test_en_locale_handle(self, monkeypatch):
        """en locale -> 'No.{n}' handle form."""
        monkeypatch.setattr(config, "locale", "en")
        label = "1. " + "가" * 22
        assert _shorten_button_label(label) == "No.1"


# ---------------------------------------------------------------------------
# 2. _overflows_to_handle: one-line width predicate (DGN-881)
# ---------------------------------------------------------------------------


class TestOverflowsPredicate:
    def test_predicate_matches_shortener_boundary(self):
        """The keep-in-body predicate and the button shortener use the SAME
        width comparison -- a label overflows for one iff for the other."""
        under = "1. " + "가" * 19   # 30.9 <= 31.0
        over = "1. " + "가" * 20    # 32.4 > 31.0
        assert _overflows_to_handle(under) is False
        assert _overflows_to_handle(over) is True
        assert _shorten_button_label(under) == under
        assert _shorten_button_label(over) != over

    def test_separator_does_not_shrink_width(self):
        """No sep-strip: a long description after '--' counts toward width."""
        label = "1. 이관 실행 -- 파일 전체를 대상 디렉토리로 이동하고 원본 삭제"
        assert _overflows_to_handle(label) is True

    def test_pipeline_symbols_gone(self):
        """DGN-881: the sep-strip pipeline is deleted wholesale."""
        import bridge.options as _opts
        assert not hasattr(_opts, "_LABEL_SEPARATORS"), (
            "_LABEL_SEPARATORS must be gone (DGN-881)"
        )
        assert not hasattr(_opts, "_label_has_description"), (
            "_label_has_description must be gone (DGN-881)"
        )
        assert not hasattr(_opts, "_BUTTON_LABEL_HARD_CAP"), (
            "_BUTTON_LABEL_HARD_CAP must be gone (DGN-879)"
        )


# ---------------------------------------------------------------------------
# 3. Boundary edges
# ---------------------------------------------------------------------------


class TestBoundaryEdges:
    def test_cjk_width_just_under_31_boundary(self):
        """'1. ' + 19 CJK = 30.9 <= 31.0 -> unchanged."""
        label = "1. " + "가" * 19
        assert _label_width(label) <= _BUTTON_LABEL_MAX_WIDTH
        assert _shorten_button_label(label) == label

    def test_pure_ascii_width_32_becomes_handle(self, ko_locale):
        """Pure-token ASCII label at 32.0 weighted -> handle (no separator at all)."""
        label = "1. " + "a" * 30  # 2.4 + 30.0 = 32.4 > 31.0
        width = _label_width(label)
        assert width > _BUTTON_LABEL_MAX_WIDTH, f"precondition width={width}"
        assert _shorten_button_label(label) == "1번"

    def test_label_ending_in_syllable_ban_not_misdetected(self):
        """A short label literally ending in '번' must pass through unchanged."""
        label = "1. 두 번째 시도"
        width = _label_width(label)
        assert width <= _BUTTON_LABEL_MAX_WIDTH, f"precondition width={width}"
        assert _shorten_button_label(label) == label


# ---------------------------------------------------------------------------
# 4. strip_consumed_options body-keep contract (DGN-879 overflow-keep)
# ---------------------------------------------------------------------------


class TestStripConsumedOptionsOverflowKeep:
    def test_overflow_handle_option_body_kept(self):
        """Body KEPT when any option degrades to a number handle (DGN-879)."""
        long_label = "875 최소배선 마무리 + telegram/bridge.md는 818 위임"
        text = (
            f"1. {long_label}\n"
            "2. 대기\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert long_label in display, (
            f"body must be kept when option overflows to handle; display={display!r}"
        )
        assert len(options) == 2, f"expected 2 options, got {options}"

    def test_all_short_bare_run_still_dropped(self):
        """All-short bare run (no overflow) still dropped from body."""
        text = (
            "Pick one:\n"
            "1. 실행\n"
            "2. 대기\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert "1. 실행" not in display, f"bare short run should be dropped: {display!r}"
        assert "2. 대기" not in display, f"bare short run should be dropped: {display!r}"
        assert len(options) == 2
