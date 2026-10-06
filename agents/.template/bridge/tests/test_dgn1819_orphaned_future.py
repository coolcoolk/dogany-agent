"""DGN-1819: an owner request's future must never be orphaned at turn close.

Measured defect (dev-instance bot.log 2026-09-26 / 09-28 / 10-01): "Query hit soft
stop ... after 1140s" and "Query timed out ... after 1200s" in the same ms --
the soft stop found nothing to interrupt because the request was no longer
in the pending deque, yet its future was never resolved. The owner got a
time-limit notice for a turn that had already ended. Two mechanisms:

  A. Empty-final drop (DGN-519): _finalize_result returned without resolving
     the future; the reader then popped the request -> orphaned future.
  B. Auto-interrupt during _finalize_result (the options classifier await):
     the drain resolved A as "interrupted" and counted a trailing result that
     had already been consumed (discard_results leak); the reader's blind
     popleft then took the NEXT request B (orphaned; B's own result swallowed
     by the leaked discard) or crashed on an empty deque (IndexError).

Covers:
  T1  process_message-level: an empty-final turn returns promptly, NOT as a
      timeout (the _finalize_result-level flip lives in test_dgn519).
  T2  race: classifier wait + auto interrupt + B dispatch -> interrupt False,
      A resolved with A text, B resolved with B text, discard_results == 0,
      pending empty.
  T2b same race, no B -> no reader crash, A resolved, pending empty.
  T2c popleft defense: a head that changed under the finalize await is not
      popped.
  T3  bot fallback: an auto-interrupt that returns False (finalizing head)
      parks the new message and the finishing turn's drain delivers it.
"""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

import bridge.sdk_bridge as sdk_mod
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState

USER_ID = 1819
_END = object()


