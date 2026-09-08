"""DGN-992: [[OPTIONS]] choices must never silently evaporate.

Two mechanisms:
(a) LABELED marker "[[OPTIONS: a | b]]" carries the button labels itself, so
    buttons build regardless of body formatting (bullet body, prose body, no
    list at all). The numbered-run parse is demoted to a body-dedup
    optimization: it strips the body list ONLY when that list provably
    duplicates the marker labels (exact match, line-adjacent, no DGN-879
    overflow-keep). A mismatched run (the DGN-984 hijack shape) stays in the
    body and never feeds the buttons.
(b) Fail-loud: a marker with ZERO buildable buttons logs a WARNING and the
    body is guaranteed intact (text fallback -- the user is never left
    choice-less with the choices also stripped).

Regression guards: bare marker + numbered run (DGN-665 path), classifier
provenance (body kept), DGN-881 number-handle overflow, empty/duplicate/
markdown labels.
"""

import asyncio
import importlib.util
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge.formatting import (  # noqa: E402
    is_options_marker_line,
    parse_options_marker_labels,
    strip_display_markers,
)
from bridge.options import (  # noqa: E402
    build_option_keyboard,
    extract_marker_labels,
    has_options_marker,
    strip_consumed_options,
    strip_options_marker,
)


LABELED = "[[OPTIONS: 흡수 | 분리 | 나중에]]"

BULLET_BODY = (
    "방향을 골라주세요.\n"
    "\n"
    "· 흡수 -- 기존 티켓에 합친다\n"
    "· 분리 -- 새 티켓으로 뗀다\n"
    "· 나중에 -- 보류한다\n"
    f"{LABELED}\n"
)


# ---------------------------------------------------------------------------
# 1. Marker parsing primitives
# ---------------------------------------------------------------------------


class TestMarkerParsing:
    def test_labeled_marker_line_recognized(self):
        assert is_options_marker_line("[[OPTIONS: a | b]]")
        assert is_options_marker_line("  [[OPTIONS: a | b]]  ")
        assert is_options_marker_line("[[OPTIONS]]")

    def test_non_marker_lines_rejected(self):
        assert not is_options_marker_line("[[IDRILL:abc]]")
        assert not is_options_marker_line("prose with [[OPTIONS]] inline")
        assert not is_options_marker_line("[[OPTIONS: a | b]] trailing")

    def test_parse_labels(self):
        assert parse_options_marker_labels("[[OPTIONS: a | b | c]]") == [
            "a", "b", "c",
        ]

    def test_parse_bare_marker_is_none(self):
        assert parse_options_marker_labels("[[OPTIONS]]") is None

    def test_parse_empty_labeled_marker_is_empty(self):
        # Grill: empty labeled marker degrades to label-less, not a crash.
        assert parse_options_marker_labels("[[OPTIONS:]]") == []
        assert parse_options_marker_labels("[[OPTIONS: | ]]") == []

    def test_labeled_marker_inside_code_block_never_arms(self):
        """Grill: a syntax EXAMPLE inside ``` must not become real buttons."""
        text = "예시:\n```\n[[OPTIONS: a | b]]\n```\n본문 계속\n"
        assert extract_marker_labels(text) == []

    def test_extract_marker_labels_last_wins(self):
        text = "[[OPTIONS: x | y]]\nbody\n[[OPTIONS: a | b]]\n"
        assert extract_marker_labels(text) == ["a", "b"]

    def test_has_options_marker_both_forms(self):
        assert has_options_marker("body\n[[OPTIONS]]\n")
        assert has_options_marker("body\n[[OPTIONS: a | b]]\n")
        assert not has_options_marker("plain body\n1. a\n2. b\n")

    def test_strip_options_marker_removes_labeled_line(self):
        clean, had = strip_options_marker(BULLET_BODY)
        assert had
        assert "[[OPTIONS" not in clean
        assert "· 흡수" in clean

    def test_strip_display_markers_drops_labeled_line(self):
        # Streamed drafts must not surface the labeled marker line.
        out = strip_display_markers(BULLET_BODY)
        assert "[[OPTIONS" not in out
        assert "· 흡수" in out


# ---------------------------------------------------------------------------
# 1b. Bare-marker TRAILING labels (DGN-992 rev2 -- the real incident shape)
# ---------------------------------------------------------------------------

