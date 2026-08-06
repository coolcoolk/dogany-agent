"""DGN-704 / DGN-704b: OPTIONS button label shortening forcing point.

_shorten_button_label() extracts the action phrase from a full option label,
strips the description clause (em-dash, double-hyphen, space-hyphen-space,
or colon), and trims to a SAFE WEIGHTED WIDTH with a Unicode ellipsis when
still too long. The number prefix is always preserved; the dot in the prefix
is never mistaken for a separator. Short labels pass through unchanged.

DGN-704b (owner iPhone 13 mini measurement, 2026-07-24): the button width
budget is a WEIGHTED sum -- CJK / full-width glyphs count as 1.5, everything
else as 1.0 -- capped at _BUTTON_LABEL_MAX_WIDTH (30.0). That covers roughly
18-20 pure-Korean chars, replacing the old flat 16-character cap that
truncated far more aggressively than the screen actually needs.

build_option_keyboard applies the shortener to the BUTTON TEXT only; the
full original label never becomes the button text. callback_data and
resolve_choice are unaffected (index-based "opt:{i}" fallback for CJK labels).
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Satisfy telegram import when the real package is absent, without poisoning
# tests that import the real package (same guard pattern as DGN-665 tests).
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
    build_option_keyboard,
    resolve_choice,
    _BUTTON_LABEL_MAX_WIDTH,
)


# ---------------------------------------------------------------------------
# (0) weighted-width helper (DGN-704b)
# ---------------------------------------------------------------------------


class TestLabelWidth:
    def test_ascii_is_one_each(self):
        assert _label_width("proceed") == 7.0

    def test_cjk_is_one_and_a_half_each(self):
        assert _label_width("가나다") == 4.5

    def test_mixed_weighting(self):
        # "a가" -> 1.0 + 1.5
        assert _label_width("a가") == 2.5

    def test_ellipsis_counts_as_one(self):
        # U+2026 is East-Asian-Ambiguous -> weighted 1.0, matching the reserve.
        assert _label_width("…") == 1.0

    def test_budget_is_thirty(self):
        assert _BUTTON_LABEL_MAX_WIDTH == 30.0


# ---------------------------------------------------------------------------
# (a) em-dash / double-hyphen / space-hyphen-space stripping
# ---------------------------------------------------------------------------


class TestEmDashShortening:
    def test_unicode_em_dash(self):
        label = "1. 이관 실행—파일 전체를 대상 디렉토리로 이동"
        assert _shorten_button_label(label) == "1. 이관 실행"

    def test_ascii_double_hyphen(self):
        label = "1. 이관 실행 -- 파일 전체를 대상 디렉토리로 이동"
        assert _shorten_button_label(label) == "1. 이관 실행"

    def test_double_hyphen_no_spaces(self):
        label = "2. 작업 재시도--재시작 후 처음부터"
        assert _shorten_button_label(label) == "2. 작업 재시도"

    def test_space_hyphen_space(self):
        label = "3. 잠시 대기 - 다음 단계 확인 후 진행"
        assert _shorten_button_label(label) == "3. 잠시 대기"


# ---------------------------------------------------------------------------
# (b) colon stripping
# ---------------------------------------------------------------------------


class TestColonShortening:
    def test_colon_separator(self):
        label = "1. 실행: 현재 설정으로 바로 배포"
        assert _shorten_button_label(label) == "1. 실행"

    def test_colon_ascii_label(self):
        label = "2. proceed: run with current config"
        assert _shorten_button_label(label) == "2. proceed"

    def test_colon_result_within_budget(self):
        label = "1. go: long description that would exceed the budget on its own"
        result = _shorten_button_label(label)
        assert result == "1. go"
        assert _label_width(result) <= _BUTTON_LABEL_MAX_WIDTH


# ---------------------------------------------------------------------------
# (c) DGN-704b: Skull-length labels that used to be over-truncated now FIT
# ---------------------------------------------------------------------------


class TestSkullLengthNowFits:
    def test_medium_korean_label_not_truncated(self):
        """~15-CJK label (was cut at flat-16) now stays whole under weighted-30."""
        label = "3. 워그 크로스에이전트 포집 스펙 먼저"
        result = _shorten_button_label(label)
        assert result == label, f"should not truncate, got {result!r}"
        assert _label_width(result) <= _BUTTON_LABEL_MAX_WIDTH

    def test_eighteen_korean_chars_fit(self):
        """18 pure-Korean body chars + '1. ' = width 30.0 exactly -> unchanged."""
        label = "1. " + "가" * 18  # 3 + 18*1.5 = 30.0
        result = _shorten_button_label(label)
        assert result == label
        assert _label_width(result) == 30.0


# ---------------------------------------------------------------------------
# (d) genuine overflow trimming + Unicode ellipsis
# ---------------------------------------------------------------------------


class TestTrimWithEllipsis:
    def test_overlong_after_separator_strip_gets_trimmed(self):
        label = "1. 이것은아주아주아주길고긴액션구절이라반드시잘려야만하는것이다 -- 설명부"
        result = _shorten_button_label(label)
        assert _label_width(result) <= _BUTTON_LABEL_MAX_WIDTH, f"result={result!r}"
        assert result.endswith("…"), f"expected ellipsis, got {result!r}"

    def test_overlong_no_separator_gets_trimmed(self):
        label = "2. 이것은분리기없는아주아주긴라벨이라반드시잘려야하는것이다정말로요"
        result = _shorten_button_label(label)
        assert _label_width(result) <= _BUTTON_LABEL_MAX_WIDTH, f"result={result!r}"
        assert result.endswith("…"), f"expected ellipsis, got {result!r}"

    def test_trimmed_width_within_budget(self):
        label = "1. " + "가" * 20  # 3 + 30 = 33 > 30 -> trimmed
        result = _shorten_button_label(label)
        assert _label_width(result) <= _BUTTON_LABEL_MAX_WIDTH
        assert result.endswith("…")

    def test_ellipsis_is_unicode_not_ascii(self):
        label = "1. " + "나" * 25
        result = _shorten_button_label(label)
        assert "…" in result
        assert "..." not in result

    def test_number_prefix_preserved_after_trim(self):
        label = "3. " + "다" * 25
        result = _shorten_button_label(label)
        assert result.startswith("3. "), f"prefix lost, got {result!r}"


# ---------------------------------------------------------------------------
# (e) number prefix dot NOT treated as separator
# ---------------------------------------------------------------------------


class TestPrefixDotNotSeparator:
    def test_prefix_dot_not_split(self):
        assert _shorten_button_label("1. 실행") == "1. 실행"

    def test_prefix_with_paren_not_split(self):
        assert _shorten_button_label("2) 재시도") == "2) 재시도"

    def test_prefix_dot_with_colon_in_body(self):
        assert _shorten_button_label("1. 실행: 빠르게") == "1. 실행"


# ---------------------------------------------------------------------------
# (f) short label passthrough
# ---------------------------------------------------------------------------


class TestShortLabelPassthrough:
    def test_ascii_short_unchanged(self):
        assert _shorten_button_label("1. proceed") == "1. proceed"

    def test_short_korean_unchanged(self):
        assert _shorten_button_label("2. 빠른 처리") == "2. 빠른 처리"

    def test_no_prefix_short_unchanged(self):
        assert _shorten_button_label("go ahead") == "go ahead"


# ---------------------------------------------------------------------------
# (g) build_option_keyboard + resolve_choice
# ---------------------------------------------------------------------------


class TestBuildKeyboardAndResolve:
    def _extract_buttons(self, markup):
        buttons = []
        for row in markup.inline_keyboard:
            for btn in row:
                buttons.append((btn.text, btn.callback_data))
        return buttons

    def test_button_text_is_shortened_body_not_truncated(self):
        options = [
            "이관 실행 -- 파일 전체를 대상 디렉토리로 이동하고 원본 삭제",
            "잠시 대기 -- 다음 단계 확인 후 진행",
        ]
        markup = build_option_keyboard(options)
        buttons = self._extract_buttons(markup)
        assert "--" not in buttons[0][0]
        assert "--" not in buttons[1][0]
        for text, _ in buttons:
            assert _label_width(text) <= _BUTTON_LABEL_MAX_WIDTH, f"too wide: {text!r}"

    def test_button_text_is_shortened_overlong(self):
        options = ["이것은아주아주아주길고긴액션구절이라반드시잘려야만하는것이다정말로"]
        markup = build_option_keyboard(options)
        buttons = self._extract_buttons(markup)
        text, _ = buttons[0]
        assert _label_width(text) <= _BUTTON_LABEL_MAX_WIDTH, f"too wide: {text!r}"
        assert text.endswith("…"), f"expected ellipsis: {text!r}"

    def test_resolve_choice_finds_correct_button_by_callback(self):
        options = [
            "이관 실행 -- 파일 이동 설명이 길어서 잘림한다",
            "잠시 대기 -- 다음 확인 후 진행",
        ]
        markup = build_option_keyboard(options)
        kb = markup.inline_keyboard
        btn1 = kb[0][0]
        assert getattr(btn1, "callback_data", None) == "opt:1"
        choice = resolve_choice("opt:1", kb)
        assert choice != "1"
        assert choice.startswith("1.")
        assert _label_width(choice) <= _BUTTON_LABEL_MAX_WIDTH

    def test_resolve_choice_second_option(self):
        options = [
            "이관 실행",
            "잠시 대기 -- 다음 확인 후 진행하고 결과를 보고한다",
        ]
        markup = build_option_keyboard(options)
        kb = markup.inline_keyboard
        choice = resolve_choice("opt:2", kb)
        assert choice.startswith("2.")
        assert _label_width(choice) <= _BUTTON_LABEL_MAX_WIDTH
        assert "--" not in choice

    def test_short_ascii_options_callback_data_full(self):
        options = ["proceed", "hold"]
        markup = build_option_keyboard(options)
        buttons = self._extract_buttons(markup)
        assert buttons[0][1] == "opt:1. proceed"
        assert buttons[1][1] == "opt:2. hold"
        kb = markup.inline_keyboard
        assert resolve_choice("opt:1. proceed", kb) == "1. proceed"
        assert resolve_choice("opt:2. hold", kb) == "2. hold"

    def test_display_options_full_label_not_shortened(self):
        """Body option list keeps the FULL label -- only button text is abbreviated."""
        from bridge.options import strip_consumed_options

        text = (
            "1. 이관 실행 -- 파일 전체를 대상 디렉토리로 이동\n"
            "2. 잠시 대기 -- 다음 단계 확인 후 진행\n"
            "[[OPTIONS]]\n"
        )
        _, options = strip_consumed_options(text)
        assert "이관 실행 -- 파일 전체를 대상 디렉토리로 이동" in options[0]
        assert "잠시 대기 -- 다음 단계 확인 후 진행" in options[1]
