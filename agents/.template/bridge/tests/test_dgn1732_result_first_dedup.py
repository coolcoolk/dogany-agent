"""DGN-1732: no duplicate bubble/keyboard after a result-first push.

Measured 2026-09-26 22:50 (dev.55): a session-inbox dispatch-return turn pushed
its first text immediately (result-first, DGN-1687/1715) with an [[OPTIONS]]
keyboard, then the turn's final text repeated the same proposal + the same
keyboard as a second bubble. The finalize now subtracts what the result-first
push already delivered (options.subtract_delivered, the DGN-947 lossless
paragraph rule) and never builds a second keyboard with the same labels.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic bridge environment
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from bridge import sdk_bridge as sdk
from bridge.options import has_options_marker, subtract_delivered
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState

RESULT = "Result: dgn1702 grill finished; one blocker found."
PROPOSAL = "Dispatch the fix now?"
KEYBOARD = "[[OPTIONS]]\n1. proceed\n2. hold"
FIRST = RESULT + "\n\n" + PROPOSAL + "\n" + KEYBOARD

# The fixtures are ASCII; the DGN-686 register guard would drop them on a
# ko-locale test host. It is not under test here.
_guard_patch = patch.object(sdk, "_register_guard", lambda text: text)


def setUpModule():
    _guard_patch.start()


def tearDownModule():
    _guard_patch.stop()


def _state(root):
    push = AsyncMock()
    state = _UserStreamState(client=MagicMock(), model=None)
    state.last_chat_id = 11
    state.last_session_id = "sess-1"
    state.proactive_push = push
    state.dispatch_return_turn_id = "turn-1732"
    with patch.object(sdk, "PROJECT_ROOT", Path(root)):
        sdk._write_dispatch_return_context("turn-1732", 7, "sess-1")
    return state, push


def _assistant(*blocks, stop="end_turn"):
    return AssistantMessage(content=list(blocks), model="claude-opus-5-5",
                            stop_reason=stop, parent_tool_use_id=None)


def _result():
    result = MagicMock(spec=ResultMessage)
    result.session_id = "sess-1"
    result.is_error = False
    result.result = ""
    result.num_turns = 1
    return result


def _proactive_turn(root, final):
    """First text + tool, then a final text, then the Result (no pending)."""
    state, push = _state(root)
    bridge = SdkBridge()
    with patch.object(sdk, "PROJECT_ROOT", Path(root)):
        for msg in (
            _assistant(TextBlock(text=FIRST),
                       ToolUseBlock(id="tu-1", name="Read", input={}), stop="tool_use"),
            _assistant(TextBlock(text=final)),
            _result(),
        ):
            asyncio.run(bridge._handle_proactive_message(7, state, msg))
    return state, push


class SubtractDeliveredTest(unittest.TestCase):
    def test_identical_final_leaves_nothing(self):
        self.assertEqual(subtract_delivered(FIRST, FIRST), "")

    def test_repeated_proposal_and_keyboard_leave_nothing(self):
        self.assertEqual(subtract_delivered(PROPOSAL + "\n" + KEYBOARD, FIRST), "")
        # Blank-line layout differences do not defeat the match.
        self.assertEqual(
            subtract_delivered(PROPOSAL + "\n\n" + KEYBOARD, FIRST), "")

    def test_labeled_keyboard_same_labels_is_dropped(self):
        first = RESULT + "\n\n" + PROPOSAL + "\n[[OPTIONS: proceed | hold]]"
        out = subtract_delivered("Also noted: CI is green.\n\n[[OPTIONS: proceed | hold]]", first)
        self.assertEqual(out, "Also noted: CI is green.")

    def test_new_content_survives_without_the_same_keyboard(self):
        final = "Also noted: CI is green.\n\n" + PROPOSAL + "\n" + KEYBOARD
        out = subtract_delivered(final, FIRST)
        self.assertEqual(out, "Also noted: CI is green.")
        self.assertFalse(has_options_marker(out))

    def test_a_different_keyboard_is_new_content(self):
        final = "Merge or rerun?\n[[OPTIONS]]\n1. merge\n2. rerun"
        self.assertEqual(subtract_delivered(final, FIRST), final)

    def test_no_delivery_is_a_noop(self):
        self.assertEqual(subtract_delivered(FIRST, None), FIRST)
        self.assertEqual(subtract_delivered(FIRST, ""), FIRST)

    def test_containment_is_not_a_match(self):
        final = RESULT + " Details follow in the ticket."
        self.assertEqual(subtract_delivered(final, FIRST), final)


class ProactivePathTest(unittest.TestCase):
    def test_first_text_with_keyboard_and_identical_final_is_one_bubble(self):
        with tempfile.TemporaryDirectory() as root:
            state, push = _proactive_turn(root, FIRST)
        push.assert_awaited_once()
        chat, body, has_options, _ = push.await_args[0]
        self.assertEqual(body, FIRST)
        self.assertTrue(has_options)
        self.assertIsNone(state.dispatch_return_delivered)

    def test_final_repeating_only_the_proposal_sends_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            _, push = _proactive_turn(root, PROPOSAL + "\n" + KEYBOARD)
        push.assert_awaited_once()

    def test_final_with_new_content_sends_only_the_new_content(self):
        with tempfile.TemporaryDirectory() as root:
            _, push = _proactive_turn(
                root, "Also noted: CI is green.\n\n" + PROPOSAL + "\n" + KEYBOARD)
        self.assertEqual(push.await_count, 2)
        _, body, has_options, _ = push.await_args_list[1][0]
        self.assertEqual(body, "Also noted: CI is green.")
        self.assertFalse(has_options)

    def test_plain_turn_unchanged(self):
        push = AsyncMock()
        state = _UserStreamState(client=MagicMock(), model=None)
        state.last_chat_id = 11
        state.proactive_push = push
        state.proactive_texts = [FIRST]
        with patch.object(SdkBridge, "_maybe_mark_options",
                          AsyncMock(side_effect=lambda _p, c: (c, False))):
            asyncio.run(SdkBridge()._flush_proactive(7, state))
        push.assert_awaited_once_with(11, FIRST, True, False)

    def test_latch_row_records_the_delivered_keyboard(self):
        with tempfile.TemporaryDirectory() as root:
            state, _ = _state(root)
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                asyncio.run(SdkBridge()._send_dispatch_return_first_text(7, state, FIRST))
                row = json.loads(sdk.dispatch_return_context_path().read_text())
            self.assertTrue(row["visible"])
            self.assertTrue(row["options_delivered"])
            self.assertEqual(state.dispatch_return_delivered, FIRST)
        with tempfile.TemporaryDirectory() as root:
            state, _ = _state(root)
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                asyncio.run(SdkBridge()._send_dispatch_return_first_text(7, state, RESULT))
                row = json.loads(sdk.dispatch_return_context_path().read_text())
            self.assertFalse(row["options_delivered"])


def _owner_request():
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    loop.close()
    req = _PendingRequest(
        user_id=7, chat_id=11, model=None, requested_session_id=None,
        permission_callback=None, typing_callback=None, future=future,
        user_message="done?",
    )
    req.sent = True
    return req


class _TerminalClient:
    async def receive_messages(self):
        yield _assistant(TextBlock(text=FIRST))
        yield _result()


class PendingPathTest(unittest.TestCase):
    def test_terminal_first_text_is_not_streamed_and_is_passed_to_finalize(self):
        with tempfile.TemporaryDirectory() as root:
            state, push = _state(root)
            state.client = _TerminalClient()
            req = _owner_request()
            req.streaming_handler = MagicMock()
            req.streaming_handler.update_if_needed = AsyncMock()
            state.pending.append(req)
            bridge = SdkBridge()
            fin = AsyncMock(return_value=False)
            with patch.object(sdk, "PROJECT_ROOT", Path(root)), \
                    patch.object(bridge, "_finalize_result", fin):
                asyncio.run(bridge._reader_loop(7, state))
        push.assert_awaited_once()
        req.streaming_handler.update_if_needed.assert_not_awaited()
        self.assertEqual(fin.await_args.kwargs["delivered"], FIRST)
        self.assertIsNone(state.dispatch_return_delivered)

    def _finalize(self, texts, delivered):
        req = _owner_request()
        req.last_assistant_texts = list(texts)
        with tempfile.TemporaryDirectory() as root, \
                patch.object(sdk, "PROJECT_ROOT", Path(root)), \
                patch.object(SdkBridge, "_maybe_mark_options",
                             AsyncMock(side_effect=lambda _p, c: (c, False))):
            state = _UserStreamState(client=MagicMock(), model=None)
            asyncio.run(SdkBridge()._finalize_result(
                7, state, req, _result(), delivered=delivered))
        return req.future.result()

    def test_identical_final_resolves_empty(self):
        resp = self._finalize([FIRST], FIRST)
        self.assertTrue(resp.success)
        self.assertEqual(resp.content, "")
        self.assertFalse(resp.has_options)

    def test_new_content_only(self):
        resp = self._finalize(
            [FIRST + "\n\nAlso noted: CI is green."], FIRST)
        self.assertTrue(resp.content.startswith("Also noted: CI is green."))
        self.assertNotIn(RESULT, resp.content)
        self.assertFalse(resp.has_options)

    def test_plain_turn_unchanged(self):
        resp = self._finalize([FIRST], None)
        self.assertIn(RESULT, resp.content)
        self.assertTrue(resp.has_options)


if __name__ == "__main__":
    unittest.main()