# Verbatim shape of the 2026-08-21 incident: labels written on the lines
# UNDER a bare marker; no numbered run, no bullets anywhere in the body.
INCIDENT_BODY = (
    "...본문...\n"
    "\n"
    "[[OPTIONS]]\n"
    "도가니\n"
    "도가니 에이전트\n"
    "도가니 프레임워크\n"
)


class TestBareMarkerTrailingLabels:
    def test_incident_shape_extracts_three_labels(self):
        assert extract_marker_labels(INCIDENT_BODY) == [
            "도가니", "도가니 에이전트", "도가니 프레임워크",
        ]

    def test_incident_shape_options_and_body(self):
        display, options = strip_consumed_options(INCIDENT_BODY)
        assert options == ["도가니", "도가니 에이전트", "도가니 프레임워크"]
        assert display == "...본문..."
        assert "[[OPTIONS" not in display

    def test_blank_line_then_prose_is_not_labels(self):
        """Prose after a blank line under the marker must not become labels."""
        text = "본문\n\n[[OPTIONS]]\n\n이건 이어지는 산문이다.\n"
        assert extract_marker_labels(text) == []

    def test_labeled_marker_beats_trailing_lines(self):
        """Priority 1 > 2: a labeled marker wins; trailing lines unseen."""
        text = "[[OPTIONS: a | b]]\nx\ny\n"
        assert extract_marker_labels(text) == ["a", "b"]

    def test_trailing_numbered_lines_shed_prefix(self):
        """DGN-984 marker-above shape: numbered trailing lines lose the 'N. '
        prefix via the existing option regex -- no double-numbered buttons."""
        text = "본문\n[[OPTIONS]]\n1. 흡수\n2. 분리\n"
        labels = extract_marker_labels(text)
        assert labels == ["흡수", "분리"]
        display, options = strip_consumed_options(text)
        assert options == ["흡수", "분리"]
        # the numbered run duplicates the labels -> deduped from the body
        assert "1. 흡수" not in display

    def test_trailing_sentence_still_becomes_button(self):
        """Guard 4: a sentence-looking trailing line is STILL a button (ugly
        beats evaporated); over-wide ones ride the DGN-881 handle and the
        body keeps the block (DGN-879)."""
        long_line = "이 줄은 라벨이라기보다 문장에 가깝게 길게 이어지는 후행 라인입니다."
        text = f"본문\n[[OPTIONS]]\n{long_line}\n짧은 라벨\n"
        display, options = strip_consumed_options(text)
        assert options == [long_line, "짧은 라벨"]
        assert long_line in display  # overflow-keep: full text stays readable
        kb = build_option_keyboard(options)
        assert kb is not None and len(kb.inline_keyboard) == 2

    def test_trailing_stops_at_foreign_marker(self):
        text = "본문\n[[OPTIONS]]\n라벨A\nsend_file:: /tmp/x.png\n라벨B\n"
        assert extract_marker_labels(text) == ["라벨A"]

    def test_marker_last_line_no_trailing_regression(self):
        """Contract shape (marker last) keeps the numbered-run path."""
        text = "Pick:\n1. proceed\n2. hold\n[[OPTIONS]]\n"
        assert extract_marker_labels(text) == []
        display, options = strip_consumed_options(text)
        assert options == ["proceed", "hold"]


# ---------------------------------------------------------------------------
# 2. strip_consumed_options with marker labels
# ---------------------------------------------------------------------------


