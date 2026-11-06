"""DGN-1732: NO_PUSH is handled at the shared delivery seat.

Incident (2026-09-26 22:31): the result-first dispatch-return push
(DGN-1687/1715) sent a first text ending in a literal "NO_PUSH" line to the
owner -- the only NO_PUSH handling lived in _flush_proactive (turn finalize),
which that path never reaches. The sentinel is now recognized by ONE function
(machine_gate.strip_no_push_sentinel) and stripped by the seat every
owner-bound text passes (bot._send_smart / _reply_smart and the render-time
machine-line gate).

promise: FDGN-578 -- an injected (no pending request) turn whose reply
carries NO_PUSH plus an off-locale body delivers nothing; an owner-language
body still reaches the owner.
"""
import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

import bridge.sdk_bridge as sdk
from bridge.machine_gate import RAIL_MODEL, apply_machine_line_gate, strip_no_push_sentinel
from bridge.formatting import sanitize_message_for_telegram, strip_display_markers
from bridge.sdk_bridge import SdkBridge, _UserStreamState


def _make_bot():
    import bridge.bot as bot_mod
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    sent = []

    async def _send_message(chat_id, text, **kwargs):
        sent.append(text)
        return MagicMock(message_id=len(sent))

    bot.application.bot.send_message = AsyncMock(side_effect=_send_message)
    bot.application.bot.delete_message = AsyncMock()
    return bot, sent


def _dispatch_state(root):
    push = AsyncMock()
    client = MagicMock()
    client.query = AsyncMock()
    state = _UserStreamState(client=client, model=None)
    state.last_chat_id = 11
    state.proactive_push = push
    bridge = SdkBridge()
    bridge._streams[7] = state
    with patch.object(sdk, "PROJECT_ROOT", Path(root)):
        assert asyncio.run(bridge.inject_background_turn(
            7, "[dispatch-return] worker finished"))
    return bridge, state, push


