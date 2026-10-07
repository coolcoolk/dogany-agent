"""DGN-1687 bridge carrier for result-first dispatch-return turns."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.sdk_bridge as sdk
from bridge.sdk_bridge import SdkBridge, _UserStreamState


class ResultFirstBridgeTest(unittest.TestCase):
    def test_dispatch_return_latch_opens_only_after_proactive_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            push = AsyncMock()
            client = MagicMock()
            client.query = AsyncMock()
            state = _UserStreamState(client=client, model=None)
            state.last_chat_id = 11
            state.proactive_push = push
            bridge = SdkBridge()
            bridge._streams[7] = state
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                self.assertTrue(asyncio.run(bridge.inject_background_turn(
                    7, "[dispatch-return] worker finished")))
                sent = client.query.call_args[0][0]
                self.assertIn("[bridge:result-first]", sent)
                path = sdk.dispatch_return_context_path()
                before = json.loads(path.read_text())
                self.assertFalse(before["visible"])

                self.assertTrue(asyncio.run(bridge._send_dispatch_return_first_text(
                    7, state, "Result: worker finished successfully.")))
                self.assertEqual(push.await_count, 1)
                self.assertTrue(json.loads(path.read_text())["visible"])

                sdk._clear_dispatch_return_context(before["turn_id"])
                self.assertFalse(path.exists())

    def test_self_canceled_return_is_quiet_and_has_no_result_first_latch(self):
        with tempfile.TemporaryDirectory() as root:
            client = MagicMock()
            client.query = AsyncMock()
            state = _UserStreamState(client=client, model=None)
            bridge = SdkBridge()
            bridge._streams[7] = state
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                self.assertTrue(asyncio.run(bridge.inject_background_turn(
                    7, "[dispatch-return] worker canceled\n\n- cancel_by: self")))
                sent = client.query.call_args[0][0]
                self.assertIn("[bridge:quiet-recovery]", sent)
                self.assertNotIn("[bridge:result-first]", sent)
                self.assertEqual(state.injected_turn_mode, "quiet")
                self.assertIsNone(state.dispatch_return_turn_id)
                self.assertFalse(sdk.dispatch_return_context_path().exists())


    def test_first_text_push_carries_has_options_for_owner_menus(self):
        """DGN-1732: the result-first push must not drop [[OPTIONS]] buttons."""
        cases = [
            ("labeled marker",
             "Result: done.\n[[OPTIONS: ship | hold]]", True),
            ("bare marker + numbered list",
             "Result: done.\n[[OPTIONS]]\n1. ship\n2. hold", True),
            ("plain text", "Result: worker finished successfully.", False),
        ]
        for name, text, expected in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as root:
                push = AsyncMock()
                client = MagicMock()
                client.query = AsyncMock()
                state = _UserStreamState(client=client, model=None)
                state.last_chat_id = 11
                state.proactive_push = push
                bridge = SdkBridge()
                bridge._streams[7] = state
                with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                    self.assertTrue(asyncio.run(bridge.inject_background_turn(
                        7, "[dispatch-return] worker finished")))
                    self.assertTrue(asyncio.run(
                        bridge._send_dispatch_return_first_text(7, state, text)))
                args = push.await_args[0]
                self.assertEqual(args[1], text.strip())
                self.assertIs(args[2], expected)
                self.assertIs(args[3], False)


if __name__ == "__main__":
    unittest.main()
