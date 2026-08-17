"""DGN-704 lineage -> DGN-881: OPTIONS button label width contract.

DGN-881 collapses the label parsing pipeline (sep-strip, colon exception,
number-dot guard, leading-separator guard, description predicate) into a
passthrough + overflow fallback:

- width <= 31.0 weighted -> the label is the button text, VERBATIM. Separators
  ("--", em-dash, " - ") are ordinary label characters and survive on the
  button (assertion INVERTED from the DGN-704 era).
- width > 31.0 -> localized number handle (i18n key option_number_handle:
  ko "{n}번" / en "No.{n}"). The number prefix always exists because
  build_option_keyboard prepends "{i}. ".

Retained from the DGN-704b/779 era: the weighted width model (CJK 1.5,
whitespace 0.4, other 1.0) and the 31.0 generation contract.

build_option_keyboard applies the passthrough/handle rule to the BUTTON TEXT
only; callback_data and resolve_choice are unaffected (index-based "opt:{i}"
fallback for CJK labels).
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

import pytest  # noqa: E402

from bridge.config import config  # noqa: E402
from bridge.options import (  # noqa: E402
    _shorten_button_label,
    _label_width,
    build_option_keyboard,
    resolve_choice,
    strip_consumed_options,
    _BUTTON_LABEL_MAX_WIDTH,
)


@pytest.fixture
def ko_locale(monkeypatch):
    """Pin the active locale to ko for handle-text assertions."""
    monkeypatch.setattr(config, "locale", "ko")


# ---------------------------------------------------------------------------
# (0) weighted-width helper (DGN-704b / DGN-779 part1) -- retained verbatim
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
        # U+2026 is East-Asian-Ambiguous -> weighted 1.0.
        assert _label_width("…") == 1.0

    def test_whitespace_counts_as_point_four(self):
        # DGN-779 part1: space counted as 0.4, not 1.0.
        assert abs(_label_width(" ") - 0.4) < 1e-9

    def test_generation_contract_is_thirty_one(self):
        # DGN-779 part1: cap raised from 30.0 to 31.0.
        assert _BUTTON_LABEL_MAX_WIDTH == 31.0

    def test_hard_cap_removed(self):
        # DGN-879: _BUTTON_LABEL_HARD_CAP removed; overflow uses number handle instead.
        import bridge.options as _opts
        assert not hasattr(_opts, "_BUTTON_LABEL_HARD_CAP"), (
            "_BUTTON_LABEL_HARD_CAP must be gone (DGN-879)"
        )


# ---------------------------------------------------------------------------
# (a) passthrough: separators survive on the button (INVERTED vs DGN-704)
# ---------------------------------------------------------------------------


class TestSeparatorPassthrough:
    """DGN-881: no sep-strip. A short label keeps its separator on the button."""

    def test_double_hyphen_label_kept_whole(self):
        label = "1. 이관 실행 -- 원본 삭제"
        assert _label_width(label) <= _BUTTON_LABEL_MAX_WIDTH
        assert _shorten_button_label(label) == label

    def test_em_dash_label_kept_whole(self):
        label = "1. 실행—빠르게"
        assert _label_width(label) <= _BUTTON_LABEL_MAX_WIDTH
        assert _shorten_button_label(label) == label

    def test_space_hyphen_space_label_kept_whole(self):
        label = "3. 잠시 대기 - 확인 후"
        assert _label_width(label) <= _BUTTON_LABEL_MAX_WIDTH
        assert _shorten_button_label(label) == label

    def test_colon_label_kept_whole(self):
        label = "1. 실행: 빠르게"
        assert _shorten_button_label(label) == label

    def test_leading_separator_label_kept_whole(self):
        # Old DGN-704c head-guard territory: now trivially passthrough.
        label = "2. --html 경유"
        assert _shorten_button_label(label) == label


# ---------------------------------------------------------------------------
# (b) passthrough: plain short labels unchanged
# ---------------------------------------------------------------------------


class TestShortLabelPassthrough:
    def test_ascii_short_unchanged(self):
        assert _shorten_button_label("1. proceed") == "1. proceed"

    def test_short_korean_unchanged(self):
        assert _shorten_button_label("2. 빠른 처리") == "2. 빠른 처리"

    def test_medium_korean_label_not_truncated(self):
        """~15-CJK label stays whole under weighted-31."""
        label = "3. 워그 크로스에이전트 포집 스펙 먼저"
        result = _shorten_button_label(label)
        assert result == label, f"should not truncate, got {result!r}"
        assert _label_width(result) <= _BUTTON_LABEL_MAX_WIDTH

    def test_eighteen_korean_chars_fit(self):
        """18 pure-Korean body chars + '1. ' -> under contract width."""
        label = "1. " + "가" * 18  # well under 31 weighted
        assert _shorten_button_label(label) == label


# ---------------------------------------------------------------------------
# (c) overflow -> localized number handle
# ---------------------------------------------------------------------------


class TestOverflowHandle:
    def test_overflow_becomes_handle_ko(self, ko_locale):
        label = "1. " + "가" * 21  # 2.4 + 31.5 = 33.9 > 31.0
        assert _label_width(label) > _BUTTON_LABEL_MAX_WIDTH
        assert _shorten_button_label(label) == "1번"

    def test_overflow_with_separator_becomes_handle_whole_width(self, ko_locale):
        # No sep-strip: the FULL label (action + separator + description) is
        # width-checked; a long description forces the handle even when the
        # action phrase alone would fit.
        label = "1. 이관 실행 -- 파일 전체를 대상 디렉토리로 이동"
        assert _label_width(label) > _BUTTON_LABEL_MAX_WIDTH
        assert _shorten_button_label(label) == "1번"

    def test_no_ellipsis_no_raw_cut(self, ko_locale):
        label = "2. " + "나" * 25
        result = _shorten_button_label(label)
        assert "…" not in result and "..." not in result
        assert result == "2번"


# ---------------------------------------------------------------------------
# (d) build_option_keyboard + resolve_choice
# ---------------------------------------------------------------------------


class TestBuildKeyboardAndResolve:
    def _extract_buttons(self, markup):
        buttons = []
        for row in markup.inline_keyboard:
            for btn in row:
                buttons.append((btn.text, btn.callback_data))
        return buttons

    def test_button_text_keeps_short_separator_labels(self):
        # INVERTED (DGN-881): separators are ordinary characters; short labels
        # keep them on the button verbatim.
        options = ["이관 실행 -- 원본 삭제", "대기 - 확인 후"]
        markup = build_option_keyboard(options)
        buttons = self._extract_buttons(markup)
        assert buttons[0][0] == "1. 이관 실행 -- 원본 삭제"
        assert "--" in buttons[0][0]
        assert buttons[1][0] == "2. 대기 - 확인 후"

    def test_button_text_with_colon_label_stays_whole(self):
        options = ["실행: 현재 설정으로 바로 배포", "대기: 확인 후 진행"]
        markup = build_option_keyboard(options)
        buttons = self._extract_buttons(markup)
        assert ":" in buttons[0][0], f"colon should survive button text: {buttons[0][0]!r}"
        assert ":" in buttons[1][0], f"colon should survive button text: {buttons[1][0]!r}"

    def test_overflow_option_button_is_handle(self, ko_locale):
        options = ["가" * 22, "대기"]  # option 1 overflows with "1. " prefix
        markup = build_option_keyboard(options)
        buttons = self._extract_buttons(markup)
        assert buttons[0][0] == "1번"
        assert buttons[1][0] == "2. 대기"

    def test_resolve_choice_finds_correct_button_by_callback(self):
        options = ["이관 실행", "잠시 대기 후 확인"]
        markup = build_option_keyboard(options)
        kb = markup.inline_keyboard
        # Korean labels exceed the 64-byte callback budget -> index fallback.
        btn2 = kb[1][0]
        assert getattr(btn2, "callback_data", None) in ("opt:2", "opt:2. 잠시 대기 후 확인")
        choice = resolve_choice(btn2.callback_data, kb)
        assert choice == "2. 잠시 대기 후 확인"

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
        """strip_consumed_options returns the FULL raw labels -- the button
        surface is the only place the handle degradation applies."""
        text = (
            "1. 이관 실행 -- 파일 전체를 대상 디렉토리로 이동\n"
            "2. 잠시 대기 -- 다음 단계 확인 후 진행\n"
            "[[OPTIONS]]\n"
        )
        _, options = strip_consumed_options(text)
        assert "이관 실행 -- 파일 전체를 대상 디렉토리로 이동" in options[0]
        assert "잠시 대기 -- 다음 단계 확인 후 진행" in options[1]
