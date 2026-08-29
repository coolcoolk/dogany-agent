"""DGN-881: thin-label contract -- passthrough button + overflow number handle.

The label parsing pipeline (sep-strip, colon exception, 704c head-guard,
description predicate) is deleted. The button rule collapses to:

- width <= 31.0 weighted -> button text = label VERBATIM (separators included).
- width > 31.0 -> localized number handle (i18n option_number_handle:
  ko "{n}번" / en "No.{n}"); the full run stays in the message body
  (strip_consumed_options overflow-keep) so the handle is identifiable.

Body drop/keep matrix (strip_consumed_options):
- all options fit on their buttons + line-adjacent run -> run dropped.
- any option overflows to a handle -> run kept (DGN-879, KILL prevention).
- non-line-adjacent run -> kept (DGN-665 adjacency guard, unchanged).

The recommendation marker is body-only: i18n SSOT keys exist for the contract
docs, but no options.py code path reads them.
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
from bridge.i18n import t_strict  # noqa: E402
from bridge.i18n import en as i18n_en, ko as i18n_ko  # noqa: E402
from bridge.options import (  # noqa: E402
    _shorten_button_label,
    _label_width,
    build_option_keyboard,
    strip_consumed_options,
    _BUTTON_LABEL_MAX_WIDTH,
)


@pytest.fixture
def ko_locale(monkeypatch):
    monkeypatch.setattr(config, "locale", "ko")


@pytest.fixture
def en_locale(monkeypatch):
    monkeypatch.setattr(config, "locale", "en")


def _button_texts(markup):
    return [btn.text for row in markup.inline_keyboard for btn in row]


# ---------------------------------------------------------------------------
# 1. Passthrough: separator-bearing labels stay verbatim on the button
# ---------------------------------------------------------------------------


class TestPassthrough:
    @pytest.mark.parametrize("label", [
        "1. 이관 실행 -- 원본 삭제",
        "2. 대기 - 확인 후",
        "1. 실행—빠르게",
        "3. 실행: 빠르게",
        "2. --html 경유",
    ])
    def test_separator_label_verbatim(self, label):
        assert _label_width(label) <= _BUTTON_LABEL_MAX_WIDTH, "precondition"
        assert _shorten_button_label(label) == label

    def test_keyboard_keeps_separator_labels(self):
        options = ["이관 실행 -- 원본 삭제", "잠시 대기"]
        texts = _button_texts(build_option_keyboard(options))
        assert texts == ["1. 이관 실행 -- 원본 삭제", "2. 잠시 대기"]


# ---------------------------------------------------------------------------
# 2. Overflow -> localized number handle (ko AND en)
# ---------------------------------------------------------------------------


class TestOverflowHandleLocalized:
    def test_ko_handle(self, ko_locale):
        """DGN-1092: one overflowing option degrades the WHOLE keyboard."""
        options = ["가" * 22, "대기"]
        texts = _button_texts(build_option_keyboard(options))
        assert texts[0] == "1번"
        assert texts[1] == "2번"

    def test_en_handle(self, en_locale):
        """DGN-1092: one overflowing option degrades the WHOLE keyboard."""
        options = ["가" * 22, "hold"]
        texts = _button_texts(build_option_keyboard(options))
        assert texts[0] == "No.1"
        assert texts[1] == "No.2"

    def test_pure_token_32_width_overflows(self, ko_locale):
        """A pure token (no separator anywhere) still overflows on width alone."""
        label = "1. " + "a" * 30  # 32.4 weighted
        assert _label_width(label) > _BUTTON_LABEL_MAX_WIDTH
        assert _shorten_button_label(label) == "1번"

    def test_boundary_31_passthrough(self):
        """Exactly at/under the 31.0 boundary -> passthrough, not handle."""
        label = "1. " + "a" * 28  # 2.4 + 28.0 = 30.4
        assert _shorten_button_label(label) == label

    def test_mixed_run_all_degrade_when_any_overflows(self, ko_locale):
        """DGN-1092: mixed run -- ANY overflowing option degrades EVERY
        button in the keyboard, never a mix of full labels and handles."""
        options = ["실행", "가" * 22, "대기"]
        texts = _button_texts(build_option_keyboard(options))
        assert texts == ["1번", "2번", "3번"]

    def test_mixed_run_all_passthrough_when_none_overflow(self):
        """DGN-1092: when no option overflows, every button keeps its full label."""
        options = ["실행", "짧은 라벨", "대기"]
        texts = _button_texts(build_option_keyboard(options))
        assert texts == ["1. 실행", "2. 짧은 라벨", "3. 대기"]


# ---------------------------------------------------------------------------
# 3. >=10 options: two-digit prefix width counts toward the contract
# ---------------------------------------------------------------------------


class TestTwoDigitPrefixWidth:
    def test_prefix_width_flips_overflow_at_position_ten(self, ko_locale):
        """The same body fits under '1. ' (2.4) but overflows under '10. '
        (3.4) -- per-label width still varies by prefix width, but DGN-1092
        makes the KEYBOARD decision bundle-level: option 10 overflowing
        degrades every button in this set, not just its own."""
        body = "가" * 19  # 28.5; +2.4 = 30.9 fits; +3.4 = 31.9 overflows
        options = [body] * 10
        assert _label_width(f"1. {body}") <= _BUTTON_LABEL_MAX_WIDTH
        assert _label_width(f"10. {body}") > _BUTTON_LABEL_MAX_WIDTH
        texts = _button_texts(build_option_keyboard(options))
        assert texts[0] == "1번"
        assert texts[8] == "9번"
        assert texts[9] == "10번", f"two-digit prefix must overflow: {texts[9]!r}"

    def test_ten_short_options_all_passthrough(self):
        options = ["opt" + str(i) for i in range(1, 11)]
        texts = _button_texts(build_option_keyboard(options))
        assert texts[9] == "10. opt10"


# ---------------------------------------------------------------------------
# 4. Body drop/keep matrix (strip_consumed_options)
# ---------------------------------------------------------------------------


class TestBodyDropKeepMatrix:
    def test_all_fit_adjacent_run_dropped(self):
        """Every button shows its label -> body run dropped (thin-label case)."""
        text = "Pick:\n1. 실행\n2. 대기\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert display == "Pick:"
        assert options == ["실행", "대기"]

    def test_separator_label_fitting_run_dropped(self):
        """Separator labels that FIT are ordinary labels now -> run dropped
        (INVERTED vs DGN-720 description-keep)."""
        text = "Pick:\n1. 실행 -- 빠르게\n2. 대기 - 나중에\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert "실행 -- 빠르게" not in display, f"fitting run must drop: {display!r}"
        assert display == "Pick:"
        assert len(options) == 2

    def test_any_overflow_keeps_whole_run(self):
        """One overflowing option -> the WHOLE run stays in the body."""
        long_label = "가" * 22
        text = f"Pick:\n1. {long_label}\n2. 대기\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert long_label in display
        assert "2. 대기" in display
        assert len(options) == 2

    def test_two_digit_prefix_overflow_keeps_run(self):
        """Overflow-keep uses the reconstructed '{n}. ' form: a body that fits
        at position 1 but overflows at position 10 still keeps the run."""
        body = "가" * 19
        lines = [f"{i}. {body}" for i in range(1, 11)]
        text = "\n".join(lines) + "\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert body in display, "run must be kept: option 10 degrades to handle"
        assert len(options) == 10

    def test_non_adjacent_run_kept(self):
        """DGN-665 adjacency guard unchanged: interleaved prose keeps the run."""
        text = "1. 실행\nsome prose\n2. 대기\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert "1. 실행" in display
        assert "some prose" in display
        assert options == ["실행", "대기"]


# ---------------------------------------------------------------------------
# 5. Recommendation marker: i18n SSOT, body-only (no code path)
# ---------------------------------------------------------------------------


class TestRecMarkerSSOT:
    def test_marker_keys_exist_in_both_catalogs(self):
        assert i18n_ko.STRINGS["option_rec_marker"] == "(추천)"
        assert i18n_en.STRINGS["option_rec_marker"] == "(rec)"

    def test_marker_resolves_per_locale(self, monkeypatch):
        monkeypatch.setattr(config, "locale", "ko")
        assert t_strict("option_rec_marker") == "(추천)"
        monkeypatch.setattr(config, "locale", "en")
        assert t_strict("option_rec_marker") == "(rec)"

    def test_handle_keys_exist_in_both_catalogs(self):
        assert i18n_ko.STRINGS["option_number_handle"] == "{n}번"
        assert i18n_en.STRINGS["option_number_handle"] == "No.{n}"

    def test_marker_not_read_by_options_code(self):
        """Body-only contract: options.py must not consume the rec marker."""
        src = (Path(__file__).resolve().parents[1] / "options.py").read_text(
            encoding="utf-8"
        )
        assert "option_rec_marker" not in src, (
            "rec marker is body-only SSOT -- options.py must not read it"
        )