class RecognizerTest(unittest.TestCase):
    def test_shapes(self):
        cases = [
            ("NO_PUSH", ("", True)),
            ("  NO_PUSH  \n", ("", True)),
            ("NO_PUSH\n\n[hook footer]", ("[hook footer]", True)),
            ("result line\n\nNO_PUSH", ("result line", True)),
            ("result line\nNO_PUSH\n\n", ("result line", True)),
            ("  indented result\nNO_PUSH", ("  indented result", True)),
            ("plain answer", ("plain answer", False)),
            ("say NO_PUSH unless needed", ("say NO_PUSH unless needed", False)),
            ("body\nPUSH", ("body\nPUSH", False)),
            ("", ("", False)),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(strip_no_push_sentinel(text), expected)

    def test_render_choke_points_strip_sentinel(self):
        self.assertEqual(apply_machine_line_gate("result line\n\nNO_PUSH", RAIL_MODEL),
                         "result line")
        self.assertEqual(apply_machine_line_gate("NO_PUSH", RAIL_MODEL), "")
        self.assertNotIn("NO_PUSH", sanitize_message_for_telegram(
            "result line\nNO_PUSH", rail=RAIL_MODEL))
        self.assertEqual(strip_display_markers("draft\nNO_PUSH"), "draft")


class SeatTest(unittest.TestCase):
    def test_send_smart_strips_trailing_sentinel(self):
        bot, sent = _make_bot()
        asyncio.run(bot._send_smart(7, "result line\n\nNO_PUSH"))
        self.assertEqual(len(sent), 1)
        self.assertIn("result line", sent[0])
        self.assertNotIn("NO_PUSH", sent[0])

    def test_send_smart_bare_sentinel_sends_nothing(self):
        bot, sent = _make_bot()
        asyncio.run(bot._send_smart(7, "NO_PUSH"))
        self.assertEqual(sent, [])

    def test_send_smart_normal_text_unchanged(self):
        bot, sent = _make_bot()
        asyncio.run(bot._send_smart(7, "normal answer"))
        self.assertEqual(len(sent), 1)
        self.assertIn("normal answer", sent[0])

    def test_proactive_push_entry_rides_the_seat(self):
        bot, sent = _make_bot()
        asyncio.run(bot._proactive_push(7, "result line\n\nNO_PUSH", False))
        self.assertEqual(len(sent), 1)
        self.assertNotIn("NO_PUSH", sent[0])

    def test_push_sentinel_semantics_unchanged(self):
        # PUSH is stripped by _flush_proactive (DGN-1619), not the seat.
        self.assertEqual(strip_no_push_sentinel("body\n\nPUSH")[1], False)


class ResultFirstTest(unittest.TestCase):
    def test_result_first_trailing_sentinel_sends_result_only(self):
        with tempfile.TemporaryDirectory() as root:
            bridge, state, push = _dispatch_state(root)
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                self.assertTrue(asyncio.run(bridge._send_dispatch_return_first_text(
                    7, state, "result line\n\nNO_PUSH")))
                self.assertEqual(push.await_count, 1)
                self.assertEqual(push.await_args[0][1], "result line")
                self.assertTrue(state.dispatch_return_result_sent)
                self.assertTrue(json.loads(
                    sdk.dispatch_return_context_path().read_text())["visible"])

    def test_result_first_bare_sentinel_is_delivered_quiet(self):
        with tempfile.TemporaryDirectory() as root:
            bridge, state, push = _dispatch_state(root)
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                self.assertTrue(asyncio.run(bridge._send_dispatch_return_first_text(
                    7, state, "NO_PUSH")))
                push.assert_not_awaited()
                # The latch opens: the tool gate must not stall the turn.
                self.assertTrue(state.dispatch_return_result_sent)
                ctx = json.loads(sdk.dispatch_return_context_path().read_text())
                self.assertTrue(ctx["visible"])
                self.assertFalse(ctx["delivering"])

    def test_result_first_normal_text_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            bridge, state, push = _dispatch_state(root)
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                self.assertTrue(asyncio.run(bridge._send_dispatch_return_first_text(
                    7, state, "Result: done.")))
                self.assertEqual(push.await_args[0][1], "Result: done.")

    def test_proactive_block_bare_sentinel_does_not_reach_flush(self):
        """End-to-end on the no-pending path: the NO_PUSH-only first block
        is consumed by the latch and the turn still finalizes silently."""
        from claude_agent_sdk import AssistantMessage, TextBlock
        with tempfile.TemporaryDirectory() as root:
            bridge, state, push = _dispatch_state(root)
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                msg = AssistantMessage(content=[TextBlock(text="NO_PUSH")], model="m")
                asyncio.run(bridge._handle_proactive_message(7, state, msg))
                self.assertEqual(state.proactive_texts, [])
                self.assertTrue(state.dispatch_return_result_sent)
                asyncio.run(bridge._flush_proactive(7, state))
            push.assert_not_awaited()


class FinalizeConsistencyTest(unittest.TestCase):
    def _flush(self, texts):
        push = AsyncMock()
        state = _UserStreamState(client=MagicMock(), model=None)
        state.last_chat_id = 11
        state.proactive_push = push
        state.proactive_texts = list(texts)
        asyncio.run(SdkBridge()._flush_proactive(7, state))
        return push

    def test_flush_bare_sentinel_silence_kept(self):
        for texts in (["NO_PUSH"], [" \n NO_PUSH \n"]):
            with self.subTest(texts=texts):
                self._flush(texts).assert_not_awaited()

    def test_flush_normal_turn_unchanged(self):
        push = self._flush(["hello owner"])
        self.assertEqual(push.await_args[0][1], "hello owner")


# DGN-2161: mixed reports survive injection through the owner delivery seat.
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "loud", "quiet"])
@pytest.mark.parametrize("text", ["NO_PUSH", " \n NO_PUSH \n"])
async def test_bare_no_push_is_silent(mode, text):
    state = _UserStreamState(client=MagicMock(), model=None)
    state.last_chat_id = 11
    state.proactive_push = AsyncMock()
    state.injected_turn_mode = mode
    state.proactive_texts = [text]
    await SdkBridge()._flush_proactive(7, state)
    state.proactive_push.assert_not_awaited()
    assert state.injected_turn_mode is None
    assert state.proactive_texts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "loud", "quiet"])
