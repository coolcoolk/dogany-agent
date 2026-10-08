"""DGN-1683 WP3: inbound turn context published for tools.

The CLI child's env is fixed at spawn, so per-turn facts (chat / thread /
inbound message id) reach tools as a small file the bridge writes when the
turn's query is dispatched and removes when the turn ends. Plain context: the
owner message and the model reply are never inspected or altered.

Covers:
  1. During the turn the file carries chat / thread / message id / source /
     request_id / runtime_epoch / user_id / ts, mode 0600; after the turn it is
     gone.
  2. A turn without `inbound` (e.g. resume / retry callers) writes nothing.
  3. Clear-if-match: a queued next turn's context is never removed by the
     previous turn finishing.
  4. A failing write never fails the turn (fail-soft).
  5. The request id is stable within a turn and unique across turns.
"""

import asyncio
import json
import os
import stat
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.sdk_bridge as sdk_mod
from bridge.sdk_bridge import ChatResponse, SdkBridge, _UserStreamState

USER_ID = 42
INBOUND = {"chat_id": -100123, "thread_id": 7, "message_id": 555, "source": "message"}


def _ctx_path():
    return sdk_mod.inbound_context_path()


def _bridge_with(on_query):
    """SdkBridge whose stream client calls `on_query()` when the turn is sent,
    then completes the head request."""
    bridge = SdkBridge()
    client = MagicMock()
    state = _UserStreamState(client=client, model=None)

    async def query(message, session_id=None):
        seen = on_query()
        req = state.pending[0]
        req.future.set_result(ChatResponse(content="ok:%s" % seen))

    client.query = query
    bridge._streams[USER_ID] = state
    return bridge, state


def _run(coro):
    return asyncio.run(coro)


class InboundContextTest(unittest.TestCase):
    def setUp(self):
        p = _ctx_path()
        if p.exists():
            p.unlink()

    tearDown = setUp

    def _process(self, bridge, state, inbound):
        async def go():
            with patch.object(bridge, "_get_or_create_stream", AsyncMock(return_value=state)):
                return await bridge.process_message(
                    user_message="hi", user_id=USER_ID, chat_id=INBOUND["chat_id"],
                    inbound=inbound,
                )
        return _run(go())

    def test_file_present_during_turn_and_removed_after(self):
        captured = {}

        def on_query():
            p = _ctx_path()
            captured["mode"] = stat.S_IMODE(os.stat(p).st_mode)
            captured["ctx"] = json.loads(p.read_text(encoding="utf-8"))
            return "seen"

        bridge, state = _bridge_with(on_query)
        resp = self._process(bridge, state, dict(INBOUND))
        self.assertEqual(resp.content, "ok:seen")
        ctx = captured["ctx"]
        self.assertEqual(captured["mode"], 0o600)
        self.assertEqual(ctx["schema"], 1)
        self.assertEqual(ctx["chat_id"], INBOUND["chat_id"])
        self.assertEqual(ctx["thread_id"], 7)
        self.assertEqual(ctx["message_id"], 555)
        self.assertEqual(ctx["source"], "message")
        self.assertEqual(ctx["user_id"], USER_ID)
        self.assertEqual(ctx["runtime_epoch"], sdk_mod._RUNTIME_EPOCH)
        self.assertTrue(ctx["request_id"] and isinstance(ctx["ts"], float))
        self.assertFalse(_ctx_path().exists(), "file must be removed when the turn ends")

    def test_no_inbound_writes_nothing(self):
        seen = {}
        bridge, state = _bridge_with(lambda: seen.setdefault("exists", _ctx_path().exists()))
        self._process(bridge, state, None)
        self.assertFalse(seen["exists"])
        self.assertFalse(_ctx_path().exists())

    def test_null_thread_is_carried_as_null(self):
        captured = {}
        bridge, state = _bridge_with(
            lambda: captured.setdefault("ctx", json.loads(_ctx_path().read_text())) and "x")
        self._process(bridge, state, {"chat_id": 1, "thread_id": None, "message_id": 9})
        self.assertIsNone(captured["ctx"]["thread_id"])
        self.assertEqual(captured["ctx"]["source"], "message")

    def test_clear_only_removes_own_context(self):
        def replace_with_next_turn():
            # a queued next turn publishes its context while this one finishes
            sdk_mod._write_inbound_context("next-turn-id", USER_ID, dict(INBOUND, message_id=556))
            return "swapped"

        bridge, state = _bridge_with(replace_with_next_turn)
        self._process(bridge, state, dict(INBOUND))
        self.assertTrue(_ctx_path().exists())
        self.assertEqual(json.loads(_ctx_path().read_text())["request_id"], "next-turn-id")
        sdk_mod._clear_inbound_context("next-turn-id")
        self.assertFalse(_ctx_path().exists())

    def test_write_failure_never_fails_the_turn(self):
        bridge, state = _bridge_with(lambda: "ran")
        with patch.object(sdk_mod.os, "replace", side_effect=OSError("disk full")):
            resp = self._process(bridge, state, dict(INBOUND))
        self.assertEqual(resp.content, "ok:ran")
        self.assertFalse(_ctx_path().exists())

    def test_request_id_unique_per_turn(self):
        ids = []
        bridge, state = _bridge_with(lambda: ids.append(json.loads(_ctx_path().read_text())["request_id"]) or "x")
        self._process(bridge, state, dict(INBOUND))
        bridge2, state2 = _bridge_with(lambda: ids.append(json.loads(_ctx_path().read_text())["request_id"]) or "x")
        self._process(bridge2, state2, dict(INBOUND))
        self.assertEqual(len(set(ids)), 2)


if __name__ == "__main__":
    unittest.main()
