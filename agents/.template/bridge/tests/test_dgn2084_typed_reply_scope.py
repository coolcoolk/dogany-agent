"""DGN-2084: how a typed message clears menus, through the real handler.

Spec (dec-424 option 1):
  1. a Telegram reply to an armed menu clears THAT menu only
     (label match -> that label, else the typed-reply word);
  2. plain text clears the newest armed menu only when it names an option
     (number or label); unmatched plain text leaves every keyboard alone;
  3. unmatched plain text still closes the live decision, so the DGN-1958
     carry never re-attaches that menu's buttons to a later message;
  4. the text still runs as a normal turn.
"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

import bridge.bot as bot_mod
import bridge.sdk_bridge as sdk_mod
from bridge import messages
from bridge.tests.test_dgn2062_typed_over_menu import BODY, CHAT, _Base

LABELS_A = ["Alpha", "Beta release"]


class _Handler(_Base):
    def setUp(self):
        super().setUp()
        patcher = patch.object(bot_mod, "DECISION_CARRY", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        bot = self.bot
        bot._check_access = AsyncMock(return_value=True)
        bot._maybe_consume_secret = AsyncMock(return_value=False)
        bot._maybe_consume_login_code = AsyncMock(return_value=False)
        bot._maybe_capture_outside_approval = AsyncMock(return_value=False)
        bot._maybe_buffer_split_part = AsyncMock(return_value=False)
        self.dispatched = []

        async def dispatch(update, user_id, text, **kw):
            self.dispatched.append(text)

        bot._dispatch_text_task = dispatch

    def send(self, text, reply_to=None):
        rtm = SimpleNamespace(message_id=reply_to) if reply_to else None
        msg = SimpleNamespace(text=text, chat_id=CHAT, reply_to_message=rtm)
        update = SimpleNamespace(message=msg,
                                 effective_user=SimpleNamespace(id=CHAT))
        with patch.object(bot_mod.session_manager, "get_session",
                          AsyncMock(return_value={})):
            asyncio.run(self.bot._handle_text_message(update, None))

    def selected_for(self, mid):
        for c in self.tg.edit_message_text.await_args_list:
            if c.kwargs["message_id"] == mid:
                return c.kwargs["text"]
        return None


class TestReplyClearsThatMenuOnly(_Handler):
    def test_reply_to_older_menu_leaves_newest_armed(self):
        self.bridge_menu(300)
        self.bridge_menu(310)
        self.send("neither, let's wait", reply_to=300)
        self.assertEqual(self.stripped_ids(), [300])
        self.assertTrue(self.selected_for(300).endswith(
            messages.SELECTED.format(choice=messages.SELECTED_TYPED)))
        self.assertIsNone(self.selected_for(310))
        self.assertEqual(self.dispatched, ["neither, let's wait"])

    def test_reply_with_label_uses_the_label(self):
        self.bridge_menu(300)
        self.bridge_menu(310)
        self.send("alpha", reply_to=300)
        self.assertEqual(self.stripped_ids(), [300])
        self.assertTrue(self.selected_for(300).endswith(
            messages.SELECTED.format(choice="1. Alpha")))

    def test_reply_to_push_menu_clears_that_push(self):
        self.push_menu(500)
        self.bridge_menu(510)
        self.send("some free text", reply_to=500)
        self.assertEqual(self.stripped_ids(), [500])
        self.assertTrue(self.selected_for(500).endswith(
            messages.SELECTED.format(choice=messages.SELECTED_TYPED)))

    def test_reply_to_non_menu_message_acts_as_plain_text(self):
        self.bridge_menu(310)
        self.send("1", reply_to=999)
        self.assertEqual(self.stripped_ids(), [310])
        self.send("free text", reply_to=999)
        self.assertEqual(self.stripped_ids(), [310])


class TestPlainText(_Handler):
    def test_plain_number_clears_newest_with_its_label(self):
        self.bridge_menu(300)
        self.bridge_menu(310, labels=["Gamma", "Delta"],
                         buttons=["1. Gamma", "2. Delta"])
        self.send("2")
        self.assertEqual(self.stripped_ids(), [310])
        self.assertTrue(self.selected_for(310).endswith(
            messages.SELECTED.format(choice="2. Delta")))

    def test_plain_unmatched_leaves_every_keyboard_armed(self):
        self.bridge_menu(300)
        self.bridge_menu(310)
        self.send("an unrelated question")
        self.tg.edit_message_reply_markup.assert_not_awaited()
        self.tg.edit_message_text.assert_not_awaited()
        self.assertEqual(self.dispatched, ["an unrelated question"])

    def test_plain_unmatched_keeps_carry_off(self):
        # 2026-10-13 incident: a free-text answer must not make DGN-1958
        # re-attach the menu's buttons to later assistant messages.
        self.bridge_menu(310)
        self.send("let's do something else entirely")
        self.tg.edit_message_reply_markup.assert_not_awaited()
        self.assertIsNone(sdk_mod.live_decision_menu(CHAT))
        self.tg.send_message.reset_mock()
        self.bot._attach_keyboard = AsyncMock(return_value=True)
        self.bot._send_file_paths = AsyncMock()
        for carrier in (401, 402, 403):
            asyncio.run(self.bot._send_content_artifacts(
                CHAT, "A later assistant message.", False, options=None,
                body="A later assistant message.", carrier_id=carrier))
        self.tg.send_message.assert_not_awaited()
        self.tg.edit_message_reply_markup.assert_not_awaited()
        row = json.loads(self.bubbles.read_text())[-1]
        self.assertTrue(row["menu"]["answered"])

    def test_typed_normalized_label_then_status_reply_has_no_keyboard(self):
        """DGN-2169: a settled menu cannot absorb the following turn."""
        labels = ["dev.7 지금 내기 (추천)", "오늘은 보류"]
        self.bridge_menu(
            46892, labels=labels,
            buttons=["1. dev.7 지금 내기 (추천)", "2. 오늘은 보류"],
            question="버전을 내는 건 사용자 승인 사항입니다.",
        )

        self.send("dev7지금내기")

        row = json.loads(self.bubbles.read_text())[-1]
        self.assertTrue(row["menu"]["answered"])
        self.bot._send_file_paths = AsyncMock()
        self.bot._attach_keyboard = AsyncMock(return_value=True)
        status = (
            "dev.7 출시 검사가 돌고 있습니다. 그 사이에 상태를 확인합니다.\n\n"
            "**dev.7**\n"
            "- 처음 시도는 바로 막혔습니다.\n"
            "- 파일들을 치우고 출시 전 검사가 돌고 있습니다."
        )
        asyncio.run(self.bot._send_content_artifacts(
            CHAT, status, False, body=status, carrier_id=46898
        ))

        self.bot._attach_keyboard.assert_not_awaited()
        self.tg.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