@pytest.mark.parametrize("blocks", [
    ["result text\n\nNO_PUSH"],
    ["NO_PUSH\n\nresult text"],
    ["result text", "NO_PUSH"],
    ["result\nNO_PUSH\ntext"],
])
async def test_mixed_no_push_delivers_with_turn_warning(mode, blocks, caplog):
    state = _UserStreamState(client=MagicMock(), model=None)
    state.last_chat_id = 11
    state.last_session_id = "session-2161"
    state.proactive_push = AsyncMock()
    state.injected_turn_mode = mode
    state.proactive_texts = blocks
    with caplog.at_level(logging.WARNING, logger="bridge.sdk_bridge"):
        await SdkBridge()._flush_proactive(7, state, turn_id="turn-2161")
    state.proactive_push.assert_awaited_once()
    body = state.proactive_push.await_args.args[1]
    assert body.replace("\n", " ").split() == ["result", "text"]
    assert "NO_PUSH" not in body
    assert "turn_id=turn-2161" in caplog.text
    assert "session=session-2161" in caplog.text
    assert "result text" not in caplog.text
    assert state.injected_turn_mode is None


class _Client:
    def __init__(self, messages):
        self.query = AsyncMock()
        self.messages = messages

    async def receive_messages(self):
        for message in self.messages:
            yield message


def _assistant(text, tool=False):
    blocks = [TextBlock(text=text)]
    if tool:
        blocks.append(ToolUseBlock(id="read-2161", name="Read", input={}))
    return AssistantMessage(content=blocks, model="test",
                            stop_reason="tool_use" if tool else "end_turn")


@pytest.mark.asyncio
@pytest.mark.parametrize("final", ["result text\n\nNO_PUSH", "NO_PUSH\nresult text"])
async def test_injected_placeholder_then_result_reaches_owner(tmp_path, caplog, final):
    # The original first-text placeholder, expressed as ASCII source.
    placeholder = "\uacb0\uacfc\ub97c \ubcfc\uac8c\uc694"
    result = MagicMock(spec=ResultMessage)
    result.session_id = "session-2161"
    result.is_error = False
    client = _Client([_assistant(placeholder, tool=True), _assistant(final), result])
    bot, sent = _make_bot()
    state = _UserStreamState(client=client, model=None)
    state.last_chat_id = 11
    state.last_session_id = "session-2161"
    state.proactive_push = bot._proactive_push
    bridge = SdkBridge()
    bridge._streams[7] = state
    with patch.object(sdk, "PROJECT_ROOT", tmp_path), \
            patch.object(sdk, "_register_guard", lambda text: text), \
            caplog.at_level(logging.WARNING, logger="bridge.sdk_bridge"):
        assert await bridge.inject_background_turn(7, "[dispatch-return] worker finished")
        turn_id = state.dispatch_return_turn_id
        assert state.injected_turn_mode == "loud"
        await bridge._reader_loop(7, state)
    assert len(sent) == 2
    assert placeholder in sent[0]
    assert sent[1] == "result text"
    assert f"turn_id={turn_id}" in caplog.text
    assert "rail=proactive" in caplog.text
    assert state.dispatch_return_turn_id is None
    assert state.dispatch_return_delivered is None
    assert state.injected_turn_mode is None
    assert not (tmp_path / ".telegram_bot" / sdk.DISPATCH_RETURN_CONTEXT_FILENAME).exists()


@pytest.mark.asyncio
async def test_duplicate_result_with_sentinel_is_not_sent_twice(caplog):
    state = _UserStreamState(client=MagicMock(), model=None)
    state.last_chat_id = 11
    state.proactive_push = AsyncMock()
    state.proactive_texts = ["result text\n\nNO_PUSH"]
    with caplog.at_level(logging.WARNING, logger="bridge.sdk_bridge"):
        await SdkBridge()._flush_proactive(
            7, state, delivered="result text", turn_id="already-delivered"
        )
    state.proactive_push.assert_not_awaited()
    assert "turn_id=already-delivered" in caplog.text


