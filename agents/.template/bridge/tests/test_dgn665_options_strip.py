"""DGN-665: strip the CONSUMED [[OPTIONS]] numbered run from the display body.

With an explicit [[OPTIONS]] marker, the choices render as inline buttons; the
numbered run that feeds the buttons must NOT also appear in the message text.
strip_consumed_options() removes exactly the last contiguous 1..N run (the same
run extract_options selects) plus the marker line. Safety: on extraction
failure the list is kept (never leave the user choice-less); a body that
strips to whitespace must not be sent as an empty bubble. The markerless
(classifier-gate) path is untouched: no marker -> the seats never strip.

Covers the helper (T1-T7) and the _send_smart / _reply_smart /
_send_content_artifacts seat wiring.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

from bridge.options import (  # noqa: E402
    extract_options,
    resolve_choice,
    strip_consumed_options,
    strip_options_marker,
)


class _Btn:
    """Minimal InlineKeyboardButton stand-in (text + callback_data)."""

    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data


# ---------------------------------------------------------------------------
# 0. Helper: resolve_choice (T8)
# ---------------------------------------------------------------------------


class TestResolveChoice:
    def test_t8_number_only_callback_resolves_full_label(self):
        """T8: number-only callback_data (opt:2) but a button text "2. 균일 표면
        유지" resolves choice to the full label (Korean overflow case)."""
        kb = [
            [_Btn("1. 빠른 처리", "opt:1")],
            [_Btn("2. 균일 표면 유지", "opt:2")],
        ]
        assert resolve_choice("opt:2", kb) == "2. 균일 표면 유지"

    def test_full_callback_still_resolves_by_match(self):
        """Short ASCII labels keep the full callback_data -> exact button match."""
        kb = [[_Btn("1. proceed", "opt:1. proceed")],
              [_Btn("2. hold", "opt:2. hold")]]
        assert resolve_choice("opt:2. hold", kb) == "2. hold"

    def test_no_keyboard_falls_back_to_payload(self):
        """Absent keyboard -> the post-"opt:" payload (legacy behavior)."""
        assert resolve_choice("opt:2", None) == "2"
        assert resolve_choice("opt:2", []) == "2"

    def test_no_match_falls_back_to_payload(self):
        """Callback with no matching button -> payload fallback (truncated kb)."""
        kb = [[_Btn("1. a", "opt:1")]]
        assert resolve_choice("opt:9", kb) == "9"


# ---------------------------------------------------------------------------
# 1. Helper: strip_consumed_options
# ---------------------------------------------------------------------------


