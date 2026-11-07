"""DGN-2062: typing over the newest armed menu clears it like a tap.

Owner-confirmed spec (17:29):
  - the owner sends text while the chat's newest inline-keyboard message
    (an assistant reply OR a push sent through the push path) is armed;
  - the text is an option number ("1", "1.") or an option label
    (normalized) -> the DGN-1813 tap end state: keyboard removed,
    "Selected: <label>" appended (4096-char rule: over the limit the line
    goes out as a silent reply to that bubble);
  - no match -> keyboard removed, "Selected: <typed reply>" appended
    (DGN-2084: only for a Telegram reply to that menu; plain unmatched
    text leaves the keyboards alone);
  - only the newest menu is cleared; older menus keep their own rule;
  - the text still runs as a normal turn.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import telegram.error

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

import bridge.bot as bot_mod
import bridge.sdk_bridge as sdk_mod
from bridge import messages
from bridge.bot import TelegramBot
from bridge.i18n import en as i18n_en
from bridge.i18n import ko as i18n_ko
from bridge.options import match_typed_choice

CHAT = 42
BODY = "Which one should we ship first?"
BUTTONS = ["1. Alpha", "2. Beta release"]


class TestMatchTypedChoice(unittest.TestCase):
    def test_bare_and_dotted_number(self):
        self.assertEqual(match_typed_choice("1", BUTTONS), "1. Alpha")
        self.assertEqual(match_typed_choice(" 2. ", BUTTONS), "2. Beta release")

    def test_number_out_of_range_or_in_prose_is_no_match(self):
        self.assertIsNone(match_typed_choice("3", BUTTONS))
        self.assertIsNone(match_typed_choice("0", BUTTONS))
        self.assertIsNone(match_typed_choice("1 more thing", BUTTONS))

    def test_label_normalized(self):
        self.assertEqual(match_typed_choice("  beta   RELEASE ", BUTTONS),
                         "2. Beta release")
        self.assertEqual(match_typed_choice("**Alpha**", BUTTONS), "1. Alpha")
        self.assertEqual(match_typed_choice("2. Beta release", BUTTONS),
                         "2. Beta release")

    def test_label_ignores_dots_spaces_and_recommendation_marker(self):
        buttons = ["1. dev.7 지금 내기 (추천)", "2. 오늘은 보류"]
        labels = ["dev.7 지금 내기 (추천)", "오늘은 보류"]
        self.assertEqual(
            match_typed_choice("dev7지금내기", buttons, labels), buttons[0]
        )

    def test_recorded_labels_cover_number_handle_buttons(self):
        handles = ["1번", "2번"]
        self.assertEqual(
            match_typed_choice("a long second option", handles,
                               ["a long first option", "a long second option"]),
            "2번",
        )

    def test_lone_button_label_and_number(self):
        self.assertEqual(match_typed_choice("later", ["later"]), "later")
        self.assertEqual(match_typed_choice("1", ["later"]), "later")

    def test_no_match(self):
        self.assertIsNone(match_typed_choice("something else", BUTTONS))
        self.assertIsNone(match_typed_choice("", BUTTONS))


class TestStrings(unittest.TestCase):
    def test_typed_reply_word_both_locales(self):
        self.assertEqual(i18n_ko.STRINGS["selected_typed"], "직접입력")
        self.assertEqual(i18n_en.STRINGS["selected_typed"], "typed reply")
        self.assertEqual(
            i18n_ko.STRINGS["selected"].format(choice=i18n_ko.STRINGS["selected_typed"]),
            "선택: 직접입력",
        )
        self.assertEqual(
            i18n_en.STRINGS["selected"].format(choice=i18n_en.STRINGS["selected_typed"]),
            "Selected: typed reply",
        )


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / ".telegram_bot").mkdir()
        self.bubbles = root / ".telegram_bot" / "options-bubbles.json"
        self.ledger = root / ".telegram_bot" / "outbound-ledger.jsonl"
        for target in (
            patch.object(sdk_mod, "options_bubbles_path", lambda: self.bubbles),
            patch.object(bot_mod, "PROJECT_ROOT", root),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.bodies = {}
        tg = MagicMock()
        tg.edit_message_reply_markup = AsyncMock(side_effect=self._strip)
        tg.edit_message_text = AsyncMock()
        tg.edit_message_caption = AsyncMock()
        tg.send_message = AsyncMock()
        self.tg = tg
        self.bot = TelegramBot.__new__(TelegramBot)
        self.bot.application = MagicMock()
        self.bot.application.bot = tg
        self.disarmed = set()

    async def _strip(self, chat_id, message_id, reply_markup=None):
        if message_id in self.disarmed:
            raise telegram.error.BadRequest(
                "Message is not modified: specified new message content and "
                "reply markup are exactly the same")
        body = self.bodies.get(message_id, BODY)
        return SimpleNamespace(message_id=message_id, text=body, text_html=body,
                               caption=None)

    def bridge_menu(self, mid, buttons=BUTTONS, labels=None, **menu):
        sdk_mod.publish_options_bubble(
            CHAT, mid, buttons,
            menu=dict({"labels": labels or ["Alpha", "Beta release"],
                       "answered": False, "covered": False}, **menu),
        )

    def push_menu(self, mid, buttons=BUTTONS, tag=None):
        row = {"ts": "2026-10-13T08:00:00Z", "chat_id": str(CHAT),
               "body": BODY, "audience": "owner",
               "message_id": str(mid), "keyboard": buttons}
        if tag:
            row["tag"] = tag
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def typed(self, text, reply_to=None):
        asyncio.run(self.bot._clear_typed_over_menu(CHAT, text, reply_to=reply_to))

    def stripped_ids(self):
        return [c.kwargs["message_id"]
                for c in self.tg.edit_message_reply_markup.await_args_list]

    def appended(self):
        self.tg.edit_message_text.assert_awaited_once()
        return self.tg.edit_message_text.await_args.kwargs


class TestTypedOverBridgeMenu(_Base):
    def test_number_is_a_tap(self):
        self.bridge_menu(300)
        self.typed("1")
        self.assertEqual(self.stripped_ids(), [300])
        kw = self.appended()
        self.assertEqual(kw["message_id"], 300)
        self.assertEqual(kw["text"], BODY + "\n\n"
                         + messages.SELECTED.format(choice="1. Alpha"))
        self.assertIsNone(kw["reply_markup"])
        self.assertEqual(kw["parse_mode"], "HTML")
        row = json.loads(self.bubbles.read_text())[-1]
        self.assertTrue(row["menu"]["answered"])

    def test_dotted_number(self):
        self.bridge_menu(300)
        self.typed("2.")
        self.assertTrue(self.appended()["text"].endswith(
            messages.SELECTED.format(choice="2. Beta release")))

    def test_label_match(self):
        self.bridge_menu(300)
        self.typed("beta  release")
        self.assertTrue(self.appended()["text"].endswith(
            messages.SELECTED.format(choice="2. Beta release")))

    def test_no_match_is_typed_reply(self):
        self.bridge_menu(300)
        # DGN-2084: an unmatched text clears the menu only as a reply to it
        self.typed("actually, neither -- let's wait a week", reply_to=300)
        self.assertEqual(self.stripped_ids(), [300])
        self.assertTrue(self.appended()["text"].endswith(
            messages.SELECTED.format(choice=messages.SELECTED_TYPED)))

    def test_plain_unmatched_keeps_keyboard(self):
        # promise: dec-424 -- unmatched plain text never removes buttons (DGN-2422)
        self.bridge_menu(300)
        self.typed("actually, neither -- let's wait a week")
        self.assertEqual(self.stripped_ids(), [])

    def test_reply_to_non_menu_keeps_keyboard(self):
        # promise: dec-424 -- a reply to a message without buttons is plain text (DGN-2422)
        self.bridge_menu(300)
        self.typed("some code from another message", reply_to=999)
        self.assertEqual(self.stripped_ids(), [])

    def test_html_body_choice_escaped(self):
        self.bridge_menu(300, buttons=["1. A<B", "2. C"], labels=["A<B", "C"])
        self.typed("1")
        self.assertTrue(self.appended()["text"].endswith(
            messages.SELECTED.format(choice="1. A&lt;B")))

    def test_registry_entry_answered(self):
        reg = self.bot._live_menu_registry()
        reg.add(CHAT, 300, ["Alpha", "Beta release"])
        self.bridge_menu(300)
        self.typed("1")
        self.assertEqual(reg.live(CHAT), [])


class TestTypedOverPushMenu(_Base):
    def test_push_path_menu_cleared(self):
        self.push_menu(500)
        self.typed("Alpha")
        self.assertEqual(self.stripped_ids(), [500])
        self.assertTrue(self.appended()["text"].endswith(
            messages.SELECTED.format(choice="1. Alpha")))

    def test_push_newer_than_bridge_menu_wins(self):
        self.bridge_menu(300)
        self.push_menu(500)
        self.typed("1")
        self.assertEqual(self.stripped_ids(), [500])

    def test_push_edit_dropped_keyboard_is_not_armed(self):
        self.push_menu(500)
        self.push_menu(500, buttons=[], tag="edit")
        self.typed("1")
        self.tg.edit_message_reply_markup.assert_not_awaited()

    def test_other_chat_ignored(self):
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"chat_id": "7", "body": BODY, "message_id": "900",
                                 "keyboard": BUTTONS}) + "\n")
        self.typed("1")
        self.tg.edit_message_reply_markup.assert_not_awaited()


class TestOverflow(_Base):
    def test_over_limit_strips_and_replies_short(self):
        self.bridge_menu(300)
        self.bodies[300] = "x" * 4090
        self.typed("1")
        self.assertEqual(self.stripped_ids(), [300])
        self.tg.edit_message_text.assert_not_awaited()
        self.tg.send_message.assert_awaited_once()
        args, kw = self.tg.send_message.await_args
        self.assertEqual(args, (CHAT, messages.SELECTED.format(choice="1. Alpha")))
        self.assertTrue(kw["disable_notification"])
        self.assertEqual(kw["reply_parameters"].message_id, 300)

    def test_refused_append_falls_back_to_reply(self):
        self.bridge_menu(300)
        self.tg.edit_message_text.side_effect = RuntimeError("Bad Request")
        self.typed("zzz", reply_to=300)
        self.tg.send_message.assert_awaited_once()
        self.assertEqual(
            self.tg.send_message.await_args.args[1],
            messages.SELECTED.format(choice=messages.SELECTED_TYPED),
        )


class TestSupersededUntouched(_Base):
    def test_only_newest_menu_cleared(self):
        self.bridge_menu(300)
        self.bridge_menu(310)
        self.typed("1")
        self.assertEqual(self.stripped_ids(), [310])

    def test_newest_already_tapped_leaves_older_alone(self):
        self.bridge_menu(300)
        self.push_menu(500)
        self.disarmed.add(500)  # tapped earlier: Telegram has no keyboard
        self.typed("1")
        self.assertEqual(self.stripped_ids(), [500])
        self.tg.edit_message_text.assert_not_awaited()
        self.tg.send_message.assert_not_awaited()

    def test_answered_flag_does_not_disarm_a_live_keyboard(self):
        # DGN-1958 marks the record answered on a typed number before this
        # runs; the keyboard is still on screen, so it is still cleared.
        self.bridge_menu(300)
        self.bridge_menu(310, answered=True)
        self.typed("1")
        self.assertEqual(self.stripped_ids(), [310])

    def test_carry_source_is_superseded_by_the_carried_bubble(self):
        self.bridge_menu(310, carried_to=320)
        self.bridge_menu(320)
        self.typed("1")
        self.assertEqual(self.stripped_ids(), [320])

    def test_no_menu_no_call(self):
        self.typed("hello")
        self.tg.edit_message_reply_markup.assert_not_awaited()


class TestTextTurnStillRuns(_Base):
    def test_handle_text_clears_then_dispatches(self):
        self.bridge_menu(300)
        order = []
        bot = self.bot
        bot._check_access = AsyncMock(return_value=True)
        bot._maybe_consume_secret = AsyncMock(return_value=False)
        bot._maybe_consume_login_code = AsyncMock(return_value=False)
        bot._maybe_capture_outside_approval = AsyncMock(return_value=False)
        bot._maybe_buffer_split_part = AsyncMock(return_value=False)
        real_clear = bot._clear_typed_over_menu

        async def clear(chat_id, text, **kw):
            order.append("clear")
            await real_clear(chat_id, text, **kw)

        async def dispatch(update, user_id, text, **kw):
            order.append(("dispatch", text))

        bot._clear_typed_over_menu = clear
        bot._dispatch_text_task = dispatch
        msg = SimpleNamespace(text="2", chat_id=CHAT)
        update = SimpleNamespace(message=msg,
                                 effective_user=SimpleNamespace(id=CHAT))
        with patch.object(bot_mod.session_manager, "get_session",
                          AsyncMock(return_value={})):
            asyncio.run(bot._handle_text_message(update, None))
        self.assertEqual(order, ["clear", ("dispatch", "2")])
        self.assertEqual(self.stripped_ids(), [300])


if __name__ == "__main__":
    unittest.main()
