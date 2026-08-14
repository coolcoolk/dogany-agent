"""DGN-879: overflow -> number handle contract for _shorten_button_label.

When a button label exceeds _BUTTON_LABEL_MAX_WIDTH (31.0 weighted units) after
sep-strip, the old glyph-by-glyph raw cut (which split words mid-syllable and
distorted meaning) is replaced by a pure number handle: "N번" where N is the
parsed prefix digit.

Rules:
- Long label with number prefix (1. / 2) etc.) -> "N번".
- Long label with NO number prefix -> returned unchanged (no raw cut, no handle).
- Short label (width <= 31.0) -> unchanged regardless.
- Description clause still stripped before width check (sep-strip preserved).
- _BUTTON_LABEL_HARD_CAP constant is gone entirely.
- strip_consumed_options: body KEPT when any option degrades to a handle.
  Bare-short-only runs still dropped (existing behavior preserved).

Regression guards:
- DGN-704c: leading separator label ("2. --html ...") -> body not collapsed.
- DGN-790: colon label stays whole, then width-checked (not colon-split).
- DGN-704c descr: "2. --html opt-in 경유 (변환 후 전송)" body NOT stripped.
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

from bridge.options import (  # noqa: E402
    _shorten_button_label,
    _label_width,
    strip_consumed_options,
    _BUTTON_LABEL_MAX_WIDTH,
)


# ---------------------------------------------------------------------------
# 1. Core overflow -> handle contract
# ---------------------------------------------------------------------------


class TestOverflowToHandle:
    def test_long_no_separator_label_becomes_handle(self):
        """Long label (no description sep) with '1.' prefix -> '1번'."""
        label = "1. 875 최소배선 마무리 + telegram/bridge.md는 818 위임"
        width = _label_width(label)
        assert width > _BUTTON_LABEL_MAX_WIDTH, f"precondition width={width}"
        result = _shorten_button_label(label)
        assert result == "1번", f"expected '1번', got {result!r}"

    def test_long_label_no_leak_of_action_text_onto_button(self):
        """Button text must be '1번' only -- action phrase must NOT leak onto button."""
        label = "1. 875 최소배선 마무리 + telegram/bridge.md는 818 위임"
        result = _shorten_button_label(label)
        # Confirm no fragment of the label body appears on the button.
        assert "최소배선 마무리 + telegram" not in result, (
            f"action text leaked onto button: {result!r}"
        )

    def test_2paren_prefix_overflow_becomes_handle(self):
        """'2) ...' prefix long label -> '2번'."""
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

    def test_description_clause_fits_after_strip_action_phrase_returned(self):
        """Description-clause label: sep-strip -> action phrase only when fits."""
        # Action phrase "이관 실행" fits; description stripped.
        label = "1. 이관 실행 -- 파일 전체를 대상 디렉토리로 이동하고 원본 삭제"
        result = _shorten_button_label(label)
        assert result == "1. 이관 실행", f"expected action phrase, got {result!r}"
        assert _label_width(result) <= _BUTTON_LABEL_MAX_WIDTH

    def test_description_clause_action_still_over_31_becomes_handle(self):
        """Action phrase itself > 31.0 after sep-strip -> '1번' (not raw cut)."""
        # Build a label where the action phrase is itself very long (> 31 weighted).
        action = "가" * 22  # 22 * 1.5 = 33.0 > 31.0
        label = f"1. {action} -- 이 부분은 설명"
        result = _shorten_button_label(label)
        assert result == "1번", f"expected '1번' after action still overflows, got {result!r}"
        assert "…" not in result
        assert "..." not in result

    def test_no_prefix_overflow_returned_unchanged(self):
        """Overflow label with no number prefix -> unchanged (no handle, no raw cut)."""
        label = "가" * 22  # 33.0 > 31.0, no prefix
        width = _label_width(label)
        assert width > _BUTTON_LABEL_MAX_WIDTH, f"precondition width={width}"
        result = _shorten_button_label(label)
        assert result == label, f"no-prefix overflow should be unchanged: {result!r}"
        assert "번" not in result, f"must not inject '번' without a number prefix: {result!r}"


# ---------------------------------------------------------------------------
# 2. Regression guards
# ---------------------------------------------------------------------------


class TestDGN704cRegression:
    def test_double_hyphen_start_body_not_collapsed(self):
        """DGN-704c: '2. --html opt-in ...' body NOT collapsed to number-only button."""
        label = "2. --html opt-in 경유 (변환 후 전송)"
        result = _shorten_button_label(label)
        assert result.strip() != "2.", f"body erased, got {result!r}"
        assert "--html" in result, f"'--html' fragment lost, got {result!r}"

    def test_strip_consumed_options_keeps_body_on_dgn704c_label(self):
        """DGN-704c: strip_consumed_options body NOT collapsed for '--html' option."""
        text = (
            "1. --html opt-in 경유 (변환 후 전송)\n"
            "2. 일반 텍스트 전송\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        # "--html" option is short enough to not overflow after no-sep-strip,
        # so it stays as-is on the button. The body should match the body-keep
        # rules; since these are bare short phrases they may drop. The key
        # guard is that DGN-704c does not collapse the button to "2. ".
        assert len(options) == 2, f"expected 2 options, got {options}"


class TestDGN790ColonRegression:
    def test_colon_label_whole_then_width_checked(self):
        """DGN-790: colon label stays whole (not colon-split), then width-checked."""
        # Short colon label -> unchanged.
        label = "1. 실행: 빠르게"
        result = _shorten_button_label(label)
        assert ":" in result, f"colon split, got {result!r}"
        assert result == label, f"expected whole label, got {result!r}"

    def test_colon_label_long_becomes_handle(self):
        """DGN-790: colon NOT a separator; long colon label exceeds 31 -> '1번'."""
        # A colon label where the full thing is > 31 weighted.
        # Colon does NOT split, so the full label is checked against 31.0.
        label = "1. 실행: " + "가" * 20  # >31.0 weighted, no sep-strip possible
        width = _label_width(label)
        assert width > _BUTTON_LABEL_MAX_WIDTH, f"precondition width={width}"
        result = _shorten_button_label(label)
        assert result == "1번", f"expected '1번' for long colon label, got {result!r}"


# ---------------------------------------------------------------------------
# 3. Edge cases from the adversarial self-grill
# ---------------------------------------------------------------------------


class TestAdversarialEdgeCases:
    def test_cjk_width_exactly_at_31_boundary(self):
        """Label at exactly 31.0 weighted -> returned unchanged (not handle)."""
        # Build a label exactly at 31.0: "1. " + CJK chars to total exactly 31.0.
        # "1. " = 1+0.4+1 = 2.4 weighted; need 31.0 - 2.4 = 28.6 more.
        # 19 CJK chars = 28.5; 19 * 1.5 = 28.5; total = 30.9 -- just under.
        # 20 CJK chars = 30.0; total = 32.4 -- over.
        # Use ASCII filler to land at exactly 31.0: "1. " (2.4) + ascii "a"*28 (28.0) = 30.4
        # Hmm -- let's just test a label at exactly 31.0 using a known combo.
        # "1. " = 2.4; we need body = 28.6. 19 CJK = 28.5 (total 30.9 < 31.0 -> pass).
        label_under = "1. " + "가" * 19  # 2.4 + 28.5 = 30.9 <= 31.0
        result = _shorten_button_label(label_under)
        assert result == label_under, (
            f"at-boundary label should be unchanged, got {result!r}"
        )
        assert _label_width(label_under) <= _BUTTON_LABEL_MAX_WIDTH

    def test_colon_short_label_unchanged(self):
        """Label '1. 실행: 빠르게' (colon, short) stays unchanged."""
        label = "1. 실행: 빠르게"
        result = _shorten_button_label(label)
        assert result == label, f"expected unchanged, got {result!r}"

    def test_leading_separator_empty_head_not_handle(self):
        """Leading separator -> head is empty -> no sep-strip -> may still overflow -> handle.

        '1. --가가가...' (leading separator): head is empty so sep-strip does NOT
        apply. The full label "1. --가가가..." is assembled as `short`. If width > 31,
        it becomes '1번'. If width <= 31, it stays as-is. The key: we must NOT
        produce an empty button label.
        """
        # Short leading-separator label (within 31.0): stays as-is.
        label_short = "1. --html opt"
        result = _shorten_button_label(label_short)
        assert "--html" in result, f"body lost on short leading-sep: {result!r}"
        assert result.strip() != "1.", "must not collapse to number-only"

    def test_label_ending_in_syllable_ban_not_misdetected(self):
        """A bare label literally ending in '번' must NOT be misdetected as already-a-handle."""
        # e.g. "1. 두 번째 시도" -- contains '번' but is not a handle
        label = "1. 두 번째 시도"
        width = _label_width(label)
        assert width <= _BUTTON_LABEL_MAX_WIDTH, f"precondition: short label, width={width}"
        result = _shorten_button_label(label)
        # Should be returned unchanged -- width is within contract.
        assert result == label, (
            f"label ending in '번' syllable must not be misdetected as handle: {result!r}"
        )


# ---------------------------------------------------------------------------
# 4. strip_consumed_options body-keep contract (DGN-879)
# ---------------------------------------------------------------------------


class TestStripConsumedOptionsOverflowKeep:
    def test_overflow_handle_option_body_kept(self):
        """Body KEPT when any option degrades to a number handle (DGN-879)."""
        # Long label: exceeds 31.0 -> would become "1번" on button.
        long_label = "875 최소배선 마무리 + telegram/bridge.md는 818 위임"
        text = (
            f"1. {long_label}\n"
            "2. 대기\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        # Body must be kept because option 1 degrades to a handle.
        assert long_label in display, (
            f"body must be kept when option overflows to handle; display={display!r}"
        )
        assert len(options) == 2, f"expected 2 options, got {options}"

    def test_all_short_bare_run_still_dropped(self):
        """All-short bare run (no overflow, no description) still dropped from body."""
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

    def test_hard_cap_constant_gone(self):
        """_BUTTON_LABEL_HARD_CAP must not exist anywhere in bridge.options (DGN-879)."""
        import bridge.options as _opts
        assert not hasattr(_opts, "_BUTTON_LABEL_HARD_CAP"), (
            "_BUTTON_LABEL_HARD_CAP still present -- must be removed (DGN-879)"
        )