class TestStripConsumedOptions:
    def test_t1_single_list_with_marker_removed(self):
        """T1: single list + marker -> display has NO numbered list, options extracted."""
        text = "1. proceed\n2. hold\n3. cancel\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert options == ["proceed", "hold", "cancel"], f"got {options}"
        assert "1." not in display and "proceed" not in display, f"got {display!r}"

    def test_t2_lead_in_prose_kept_list_removed(self):
        """T2: lead-in prose + list + marker -> lead-in kept, list removed."""
        text = (
            "Which way do you want to go?\n"
            "\n"
            "1. proceed\n"
            "2. hold\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert options == ["proceed", "hold"], f"got {options}"
        assert display == "Which way do you want to go?", f"got {display!r}"

    def test_t3_detailed_plus_bare_list_only_last_run_removed(self):
        """T3: detailed list + bare list + marker (Skull case) -> only the LAST
        (bare) run removed; the detailed list stays as prose context."""
        text = (
            "Here are your choices:\n"
            "1. proceed with the plan -- fastest, some risk\n"
            "2. hold and review -- safer, slower\n"
            "\n"
            "Which do you want?\n"
            "\n"
            "1. proceed\n"
            "2. hold\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert options == ["proceed", "hold"], f"got {options}"
        assert "1. proceed with the plan -- fastest, some risk" in display
        assert "2. hold and review -- safer, slower" in display
        assert "Which do you want?" in display
        assert "1. proceed\n" not in display + "\n", f"got {display!r}"
        assert not display.rstrip().endswith("2. hold"), f"got {display!r}"

    def test_t4_no_marker_list_kept_by_seat_gate(self):
        """T4: numbered list, NO marker (classifier path) -> the seats never call
        the helper (has_marker False), so the list is KEPT. The helper itself
        still reports what it WOULD strip; the gate lives at the seat -- assert
        the marker detection stays False so the seat gate cannot fire."""
        text = "Status report:\n1. did the thing\n2. next thing\n"
        _, has_marker = strip_options_marker(text)
        assert has_marker is False

    def test_item2_wrapped_label_run_keeps_body(self):
        """Item 2: a sub-bullet between numbered items makes the run non-line-
        adjacent -> options still build, but the body list is KEPT (deleting only
        the numbered lines would orphan the sub-bullet)."""
        text = (
            "1. proceed\n"
            "   - detail under proceed\n"
            "2. hold\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert options == ["proceed", "hold"], f"got {options}"
        assert "1. proceed" in display
        assert "   - detail under proceed" in display
        assert "2. hold" in display

    def test_item2_prose_interleaved_run_keeps_body(self):
        """Item 2: prose between numbered items -> non-adjacent -> body kept."""
        text = "1. do X\nsome prose here\n2. do Y\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert options == ["do X", "do Y"], f"got {options}"
        assert "1. do X" in display
        assert "some prose here" in display
        assert "2. do Y" in display

    def test_item2_adjacent_run_still_strips(self):
        """Item 2: a clean line-adjacent run still strips as before."""
        text = "Pick:\n1. proceed\n2. hold\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert options == ["proceed", "hold"], f"got {options}"
        assert "1. proceed" not in display
        assert "2. hold" not in display
        assert display == "Pick:", f"got {display!r}"

    def test_t5_list_only_display_strips_to_whitespace(self):
        """T5: list-only, no lead-in, + marker -> display is whitespace-only so
        the caller must skip the empty bubble."""
        text = "1. approve\n2. reject\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert options == ["approve", "reject"], f"got {options}"
        assert display.strip() == "", f"got {display!r}"

    def test_t6_code_block_numbers_not_treated_as_options(self):
        """T6: numbered items inside a fenced code block are not options
        (DGN-085 regression) -- code stays in the display verbatim."""
        text = (
            "Steps as code:\n"
            "```\n"
            "1. code_step_one()\n"
            "2. code_step_two()\n"
            "```\n"
            "\n"
            "1. run it\n"
            "2. skip it\n"
            "[[OPTIONS]]\n"
        )
        display, options = strip_consumed_options(text)
        assert options == ["run it", "skip it"], f"got {options}"
        assert "1. code_step_one()" in display
        assert "2. code_step_two()" in display
        assert "run it" not in display

    def test_t6b_code_block_only_yields_no_options(self):
        """T6: a reply whose only numbered lines sit in a code block -> no
        options, display unchanged apart from the marker line."""
        text = "```\n1. only_in_code()\n2. still_code()\n```\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert options == [], f"got {options}"
        assert "1. only_in_code()" in display

    def test_t7_extraction_failure_keeps_display(self):
        """T7: extract yields empty (stray 1. then gap, DGN-494 guard) ->
        display unchanged (list kept, safety)."""
        text = "1. First\n3. Skip two\n[[OPTIONS]]\n"
        display, options = strip_consumed_options(text)
        assert options == [], f"got {options}"
        assert "1. First" in display and "3. Skip two" in display

    def test_matches_extract_options_selection(self):
        """The helper must consume the SAME run extract_options selects."""
        text = (
            "1. context one\n"
            "2. context two\n"
            "\n"
            "1. do this\n"
            "2. do that\n"
            "[[OPTIONS]]\n"
        )
        _, options = strip_consumed_options(text)
        assert options == extract_options(text)

    def test_marker_line_always_removed(self):
        """The [[OPTIONS]] marker line never survives into the display."""
        for text in (
            "1. a\n2. b\n[[OPTIONS]]\n",
            "prose only\n[[OPTIONS]]\n",
            "1. First\n3. Skip\n[[OPTIONS]]\n",
        ):
            display, _ = strip_consumed_options(text)
            assert "[[OPTIONS]]" not in display, f"got {display!r}"

    def test_empty_input(self):
        assert strip_consumed_options("") == ("", [])


# ---------------------------------------------------------------------------
# 2. Seat wiring: _send_smart / _reply_smart / _send_content_artifacts
# ---------------------------------------------------------------------------


def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


def _make_chat_bot(bot_mod, sent):
    """Bot whose application.bot records every send_message (for _send_smart)."""
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()

    # Metal-local shim (DGN-696): accept link_preview_options -- Metal-ahead
    # DGN-376 passes it on every send; upstream mock should absorb kwargs.
    async def _send_message(chat_id, text, parse_mode=None, reply_markup=None,
                            link_preview_options=None):
        sent.append({"text": text, "reply_markup": reply_markup})

    bot.application.bot = MagicMock()
    bot.application.bot.send_message = AsyncMock(side_effect=_send_message)
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = {}
    return bot


def _make_message(sent, chat_id=42, message_id=100):
    """Triggering-message mock; records every reply_text send (for _reply_smart)."""
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.message_id = message_id
    from datetime import datetime, timezone

    msg.date = datetime.now(timezone.utc)

    async def _reply_text(text, parse_mode=None, reply_markup=None,
                          link_preview_options=None, reply_parameters=None):
        sent.append({"text": text, "reply_markup": reply_markup})

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    tg_bot = MagicMock()
    msg.get_bot.return_value = tg_bot
    tg_bot.delete_message = AsyncMock()
    return msg


def _run(coro):
    return asyncio.run(coro)


MARKER_REPLY = (
    "Pick a direction:\n"
    "\n"
    "1. proceed\n"
    "2. hold\n"
    "[[OPTIONS]]\n"
)


class TestSendSmartSeat:
    def test_marker_body_stripped_buttons_sent(self):
        """T1/T2 seat: body carries lead-in only; buttons carry the choices."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, MARKER_REPLY, force_options=True))

        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        assert len(bodies) == 1, f"got {sent}"
        assert "Pick a direction:" in bodies[0]["text"]
        assert "1. proceed" not in bodies[0]["text"]
        assert "2. hold" not in bodies[0]["text"]

    def test_list_only_body_skips_empty_bubble(self):
        """T5 seat: list-only reply -> ONLY the SELECT_PROMPT+buttons message."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, "1. approve\n2. reject\n[[OPTIONS]]\n",
                             force_options=True))

        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is not None

    def test_no_marker_list_kept_no_buttons(self):
        """T4 seat: markerless numbered list -> list stays in the body, no
        buttons, no strip (classifier-gate path untouched)."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, "Report:\n1. did a\n2. did b\n",
                             force_options=True))

        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is None
        assert "1. did a" in sent[0]["text"]
        assert "2. did b" in sent[0]["text"]

    def test_extraction_failure_keeps_list_no_buttons(self):
        """T7 seat: marker present but run invalid -> list kept, no buttons."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, "1. First\n3. Skip two\n[[OPTIONS]]\n",
                             force_options=True))

        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is None
        assert "1. First" in sent[0]["text"]
        assert "3. Skip two" in sent[0]["text"]

    def test_no_force_options_keeps_list(self):
        """Marker in text but force_options False -> no strip, no buttons."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, MARKER_REPLY, force_options=False))

        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is None
        assert "1. proceed" in sent[0]["text"]


class TestReplySmartSeat:
    def test_marker_body_stripped_buttons_sent(self):
        """T1/T2 seat: lead-in body + separate buttons message, list nowhere."""
        bot_mod = _load_bot()
        sent = []
        msg = _make_message(sent)
        bot = _make_chat_bot(bot_mod, [])

        _run(bot._reply_smart(msg, MARKER_REPLY, force_options=True))

        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        assert len(bodies) == 1, f"got {sent}"
        assert "Pick a direction:" in bodies[0]["text"]
        assert "1. proceed" not in bodies[0]["text"]

    def test_list_only_body_skips_empty_bubble(self):
        """T5 seat: list-only reply -> only the SELECT_PROMPT+buttons send."""
        bot_mod = _load_bot()
        sent = []
        msg = _make_message(sent)
        bot = _make_chat_bot(bot_mod, [])

        _run(bot._reply_smart(msg, "1. approve\n2. reject\n[[OPTIONS]]\n",
                              force_options=True))

        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is not None

    def test_t3_skull_case_detailed_list_kept(self):
        """T3 seat: detailed + bare double list -> detailed stays in body,
        bare run becomes buttons only."""
        bot_mod = _load_bot()
        sent = []
        msg = _make_message(sent)
        bot = _make_chat_bot(bot_mod, [])
        text = (
            "Options in detail:\n"
            "1. proceed with the plan -- fastest\n"
            "2. hold and review -- safer\n"
            "\n"
            "1. proceed\n"
            "2. hold\n"
            "[[OPTIONS]]\n"
        )

        _run(bot._reply_smart(msg, text, force_options=True))

        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        body_text = "\n".join(e["text"] for e in bodies)
        assert "1. proceed with the plan -- fastest" in body_text
        assert "2. hold and review -- safer" in body_text
        assert "1. proceed\n" not in body_text + "\n"

    def test_no_marker_list_kept_no_buttons(self):
        """T4 seat: markerless list stays in the body; no buttons message."""
        bot_mod = _load_bot()
        sent = []
        msg = _make_message(sent)
        bot = _make_chat_bot(bot_mod, [])

        _run(bot._reply_smart(msg, "Report:\n1. did a\n2. did b\n",
                              force_options=True))

        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is None
        assert "1. did a" in sent[0]["text"]

    def test_extraction_failure_keeps_list_no_buttons(self):
        """T7 seat: invalid run + marker -> list kept, no buttons message."""
        bot_mod = _load_bot()
        sent = []
        msg = _make_message(sent)
        bot = _make_chat_bot(bot_mod, [])

        _run(bot._reply_smart(msg, "1. First\n3. Skip two\n[[OPTIONS]]\n",
                              force_options=True))

        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is None
        assert "1. First" in sent[0]["text"]


# ---------------------------------------------------------------------------
# 3. Item 1: streamed path -- the terminal AssistantMessage live-streams into
# drafts, so a normal decision-ask is streamed=True. The draft baked in the
# unstripped list; the seat must delete the draft and re-send the stripped body.
# ---------------------------------------------------------------------------


class TestStreamedSeat:
    def test_send_smart_streamed_lead_in_strips_and_rebuilds(self):
        """Item 1a (_send_smart): streamed lead-in + list + marker -> draft
        deleted, stripped body re-sent, buttons rendered."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, MARKER_REPLY, force_options=True,
                             streamed=True, draft_message_ids=[7]))

        bot.application.bot.delete_message.assert_awaited_once_with(42, 7)
        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(bodies) == 1, f"got {sent}"
        assert len(buttons) == 1, f"got {sent}"
        assert "Pick a direction:" in bodies[0]["text"]
        assert "1. proceed" not in bodies[0]["text"]

    def test_send_smart_streamed_list_only_no_empty_bubble(self):
        """Item 1b (_send_smart): streamed list-only reply -> draft deleted, NO
        empty bubble, buttons rendered."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, "1. approve\n2. reject\n[[OPTIONS]]\n",
                             force_options=True, streamed=True,
                             draft_message_ids=[9]))

        bot.application.bot.delete_message.assert_awaited_once_with(42, 9)
        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is not None

    def test_reply_smart_streamed_lead_in_strips_and_rebuilds(self):
        """Item 1a (_reply_smart): streamed lead-in + list + marker -> draft
        deleted, stripped body re-sent, buttons rendered."""
        bot_mod = _load_bot()
        sent = []
        msg = _make_message(sent)
        bot = _make_chat_bot(bot_mod, [])
        tg_bot = msg.get_bot.return_value

        _run(bot._reply_smart(msg, MARKER_REPLY, force_options=True,
                              streamed=True, draft_message_ids=[7]))

        tg_bot.delete_message.assert_awaited_once_with(42, 7)
        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(bodies) == 1, f"got {sent}"
        assert len(buttons) == 1, f"got {sent}"
        assert "Pick a direction:" in bodies[0]["text"]
        assert "1. proceed" not in bodies[0]["text"]

    def test_reply_smart_streamed_list_only_no_empty_bubble(self):
        """Item 1b (_reply_smart): streamed list-only reply -> draft deleted, NO
        empty bubble, buttons rendered."""
        bot_mod = _load_bot()
        sent = []
        msg = _make_message(sent)
        bot = _make_chat_bot(bot_mod, [])
        tg_bot = msg.get_bot.return_value

        _run(bot._reply_smart(msg, "1. approve\n2. reject\n[[OPTIONS]]\n",
                              force_options=True, streamed=True,
                              draft_message_ids=[9]))

        tg_bot.delete_message.assert_awaited_once_with(42, 9)
        assert len(sent) == 1, f"got {sent}"
        assert sent[0]["reply_markup"] is not None


# ---------------------------------------------------------------------------
# 4. Item 3: classifier-injected marker -> buttons YES, body list KEPT.
# ---------------------------------------------------------------------------


class TestClassifierInjectedSeat:
    def test_send_smart_classifier_injected_keeps_list_renders_buttons(self):
        """Item 3 (_send_smart): a classifier-injected marker renders buttons but
        must NOT strip the body list (owner lock: strip only on authored marker)."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        _run(bot._send_smart(42, MARKER_REPLY, force_options=True,
                             classifier_injected=True))

        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        assert len(bodies) == 1, f"got {sent}"
        assert "1. proceed" in bodies[0]["text"], "classifier path must KEEP the list"
        assert "2. hold" in bodies[0]["text"]

    def test_reply_smart_classifier_injected_keeps_list_renders_buttons(self):
        """Item 3 (_reply_smart): classifier-injected -> buttons yes, list KEPT."""
        bot_mod = _load_bot()
        sent = []
        msg = _make_message(sent)
        bot = _make_chat_bot(bot_mod, [])

        _run(bot._reply_smart(msg, MARKER_REPLY, force_options=True,
                              classifier_injected=True))

        bodies = [e for e in sent if e["reply_markup"] is None]
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        body_text = "\n".join(e["text"] for e in bodies)
        assert "1. proceed" in body_text, "classifier path must KEEP the list"
        assert "2. hold" in body_text


