"""DGN-704 / DGN-704b / DGN-790: OPTIONS button label shortening + output integration.

_shorten_button_label() extracts the action phrase from a full option label,
strips the description clause (em-dash, double-hyphen, or space-hyphen-space --
NOT colon, DGN-790 Q4), and returns the result. Under DGN-790 part2, manual
trimming with "..." is removed: labels at or under the hard cap (40.0 weighted)
are returned as-is; only runaway labels above the hard cap are force-trimmed
(raw cut, no ellipsis appended -- Telegram client supplies its own ellipsis).

DGN-790 item 4 (DGN-720): strip_consumed_options keeps the full option run in
the display body when any label has a description clause, so the user sees the
explanatory text above the buttons. Bare-phrase-only runs are still dropped.

DGN-704b (on-device width measurement, DGN-779): the
button width budget is a WEIGHTED sum -- CJK / full-width glyphs count as 1.5,
whitespace as 0.4, everything else as 1.0 -- generation contract at 31.0.

build_option_keyboard applies the shortener to the BUTTON TEXT only; the full
original label never becomes the button text. callback_data and resolve_choice
are unaffected (index-based "opt:{i}" fallback for CJK labels).
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
    _label_has_description,
    build_option_keyboard,
    resolve_choice,
    strip_consumed_options,
    _BUTTON_LABEL_MAX_WIDTH,
    _BUTTON_LABEL_HARD_CAP,
)


# ---------------------------------------------------------------------------
# (0) weighted-width helper (DGN-704b / DGN-779 part1)
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

    def test_hard_cap_is_forty(self):
        # DGN-790 part2: hard cap for runaway safeguard.
        assert _BUTTON_LABEL_HARD_CAP == 40.0


# ---------------------------------------------------------------------------
# (a) em-dash / double-hyphen / space-hyphen-space stripping (unchanged)
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
# (b) colon NO LONGER a separator (DGN-790 Q4)
# ---------------------------------------------------------------------------


class TestColonNotSeparator:
    def test_colon_label_kept_whole_korean(self):
        # DGN-790: colon removed from _LABEL_SEPARATORS.
        # "실행: 현재 설정으로 바로 배포" stays whole -- colon is not a boundary.
        label = "1. 실행: 현재 설정으로 바로 배포"
        result = _shorten_button_label(label)
        assert ":" in result, f"colon should survive, got {result!r}"
        # Label is short enough to stay whole.
        assert result == label, f"expected whole label, got {result!r}"

    def test_colon_label_kept_whole_ascii(self):
        label = "2. proceed: run with current config"
        result = _shorten_button_label(label)
        assert ":" in result, f"colon should survive, got {result!r}"
        assert result == label

    def test_colon_haiku_classifier_style(self):
        # Haiku sometimes outputs "label: description" style -- should keep whole.
        label = "1. 계약 확정: telegram.md 초안 락 후 코드 진행"
        result = _shorten_button_label(label)
        assert ":" in result, f"colon split incorrectly, got {result!r}"


# ---------------------------------------------------------------------------
# (c) DGN-704b: labels now fit under weighted-31 generation contract
# ---------------------------------------------------------------------------


class TestSkullLengthNowFits:
    def test_medium_korean_label_not_truncated(self):
        """~15-CJK label stays whole under weighted-31."""
        label = "3. 워그 크로스에이전트 포집 스펙 먼저"
        result = _shorten_button_label(label)
        assert result == label, f"should not truncate, got {result!r}"
        assert _label_width(result) <= _BUTTON_LABEL_MAX_WIDTH

    def test_eighteen_korean_chars_fit(self):
        """18 pure-Korean body chars + '1. ' -> under contract width."""
        label = "1. " + "가" * 18  # well under 31 weighted
        result = _shorten_button_label(label)
        assert result == label


# ---------------------------------------------------------------------------
# (d) DGN-790 part2: no manual ellipsis trim; hard cap is 40 (runaway only)
# ---------------------------------------------------------------------------


class TestHardCapBehavior:
    def test_label_between_31_and_40_not_trimmed(self):
        """Labels in the 31-40 weighted range are NOT trimmed (DGN-790 part2)."""
        # 21 CJK chars = 31.5 weighted; plus "1. " prefix = ~32.something
        label = "1. " + "가" * 21
        width = _label_width(label)
        assert width > 31.0, f"precondition: width={width}"
        assert width <= 40.0, f"precondition: width={width}"
        result = _shorten_button_label(label)
        assert result == label, f"should not trim in 31-40 range, got {result!r}"
        assert "…" not in result, "no ellipsis expected (DGN-790 part2)"

    def test_overlong_after_separator_strip_no_ellipsis(self):
        """After sep-strip, a still-long label under hard cap returns as-is."""
        # 20 CJK action phrase = 30.0 weighted -- under hard cap 40.
        label = "1. " + "가" * 20 + " -- 설명부분"
        result = _shorten_button_label(label)
        assert "…" not in result, f"no ellipsis expected, got {result!r}"
        assert "--" not in result, "sep-strip should still remove description"

    def test_overlong_no_separator_under_40_not_trimmed(self):
        """No-separator label under hard cap (<=40) returns whole, no ellipsis."""
        label = "2. " + "가" * 20  # width ~32 -- under hard cap
        width = _label_width(label)
        assert width <= 40.0, f"precondition: width={width}"
        result = _shorten_button_label(label)
        assert result == label, f"should not trim under 40, got {result!r}"
        assert "…" not in result

    def test_hard_cap_exceeded_raw_cut_no_ellipsis(self):
        """Labels above hard cap (40) are raw-cut; no '...' or '...' appended."""
        # 28 CJK chars * 1.5 = 42 > 40 hard cap
        label = "1. " + "가" * 28
        result = _shorten_button_label(label)
        assert "…" not in result, f"no ellipsis on hard-cap cut, got {result!r}"
        assert "..." not in result
        assert _label_width(result) <= _BUTTON_LABEL_HARD_CAP

    def test_hard_cap_exceeded_prefix_preserved(self):
        """Number prefix is preserved even after hard-cap raw cut."""
        label = "3. " + "다" * 30  # way over 40
        result = _shorten_button_label(label)
        assert result.startswith("3. "), f"prefix lost, got {result!r}"
        assert _label_width(result) <= _BUTTON_LABEL_HARD_CAP


# ---------------------------------------------------------------------------
# (e) number prefix dot NOT treated as separator (DGN-704c)
# ---------------------------------------------------------------------------


class TestPrefixDotNotSeparator:
    def test_prefix_dot_not_split(self):
        assert _shorten_button_label("1. 실행") == "1. 실행"

    def test_prefix_with_paren_not_split(self):
        assert _shorten_button_label("2) 재시도") == "2) 재시도"

    def test_prefix_dot_with_colon_in_body_kept_whole(self):
        # DGN-790: colon is no longer a separator; the whole label stays.
        result = _shorten_button_label("1. 실행: 빠르게")
        assert ":" in result, f"colon should survive, got {result!r}"
        assert result == "1. 실행: 빠르게"


# ---------------------------------------------------------------------------
# (e2) separator at label START must not erase the body (DGN-704c)
# ---------------------------------------------------------------------------


class TestSeparatorAtLabelStart:
    """Regression: a body that STARTS with a separator (e.g. "--html ...")
    used to collapse to an empty body -- the button showed only "2. ".
    The separator split now applies only when an action phrase precedes it;
    a leading separator is treated as part of the label body."""

    def test_double_hyphen_start_keeps_body(self):
        assert _shorten_button_label("2. --html 경유") == "2. --html 경유"

    def test_em_dash_start_keeps_body(self):
        assert _shorten_button_label("1. —강제 진행") == "1. —강제 진행"

    def test_colon_start_keeps_body(self):
        # Colon is no longer a separator at all (DGN-790), so the whole
        # label is kept regardless of colon position.
        result = _shorten_button_label("3. :옵션 유지")
        assert ":옵션 유지" in result, f"body lost, got {result!r}"

    def test_repro_dgn_number_only_button(self):
        # Original repro: "2. --html opt-in ..." -> button text was "2. ".
        result = _shorten_button_label("2. --html opt-in 경유 (변환 후 전송)")
        assert result.strip() != "2.", f"body erased, got {result!r}"
        assert "--html" in result, f"body lost, got {result!r}"

    def test_no_prefix_leading_separator_keeps_body(self):
        assert _shorten_button_label("--force 재시도") == "--force 재시도"

    def test_leading_separator_overlong_within_hard_cap_no_ellipsis(self):
        # Leading separator + body under hard cap 40: returned as-is (no "...").
        # 20 CJK with "1. --" prefix = ~33 weighted -- under 40.
        label = "1. --" + "가" * 20
        result = _shorten_button_label(label)
        assert "…" not in result, f"no ellipsis expected under hard cap: {result!r}"

    def test_normal_split_still_works(self):
        # Guard must not weaken the normal case: action phrase before the
        # separator still gets the description clause stripped.
        assert _shorten_button_label("1. 실행 -- 설명 문구") == "1. 실행"


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

    def test_button_text_strips_description_after_dash(self):
        options = [
            "이관 실행 -- 파일 전체를 대상 디렉토리로 이동하고 원본 삭제",
            "잠시 대기 -- 다음 단계 확인 후 진행",
        ]
        markup = build_option_keyboard(options)
        buttons = self._extract_buttons(markup)
        assert "--" not in buttons[0][0]
        assert "--" not in buttons[1][0]

    def test_button_text_with_colon_label_stays_whole(self):
        # DGN-790: colon labels no longer split; button keeps the colon.
        options = ["실행: 현재 설정으로 바로 배포", "대기: 확인 후 진행"]
        markup = build_option_keyboard(options)
        buttons = self._extract_buttons(markup)
        assert ":" in buttons[0][0], f"colon should survive button text: {buttons[0][0]!r}"
        assert ":" in buttons[1][0], f"colon should survive button text: {buttons[1][0]!r}"

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
        # Button text no longer has ellipsis (DGN-790 part2).
        assert "--" not in choice

    def test_resolve_choice_second_option(self):
        options = [
            "이관 실행",
            "잠시 대기 -- 다음 확인 후 진행하고 결과를 보고한다",
        ]
        markup = build_option_keyboard(options)
        kb = markup.inline_keyboard
        choice = resolve_choice("opt:2", kb)
        assert choice.startswith("2.")
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
        text = (
            "1. 이관 실행 -- 파일 전체를 대상 디렉토리로 이동\n"
            "2. 잠시 대기 -- 다음 단계 확인 후 진행\n"
            "[[OPTIONS]]\n"
        )
        _, options = strip_consumed_options(text)
        assert "이관 실행 -- 파일 전체를 대상 디렉토리로 이동" in options[0]
        assert "잠시 대기 -- 다음 단계 확인 후 진행" in options[1]


# ---------------------------------------------------------------------------
# (h) DGN-720 / DGN-790: strip_consumed_options body keep vs drop logic
# ---------------------------------------------------------------------------


class TestStripConsumedOptionsBodyKeep:
    """DGN-790 item 4: runs with description labels stay in body; bare runs drop."""

    def test_description_run_kept_in_body(self):
        """Run where any label has a description -> body lines kept."""
        text = (
            "Choose an action:\n"
            "1. 계약 확정 -- telegram.md 초안 락, 코드는 뒤로\n"
            "2. 잠시 대기 -- 다음 단계 검토 후 진행\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert "계약 확정 -- telegram.md 초안 락, 코드는 뒤로" in display
        assert "잠시 대기 -- 다음 단계 검토 후 진행" in display
        assert len(options) == 2

    def test_bare_phrase_run_dropped_from_body(self):
        """Run with no description labels -> body lines dropped (original behavior)."""
        text = (
            "Pick one:\n"
            "1. 실행\n"
            "2. 대기\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert "1. 실행" not in display
        assert "2. 대기" not in display
        assert len(options) == 2

    def test_mixed_run_one_description_keeps_all(self):
        """Even one label with a description keeps the whole run in body."""
        text = (
            "1. 실행\n"
            "2. 대기 -- 확인 후 진행\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert "1. 실행" in display
        assert "대기 -- 확인 후 진행" in display
        assert len(options) == 2

    def test_colon_label_not_treated_as_description(self):
        """Colon is NOT a description separator (DGN-790), so colon labels
        are bare phrases -> run is dropped from body."""
        text = (
            "1. 실행: 빠르게\n"
            "2. 대기: 나중에\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        # No em-dash/double-hyphen/space-hyphen-space -> bare phrases -> drop.
        assert "1. 실행: 빠르게" not in display
        assert len(options) == 2

    def test_haiku_classifier_em_dash_list_body_kept(self):
        """Haiku-style numbered list with em-dash descriptions: body kept per DGN-720."""
        text = (
            "다음 중 선택:\n"
            "1. 스킵 -- 현재 단계 건너뜀\n"
            "2. 진행 -- 계속 실행\n"
            "3. 중단 -- 작업 취소\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert "스킵 -- 현재 단계 건너뜀" in display
        assert "진행 -- 계속 실행" in display
        assert len(options) == 3

    def test_704c_number_only_head_guard_respected(self):
        """DGN-704c regression: body starting with separator is NOT a description."""
        # Label body starts with "--": _label_has_description returns False (head empty).
        text = (
            "1. --force 재시도\n"
            "2. --skip 건너뜀\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        # "--force" at start = not a description boundary -> bare labels -> drop.
        assert "1. --force 재시도" not in display
        assert len(options) == 2


# ---------------------------------------------------------------------------
# (i) _label_has_description predicate (shared with button sep-strip)
# ---------------------------------------------------------------------------


class TestLabelHasDescription:
    def test_em_dash_is_description(self):
        assert _label_has_description("1. 실행 -- 설명") is True

    def test_space_hyphen_is_description(self):
        assert _label_has_description("2. 대기 - 다음 단계") is True

    def test_bare_phrase_no_description(self):
        assert _label_has_description("1. 실행") is False

    def test_colon_not_description(self):
        # DGN-790: colon is not in _LABEL_SEPARATORS.
        assert _label_has_description("1. 실행: 빠르게") is False

    def test_leading_double_hyphen_not_description(self):
        # DGN-704c guard: body starts with separator -> head is empty -> not a boundary.
        assert _label_has_description("2. --html 경유") is False

    def test_leading_em_dash_not_description(self):
        assert _label_has_description("1. —강제 진행") is False

    def test_no_prefix_with_separator_is_description(self):
        # No number prefix, but separator after an action phrase.
        assert _label_has_description("실행 -- 설명 있음") is True