class TestStripConsumedWithMarkerLabels:
    def test_bullet_body_gets_marker_labels_body_kept(self):
        """Core DGN-992 case: bullet body -> buttons from marker, body intact."""
        display, options = strip_consumed_options(BULLET_BODY)
        assert options == ["흡수", "분리", "나중에"]
        assert "· 흡수 -- 기존 티켓에 합친다" in display
        assert "[[OPTIONS" not in display

    def test_no_list_at_all_gets_marker_labels(self):
        text = f"프로즈 설명만 있는 본문입니다.\n{LABELED}\n"
        display, options = strip_consumed_options(text)
        assert options == ["흡수", "분리", "나중에"]
        assert display == "프로즈 설명만 있는 본문입니다."

    def test_matching_numbered_run_is_deduped(self):
        text = (
            "골라주세요.\n"
            "1. 흡수\n"
            "2. 분리\n"
            "3. 나중에\n"
            f"{LABELED}\n"
        )
        display, options = strip_consumed_options(text)
        assert options == ["흡수", "분리", "나중에"]
        assert "1. 흡수" not in display
        assert "골라주세요." in display

    def test_mismatched_numbered_run_stays_no_hijack(self):
        """DGN-984 guard: an unrelated numbered run (action plan) must neither
        feed the buttons nor be stripped from the body."""
        text = (
            "제안 순서:\n"
            "1. 스펙 개정\n"
            "2. 그릴\n"
            "3. 락\n"
            "\n"
            "결정해 주세요.\n"
            f"{LABELED}\n"
        )
        display, options = strip_consumed_options(text)
        assert options == ["흡수", "분리", "나중에"]
        assert "1. 스펙 개정" in display
        assert "2. 그릴" in display

    def test_precomputed_marker_labels_param(self):
        """bot paths pass labels precomputed from the ORIGINAL content."""
        stripped_body = "· 흡수\n· 분리\n"
        display, options = strip_consumed_options(
            stripped_body, marker_labels=["흡수", "분리"]
        )
        assert options == ["흡수", "분리"]
        assert display == stripped_body

    def test_empty_labeled_marker_falls_back_to_bare_path(self):
        text = "1. proceed\n2. hold\n[[OPTIONS:]]\n"
        display, options = strip_consumed_options(text)
        assert options == ["proceed", "hold"]
        assert "1. proceed" not in display

    def test_bare_marker_numbered_run_regression(self):
        """DGN-665 path unchanged: bare marker + numbered run -> strip + options."""
        text = "Pick:\n1. proceed\n2. hold\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert options == ["proceed", "hold"]
        assert "1. proceed" not in display
        assert "Pick:" in display

    def test_overflow_label_keeps_body(self):
        """DGN-879 keep rule applies to marker labels: an overflowing label
        keeps the body so the full text stays readable above the handle."""
        long_label = "아주 길고 긴 한국어 라벨이라서 버튼 폭 계약을 확실히 넘어갑니다"
        text = (
            f"1. {long_label}\n"
            "2. 짧은 라벨\n"
            f"[[OPTIONS: {long_label} | 짧은 라벨]]\n"
        )
        display, options = strip_consumed_options(text)
        assert options == [long_label, "짧은 라벨"]
        assert f"1. {long_label}" in display


# ---------------------------------------------------------------------------
# 3. Keyboard build from marker labels
# ---------------------------------------------------------------------------


class TestKeyboardFromMarkerLabels:
    def test_buttons_build_from_marker_labels(self):
        kb = build_option_keyboard(["흡수", "분리", "나중에"])
        assert kb is not None
        rows = kb.inline_keyboard
        assert len(rows) == 3
        assert rows[0][0].text == "1. 흡수"

    def test_markdown_in_label_stripped_on_button(self):
        kb = build_option_keyboard(["**bold** choice", "`code` choice"])
        rows = kb.inline_keyboard
        assert rows[0][0].text == "1. bold choice"
        assert rows[1][0].text == "2. code choice"

    def test_duplicate_labels_still_two_distinct_buttons(self):
        kb = build_option_keyboard(["same", "same"])
        rows = kb.inline_keyboard
        assert rows[0][0].text == "1. same"
        assert rows[1][0].text == "2. same"
        assert rows[0][0].callback_data != rows[1][0].callback_data

    def test_overflow_label_degrades_to_number_handle(self):
        """DGN-1092 regression: over-wide label degrades the WHOLE keyboard
        (bundle-level, not per-button -- DGN-881's per-button decision let a
        keyboard mix full labels and number handles)."""
        from bridge.i18n import t

        long_label = "아주 길고 긴 한국어 라벨이라서 버튼 폭 계약을 확실히 넘어갑니다"
        kb = build_option_keyboard([long_label, "짧은 라벨"])
        rows = kb.inline_keyboard
        assert rows[0][0].text == t("option_number_handle").format(n="1")
        assert rows[1][0].text == t("option_number_handle").format(n="2")


# ---------------------------------------------------------------------------
# 4. Seat wiring: _send_smart / _send_content_artifacts
# ---------------------------------------------------------------------------