@pytest.mark.asyncio
async def test_bare_final_still_silences_buffered_tool_narration(tmp_path, caplog):
    result = MagicMock(spec=ResultMessage)
    result.session_id = "session-2161"
    result.is_error = False
    client = _Client([_assistant("Checking the log.", tool=True),
                      _assistant(" \nNO_PUSH\n"), result])
    state = _UserStreamState(client=client, model=None)
    state.last_chat_id = 11
    state.proactive_push = AsyncMock()
    with patch.object(sdk, "PROJECT_ROOT", tmp_path), \
            patch.object(sdk, "_register_guard", lambda text: text), \
            caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        await SdkBridge()._reader_loop(7, state)
    state.proactive_push.assert_not_awaited()
    assert "7 chars (final only; buffered_chars=" in caplog.text
    assert state.proactive_last_text is None


@pytest.mark.asyncio
async def test_split_text_blocks_in_one_final_preserve_report(tmp_path, caplog):
    result = MagicMock(spec=ResultMessage)
    result.session_id = "session-2161"
    result.is_error = False
    final = AssistantMessage(content=[TextBlock(text="result text"),
                                      TextBlock(text="NO_PUSH")], model="test",
                             stop_reason="end_turn")
    state = _UserStreamState(client=_Client([final, result]), model=None)
    state.last_chat_id = 11
    state.proactive_push = AsyncMock()
    with patch.object(sdk, "PROJECT_ROOT", tmp_path), \
            patch.object(sdk, "_register_guard", lambda text: text), \
            caplog.at_level(logging.WARNING, logger="bridge.sdk_bridge"):
        await SdkBridge()._reader_loop(7, state)
    state.proactive_push.assert_awaited_once_with(11, "result text", False, False)
    assert "NO_PUSH with content" in caplog.text


@pytest.mark.asyncio
async def test_quiet_injected_mixed_no_push_does_not_leak_into_owner_turn(
        tmp_path, monkeypatch):
    """A pending owner request must not make a quiet report owner-visible."""
    import bridge.bot as bot_mod
    from bridge.tests.test_dgn1651_stop_block_duplicate import _Screen, _pending

    monkeypatch.setattr(sdk, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sdk, "_register_guard", lambda text: text)
    monkeypatch.setattr(sdk, "_scaffold_guard", lambda text: text)
    monkeypatch.setattr(sdk.notice_spool, "reserve_for_synthesis", lambda _: None)

    hidden = "All live files already match the incoming version.\nNO_PUSH"
    owner_reply = "Here is the answer to the owner's message."
    first = MagicMock(spec=ResultMessage, is_error=False,
                      session_id="collision-session", result=hidden)
    second = MagicMock(spec=ResultMessage, is_error=False,
                       session_id="collision-session", result=owner_reply)
    client = _Client([
        _assistant(hidden), first, _assistant(owner_reply), second,
    ])
    state = _UserStreamState(client=client, model=None)
    state.injected_turn_mode = "quiet"
    quiet_req = _pending(None)
    quiet_req.sent = True
    owner_req = _pending(None)
    owner_req.sent = True
    state.pending.extend([quiet_req, owner_req])

    await SdkBridge()._reader_loop(7, state)

    assert quiet_req.future.result().content == "NO_PUSH"
    assert owner_req.future.result().content == owner_reply
    assert state.injected_turn_mode is None

    screen = _Screen()
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    bot.application.bot = screen.bot()
    message = MagicMock()
    message.chat.id = 1
    message.get_bot.return_value = bot.application.bot

    async def reply_text(text, **kwargs):
        return await bot.application.bot.send_message(
            chat_id=1, text=text, **kwargs)

    message.reply_text = AsyncMock(side_effect=reply_text)
    with patch.object(bot, "_reply_link_id", return_value=None), \
            patch.object(bot, "_send_content_artifacts", new=AsyncMock()):
        await bot._reply_smart(message, quiet_req.future.result().content)
        await bot._reply_smart(message, owner_req.future.result().content)
    assert screen.view() == [owner_reply]


