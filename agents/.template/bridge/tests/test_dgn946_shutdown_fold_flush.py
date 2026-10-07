"""DGN-946: shutdown fold flush -- no fold bubble freezes in raw live form.

Root cause (measured, bot.log): on a real stop the run loop tears the
Telegram HTTP client down (application.shutdown()), while _process_message
tasks are still awaiting their futures. asyncio.run() cancels those tasks
only AFTER the run coroutine returns ("Bot stopped"), so the CancelledError
cleanup hook (_fold_finalize) fires on a dead client -- RuntimeError('This
HTTPXRequest is not initialized!') -- and the fold bubble freezes in its
last live (mid-stream) form.

Fix under test: SdkBridge.flush_folds_for_shutdown() drains every in-flight
fold WHILE the HTTP client is still alive (called from the run loop finally,
gated on the real stop_event, before _graceful_shutdown). The fold_finalized
latch then turns the late cancellation hooks into network-free no-ops.

Covers:
- flush finalizes a grown fold (collapse + stop caption edit goes out).
- after the flush, the late (post-teardown) cleanup path makes ZERO network
  calls -- the idempotent latch is respected, no double edit.
- bubbles with nothing to finalize (no fold / already finalized) are skipped.
- the sweep respects its wall-clock budget: a hanging edit cannot stall
  shutdown, and the timed-out bubble is left in a SAFE state (latch set, so
  no post-teardown error spam).
- the sweep never raises, even when the edit fails hard.
- run-loop wiring: the flush is called in _run_async's finally, gated on
  stop_event, BEFORE _graceful_shutdown tears the HTTP client down.
"""

import asyncio
import inspect
import unittest
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

# conftest.py already sets PROJECT_ROOT and TELEGRAM_BOT_TOKEN before import.
from bridge.formatting import FOLD_CAPTION_STOPPED
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState


def _mock_bot(message_id: int = 777) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=message_id))
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _mock_handler(bot: Optional[MagicMock] = None) -> MagicMock:
    handler = MagicMock()
    handler.bot = bot or _mock_bot()
    handler.drafts = []
    handler.update_if_needed = AsyncMock(return_value=True)
    handler.finalize_all = AsyncMock(return_value=True)
    handler.cancel = AsyncMock(return_value=True)
    return handler


def _make_pending_req(streaming_handler=None) -> _PendingRequest:
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


def _grown_req():
    handler = _mock_handler()
    req = _make_pending_req(streaming_handler=handler)
    req.fold_msg_id = 777
    req.fold_buf = ["step one", "step two"]
    req.interim_texts = list(req.fold_buf)
    return req, handler


def _bridge_with(reqs) -> SdkBridge:
    bridge_obj = SdkBridge()
    state = _UserStreamState(client=MagicMock(), model=None)
    for req in reqs:
        state.pending.append(req)
    bridge_obj._streams[1] = state
    return bridge_obj


def _edit_texts(bot: MagicMock) -> List[str]:
    return [c.kwargs.get("text", "") for c in bot.edit_message_text.call_args_list]


