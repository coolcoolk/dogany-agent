"""DGN-1499: the turn timeout stops the session BEFORE any kill.

Measured defect (bot.log 2026-09-15 18:31:41/18:40:55): order was
timeout -> disconnect (3s budget, loses to the SDK's own graceful sequence
on a busy CLI) -> force-kill. The designed last resort fired as the first
resort on every long turn, and the in-flight turn never got a chance to
conclude and preserve its output.

Hard rule under test (owner directive 2026-09-16):
  T-N  send the turn a stop signal (SDK control interrupt, /stop's path)
  T    only if that fails, the legacy teardown runs (kill = last resort)

Covers:
  1. Soft path through process_message: at the soft deadline the stop signal
     is sent, the stream state AND client survive (no disconnect, no kill),
     and the response carries timed_out=True + the resume sid so
     bot._auto_resume_loop resumes on the same live client.
  2. Stuck CLI: interrupt raising falls back to the legacy hard teardown --
     disconnect attempted, force-kill as the final fallback. Never silent.
  3. Nothing to stop (no dispatched head): soft path declines, hard path runs.
  4. Grace disabled (0) or not fitting under PROCESS_TIMEOUT: the soft budget
     degrades to the legacy single deadline and the soft path declines.
  5. Fold caption: the timeout trigger confirms a grown fold with the TIMEOUT
     caption (parity with handle_timeout_preserve); /stop keeps the STOP one.
  6. partial_preserved reflects drafts that existed BEFORE the interrupt
     finalized them.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.sdk_bridge as sdk_mod
from bridge import messages
from bridge.formatting import FOLD_CAPTION_STOPPED, FOLD_CAPTION_TIMEOUT
from bridge.sdk_bridge import (
    ChatResponse,
    SdkBridge,
    _PendingRequest,
    _UserStreamState,
)

USER_ID = 42


def _make_client(connected: bool = True) -> MagicMock:
    client = MagicMock()
    client.interrupt = AsyncMock()
    client.disconnect = AsyncMock()
    client._query = object() if connected else None
    return client


def _make_request(sent: bool, with_drafts: bool = False) -> _PendingRequest:
    handler = MagicMock()
    handler.finalize_all = AsyncMock()
    handler.cancel = AsyncMock()
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


def _timeout_knobs(process_timeout, grace):
    """Patch the module-level knobs the soft path reads at call time."""
    return (
        patch.object(sdk_mod, "PROCESS_TIMEOUT", process_timeout),
        patch.object(sdk_mod, "TIMEOUT_STOP_GRACE", grace),
    )


class TestSoftStopBeforeKill(unittest.TestCase):
    """At the soft deadline the turn is stopped, never killed."""

    def test_process_message_timeout_stops_and_keeps_session(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.last_session_id = "sid-live"
            bridge._streams[USER_ID] = state

            async def fake_dispatch(st):
                # Mark the head dispatched, like the real dispatcher does,
                # but never resolve the future -> the turn "runs long".
                st.pending[0].sent = True

            p_timeout, p_grace = _timeout_knobs(0.6, 0.5)
            with p_timeout, p_grace, patch.object(
                SdkBridge, "_get_or_create_stream", new=AsyncMock(return_value=state)
            ), patch.object(
                SdkBridge, "_dispatch_next_query", new=AsyncMock(side_effect=fake_dispatch)
            ), patch.object(
                SdkBridge, "_force_kill_client_subprocess"
            ) as force_kill:
                response = await bridge.process_message(
                    user_message="long turn",
                    user_id=USER_ID,
                    chat_id=1,
                )

            # The stop signal went out on the SDK control channel.
            client.interrupt.assert_awaited_once()
            # Session survival: state kept, no disconnect, no kill.
            self.assertIn(USER_ID, bridge._streams)
            client.disconnect.assert_not_awaited()
            force_kill.assert_not_called()
            # The response is the timeout shape auto-resume consumes.
            self.assertTrue(response.timed_out)
            self.assertEqual(response.error, "timeout")
            self.assertEqual(response.resume_session_id, "sid-live")
            self.assertEqual(
                response.content, messages.TIMEOUT_PAUSED.format(timeout=0.6)
            )

        asyncio.run(scenario())

    def test_partial_flag_captured_before_finalize(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.last_session_id = "sid-p"
            head = _make_request(sent=True, with_drafts=True)
            state.pending.append(head)
            bridge._streams[USER_ID] = state

            p_timeout, p_grace = _timeout_knobs(600, 20)
            with p_timeout, p_grace:
                response = await bridge._timeout_stop_then_preserve(USER_ID)

            self.assertIsNotNone(response)
            # Drafts existed when the timeout hit -> preserved, and the
            # interrupt path finalized them in place (not deleted).
            self.assertTrue(response.partial_preserved)
            self.assertTrue(response.streamed)
            head.streaming_handler.finalize_all.assert_awaited_once()

        asyncio.run(scenario())


class TestSoftStopFallsBackToHardTeardown(unittest.TestCase):
    """Kill stays reachable -- but only as the LAST resort."""

    def test_stuck_cli_falls_back_to_teardown_and_force_kill(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            # Stuck CLI: the control channel never acks the interrupt.
            client.interrupt = AsyncMock(side_effect=asyncio.TimeoutError())
            # And the graceful disconnect fails too (the measured incident).
            client.disconnect = AsyncMock(side_effect=RuntimeError("busy"))
            state = _UserStreamState(client=client, model=None)
            state.last_session_id = "sid-stuck"
            head = _make_request(sent=True)
            state.pending.append(head)
            bridge._streams[USER_ID] = state

            p_timeout, p_grace = _timeout_knobs(600, 20)
            with p_timeout, p_grace, patch.object(
                SdkBridge, "_force_kill_client_subprocess"
            ) as force_kill:
                soft = await bridge._timeout_stop_then_preserve(USER_ID)
                self.assertIsNone(soft)
                # The caller (process_message) then runs the legacy path:
                sid, _partial = await bridge.handle_timeout_preserve(USER_ID)

            self.assertEqual(sid, "sid-stuck")
            client.disconnect.assert_awaited_once()
            force_kill.assert_called_once()
            self.assertNotIn(USER_ID, bridge._streams)

        asyncio.run(scenario())

    def test_no_dispatched_head_declines_soft_path(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.pending.append(_make_request(sent=False))
            bridge._streams[USER_ID] = state

            p_timeout, p_grace = _timeout_knobs(600, 20)
            with p_timeout, p_grace:
                self.assertIsNone(
                    await bridge._timeout_stop_then_preserve(USER_ID)
                )
            client.interrupt.assert_not_awaited()

        asyncio.run(scenario())

    def test_no_stream_declines_soft_path(self):
        async def scenario():
            bridge = SdkBridge()
            p_timeout, p_grace = _timeout_knobs(600, 20)
            with p_timeout, p_grace:
                self.assertIsNone(
                    await bridge._timeout_stop_then_preserve(USER_ID)
                )

        asyncio.run(scenario())


class TestGraceKnob(unittest.TestCase):
    """Grace 0 / non-fitting grace degrade to the legacy single deadline."""

    def test_grace_zero_disables_soft_window(self):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.pending.append(_make_request(sent=True))
            bridge._streams[USER_ID] = state

            p_timeout, p_grace = _timeout_knobs(600, 0)
            with p_timeout, p_grace:
                self.assertEqual(SdkBridge._soft_turn_budget(), 600)
                self.assertIsNone(
                    await bridge._timeout_stop_then_preserve(USER_ID)
                )
            client.interrupt.assert_not_awaited()

        asyncio.run(scenario())

    def test_grace_not_fitting_disables_soft_window(self):
        async def scenario():
            bridge = SdkBridge()
            p_timeout, p_grace = _timeout_knobs(10, 20)
            with p_timeout, p_grace:
                self.assertEqual(SdkBridge._soft_turn_budget(), 10)
                self.assertIsNone(
                    await bridge._timeout_stop_then_preserve(USER_ID)
                )

        asyncio.run(scenario())

    def test_grace_fitting_carves_budget_out_of_total(self):
        p_timeout, p_grace = _timeout_knobs(550, 20)
        with p_timeout, p_grace:
            self.assertEqual(SdkBridge._soft_turn_budget(), 530)


class TestFoldCaptionByTrigger(unittest.TestCase):
    """Timeout trigger stamps the TIMEOUT fold caption; /stop keeps STOP's."""

    def _run_interrupt(self, trigger):
        async def scenario():
            bridge = SdkBridge()
            client = _make_client()
            state = _UserStreamState(client=client, model=None)
            state.pending.append(_make_request(sent=True))
            bridge._streams[USER_ID] = state
            with patch.object(
                SdkBridge, "_fold_finalize", new=AsyncMock(return_value=False)
            ) as fold:
                await bridge.interrupt(USER_ID, trigger=trigger)
            return fold.await_args.args[1]

        return asyncio.run(scenario())

    def test_timeout_trigger_uses_timeout_caption(self):
        self.assertEqual(self._run_interrupt("timeout"), FOLD_CAPTION_TIMEOUT)

    def test_stop_trigger_keeps_stop_caption(self):
        self.assertEqual(self._run_interrupt("stop"), FOLD_CAPTION_STOPPED)


if __name__ == "__main__":
    unittest.main()
