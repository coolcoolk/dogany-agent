"""Options-bubble binding (v2.7 T1 grill round 3, findings 5 and 11).

  5. The bridge publishes which bubble carried each [[OPTIONS]] keyboard it
     rendered (options-bubbles.json: chat, message id, button texts, time),
     so the Why net binds a tap to the bubble that carried its ask; an older
     bubble with the same labels is another question.
 11. A tap carries `bubble_text_sha`, the tapped bubble's text as the owner
     saw it, so a morning bubble edited after it was sent (push.sh --edit)
     no longer answers the question it was sent with.  The hash is pinned
     to the Why net's own copy (routines/lib/why_net/store.py).
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

import bridge.sdk_bridge as sdk_mod  # noqa: E402
from bridge.bot import TelegramBot  # noqa: E402

STORE = Path(__file__).resolve().parents[2] / "routines" / "lib" / "why_net" / "store.py"


def _why_store():
    spec = importlib.util.spec_from_file_location("_why_store", STORE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "options-bubbles.json"
        p = patch.object(sdk_mod, "options_bubbles_path", lambda: self.path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def rows(self):
        return json.loads(self.path.read_text(encoding="utf-8"))


class PublishTest(_Tmp):
    def test_rows_newest_last_and_capped(self):
        for i in range(sdk_mod.OPTIONS_BUBBLES_MAX + 3):
            sdk_mod.publish_options_bubble(42, 100 + i, ["A", "B"])
        rows = self.rows()
        self.assertEqual(len(rows), sdk_mod.OPTIONS_BUBBLES_MAX)
        self.assertEqual(rows[-1]["message_id"], 100 + sdk_mod.OPTIONS_BUBBLES_MAX + 2)
        self.assertEqual((rows[-1]["chat_id"], rows[-1]["keyboard"]), (42, ["A", "B"]))
        self.assertIsInstance(rows[-1]["ts"], float)

    def test_no_message_id_no_row(self):
        sdk_mod.publish_options_bubble(42, None, ["A"])
        sdk_mod.publish_options_bubble(42, True, ["A"])
        self.assertFalse(self.path.exists())


class RenderSeatTest(_Tmp):
    def _bot(self, sent_id=77):
        bot = TelegramBot.__new__(TelegramBot)
        bot.application = MagicMock()
        bot.application.bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=sent_id))
        bot._send_file_paths = AsyncMock()
        return bot

    def test_prompt_line_carrier_is_published(self):
        bot = self._bot()
        asyncio.run(bot._send_content_artifacts(
            42, "Pick one\n[[OPTIONS: A | B]]", True, carrier_id=None,
            body="Pick one"))
        [row] = self.rows()
        self.assertEqual((row["chat_id"], row["message_id"], row["keyboard"]),
                         (42, 77, ["1. A", "2. B"]))

    def test_body_carrier_is_published(self):
        bot = self._bot()
        bot._attach_keyboard = AsyncMock(return_value=True)
        asyncio.run(bot._send_content_artifacts(
            42, "Pick one\n[[OPTIONS: A | B]]", True, carrier_id=55,
            body="Pick one"))
        [row] = self.rows()
        # DGN-1928: two or more buttons always carry their number.
        self.assertEqual((row["message_id"], row["keyboard"]), (55, ["1. A", "2. B"]))
        bot.application.bot.send_message.assert_not_called()

    def test_no_marker_no_row(self):
        asyncio.run(self._bot()._send_content_artifacts(42, "plain", True))
        self.assertFalse(self.path.exists())


class BubbleTextShaTest(unittest.TestCase):
    def test_parity_with_the_why_net(self):
        store = _why_store()
        for text in ("이 작업, 어떤 이유가 더 가까워요?\n다른 이유면 그대로 적어주세요.",
                     unicodedata.normalize("NFD", "속도가 먼저"), "a  b\n\tc", ""):
            self.assertEqual(sdk_mod.bubble_text_sha(text), store.bubble_text_sha(text))
        self.assertIsNone(sdk_mod.bubble_text_sha(None))

    def test_whitespace_only_differences_match(self):
        self.assertEqual(sdk_mod.bubble_text_sha("a b\nc"), sdk_mod.bubble_text_sha("a b\n\nc "))
        self.assertNotEqual(sdk_mod.bubble_text_sha("a b"), sdk_mod.bubble_text_sha("a c"))

    def test_tap_carries_the_tapped_bubble_text(self):
        kb = [[SimpleNamespace(text="A", callback_data="opt:A")]]
        q = SimpleNamespace(id="q1", message=SimpleNamespace(text="Pick one", caption=None))
        out = TelegramBot._tap_binding(q, "opt:A", kb)
        self.assertEqual(out["bubble_text_sha"], sdk_mod.bubble_text_sha("Pick one"))
        q = SimpleNamespace(id="q1", message=SimpleNamespace(text=None, caption="Cap"))
        self.assertEqual(TelegramBot._tap_binding(q, "opt:A", kb)["bubble_text_sha"],
                         sdk_mod.bubble_text_sha("Cap"))

    def test_tap_inbound_publishes_bubble_text_sha(self):
        self.assertIn("bubble_text_sha", sdk_mod._INBOUND_TAP_FIELDS)


if __name__ == "__main__":
    unittest.main()
