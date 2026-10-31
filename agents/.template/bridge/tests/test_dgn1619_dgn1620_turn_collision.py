"""DGN-1619/DGN-1620: sentinels and delivery survive colliding injections."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from bridge.sdk_bridge import SdkBridge, _UserStreamState


def _state():
    client = MagicMock()
    client.query = AsyncMock()
    state = _UserStreamState(client=client, model=None)
    state.last_chat_id = 111
    state.last_session_id = "session-1"
    state.proactive_push = AsyncMock()
    return state


class TestInjectedTurnCollision(unittest.TestCase):
    def _inject_and_flush(self, order, content):
        bridge = SdkBridge()
        state = _state()
        bridge._streams[1] = state
        for quiet in order:
            self.assertTrue(
                asyncio.run(bridge.inject_background_turn(1, "record", quiet=quiet))
            )
        state.proactive_texts = [content]
        asyncio.run(bridge._flush_proactive(1, state))
        return state

    def test_sentinels_and_both_injection_orders(self):
        # 1: a delivering non-quiet turn strips PUSH.
        state = self._inject_and_flush([False], "non-quiet report\nPUSH")
        state.proactive_push.assert_awaited_once()
        self.assertEqual(state.proactive_push.await_args.args[1], "non-quiet report")

        # 2: pure quiet behavior is unchanged: no PUSH suppresses, PUSH
        # delivers its body, and NO_PUSH suppresses even when PUSH is present.
        state = self._inject_and_flush([True], "quiet acknowledgement")
        state.proactive_push.assert_not_awaited()
        state = self._inject_and_flush([True], "quiet report\nPUSH")
        state.proactive_push.assert_awaited_once()
        self.assertEqual(state.proactive_push.await_args.args[1], "quiet report")
        state = self._inject_and_flush([True], "quiet report\nNO_PUSH")
        state.proactive_push.assert_not_awaited()

        # 3: quiet then non-quiet delivers and still removes PUSH.
        state = self._inject_and_flush([True, False], "first order\nPUSH")
        state.proactive_push.assert_awaited_once()
        self.assertEqual(state.proactive_push.await_args.args[1], "first order")

        # 4: non-quiet then quiet is the measured loss: loud remains latched.
        state = self._inject_and_flush([False, True], "second order")
        state.proactive_push.assert_awaited_once()
        self.assertEqual(state.proactive_push.await_args.args[1], "second order")

        # 5: NO_PUSH is unchanged on a loud path too.
        state = self._inject_and_flush([False], "loud no-push\nNO_PUSH")
        state.proactive_push.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