# ---------------------------------------------------------------------------
# 5. MAJOR-2: the FOUR request-response call sites (skill run, main chat turn,
# option callback reply, resume continuation) must THREAD the provenance flag
# from the ChatResponse into the seat. The seats gate correctly (section 4),
# but if a call site omits classifier_injected= the default False makes a
# classifier-INJECTED marker look agent-AUTHORED and the body list gets
# stripped. The handlers live in nested closures the unit harness cannot
# drive end-to-end, so this asserts the wiring structurally via the AST:
# every _reply_smart/_send_smart call that forwards a ChatResponse (marked by
# its streamed= kwarg) must also pass
# classifier_injected=getattr(<response>, "options_classifier_injected", False).
# ---------------------------------------------------------------------------


class TestCallSiteWiring:
    @staticmethod
    def _response_seat_calls():
        import ast
        import inspect

        bot_mod = _load_bot()
        tree = ast.parse(inspect.getsource(bot_mod))
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr in ("_reply_smart", "_send_smart")):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            # streamed=<resp>.streamed marks the four request-response sites;
            # the proactive-push seat call and the defs themselves lack it.
            if "streamed" in kwargs:
                calls.append((func.attr, node.lineno, kwargs))
        return calls

    def test_all_four_request_response_sites_exist(self):
        # DGN-686 added a 5th request-response seat: the [retry] callback re-run
        # (_handle_retry_callback) delivers a normal response via _send_smart and
        # must thread the classifier flag like every other seat.
        calls = self._response_seat_calls()
        assert len(calls) == 5, (
            f"expected 5 request-response seat calls, got {len(calls)}: "
            f"{[(n, ln) for n, ln, _ in calls]}"
        )
        assert sorted(n for n, _, _ in calls) == (
            ["_reply_smart", "_reply_smart", "_send_smart", "_send_smart",
             "_send_smart"]
        )

    def test_each_site_threads_classifier_injected_flag(self):
        import ast

        for name, lineno, kwargs in self._response_seat_calls():
            assert "classifier_injected" in kwargs, (
                f"{name} call at line {lineno} does not pass "
                f"classifier_injected= -- classifier-injected markers would "
                f"be treated as authored and the body list stripped (MAJOR-2)"
            )
            val = kwargs["classifier_injected"]
            # Must be getattr(<response>, "options_classifier_injected", False)
            # so any ChatResponse-shaped object lacking the field stays safe.
            assert (
                isinstance(val, ast.Call)
                and isinstance(val.func, ast.Name)
                and val.func.id == "getattr"
                and len(val.args) == 3
                and isinstance(val.args[1], ast.Constant)
                and val.args[1].value == "options_classifier_injected"
                and isinstance(val.args[2], ast.Constant)
                and val.args[2].value is False
            ), (
                f"{name} call at line {lineno}: classifier_injected must be "
                f"getattr(<response>, 'options_classifier_injected', False)"
            )
            # The getattr target must be the same object whose .streamed is
            # forwarded (i.e. the ChatResponse local at that site).
            streamed = kwargs["streamed"]
            assert (
                isinstance(streamed, ast.Attribute)
                and isinstance(streamed.value, ast.Name)
                and isinstance(val.args[0], ast.Name)
                and val.args[0].id == streamed.value.id
            ), (
                f"{name} call at line {lineno}: classifier_injected getattr "
                f"target must be the same ChatResponse variable as streamed="
            )
