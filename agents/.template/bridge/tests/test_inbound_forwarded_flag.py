"""Inbound-context `forwarded` flag.

A forwarded Telegram message carries someone else's words (often the agent's
own earlier bubble).  Tools that treat an owner turn as the owner's own
speech (the Why net owner-turn ledger) must be able to tell, so the bridge
publishes `forwarded` in inbound-context.json.

Covers:
  1. _is_forwarded: a MessageOrigin* forward_origin, a datetime forward_date
     or is_automatic_forward=True is a forward; a plain message and a bare
     test double are not.
  2. The flag rides inbound-context.json (default False when absent).
  3. A merged (coalesced) turn is forwarded when ANY part was forwarded.
"""

import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.sdk_bridge as sdk_mod
from bridge.bot import TelegramBot
from bridge.sdk_bridge import ChatResponse, SdkBridge, _UserStreamState

USER_ID = 42


class MessageOriginUser:  # stands in for telegram.MessageOriginUser
    pass


class IsForwardedTest(unittest.TestCase):
    def test_shapes(self):
        f = TelegramBot._is_forwarded
        self.assertTrue(f(SimpleNamespace(forward_origin=MessageOriginUser())))
        self.assertTrue(f(SimpleNamespace(forward_date=datetime.now(timezone.utc))))
        self.assertTrue(f(SimpleNamespace(is_automatic_forward=True)))
        self.assertFalse(f(SimpleNamespace(text="hi")))
        self.assertFalse(f(SimpleNamespace(forward_origin=None, forward_date=None,
                                           is_automatic_forward=False)))
        self.assertFalse(f(MagicMock()))
        self.assertFalse(f(None))


class InboundForwardedTest(unittest.TestCase):
    def setUp(self):
        p = sdk_mod.inbound_context_path()
        if p.exists():
            p.unlink()

    tearDown = setUp

    def _seen_ctx(self, inbound):
        bridge = SdkBridge()
        client = MagicMock()
        state = _UserStreamState(client=client, model=None)
        seen = {}

        async def query(message, session_id=None):
            seen["ctx"] = json.loads(sdk_mod.inbound_context_path().read_text())
            state.pending[0].future.set_result(ChatResponse(content="ok"))

        client.query = query
        bridge._streams[USER_ID] = state

        async def go():
            with patch.object(bridge, "_get_or_create_stream", AsyncMock(return_value=state)):
                await bridge.process_message(user_message="hi", user_id=USER_ID,
                                             chat_id=1, inbound=inbound)
        asyncio.run(go())
        return seen["ctx"]

    def test_flag_published(self):
        ctx = self._seen_ctx({"chat_id": 1, "message_id": 5, "source": "message",
                              "forwarded": True})
        self.assertIs(ctx["forwarded"], True)
        ctx = self._seen_ctx({"chat_id": 1, "message_id": 6, "source": "message"})
        self.assertIs(ctx["forwarded"], False)


class MergedTurnTest(unittest.TestCase):
    def test_any_forwarded_part_marks_the_merged_turn(self):
        bot = TelegramBot.__new__(TelegramBot)
        bot._user_pending_texts = {USER_ID: [
            ("own words", None, SimpleNamespace(message=SimpleNamespace())),
            ("their words", None, SimpleNamespace(
                message=SimpleNamespace(forward_origin=MessageOriginUser()))),
        ]}
        bot._debounce_texts = {}
        bot._debounce_timers = {}
        bot._interrupt_deferred_since = {}
        bot._get_user_queue_lock = lambda uid: asyncio.Lock()
        bot._cancel_typing_refresh = lambda uid: None
        bot._bundle_texts = lambda items: "\n".join(i[0] for i in items)
        calls = []

        async def dispatch(user_id, text, ts, update, **kw):
            calls.append(kw)
        bot._dispatch_text_turn = dispatch
        asyncio.run(TelegramBot._drain_pending_texts(bot, USER_ID))
        self.assertEqual(calls, [{"forwarded": True, "voice": False}])


if __name__ == "__main__":
    unittest.main()
