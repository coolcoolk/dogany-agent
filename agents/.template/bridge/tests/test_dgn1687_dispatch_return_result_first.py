"""DGN-1687 bridge carrier for result-first dispatch-return turns.

DGN-1934: "delivered" now also covers the bridge's own interim delivery
mid-turn (recorded as ``delivered_via``), and the injected instruction no
longer asks for a proceed / hold menu on every return.

DGN-1940 (dec-323): the return is handled autonomously; a one-line direction
reaches the owner only when the follow-up runs long.
"""
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

                after = json.loads(path.read_text())
                # DGN-1934: the record names what delivered the result.
                self.assertEqual(after["delivered_via"], "first_text")

                sdk._clear_dispatch_return_context(before["turn_id"])
                self.assertFalse(path.exists())

    def test_injected_instruction_continues_safe_follow_up_in_turn(self):
        """DGN-1934: the result line opens tools in the same turn; a menu is
        asked for only on a real owner decision, never as proceed / hold
        boilerplate."""
        with tempfile.TemporaryDirectory() as root:
            client = MagicMock()
            client.query = AsyncMock()
            state = _UserStreamState(client=client, model=None)
            bridge = SdkBridge()
            bridge._streams[7] = state
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                self.assertTrue(asyncio.run(bridge.inject_background_turn(
                    7, "[dispatch-return] worker finished")))
            sent = client.query.call_args[0][0]
            self.assertIn("[bridge:result-first]", sent)
            self.assertIn("same turn", sent)
            self.assertIn("real owner decision", sent)
            self.assertNotIn("proceed / hold", sent)

    def test_injected_instruction_is_autonomous_with_long_run_direction(self):
        """DGN-1940 (dec-323): the agent handles the return itself; the owner
        gets a one-line direction only when the follow-up runs long, and is
        never asked which way to go."""
        with tempfile.TemporaryDirectory() as root:
            client = MagicMock()
            client.query = AsyncMock()
            state = _UserStreamState(client=client, model=None)
            bridge = SdkBridge()
            bridge._streams[7] = state
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                self.assertTrue(asyncio.run(bridge.inject_background_turn(
                    7, "[dispatch-return] worker finished")))
            sent = client.query.call_args[0][0]
            self.assertIn("Handle this dispatch return yourself", sent)
            self.assertIn("do not ask the owner which way to go", sent)
            self.assertIn("run long", sent)
            self.assertIn("one-line direction", sent)
            self.assertIn("without waiting for a reply", sent)
            # Ordinary (non dispatch-return) injected turns carry none of it.
            client.query.reset_mock()
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                self.assertTrue(asyncio.run(bridge.inject_background_turn(
                    7, "[cron] nightly digest ready")))
            plain = client.query.call_args[0][0]
            self.assertNotIn("one-line direction", plain)

    def test_interim_delivery_opens_the_latch_once(self):
        """DGN-1934: a dispatch-return text that reached the owner through
        the bridge's own interim surface counts as delivered."""
        with tempfile.TemporaryDirectory() as root:
            state = _UserStreamState(client=MagicMock(), model=None)
            state.last_session_id = "sess-1"
            state.dispatch_return_turn_id = "turn-1934"
            with patch.object(sdk, "PROJECT_ROOT", Path(root)):
                sdk._write_dispatch_return_context("turn-1934", 7, "sess-1")
                path = sdk.dispatch_return_context_path()
                self.assertTrue(SdkBridge._record_dispatch_return_interim(7, state))
                row = json.loads(path.read_text())
                self.assertTrue(row["visible"])
                self.assertEqual(row["delivered_via"], "interim")
                self.assertTrue(state.dispatch_return_result_sent)
                # Nothing was pushed, so finalize has nothing to subtract.
                self.assertIsNone(state.dispatch_return_delivered)
                # Already open: a second record is a no-op.
                self.assertFalse(SdkBridge._record_dispatch_return_interim(7, state))
                # No dispatch-return turn: never writes a latch.
                quiet = _UserStreamState(client=MagicMock(), model=None)
                path.unlink()
                self.assertFalse(SdkBridge._record_dispatch_return_interim(7, quiet))
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