class _QueueClient:
    """Fake streaming client: receive_messages() yields what the test feeds."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.query = AsyncMock()
        self.interrupt = AsyncMock()
        self.disconnect = AsyncMock()
        self._query = object()

    async def receive_messages(self):
        while True:
            item = await self.q.get()
            if item is _END:
                return
            yield item


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)] if text else [],
        model="m",
        stop_reason="end_turn",
        session_id="sid-1819",
    )


def _result(text=None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sid-1819",
        result=text,
    )


def _request(msg: str, sent: bool) -> _PendingRequest:
    return _PendingRequest(
        user_id=USER_ID,
        chat_id=1,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=asyncio.get_running_loop().create_future(),
        user_message=msg,
        sent=sent,
    )


def _setup():
    bridge = SdkBridge()
    client = _QueueClient()
    state = _UserStreamState(client=client, model=None)
    state.last_session_id = "sid-1819"
    state.last_chat_id = 1
    state.proactive_push = AsyncMock()
    bridge._streams[USER_ID] = state
    return bridge, client, state


def _blocking_classifier():
    """Patch target for _maybe_mark_options: the FIRST call blocks on
    `release` (the Haiku round-trip), later calls pass through."""
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fake(prev_message, content):
        calls.append(content)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return content, False

    return staticmethod(fake), entered, release


class TestEmptyFinalProcessMessage(unittest.TestCase):
    """T1: an empty-final turn resolves promptly through process_message."""

    def test_empty_final_is_not_a_timeout(self):
        async def scenario():
            bridge, client, state = _setup()

            async def on_query(*a, **k):
                # The turn ends with no surviving text (e.g. every block was
                # dropped by the register guard) -> DGN-519 empty-final drop.
                client.q.put_nowait(_assistant(""))
                client.q.put_nowait(_result(None))

            client.query.side_effect = on_query
            reader = asyncio.create_task(bridge._reader_loop(USER_ID, state))
            with patch.object(sdk_mod, "PROCESS_TIMEOUT", 1.0), patch.object(
                sdk_mod, "TIMEOUT_STOP_GRACE", 0.5
            ), patch.object(
                SdkBridge, "_get_or_create_stream", new=AsyncMock(return_value=state)
            ), patch.object(SdkBridge, "_force_kill_client_subprocess"):
                response = await bridge.process_message(
                    user_message="hi", user_id=USER_ID, chat_id=1
                )
            client.q.put_nowait(_END)
            await reader

            self.assertFalse(response.timed_out, "empty-final must not time out")
            self.assertTrue(response.success)
            self.assertEqual(response.content, "")
            self.assertTrue(response.streamed)
            self.assertEqual(list(state.pending), [])
            client.interrupt.assert_not_awaited()

        asyncio.run(scenario())


class TestInterruptDuringFinalize(unittest.TestCase):
    """T2 / T2b: auto-interrupt landing while the reader finalizes A."""

    def _run(self, with_b: bool):
        async def scenario():
            bridge, client, state = _setup()
            fake, entered, release = _blocking_classifier()
            a = _request("question A", sent=True)
            state.pending.append(a)
            with patch.object(SdkBridge, "_maybe_mark_options", new=fake), patch.object(
                SdkBridge, "_force_kill_client_subprocess"
            ) as force_kill:
                reader = asyncio.create_task(bridge._reader_loop(USER_ID, state))
                client.q.put_nowait(_assistant("Answer A\n1. one\n2. two"))
                client.q.put_nowait(_result())
                await asyncio.wait_for(entered.wait(), 2)

                # The owner's next message clears the debounce window while
                # A's finalize awaits the classifier.
                interrupted = await bridge.interrupt(USER_ID, trigger="auto")

                b = None
                if with_b:
                    b = _request("message B", sent=False)
                    async with state.send_lock:
                        state.pending.append(b)
                        await bridge._dispatch_next_query(state)

                release.set()
                await asyncio.wait_for(asyncio.shield(a.future), 2)
                if with_b:
                    # B is dispatched once A is off the head, then answers.
                    for _ in range(50):
                        if b.sent:
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(b.sent, "B was never dispatched")
                    client.q.put_nowait(_assistant("Answer B"))
                    client.q.put_nowait(_result())
                    b_resp = await asyncio.wait_for(asyncio.shield(b.future), 2)
                    self.assertIn("Answer B", b_resp.content)
                    self.assertTrue(b_resp.success)
                    self.assertFalse(b_resp.timed_out)
                    self.assertEqual(client.query.await_count, 1)
                client.q.put_nowait(_END)
                await asyncio.wait_for(reader, 2)

            self.assertFalse(interrupted, "a finalizing head must not be interrupted")
            client.interrupt.assert_not_awaited()
            a_resp = a.future.result()
            self.assertTrue(a_resp.success)
            self.assertIn("Answer A", a_resp.content)
            self.assertEqual(state.discard_results, 0)
            self.assertEqual(list(state.pending), [])
            # No reader crash: the crash path pops the stream and force-kills.
            self.assertIn(USER_ID, bridge._streams)
            force_kill.assert_not_called()
            state.proactive_push.assert_not_awaited()

        asyncio.run(scenario())

    def test_t2_interrupt_during_finalize_with_next_message(self):
        self._run(with_b=True)

    def test_t2b_interrupt_during_finalize_without_next_message(self):
        self._run(with_b=False)


class TestPopleftDefense(unittest.TestCase):
    """T2c: the reader pops only the request it just finalized."""

    def test_changed_head_is_not_popped(self):
        async def scenario():
            bridge, client, state = _setup()
            a = _request("A", sent=True)
            other = _request("other", sent=False)
            state.pending.append(a)

            async def fake_finalize(self_, user_id, st, req, msg, delivered=None):
                # The queue changes under the finalize await.
                st.pending.clear()
                st.pending.append(other)
                if not req.future.done():
                    req.future.set_result(sdk_mod.ChatResponse(content="A"))
                return False

            with patch.object(SdkBridge, "_finalize_result", new=fake_finalize), patch.object(
                SdkBridge, "_dispatch_next_query", new=AsyncMock()
            ):
                reader = asyncio.create_task(bridge._reader_loop(USER_ID, state))
                client.q.put_nowait(_result())
                client.q.put_nowait(_END)
                await asyncio.wait_for(reader, 2)

            self.assertEqual(list(state.pending), [other])
            self.assertFalse(other.future.done())
            self.assertIn(USER_ID, bridge._streams)

        asyncio.run(scenario())

    def test_empty_deque_after_finalize_does_not_crash(self):
        async def scenario():
            bridge, client, state = _setup()
            a = _request("A", sent=True)
            state.pending.append(a)

            async def fake_finalize(self_, user_id, st, req, msg, delivered=None):
                st.pending.clear()
                if not req.future.done():
                    req.future.set_result(sdk_mod.ChatResponse(content="A"))
                return False

            with patch.object(SdkBridge, "_finalize_result", new=fake_finalize), patch.object(
                SdkBridge, "_dispatch_next_query", new=AsyncMock()
            ), patch.object(SdkBridge, "_force_kill_client_subprocess") as force_kill:
                reader = asyncio.create_task(bridge._reader_loop(USER_ID, state))
                client.q.put_nowait(_result())
                client.q.put_nowait(_END)
                await asyncio.wait_for(reader, 2)

            force_kill.assert_not_called()
            self.assertIn(USER_ID, bridge._streams)

        asyncio.run(scenario())


class TestFinalizingFlagLifecycle(unittest.TestCase):
    """The flag is only live while the reader is inside _finalize_result."""

    def test_flag_set_during_finalize_and_interrupt_declines(self):
        async def scenario():
            bridge, client, state = _setup()
            a = _request("A", sent=True)
            state.pending.append(a)
            # Live turn: interrupt is allowed (flag not set before the result).
            self.assertFalse(a.finalizing)
            seen = []

            async def fake_finalize(self_, user_id, st, req, msg, delivered=None):
                seen.append(req.finalizing)
                seen.append(await bridge.interrupt(USER_ID, trigger="stop"))
                req.future.set_result(sdk_mod.ChatResponse(content="A"))
                return False

            with patch.object(SdkBridge, "_finalize_result", new=fake_finalize), patch.object(
                SdkBridge, "_dispatch_next_query", new=AsyncMock()
            ):
                reader = asyncio.create_task(bridge._reader_loop(USER_ID, state))
                client.q.put_nowait(_result())
                client.q.put_nowait(_END)
                await asyncio.wait_for(reader, 2)

            self.assertEqual(seen, [True, False])
            client.interrupt.assert_not_awaited()
            self.assertEqual(state.discard_results, 0)

        asyncio.run(scenario())

    def test_flake_retry_clears_flag(self):
        async def scenario():
            bridge, client, state = _setup()
            a = _request("A", sent=True)
            state.pending.append(a)
            calls = []

            async def fake_finalize(self_, user_id, st, req, msg, delivered=None):
                calls.append(req.finalizing)
                if len(calls) == 1:
                    return True  # DGN-670 retry re-dispatched; head stays
                req.future.set_result(sdk_mod.ChatResponse(content="A"))
                return False

            flags_between = []
            with patch.object(SdkBridge, "_finalize_result", new=fake_finalize), patch.object(
                SdkBridge, "_dispatch_next_query", new=AsyncMock()
            ):
                reader = asyncio.create_task(bridge._reader_loop(USER_ID, state))
                client.q.put_nowait(_result())
                for _ in range(50):
                    if calls:
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.01)
                flags_between.append(a.finalizing)
                client.q.put_nowait(_result())
                client.q.put_nowait(_END)
                await asyncio.wait_for(reader, 2)

            self.assertEqual(flags_between, [False], "retry turn must be interruptible")
            self.assertEqual(calls, [True, True])

        asyncio.run(scenario())


# --- T3: bot-level fallback when the auto-interrupt declines -------------


@pytest.mark.asyncio
async def test_bot_auto_interrupt_declined_delivers_after_settle(monkeypatch):
    from bridge import bot as bot_mod
    from bridge import sdk_bridge as sdk_bridge_mod
    from bridge.tests.test_dgn911_inflight_debounce import (
        _await_all_tasks,
        _make_bot,
        _mock_interrupt,
        _mock_stop_guard,
        _patch_common,
        _upd,
    )

    b = _make_bot()
    _patch_common(monkeypatch, window=0.05)
    user_id = 1819
    calls = []
    barrier = asyncio.Event()
    # The head is finalizing: interrupt() declines (returns False).
    interrupt_calls = _mock_interrupt(monkeypatch, result=False)
    stop_calls = _mock_stop_guard(monkeypatch)

    async def mock_process(update, uid, text, **kwargs):
        if text == "finishing-turn":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 10, 1, 5, 20, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 10, 1, 5, 20, 24, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "finishing-turn", ts0, _upd(ts0))
    await b._enqueue_text_task(user_id, "message-B", ts1, _upd(ts1))

    await asyncio.sleep(0.2)  # window expires; interrupt declines
    assert interrupt_calls == [user_id]
    # Parked in the coalescing buffer, not dropped and not yet sent.
    assert len(b._user_pending_texts.get(user_id, [])) == 1
    assert calls == []

    barrier.set()  # A settles -> the done-path drain sends B
    await _await_all_tasks(b, user_id)

    assert calls == ["finishing-turn", "message-B"]
    assert stop_calls == []
    assert b._user_pending_texts.get(user_id, []) == []
    assert b._debounce_texts.get(user_id) is None