def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


def _make_chat_bot(bot_mod, sent):
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()

    async def _send_message(chat_id, text, parse_mode=None, reply_markup=None,
                            link_preview_options=None, disable_notification=None):
        sent.append({"text": text, "reply_markup": reply_markup})

    bot.application.bot = MagicMock()
    bot.application.bot.send_message = AsyncMock(side_effect=_send_message)
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = {}
    return bot


def _run(coro):
    return asyncio.run(coro)


class TestSendSmartSeatLabeledMarker:
    def test_bullet_body_labeled_marker_buttons_sent(self):
        """The DGN-992 incident shape now renders buttons: bullet body kept,
        keyboard built from the marker labels."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, BULLET_BODY, force_options=True))

        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        assert len(bodies) == 1, f"got {sent}"
        assert "· 흡수" in bodies[0]["text"]
        assert "[[OPTIONS" not in bodies[0]["text"]
        kb_rows = buttons[0]["reply_markup"].inline_keyboard
        assert [r[0].text for r in kb_rows] == ["1. 흡수", "2. 분리", "3. 나중에"]

    def test_incident_shape_three_buttons_rendered(self):
        """DGN-992 rev2 seat: the verbatim incident content produces exactly
        3 buttons; the label lines are consumed out of the body."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, INCIDENT_BODY, force_options=True))

        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        kb_rows = buttons[0]["reply_markup"].inline_keyboard
        assert [r[0].text for r in kb_rows] == [
            "1. 도가니", "2. 도가니 에이전트", "3. 도가니 프레임워크",
        ]
        assert len(bodies) == 1
        assert bodies[0]["text"].strip().endswith("...본문...")
        assert "도가니 프레임워크" not in bodies[0]["text"]

    def test_marker_only_content_buttons_carry_turn(self):
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, f"{LABELED}\n", force_options=True))

        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is not None

    def test_bare_marker_numbered_run_regression(self):
        """DGN-665 seat regression: bare marker + numbered run unchanged."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, "Pick:\n1. proceed\n2. hold\n[[OPTIONS]]\n",
                             force_options=True))

        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1
        assert "1. proceed" not in bodies[0]["text"]

    def test_classifier_injected_body_list_kept(self):
        """Classifier provenance regression: buttons render, body list stays."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, "Pick:\n1. proceed\n2. hold\n[[OPTIONS]]\n",
                             force_options=True, classifier_injected=True))

        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        assert "1. proceed" in bodies[0]["text"]

    def test_zero_buttons_fail_loud_body_kept(self, caplog):
        """(b): bare marker + no numbered run -> no buttons, WARNING logged,
        the author's bullet list stays readable as the text fallback."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        content = (
            "골라주세요.\n"
            "· 흡수\n"
            "· 분리\n"
            "[[OPTIONS]]\n"
        )

        with caplog.at_level(logging.WARNING, logger="bridge.bot"):
            _run(bot._send_smart(42, content, force_options=True))

        buttons = [e for e in sent if e["reply_markup"] is not None]
        bodies = [e for e in sent if e["reply_markup"] is None]
        assert not buttons
        assert len(bodies) == 1
        assert "· 흡수" in bodies[0]["text"]
        assert any(
            "no buttons could be built" in rec.message for rec in caplog.records
        ), f"records: {[r.message for r in caplog.records]}"


class TestClassifierGate:
    def test_labeled_marker_suppresses_injection(self):
        """_maybe_mark_options must not double-inject over a labeled marker."""
        _load_bot()  # seeds the claude_agent_sdk mock if absent
        import bridge.sdk_bridge as sdk_mod

        content = (
            "1. one\n"
            "2. two\n"
            f"{LABELED}\n"
        )
        out, injected = _run(
            sdk_mod.SdkBridge._maybe_mark_options("prev", content)
        )
        assert out == content
        assert injected is False

    def test_bare_marker_still_suppresses_injection(self):
        _load_bot()  # seeds the claude_agent_sdk mock if absent
        import bridge.sdk_bridge as sdk_mod

        content = "1. one\n2. two\n[[OPTIONS]]\n"
        out, injected = _run(
            sdk_mod.SdkBridge._maybe_mark_options("prev", content)
        )
        assert out == content
        assert injected is False