@pytest.mark.asyncio
@pytest.mark.parametrize("grown", [False, True])
@pytest.mark.parametrize("separator", ["\n", "\n\n"])
async def test_generated_language_with_no_push_delivers_once(
    tmp_path, monkeypatch, caplog, grown, separator
):
    from claude_agent_sdk import UserMessage
    import bridge.bot as bot_mod
    from bridge.streaming import StreamingMessageHandler
    from bridge.tests.test_dgn1651_stop_block_duplicate import _Screen, _pending

    for name in list(os.environ):
        # ESTATE:BEGIN isolate inherited estate settings as well as Git configuration
        if name.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
        # ESTATE:ELSE
        # if name.startswith("GIT_CONFIG_"):
        # ESTATE:END
            monkeypatch.delenv(name)
    monkeypatch.setattr(sdk, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sdk.config, "locale", "ko")
    monkeypatch.setattr(sdk, "FINAL_LANG_GATE", True)
    monkeypatch.setattr(sdk, "OUTPUT_LANG_GUARD", True)
    monkeypatch.setattr(sdk, "FINAL_LANG_MIN_WORDS", 8)
    monkeypatch.setattr(sdk, "FINAL_LANG_EN_RATIO", 0.75)
    monkeypatch.setattr(sdk, "INTERIM_MODE", "fold")
    monkeypatch.setattr(sdk, "STREAM_INTERIM", False)
    monkeypatch.setattr(sdk, "FOLD_CREATE_MIN_INTERIMS", 1 if grown else 100)
    monkeypatch.setattr(sdk, "FOLD_CREATE_MIN_CHARS", 10000)
    monkeypatch.setattr(sdk.notice_spool, "reserve_for_synthesis", lambda _: None)

    # Replay the logged counts without relying on the private incident text.
    english = ("word " * 71 + "\ud655\uc778 \ubcf4\uace0").ljust(457)
    korean = ("\uc791\uc5c5 \uacb0\uacfc\ub97c \ud655\uc778\ud588\uace0 \uc218\uc815\ud55c \ubd80\ubd84\uc758 "
              "\uac80\uc0ac\ub3c4 \ubaa8\ub450 \ud1b5\uacfc\ud588\uc2b5\ub2c8\ub2e4. " * 8)[:216].strip()
    rewrite = english + separator + "NO_PUSH"
    screen = _Screen()
    transport = screen.bot()
    handler = StreamingMessageHandler(transport, chat_id=1, user_id=1)
    req = _pending(handler)
    req.sent = True
    result = MagicMock(spec=ResultMessage)
    result.is_error = False
    result.session_id = "dgn2235-replay"
    result.result = rewrite

    async def receive():
        yield AssistantMessage(content=[TextBlock(text=rewrite)], model="test")
        assert sdk._final_lang_gate_decision(
            {"last_assistant_message": rewrite}, req
        ) == {}
        yield result

    client = MagicMock()
    client.receive_messages = receive
    state = _UserStreamState(client=client, model=None)
    state.pending.append(req)
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    bot.application.bot = transport
    state.proactive_push = bot._proactive_push
    bridge = SdkBridge()
    bridge._streams[1] = state
    with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        await bridge._reader_loop(1, state)
    assert req.future.done()
    response = req.future.result()
    message = MagicMock()
    message.chat.id = 1
    message.get_bot.return_value = transport
    async def reply_text(text, **kwargs):
        return await transport.send_message(chat_id=1, text=text, **kwargs)

    message.reply_text = AsyncMock(side_effect=reply_text)
    with patch.object(bot, "_reply_link_id", return_value=None), \
            patch.object(bot, "_send_content_artifacts", new=AsyncMock()):
        await bot._reply_smart(
            message, response.content, streamed=response.streamed,
            draft_message_ids=response.draft_message_ids,
            assembled=response.turn_assembled,
        )
    assert req.lang_gate_blocks == 0
    assert "Interim locale gate dropped" not in caplog.text
    assert "rail=final; sentinel stripped" in caplog.text
    assert screen.view() == [english.strip()]


class _Dgn2378Client:
    def __init__(self, messages):
        self.messages = messages
        self.query = AsyncMock()

    async def receive_messages(self):
        for message in self.messages:
            yield message


async def _run_dgn2378_injected_turn(reply):
    prompt = "[update-refit] Batched background adaptation work."
    client = _Dgn2378Client([])
    push = AsyncMock()
    state = _UserStreamState(client=client, model=None)
    state.last_chat_id = 11
    state.last_session_id = "session-2378"
    state.proactive_push = push
    bridge = SdkBridge()
    bridge._streams[7] = state
    assert await bridge.inject_background_turn(7, prompt)
    injected = client.query.await_args.args[0]
    result = MagicMock(spec=ResultMessage)
    result.session_id = "session-2378"
    result.is_error = False
    client.messages = [
        AssistantMessage(content=[TextBlock(text=injected)], model="test"),
        AssistantMessage(content=[TextBlock(text=reply)], model="test"),
        result,
    ]
    await bridge._reader_loop(7, state)
    return push, state


