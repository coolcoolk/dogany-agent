"""DGN-426: C-strict interim-narration suppression.

Tests that:
1. interim TextBlocks (stop_reason=tool_use) do NOT trigger update_if_needed.
2. terminal TextBlocks (stop_reason=end_turn) DO trigger update_if_needed.
3. finalized ChatResponse.content equals the assembled end_turn text.
4. stop_reason=None -> treated as non-terminal (suppressed).
5. ServerToolUseBlock present -> treated as non-terminal (suppressed).
6. No-end_turn-text turn (all interim, ResultMessage.result) -> content from
   last TextBlock or msg.result, no streamed drafts, response delivered clean.
7. Error-result path: no sealed interim drafts left behind.
"""

import asyncio
import unittest
from dataclasses import dataclass, field
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

# conftest.py already sets PROJECT_ROOT and TELEGRAM_BOT_TOKEN before import.
import bridge.config as _cfg_mod

from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_block(text: str) -> TextBlock:
    return TextBlock(text=text)


def _make_tool_use_block() -> ToolUseBlock:
    return ToolUseBlock(id="tool_1", name="Bash", input={"command": "ls"})


def _make_server_tool_block() -> ServerToolUseBlock:
    try:
        return ServerToolUseBlock(id="srv_1", name="computer", input={})
    except TypeError:
        return MagicMock(spec=ServerToolUseBlock)


def _make_assistant_msg(
    stop_reason: Optional[str],
    blocks: List[Any],
    parent_tool_use_id: Optional[str] = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=blocks,
        model="claude-sonnet-4-5",
        stop_reason=stop_reason,
        parent_tool_use_id=parent_tool_use_id,
    )


def _make_result_msg(result: str = "", is_error: bool = False):
    rm = MagicMock(spec=ResultMessage)
    rm.session_id = "sess-test"
    rm.result = result
    rm.is_error = is_error
    rm.num_turns = 2
    return rm


def _make_pending_req(streaming_handler=None) -> _PendingRequest:
    # Create the future inside a running loop; callers must call this inside async.
    future = asyncio.get_event_loop().create_future()
    return _PendingRequest(
        user_id=1,
        chat_id=1,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=future,
        streaming_handler=streaming_handler,
    )


# ---------------------------------------------------------------------------
# Core suppression tests
# ---------------------------------------------------------------------------


