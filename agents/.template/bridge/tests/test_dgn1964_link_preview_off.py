"""DGN-1964: link previews default OFF on framework sends.

The pinned day card and travel alerts put map links on place names, so every
pin / reply carried a big map preview card. Fixtures per send path:
  - dashboard pin: edit site and send site, HTML and plain-fallback legs,
    preview OFF by default; a link_preview:: line opts that pin back in
    (line stripped, link_preview_options left unset = Telegram default).
  - agent reply whose only links are inline place links: preview OFF
    (DGN-376 default, pinned here with the real place-link shape).
push.sh is covered by routines/tests/test-push-link-preview.sh.
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import telegram.error

from bridge.dashboard import DashboardSync

OWNER_ID = 42
PLACE = "Day 2: [Seongsan Ilchulbong](https://naver.me/xYz123) 09:00"
OPT_IN = PLACE + "\nlink_preview::"


def _off(lp):
    return lp is not None and getattr(lp, "is_disabled", None) is True


def _mock_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    fake_msg = MagicMock()
    fake_msg.message_id = 9999
    bot.send_message = AsyncMock(return_value=fake_msg)
    bot.pin_chat_message = AsyncMock()
    bot.unpin_chat_message = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


class _DashCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-dgn1964-")
        d = Path(self._td.name)
        self._bot = _mock_bot()
        self._ds = DashboardSync(
            bot=self._bot,
            turn_active=lambda uid: False,
            dashboard_path=d / "dashboard.md",
            state_path=d / "state.json",
        )

    def tearDown(self):
        self._td.cleanup()

    def _edit(self, text):
        self._ds._message_id = 1111
        asyncio.run(self._ds._sync(OWNER_ID, text, 100.0))
        return self._bot.edit_message_text.await_args_list

    def _send(self, text):
        asyncio.run(self._ds._recreate(OWNER_ID, text, 100.0, time.monotonic()))
        return self._bot.send_message.await_args_list


class TestPinEdit(_DashCase):
    def test_html_edit_preview_off(self):
        kw = self._edit(PLACE)[-1].kwargs
        self.assertEqual(kw["parse_mode"], "HTML")
        self.assertIn("naver.me", kw["text"])
        self.assertTrue(_off(kw["link_preview_options"]))

    def test_plain_conversion_fallback_preview_off(self):
        with patch("bridge.dashboard.sanitize_message_for_telegram",
                   side_effect=RuntimeError("boom")):
            kw = self._edit(PLACE)[-1].kwargs
        self.assertNotIn("parse_mode", kw)
        self.assertTrue(_off(kw["link_preview_options"]))

    def test_parse_error_retry_preview_off(self):
        err = telegram.error.BadRequest("Can't parse entities: bad tag")
        self._bot.edit_message_text = AsyncMock(side_effect=[err, MagicMock()])
        calls = self._edit(PLACE)
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertTrue(_off(c.kwargs["link_preview_options"]))

    def test_opt_in_keeps_preview_and_strips_marker(self):
        kw = self._edit(OPT_IN)[-1].kwargs
        self.assertIsNone(kw["link_preview_options"])
        self.assertNotIn("link_preview::", kw["text"])


class TestPinSend(_DashCase):
    def test_html_send_preview_off(self):
        kw = self._send(PLACE)[-1].kwargs
        self.assertEqual(kw["parse_mode"], "HTML")
        self.assertTrue(_off(kw["link_preview_options"]))
        self.assertEqual(self._ds._message_id, 9999)

    def test_plain_conversion_fallback_preview_off(self):
        with patch("bridge.dashboard.balance_telegram_html",
                   side_effect=RuntimeError("boom")):
            kw = self._send(PLACE)[-1].kwargs
        self.assertNotIn("parse_mode", kw)
        self.assertTrue(_off(kw["link_preview_options"]))

    def test_parse_error_retry_preview_off(self):
        fake_msg = MagicMock()
        fake_msg.message_id = 9999
        err = telegram.error.BadRequest("Can't parse entities: bad tag")
        self._bot.send_message = AsyncMock(side_effect=[err, fake_msg])
        calls = self._send(PLACE)
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertTrue(_off(c.kwargs["link_preview_options"]))

    def test_opt_in_keeps_preview_and_strips_marker(self):
        kw = self._send(OPT_IN)[-1].kwargs
        self.assertIsNone(kw["link_preview_options"])
        self.assertNotIn("link_preview::", kw["text"])


class TestAgentReplyPlaceLink(unittest.TestCase):
    def _bot(self):
        sdk = MagicMock()
        sdk.PermissionResultAllow = MagicMock
        sdk.PermissionResultDeny = MagicMock
        sys.modules.setdefault("claude_agent_sdk", sdk)
        import bridge.bot as bot_mod
        bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
        bot.application = MagicMock()
        bot.application.bot = MagicMock()
        bot.application.bot.delete_message = AsyncMock()
        return bot

    def _reply(self, text):
        sent = []

        async def _reply_text(text, parse_mode=None, reply_markup=None,
                              link_preview_options=None):
            sent.append({"text": text, "lp": link_preview_options})

        msg = MagicMock()
        msg.chat.id = OWNER_ID
        msg.reply_text = AsyncMock(side_effect=_reply_text)
        msg.get_bot.return_value = MagicMock()
        msg.get_bot.return_value.delete_message = AsyncMock()
        asyncio.run(self._bot()._reply_smart(msg, text))
        return sent

    def test_inline_place_link_reply_preview_off(self):
        sent = self._reply("Tomorrow: " + PLACE)
        self.assertEqual(len(sent), 1)
        self.assertIn("naver.me", sent[0]["text"])
        self.assertTrue(_off(sent[0]["lp"]))

    def test_reply_opt_in_keeps_preview(self):
        sent = self._reply(OPT_IN)
        self.assertIsNone(sent[0]["lp"])
        self.assertNotIn("link_preview::", sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