@pytest.mark.asyncio
async def test_dgn2378_update_refit_prompt_and_no_push_send_nothing():
    push, state = await _run_dgn2378_injected_turn("  NO_PUSH\n")

    push.assert_not_awaited()
    assert state.proactive_texts == []
    assert state.injected_prompt_text is None


@pytest.mark.asyncio
async def test_dgn2378_update_refit_delivers_only_actionable_reply():
    push, state = await _run_dgn2378_injected_turn(
        "The owner must reconnect the calendar."
    )

    push.assert_awaited_once()
    assert push.await_args.args[1] == "The owner must reconnect the calendar."
    assert state.injected_prompt_text is None


async def _run_fdgn578_quiet_recovery(monkeypatch, reply):
    for name in list(os.environ):
        # ESTATE:BEGIN isolate inherited estate settings as well as Git configuration
        if name.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
        # ESTATE:ELSE
        # if name.startswith("GIT_CONFIG_"):
        # ESTATE:END
            monkeypatch.delenv(name)
    monkeypatch.setattr(sdk.config, "locale", "ko")
    monkeypatch.setattr(sdk, "FINAL_LANG_GATE", True)
    monkeypatch.setattr(sdk, "OUTPUT_LANG_GUARD", True)
    client = _Dgn2378Client([])
    push = AsyncMock()
    state = _UserStreamState(client=client, model=None)
    state.last_chat_id = 11
    state.last_session_id = "session-578"
    state.proactive_push = push
    bridge = SdkBridge()
    bridge._streams[7] = state
    assert await bridge.inject_background_turn(
        7, "[dispatch-return] m-recover-0526 canceled\n- cancel_by: self")
    assert "[bridge:quiet-recovery]" in client.query.await_args.args[0]
    assert state.injected_turn_mode == "quiet"
    result = MagicMock(spec=ResultMessage)
    result.session_id = "session-578"
    result.is_error = False
    # No tool call in the turn: the PostToolUse language line never fired.
    client.messages = [
        AssistantMessage(content=[TextBlock(text=reply)], model="test"),
        result,
    ]
    await bridge._reader_loop(7, state)
    return push


@pytest.mark.asyncio
async def test_fdgn578_quiet_recovery_english_with_no_push_delivers_body(
        monkeypatch, caplog):
    # English-dominant with a few Korean words: the zero-Hangul drop tier
    # (DGN-686) passes it, so only the language gate can stop it.
    reply = ("Recovered the worker branch m-recover-0526 and checked the "
             "\ud06c\ub808\ub527 artifacts; nothing new needs the owner "
             "right now, \ud655\uc778 \uc644\ub8cc.\n\nNO_PUSH")
    with caplog.at_level(logging.WARNING, logger="bridge.sdk_bridge"):
        push = await _run_fdgn578_quiet_recovery(monkeypatch, reply)
    push.assert_awaited_once()
    assert push.await_args.args[1] == reply.removesuffix("\n\nNO_PUSH")
    assert "NO_PUSH with off-locale content dropped" not in caplog.text


@pytest.mark.asyncio
async def test_fdgn578_quiet_recovery_korean_with_no_push_still_delivers(
        monkeypatch):
    korean = ("\uc791\uc5c5 \ube0c\ub79c\uce58\ub97c \ubcf5\uad6c\ud588\uace0 "
              "\uc0b0\ucd9c\ubb3c\ub3c4 \ubaa8\ub450 \ud655\uc778\ud588\uc2b5\ub2c8\ub2e4 "
              "\uc9c0\uae08 \ud558\uc2e4 \uc77c\uc740 \uc5c6\uc2b5\ub2c8\ub2e4")
    push = await _run_fdgn578_quiet_recovery(monkeypatch, korean + "\n\nNO_PUSH")
    push.assert_awaited_once()
    assert push.await_args.args[1] == korean


if __name__ == "__main__":
    unittest.main()
