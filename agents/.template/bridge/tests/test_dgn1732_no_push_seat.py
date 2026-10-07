"""DGN-1732: NO_PUSH is handled at the shared delivery seat.

Incident (2026-09-26 22:31): the result-first dispatch-return push
(DGN-1687/1715) sent a first text ending in a literal "NO_PUSH" line to the
owner -- the only NO_PUSH handling lived in _flush_proactive (turn finalize),
which that path never reaches. The sentinel is now recognized by ONE function
(machine_gate.strip_no_push_sentinel) and stripped by the seat every
owner-bound text passes (bot._send_smart / _reply_smart and the render-time
machine-line gate).
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
            ("NO_PUSH\n\n[hook footer]", ("", True)),
            ("result line\n\nNO_PUSH", ("result line", True)),
            ("result line\nNO_PUSH\n\n", ("result line", True)),
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

    def test_flush_whole_turn_silence_kept(self):
        for texts in (["NO_PUSH"], ["report body\nNO_PUSH"], ["NO_PUSH\n[footer]"]):
            with self.subTest(texts=texts):
                self._flush(texts).assert_not_awaited()

    def test_flush_normal_turn_unchanged(self):
        push = self._flush(["hello owner"])
        self.assertEqual(push.await_args[0][1], "hello owner")


if __name__ == "__main__":
    unittest.main()