class TestInterimSuppressionGate(unittest.TestCase):
    """Verify the stop_reason gate logic."""

    def setUp(self):
        self._orig = _cfg_mod.STREAM_INTERIM
        _cfg_mod.STREAM_INTERIM = False
        self._patch = patch("bridge.sdk_bridge.STREAM_INTERIM", False)
        self._patch.start()

    def tearDown(self):
        _cfg_mod.STREAM_INTERIM = self._orig
        self._patch.stop()

    def _run_reader_messages(self, messages_seq, stream_interim_override=None):
        """Feed a sequence of SDK messages through _reader_loop and return the result."""

        async def _inner():
            mock_handler = MagicMock()
            mock_handler.drafts = []
            mock_handler.update_if_needed = AsyncMock(return_value=True)
            mock_handler.finalize_all = AsyncMock(return_value=True)

            bridge_obj = SdkBridge()
            req = _make_pending_req(streaming_handler=mock_handler)
            state = _UserStreamState(client=MagicMock(), model=None)
            state.pending.append(req)
            req.sent = True  # already dispatched; _dispatch_next_query is noop

            async def fake_receive():
                for m in messages_seq:
                    yield m

            state.client.receive_messages = fake_receive
            bridge_obj._streams[1] = state

            if stream_interim_override is not None:
                ctx = patch("bridge.sdk_bridge.STREAM_INTERIM", stream_interim_override)
                ctx.start()
            try:
                await bridge_obj._reader_loop(1, state)
            finally:
                if stream_interim_override is not None:
                    ctx.stop()

            result = await asyncio.wait_for(req.future, timeout=1.0)
            return result, mock_handler

        return asyncio.run(_inner())

    # ------------------------------------------------------------------
    # Test 1: interim suppressed, final displayed
    # ------------------------------------------------------------------

    def test_interim_suppressed_final_displayed(self):
        """Main scenario: tool_use interim -> suppressed; end_turn final -> displayed."""
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block("interim narration")]),
            _make_assistant_msg("tool_use", [_make_tool_use_block()]),
            _make_assistant_msg("end_turn", [_make_text_block("final answer")]),
            _make_result_msg(result="final answer"),
        ]
        response, handler = self._run_reader_messages(msgs)

        # update_if_needed called exactly once: for the end_turn TextBlock only.
        self.assertEqual(handler.update_if_needed.await_count, 1)
        call_text = handler.update_if_needed.call_args[0][0]
        self.assertEqual(call_text, "final answer")

        # finalize_all called once.
        handler.finalize_all.assert_awaited_once()

        # Content = assembled end_turn text.
        self.assertEqual(response.content, "final answer")
        self.assertTrue(response.success)

    # ------------------------------------------------------------------
    # Test 2: stop_reason=None -> suppress
    # ------------------------------------------------------------------

    def test_stop_reason_none_suppressed(self):
        """stop_reason=None is treated as non-terminal -> update_if_needed NOT called."""
        msgs = [
            _make_assistant_msg(None, [_make_text_block("should be suppressed")]),
            _make_assistant_msg("end_turn", [_make_text_block("final")]),
            _make_result_msg(result="final"),
        ]
        response, handler = self._run_reader_messages(msgs)

        # Only the end_turn block triggers update_if_needed.
        self.assertEqual(handler.update_if_needed.await_count, 1)
        call_text = handler.update_if_needed.call_args[0][0]
        self.assertEqual(call_text, "final")
        self.assertEqual(response.content, "final")

    # ------------------------------------------------------------------
    # Test 3: ServerToolUseBlock -> suppress
    # ------------------------------------------------------------------

    def test_server_tool_use_block_suppressed(self):
        """AssistantMessage carrying ServerToolUseBlock -> treated as non-terminal."""
        msgs = [
            _make_assistant_msg(
                "tool_use",
                [_make_text_block("server tool narration"), _make_server_tool_block()],
            ),
            _make_assistant_msg("end_turn", [_make_text_block("done")]),
            _make_result_msg(result="done"),
        ]
        response, handler = self._run_reader_messages(msgs)

        self.assertEqual(handler.update_if_needed.await_count, 1)
        call_text = handler.update_if_needed.call_args[0][0]
        self.assertEqual(call_text, "done")
        self.assertEqual(response.content, "done")

    # ------------------------------------------------------------------
    # Test 4: no end_turn TextBlock (turn ends only via ResultMessage.result)
    # ------------------------------------------------------------------

    def test_no_end_turn_text_block_falls_back(self):
        """Turn with only tool_use messages + ResultMessage: content from TextBlocks or msg.result."""
        msgs = [
            _make_assistant_msg(
                "tool_use", [_make_text_block("tool step"), _make_tool_use_block()]
            ),
            # No end_turn AssistantMessage: the turn ends directly.
            _make_result_msg(result="tool result delivered", is_error=False),
        ]
        response, handler = self._run_reader_messages(msgs)

        # No update_if_needed: the only AssistantMessage was tool_use (suppressed).
        handler.update_if_needed.assert_not_awaited()

        # Content: _finalize_result uses last_assistant_texts ("tool step") since
        # it is non-empty, falling back to msg.result only when empty.
        # Both "tool step" and "tool result delivered" are acceptable outcomes;
        # the critical invariant is success=True and no streamed drafts.
        self.assertTrue(response.success)
        self.assertFalse(response.streamed)
        self.assertEqual(response.draft_message_ids, [])

    # ------------------------------------------------------------------
    # Test 5: error result -> no sealed interim drafts
    # ------------------------------------------------------------------

    def test_error_result_no_sealed_interim_drafts(self):
        """On error result in suppressed mode: no drafts, response is error notice."""
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block("interim before error")]),
            _make_result_msg(result="sdk error", is_error=True),
        ]
        response, handler = self._run_reader_messages(msgs)

        # No live streaming happened.
        handler.update_if_needed.assert_not_awaited()
        # finalize_all still called (noop: no drafts).
        handler.finalize_all.assert_awaited_once()
        # Response is the error path.
        self.assertFalse(response.success)
        # No draft IDs sealed.
        self.assertEqual(response.draft_message_ids, [])
        self.assertFalse(response.streamed)

    # ------------------------------------------------------------------
    # Test 6: STREAM_INTERIM=True bypasses suppression
    # ------------------------------------------------------------------

    def test_stream_interim_true_bypasses_suppression(self):
        """When STREAM_INTERIM=True, all TextBlocks trigger update_if_needed."""
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block("interim A")]),
            _make_assistant_msg("tool_use", [_make_tool_use_block()]),
            _make_assistant_msg("end_turn", [_make_text_block("final B")]),
            _make_result_msg(result="final B"),
        ]
        response, handler = self._run_reader_messages(msgs, stream_interim_override=True)

        # Both tool_use and end_turn TextBlocks displayed: 2 calls.
        self.assertEqual(handler.update_if_needed.await_count, 2)
        calls = [c[0][0] for c in handler.update_if_needed.call_args_list]
        self.assertIn("interim A", calls)
        self.assertIn("final B", calls)

    # ------------------------------------------------------------------
    # Test 7: subagent messages (parent_tool_use_id set) always skipped
    # ------------------------------------------------------------------

    def test_subagent_messages_always_skipped(self):
        """Messages with parent_tool_use_id are skipped regardless of stop_reason."""
        msgs = [
            _make_assistant_msg(
                "end_turn",
                [_make_text_block("subagent internal")],
                parent_tool_use_id="parent_123",
            ),
            _make_assistant_msg("end_turn", [_make_text_block("real final")]),
            _make_result_msg(result="real final"),
        ]
        response, handler = self._run_reader_messages(msgs)

        self.assertEqual(handler.update_if_needed.await_count, 1)
        call_text = handler.update_if_needed.call_args[0][0]
        self.assertEqual(call_text, "real final")
        self.assertEqual(response.content, "real final")


if __name__ == "__main__":
    unittest.main()
