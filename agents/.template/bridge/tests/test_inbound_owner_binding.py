"""Inbound-context owner binding (v2.7 T1 grill round 2).

The Why net owner-turn ledger must not be satisfiable from agent-writable
files alone, so the bridge publishes what only it saw:

  1. `text_sha`: sha256 of the NFC prompt this turn sends to the CLI.  A
     hook that sees another prompt (an agent-run `why-net ups`, a cron
     session within the freshness window) cannot claim the request_id.
  2. `voice`: the turn text is a Whisper transcript (typed check on
     telegram.Voice; a bare test double is not voice).  A merged turn is
     voice when ANY part was.
  3. a tap carries its binding: callback_data, callback_query_id, the
     keyboard as sent and the tapped 1-based index.
"""

import asyncio
import hashlib
import json
import unicodedata
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.sdk_bridge as sdk_mod
from bridge.bot import TelegramBot
from bridge.sdk_bridge import ChatResponse, SdkBridge, _UserStreamState

USER_ID = 42


class Voice:  # stands in for telegram.Voice
    pass


def _sha(text):
    return hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8")).hexdigest()


class InboundPublishTest(unittest.TestCase):
    def setUp(self):
        p = sdk_mod.inbound_context_path()
        if p.exists():
            p.unlink()

    tearDown = setUp

    def _seen_ctx(self, inbound, text="hi"):
        bridge = SdkBridge()
        client = MagicMock()
        state = _UserStreamState(client=client, model=None)
        seen = {}

        async def query(message, session_id=None):
            seen["ctx"] = json.loads(sdk_mod.inbound_context_path().read_text())
            seen["prompt"] = message
            state.pending[0].future.set_result(ChatResponse(content="ok"))

        client.query = query
        bridge._streams[USER_ID] = state

        async def go():
            with patch.object(bridge, "_get_or_create_stream", AsyncMock(return_value=state)):
                await bridge.process_message(user_message=text, user_id=USER_ID,
                                             chat_id=1, inbound=inbound)
        asyncio.run(go())
        return seen

    def test_text_sha_is_the_sent_prompt(self):
        nfd = unicodedata.normalize("NFD", "속도가 먼저")
        seen = self._seen_ctx({"chat_id": 1, "message_id": 5, "source": "message"}, nfd)
        self.assertEqual(seen["ctx"]["text_sha"], _sha(seen["prompt"]))
        self.assertIs(seen["ctx"]["voice"], False)
        self.assertNotIn("keyboard", seen["ctx"])

    def test_voice_and_tap_fields_ride_along(self):
        seen = self._seen_ctx({"chat_id": 1, "message_id": 6, "source": "message",
                               "voice": True})
        self.assertIs(seen["ctx"]["voice"], True)
        seen = self._seen_ctx({"chat_id": 1, "message_id": 7, "source": "callback",
                               "callback_data": "opt:1", "callback_query_id": "q1",
                               "keyboard": ["A", "B"], "option_index": 1}, "A")
        ctx = seen["ctx"]
        self.assertEqual((ctx["callback_data"], ctx["callback_query_id"],
                          ctx["keyboard"], ctx["option_index"]),
                         ("opt:1", "q1", ["A", "B"], 1))
        self.assertEqual(ctx["text_sha"], _sha("A"))


class VoiceFlagTest(unittest.TestCase):
    def test_shapes(self):
        v = TelegramBot._is_voice
        self.assertTrue(v(SimpleNamespace(voice=Voice())))
        self.assertFalse(v(SimpleNamespace(text="hi")))
        self.assertFalse(v(SimpleNamespace(voice=None)))
        self.assertFalse(v(MagicMock()))
        self.assertFalse(v(None))

    def test_any_voice_part_marks_the_merged_turn(self):
        bot = TelegramBot.__new__(TelegramBot)
        bot._user_pending_texts = {USER_ID: [
            ("typed", None, SimpleNamespace(message=SimpleNamespace())),
            ("spoken", None, SimpleNamespace(message=SimpleNamespace(voice=Voice()))),
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
        self.assertEqual(calls, [{"forwarded": False, "voice": True}])


class TapBindingTest(unittest.TestCase):
    def test_keyboard_and_index(self):
        kb = [[SimpleNamespace(text="1. A", callback_data="opt:1. A")],
              [SimpleNamespace(text="2. B", callback_data="opt:2")]]
        out = TelegramBot._tap_binding(SimpleNamespace(id="q9"), "opt:2", kb)
        self.assertEqual(out, {"callback_data": "opt:2", "callback_query_id": "q9",
                               "keyboard": ["1. A", "2. B"], "option_index": 2})

    def test_no_keyboard_no_binding(self):
        out = TelegramBot._tap_binding(SimpleNamespace(id="q9"), "opt:2", None)
        self.assertNotIn("keyboard", out)
        self.assertNotIn("option_index", out)


if __name__ == "__main__":
    unittest.main()
