"""DGN-612: timeout teardown must not emit a duplicate STILL_WORKING notice.

Root cause: handle_timeout_preserve called _disconnect_user_stream with
cancel_message=messages.STILL_WORKING. Any OTHER request still queued behind
the timed-out one (state.pending -- e.g. a second message the user sent while
the first was in flight) had its future resolved with that same string as its
own ChatResponse.content. That response then flows through the NORMAL
(non-auto-resume) reply path for a different user message and gets sent as
a plain reply -- a second, unrelated STILL_WORKING bubble on top of the one
_auto_resume_loop (bot.py) already sent for the timed-out turn.

Fix: handle_timeout_preserve now calls _disconnect_user_stream(..., silent=True).
A queued future is resolved with content="" instead, so bot._reply_smart's
`if display.strip():` guard emits nothing for it. `silent` is a dedicated
keyword-only flag, NOT an overload of cancel_message=None -- every other
_disconnect_user_stream call site in sdk_bridge.py passes no cancel_message
and relies on that resolving pending futures with messages.TASK_TERMINATED
(stale-stream recreate, model/session swap, reconnect-and-retry); collapsing
"no message supplied" into "stay silent" would have silenced those too.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from bridge import messages
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState


def _make_future():
    return asyncio.get_event_loop().create_future()


def _make_req(user_id: int = 1) -> _PendingRequest:
    return _PendingRequest(
        user_id=user_id,
        chat_id=user_id,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=_make_future(),
    )


class TestDGN612StillWorkingDedup(unittest.TestCase):
    """Verify that handle_timeout_preserve does not emit STILL_WORKING via
    the pending-future path (silent teardown)."""

    def test_timeout_teardown_resolves_queued_future_with_empty_content(self):
        """A queued second request must be resolved with content="" (not
        STILL_WORKING) when the first request times out."""

        async def _inner():
            bridge_obj = SdkBridge()
            state = _UserStreamState(client=MagicMock(), model=None)
            state.client.disconnect = AsyncMock()
            state.last_session_id = "sess-1"

            # Simulate two pending requests: req_a (timed-out, future already
            # cancelled by wait_for) and req_b (queued behind it, still live).
            req_a = _make_req()
            req_b = _make_req()

            req_a.future.cancel()  # mimics wait_for cancellation on timeout

            state.pending.append(req_a)
            state.pending.append(req_b)
            bridge_obj._streams[1] = state

            await bridge_obj.handle_timeout_preserve(1)
            return req_a, req_b

        req_a, req_b = asyncio.run(_inner())

        # req_a: already cancelled -- set_result is skipped, stays cancelled.
        self.assertTrue(req_a.future.cancelled())

        # req_b: resolved silently with empty content -- NOT STILL_WORKING.
        self.assertTrue(req_b.future.done())
        self.assertFalse(req_b.future.cancelled())
        result = req_b.future.result()
        self.assertEqual(
            result.content,
            "",
            "silent teardown must not carry STILL_WORKING into a queued request's future",
        )
        self.assertNotEqual(result.content, messages.STILL_WORKING)

    def test_disconnect_default_still_terminates_queued_future_loudly(self):
        """Guard against re-collapsing cancel_message=None into silence: every
        OTHER _disconnect_user_stream call site (stale-stream recreate,
        model/session swap, reconnect-and-retry) invokes it with NO
        cancel_message and relies on TASK_TERMINATED reaching a queued
        future. silent defaults to False, so that must be unchanged."""

        async def _inner():
            bridge_obj = SdkBridge()
            state = _UserStreamState(client=MagicMock(), model=None)
            state.client.disconnect = AsyncMock()

            req = _make_req()
            state.pending.append(req)
            bridge_obj._streams[1] = state

            await bridge_obj._disconnect_user_stream(1)
            return req

        req = asyncio.run(_inner())
        self.assertTrue(req.future.done())
        self.assertFalse(req.future.cancelled())
        self.assertEqual(req.future.result().content, messages.TASK_TERMINATED)

    def test_disconnect_with_explicit_cancel_message_still_carries_message(self):
        """Explicit cancel_message (e.g. the /stop path) must still surface
        the message -- silent mode is opt-in via silent=True only."""

        async def _inner():
            bridge_obj = SdkBridge()
            state = _UserStreamState(client=MagicMock(), model=None)
            state.client.disconnect = AsyncMock()

            req = _make_req()
            state.pending.append(req)
            bridge_obj._streams[1] = state

            await bridge_obj._disconnect_user_stream(
                1, cancel_message=messages.TASK_TERMINATED
            )
            return req

        req = asyncio.run(_inner())
        self.assertTrue(req.future.done())
        self.assertFalse(req.future.cancelled())
        self.assertEqual(req.future.result().content, messages.TASK_TERMINATED)

    def test_disconnect_silent_true_resolves_empty_regardless_of_cancel_message(self):
        """silent=True must produce content="" even if a cancel_message
        happens to be passed alongside it -- silent wins."""

        async def _inner():
            bridge_obj = SdkBridge()
            state = _UserStreamState(client=MagicMock(), model=None)
            state.client.disconnect = AsyncMock()

            req = _make_req()
            state.pending.append(req)
            bridge_obj._streams[1] = state

            await bridge_obj._disconnect_user_stream(
                1, cancel_message=messages.STILL_WORKING, silent=True
            )
            return req

        req = asyncio.run(_inner())
        self.assertTrue(req.future.done())
        self.assertFalse(req.future.cancelled())
        self.assertEqual(req.future.result().content, "")


if __name__ == "__main__":
    unittest.main()