class TestShutdownFoldFlush(unittest.TestCase):
    def test_flush_finalizes_grown_fold(self):
        async def _inner():
            req, handler = _grown_req()
            bridge_obj = _bridge_with([req])
            flushed = await bridge_obj.flush_folds_for_shutdown()
            return flushed, req, handler

        flushed, req, handler = asyncio.run(_inner())
        self.assertEqual(flushed, 1)
        self.assertTrue(req.fold_finalized)
        html = _edit_texts(handler.bot)[0]
        self.assertTrue(
            html.startswith("<blockquote expandable>" + FOLD_CAPTION_STOPPED)
        )

    def test_late_cleanup_after_flush_makes_no_network_call(self):
        # The post-teardown CancelledError hooks call _fold_finalize AGAIN on
        # a dead HTTP client. After the flush, the latch must short-circuit
        # them BEFORE any network attempt -- no RuntimeError, no raw freeze.
        async def _inner():
            req, handler = _grown_req()
            bridge_obj = _bridge_with([req])
            await bridge_obj.flush_folds_for_shutdown()
            edits_after_flush = handler.bot.edit_message_text.await_count
            # Simulate the torn-down client: any edit now raises.
            handler.bot.edit_message_text = AsyncMock(
                side_effect=RuntimeError("This HTTPXRequest is not initialized!")
            )
            had_fold = await bridge_obj._fold_finalize(req, FOLD_CAPTION_STOPPED)
            return edits_after_flush, had_fold, handler

        edits_after_flush, had_fold, handler = asyncio.run(_inner())
        self.assertEqual(edits_after_flush, 1)
        self.assertFalse(had_fold)  # latch: nothing left to finalize
        handler.bot.edit_message_text.assert_not_awaited()

    def test_flush_skips_foldless_and_already_finalized(self):
        async def _inner():
            no_fold_req, no_fold_handler = _grown_req()
            no_fold_req.fold_msg_id = None
            done_req, done_handler = _grown_req()
            done_req.fold_finalized = True
            bridge_obj = _bridge_with([no_fold_req, done_req])
            flushed = await bridge_obj.flush_folds_for_shutdown()
            return flushed, no_fold_handler, done_handler

        flushed, no_fold_handler, done_handler = asyncio.run(_inner())
        self.assertEqual(flushed, 0)
        no_fold_handler.bot.edit_message_text.assert_not_awaited()
        done_handler.bot.edit_message_text.assert_not_awaited()

    def test_flush_respects_budget_and_leaves_safe_state(self):
        # A hanging Telegram edit must not stall shutdown: the sweep gives up
        # at its wall-clock budget. The timed-out bubble stays SAFE: the
        # latch was set before the edit, so the post-teardown hooks stay
        # network-free (degraded to today's last-live-form, never worse).
        async def _inner():
            req, handler = _grown_req()

            async def _hang(**_kwargs):
                await asyncio.sleep(30)

            handler.bot.edit_message_text = AsyncMock(side_effect=_hang)
            bridge_obj = _bridge_with([req])
            loop = asyncio.get_event_loop()
            t0 = loop.time()
            flushed = await bridge_obj.flush_folds_for_shutdown(budget=0.2)
            elapsed = loop.time() - t0
            return flushed, elapsed, req

        flushed, elapsed, req = asyncio.run(_inner())
        self.assertEqual(flushed, 0)
        self.assertLess(elapsed, 2.0)  # bounded, no 30s stall
        self.assertTrue(req.fold_finalized)  # safe latch despite timeout

    def test_flush_never_raises_on_hard_edit_failure(self):
        async def _inner():
            req, handler = _grown_req()
            handler.bot.edit_message_text = AsyncMock(
                side_effect=RuntimeError("boom")
            )
            bridge_obj = _bridge_with([req])
            # Must not raise; _fold_finalize absorbs the edit failure.
            flushed = await bridge_obj.flush_folds_for_shutdown()
            return flushed, req

        flushed, req = asyncio.run(_inner())
        self.assertEqual(flushed, 1)
        self.assertTrue(req.fold_finalized)

    def test_flush_covers_multiple_streams_and_requests(self):
        async def _inner():
            req_a, handler_a = _grown_req()
            req_b, handler_b = _grown_req()
            bridge_obj = _bridge_with([req_a, req_b])
            flushed = await bridge_obj.flush_folds_for_shutdown()
            return flushed, handler_a, handler_b

        flushed, handler_a, handler_b = asyncio.run(_inner())
        self.assertEqual(flushed, 2)
        self.assertEqual(handler_a.bot.edit_message_text.await_count, 1)
        self.assertEqual(handler_b.bot.edit_message_text.await_count, 1)


class TestRunLoopWiring(unittest.TestCase):
    """The flush must run in the run loop's finally, gated on the REAL stop
    event, BEFORE _graceful_shutdown tears the HTTP client down. Transient
    restart branches (Conflict/NetworkError) must NOT flush -- their turns
    stay alive and a flush would collapse a still-growing fold."""

    def test_flush_called_before_graceful_shutdown_gated_on_stop(self):
        from bridge.bot import TelegramBot

        src = inspect.getsource(TelegramBot._run_async)
        self.assertIn("flush_folds_for_shutdown", src)
        flush_at = src.index("flush_folds_for_shutdown")
        # The ONLY _graceful_shutdown call after the flush is the finally's
        # teardown; every earlier one belongs to a transient-restart branch.
        self.assertIn("_graceful_shutdown()", src[flush_at:])
        # Gate: the flush rides on the real stop event.
        gate_at = src.rindex("stop_event.is_set()", 0, flush_at)
        gated_block = src[gate_at:flush_at]
        self.assertNotIn("_graceful_shutdown", gated_block)


if __name__ == "__main__":
    unittest.main()
