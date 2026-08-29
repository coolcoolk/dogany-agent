"""DGN-581: ESC-style soft interrupt for /stop.

Covers:
  1. sdk_bridge.interrupt() on a live turn: sends the SDK control interrupt,
     keeps the stream state + client + subprocess alive (no disconnect, no
     force-kill), finalizes drafted bubbles in place, drains the whole pending
     queue (DRAIN policy), resolves every future silently (empty content +
     streamed=True), and marks the trailing ResultMessage for a swallow.
  2. interrupt() returns False when there is nothing to catch: no stream, no
     pending, head not dispatched, or no connected streaming query.
  3. interrupt() raising propagates to the caller (so /stop can fall back to
     the hard teardown -- never a silent no-op).
  4. Reader-loop swallow of the interrupted turn's trailing ResultMessage
     (discard_results) without leaking it to the proactive-push path, while a
     genuine proactive result still flushes.
  4b. M1 regression: reader-loop discards stale trailing ResultMessage even
     when a new request is already pending (the DRAIN race window).  Without
     the fix the stale result would resolve the new request's future with the
     old content or leave it permanently unresolved (hang).
  5. bot._cmd_stop wiring: soft-first (reply STOP_INTERRUPTED, no teardown),
     hard fallback on no-target (STOP_NOTHING when idle / STOP_PAUSED when a
     stream was killed) and on interrupt failure.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import ResultMessage

from bridge import messages
from bridge.sdk_bridge import (
    ChatResponse,
    SdkBridge,
    _PendingRequest,
    _UserStreamState,
)

USER_ID = 7


def _make_client(connected: bool = True) -> MagicMock:
    client = MagicMock()
    client.interrupt = AsyncMock()
    client.disconnect = AsyncMock()
    client._query = object() if connected else None
    return client


def _make_request(sent: bool, with_drafts: bool = False) -> _PendingRequest:
    handler = MagicMock()
    handler.finalize_all = AsyncMock()
    handler.drafts = [MagicMock(message_id=111)] if with_drafts else []
    return _PendingRequest(
        user_id=USER_ID,
        chat_id=1,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=asyncio.get_running_loop().create_future(),
        user_message="msg",
        sent=sent,
        streaming_handler=handler,
    )


def _make_result(is_error: bool = True) -> ResultMessage:
    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="sid-1",
    )


class TestSoftInterruptLiveTurn(unittest.TestCase):
    """interrupt() on a live turn keeps the session alive and drains the queue."""

    def test_live_turn_interrupted_in_place(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.last_session_id = "sid-live"
            head = _make_request(sent=True, with_drafts=True)
            queued = _make_request(sent=False)
            state.pending.append(head)
            state.pending.append(queued)
            bridge._streams[USER_ID] = state

            with patch.object(
                SdkBridge, "_force_kill_client_subprocess"
            ) as force_kill:
                result = await bridge.interrupt(USER_ID)

            # Soft interrupt succeeded via the SDK control request.
            self.assertTrue(result)
            client.interrupt.assert_awaited_once()

            # Session survival: stream state kept, no disconnect, no kill.
            self.assertIn(USER_ID, bridge._streams)
            client.disconnect.assert_not_awaited()
            force_kill.assert_not_called()

            # DRAIN policy: the whole pending queue is emptied.
            self.assertEqual(len(state.pending), 0)

            # In-flight drafts preserved via the finalize path.
            head.streaming_handler.finalize_all.assert_awaited_once()

            # Futures resolve silently: empty content + streamed=True renders
            # nothing at the bot reply layer.
            for req in (head, queued):
                self.assertTrue(req.future.done())
                resp: ChatResponse = req.future.result()
                self.assertEqual(resp.content, "")
                self.assertTrue(resp.streamed)
                self.assertFalse(resp.success)
                self.assertEqual(resp.error, "interrupted")
            self.assertEqual(head.future.result().session_id, "sid-live")

            # Only the dispatched head produces a trailing ResultMessage.
            self.assertEqual(state.discard_results, 1)

        asyncio.run(scenario())


class TestSoftInterruptNoTarget(unittest.TestCase):
    """interrupt() returns False when there is nothing to catch."""

    def test_no_stream_returns_false(self):
        async def scenario():
            bridge = SdkBridge()
            self.assertFalse(await bridge.interrupt(USER_ID))

        asyncio.run(scenario())

    def test_no_pending_returns_false(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            bridge._streams[USER_ID] = _UserStreamState(client=client, model=None)
            self.assertFalse(await bridge.interrupt(USER_ID))
            client.interrupt.assert_not_awaited()

        asyncio.run(scenario())

    def test_undispatched_head_returns_false(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.pending.append(_make_request(sent=False))
            bridge._streams[USER_ID] = state
            self.assertFalse(await bridge.interrupt(USER_ID))
            client.interrupt.assert_not_awaited()
            # Nothing drained on the no-target path.
            self.assertEqual(len(state.pending), 1)

        asyncio.run(scenario())

    def test_disconnected_query_returns_false(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client(connected=False)
            state = _UserStreamState(client=client, model=None)
            state.pending.append(_make_request(sent=True))
            bridge._streams[USER_ID] = state
            self.assertFalse(await bridge.interrupt(USER_ID))
            client.interrupt.assert_not_awaited()

        asyncio.run(scenario())


class TestSoftInterruptFailure(unittest.TestCase):
    """A failing interrupt send propagates so the caller can hard-teardown."""

    def test_interrupt_raise_propagates_and_leaves_state(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            client.interrupt = AsyncMock(side_effect=RuntimeError("stuck turn"))
            state = _UserStreamState(client=client, model=None)
            state.pending.append(_make_request(sent=True))
            bridge._streams[USER_ID] = state
            with self.assertRaises(RuntimeError):
                await bridge.interrupt(USER_ID)
            # Queue untouched: the hard-teardown fallback owns the cleanup.
            self.assertEqual(len(state.pending), 1)
            self.assertIn(USER_ID, bridge._streams)

        asyncio.run(scenario())


class TestDiscardTrailingResult(unittest.TestCase):
    """The interrupted turn's trailing ResultMessage must not leak as a push."""

    def test_trailing_result_swallowed(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.discard_results = 1
            state.proactive_texts = ["aborted tail text"]
            state.last_chat_id = 1
            state.proactive_push = AsyncMock()
            bridge._streams[USER_ID] = state

            await bridge._handle_proactive_message(USER_ID, state, _make_result())

            self.assertEqual(state.discard_results, 0)
            self.assertEqual(state.proactive_texts, [])
            state.proactive_push.assert_not_awaited()

        asyncio.run(scenario())

    def test_genuine_proactive_result_still_flushes(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.proactive_texts = ["real proactive text"]
            state.last_chat_id = 1
            state.proactive_push = AsyncMock()
            bridge._streams[USER_ID] = state

            await bridge._handle_proactive_message(
                USER_ID, state, _make_result(is_error=False)
            )

            state.proactive_push.assert_awaited_once()

        asyncio.run(scenario())


class TestM1ReaderLoopDiscard(unittest.TestCase):
    """M1 regression: stale trailing ResultMessage discarded via reader loop
    even when a new pending request is already queued (the DRAIN race window).

    Scenario: interrupt() drains the queue -> discard_results=1.  Before the
    CLI emits the trailing ResultMessage, the user sends a new message which
    gets appended to state.pending.  The reader loop now sees the trailing
    ResultMessage with state.pending non-empty.  Without the fix, the result
    would be misrouted to the new request's future (content corruption or hang).
    With the fix, the result is silently discarded and discard_results returns
    to 0; the new request's future stays unresolved until a genuine result arrives.
    """

    def _make_new_request(self):
        loop = asyncio.get_running_loop()
        handler = MagicMock()
        handler.finalize_all = AsyncMock()
        handler.drafts = []
        return _PendingRequest(
            user_id=USER_ID,
            chat_id=1,
            model=None,
            requested_session_id=None,
            permission_callback=None,
            typing_callback=None,
            future=loop.create_future(),
            user_message="new message after interrupt",
            sent=False,
            streaming_handler=handler,
        )

    def test_stale_result_discarded_when_new_request_pending(self):
        """Stale trailing result is dropped; new request future stays pending."""

        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.last_session_id = "sid-x"

            # Simulate post-interrupt state: queue drained, discard_results=1.
            state.discard_results = 1

            # New request arrived before the trailing ResultMessage (the race).
            new_req = self._make_new_request()
            state.pending.append(new_req)
            bridge._streams[USER_ID] = state

            stale_result = _make_result(is_error=False)

            async def fake_receive():
                yield stale_result

            state.client.receive_messages = fake_receive

            # Run the reader loop; it will consume the stale ResultMessage and exit.
            with patch.object(SdkBridge, "_dispatch_next_query", new=AsyncMock()):
                await bridge._reader_loop(USER_ID, state)

            # discard_results consumed.
            self.assertEqual(state.discard_results, 0)
            # The new request's future must NOT have been resolved by the stale result.
            self.assertFalse(
                new_req.future.done(),
                "New request future must stay unresolved -- stale result must be discarded",
            )
            # The new request is still in the queue, waiting for a real result.
            self.assertIn(new_req, state.pending)

        asyncio.run(scenario())

    def test_stale_result_with_text_does_not_corrupt_new_request(self):
        """Even a non-empty stale result must not resolve the new request's future."""

        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.discard_results = 1
            new_req = self._make_new_request()
            state.pending.append(new_req)
            bridge._streams[USER_ID] = state

            # Stale result carries actual text that would corrupt the new request.
            stale_result = ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="sid-old",
                result="OLD interrupted tail text",
            )

            async def fake_receive():
                yield stale_result

            state.client.receive_messages = fake_receive
            with patch.object(SdkBridge, "_dispatch_next_query", new=AsyncMock()):
                await bridge._reader_loop(USER_ID, state)

            self.assertEqual(state.discard_results, 0)
            self.assertFalse(
                new_req.future.done(),
                "New request future must not be resolved with old interrupted tail text",
            )

        asyncio.run(scenario())


class TestCmdStopWiring(unittest.TestCase):
    """/stop soft-first with hard fallback."""

    def _make_update(self):
        update = MagicMock()
        update.effective_user.id = USER_ID
        update.message.reply_text = AsyncMock()
        return update

    def _run(self, coro):
        asyncio.run(coro)

    def _patched_bot(self, mock_sdk):
        from bridge.bot import TelegramBot

        access = patch.object(
            TelegramBot, "_check_access", new=AsyncMock(return_value=True)
        )
        sdk = patch("bridge.bot.sdk_bridge", mock_sdk)
        return TelegramBot, access, sdk

    def _mock_sdk(self, interrupt_result=None, interrupt_error=None, stop_result=False):
        mock_sdk = MagicMock()
        if interrupt_error is not None:
            mock_sdk.interrupt = AsyncMock(side_effect=interrupt_error)
        else:
            mock_sdk.interrupt = AsyncMock(return_value=interrupt_result)
        mock_sdk.stop = AsyncMock(return_value=stop_result)
        mock_sdk.cancel_user_streaming = AsyncMock(return_value=False)
        # DGN-1015: real sdk_bridge.pop_interrupt_killed() returns [] when no
        # background subagent died -- a bare MagicMock() default (truthy,
        # empty-iterable) would falsely trip the new kill-notice append.
        mock_sdk.pop_interrupt_killed = MagicMock(return_value=[])
        return mock_sdk

    def test_stop_soft_success_keeps_session(self):
        async def scenario():
            mock_sdk = self._mock_sdk(interrupt_result=True)
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                await bot._cmd_stop(update, None)
            # DGN-1016: /stop tags its interrupt origin for log attribution.
            mock_sdk.interrupt.assert_awaited_once_with(USER_ID, trigger="stop")
            # Soft success: NO hard teardown of any kind.
            mock_sdk.stop.assert_not_awaited()
            mock_sdk.cancel_user_streaming.assert_not_awaited()
            # DGN-991 stopgap A (rev3): soft success carries the background note.
            update.message.reply_text.assert_awaited_once_with(
                f"{messages.STOP_INTERRUPTED}\n{messages.STOP_BG_NOTE}"
            )

        self._run(scenario())

    def test_stop_no_target_idle_reports_nothing(self):
        async def scenario():
            mock_sdk = self._mock_sdk(interrupt_result=False, stop_result=False)
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                await bot._cmd_stop(update, None)
            mock_sdk.stop.assert_awaited_once_with(USER_ID)
            update.message.reply_text.assert_awaited_once_with(messages.STOP_NOTHING)

        self._run(scenario())

    def test_stop_no_target_live_stream_hard_stops(self):
        async def scenario():
            mock_sdk = self._mock_sdk(interrupt_result=False, stop_result=True)
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                await bot._cmd_stop(update, None)
            mock_sdk.stop.assert_awaited_once_with(USER_ID)
            # DGN-991 stopgap B: a real teardown (stream killed) reports the
            # honest forced copy, not the "session intact" claim.
            update.message.reply_text.assert_awaited_once_with(messages.STOP_FORCED)

        self._run(scenario())

    def test_stop_interrupt_failure_falls_back_to_hard_teardown(self):
        async def scenario():
            mock_sdk = self._mock_sdk(
                interrupt_error=RuntimeError("control channel stuck"),
                stop_result=True,
            )
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                await bot._cmd_stop(update, None)
            # Never a silent no-op: the hard teardown ran and was reported.
            # DGN-991 stopgap B: teardown that killed the stream -> forced copy.
            mock_sdk.stop.assert_awaited_once_with(USER_ID)
            update.message.reply_text.assert_awaited_once_with(messages.STOP_FORCED)

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()
