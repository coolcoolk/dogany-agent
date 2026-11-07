"""DGN-2331: an old menu never captures a typed reply.

  1. typed text binds only to the newest menu of the chat;
  2. audience=operator alert menus are tap-only (typed text never binds,
     nor falls through to an older menu) and their buttons expire after 1 h;
  3. a bound tap or typed reply retains the selection echo;
  4. a typed reply that does not bind reaches the agent unchanged.
"""
# test-component: bridge stale menu capture
# test-tier: 1
# test-prereq: tmp-files
# test-new-component: typed-reply binding and stale/operator menu expiry is its own boundary; the owner-menu suite covers delivery and latching, not capture scope

import asyncio
import datetime
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

import bridge.bot as bot_mod
from bridge import messages
from bridge.tests.test_dgn2062_typed_over_menu import BODY, BUTTONS, CHAT
from bridge.tests.test_dgn2084_typed_reply_scope import _Handler

HOUR = 3600
ALERT = 100
QUESTION = 200


def _iso(age_seconds):
    when = datetime.datetime.fromtimestamp(
        time.time() - age_seconds, datetime.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Fixture(_Handler):
    def alert(self, mid=ALERT, age=44 * HOUR, audience="operator", ts=True):
        row = {"chat_id": str(CHAT), "body": "backup failed", "message_id": str(mid),
               "audience": audience, "keyboard": ["Check the cause"]}
        if ts:
            row["ts"] = _iso(age)
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def recorded(self, mid):
        row = self.bot._option_menus().get(CHAT, mid)
        return row["choice"] if row else None

    def nothing_sent(self):
        self.tg.edit_message_text.assert_not_awaited()
        self.tg.edit_message_caption.assert_not_awaited()
        self.tg.send_message.assert_not_awaited()


class TestTypedNeverBindsOperatorAlert(_Fixture):
    def test_gate_stale_alert_and_newer_question(self):
        self.alert()
        self.bridge_menu(QUESTION)
        self.send("1")
        self.assertEqual(self.stripped_ids(), [QUESTION])
        self.assertEqual(self.recorded(QUESTION), "1. Alpha")
        self.assertIsNone(self.recorded(ALERT))
        self.assertEqual(
            self.tg.edit_message_text.await_args.kwargs["text"],
            BODY + "\n\n" + messages.SELECTED.format(choice="1. Alpha"),
        )
        self.tg.edit_message_caption.assert_not_awaited()
        self.tg.send_message.assert_not_awaited()
        self.assertEqual(self.dispatched, ["1"])

    def test_alert_is_newest_typed_digit_binds_nothing(self):
        self.alert(mid=300)
        self.send("1")
        self.tg.edit_message_reply_markup.assert_not_awaited()
        self.assertIsNone(self.recorded(300))
        self.nothing_sent()
        self.assertEqual(self.dispatched, ["1"])

    def test_alert_label_typed_binds_nothing(self):
        self.alert(mid=300, age=60)
        self.send("Check the cause")
        self.tg.edit_message_reply_markup.assert_not_awaited()
        self.assertEqual(self.dispatched, ["Check the cause"])

    def test_no_fall_through_to_an_older_agent_menu(self):
        self.bridge_menu(QUESTION)
        self.alert(mid=300, age=60)
        self.send("1")
        self.tg.edit_message_reply_markup.assert_not_awaited()
        self.assertEqual(self.dispatched, ["1"])

    def test_reply_to_alert_does_not_bind(self):
        self.alert(mid=300, age=60)
        self.send("whatever", reply_to=300)
        self.tg.edit_message_reply_markup.assert_not_awaited()
        self.assertIsNone(self.recorded(300))
        self.assertEqual(self.dispatched, ["whatever"])

    def test_alert_without_send_time_is_still_tap_only(self):
        self.alert(mid=300, ts=False)
        self.send("1")
        self.tg.edit_message_reply_markup.assert_not_awaited()


class TestOlderMenuNeverBinds(_Fixture):
    def test_older_owner_menu_ignored_when_newer_menu_exists(self):
        self.push_menu(500)
        self.bridge_menu(600, labels=["Gamma", "Delta"],
                         buttons=["1. Gamma", "2. Delta"])
        self.send("Alpha")  # names only the OLDER menu's option
        self.tg.edit_message_reply_markup.assert_not_awaited()
        self.assertEqual(self.dispatched, ["Alpha"])
        self.send("2")
        self.assertEqual(self.stripped_ids(), [600])

    def test_unbound_reply_passes_through_unchanged(self):
        self.bridge_menu(300)
        self.send("  something else entirely ")
        self.tg.edit_message_reply_markup.assert_not_awaited()
        self.assertEqual(self.dispatched, ["  something else entirely "])


class TestOperatorTapExpiry(_Fixture):
    def tap(self, mid, age=None, audience="operator"):
        if age is not None:
            self.alert(mid=mid, age=age, audience=audience)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=CHAT), message_id=mid, date=None,
            text=BODY, text_html=BODY, caption=None, caption_html=None,
            reply_text=AsyncMock(),
            reply_markup=SimpleNamespace(inline_keyboard=[[
                SimpleNamespace(text="Check the cause", callback_data="opt:1")]]),
        )
        query = MagicMock(data="opt:1", message=message)
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update = SimpleNamespace(callback_query=query,
                                 effective_user=SimpleNamespace(id=CHAT),
                                 effective_chat=message.chat)
        enqueue = AsyncMock(return_value=True)
        bot = self.bot
        with patch.object(bot, "_check_access", AsyncMock(return_value=True)), \
                patch.object(bot, "_maybe_capture_outside_approval",
                             AsyncMock(return_value=False)), \
                patch.object(bot, "_enqueue_user_task", enqueue):
            asyncio.run(bot._handle_callback(update, MagicMock()))
        return query, message, enqueue

    def test_tap_after_an_hour_is_expired_and_does_nothing(self):
        query, message, enqueue = self.tap(ALERT, age=HOUR + 60)
        query.answer.assert_awaited_once_with(messages.OPTIONS_EXPIRED, show_alert=True)
        enqueue.assert_not_awaited()
        self.assertIsNone(self.recorded(ALERT))
        query.edit_message_reply_markup.assert_not_awaited()

    def test_tap_after_44_hours_is_expired(self):
        query, _, enqueue = self.tap(ALERT, age=44 * HOUR)
        query.answer.assert_awaited_once_with(messages.OPTIONS_EXPIRED, show_alert=True)
        enqueue.assert_not_awaited()

    def test_tap_within_an_hour_works_with_echo(self):
        query, message, enqueue = self.tap(ALERT, age=HOUR - 60)
        enqueue.assert_awaited_once()
        self.assertEqual(self.recorded(ALERT), "Check the cause")
        query.edit_message_reply_markup.assert_not_awaited()
        query.edit_message_text.assert_awaited_once_with(
            BODY + "\n\n" + messages.SELECTED.format(choice="Check the cause"),
            parse_mode="HTML",
            reply_markup=None,
        )
        query.edit_message_caption.assert_not_awaited()
        message.reply_text.assert_not_awaited()

    def test_owner_menu_taps_never_expire(self):
        query, _, enqueue = self.tap(ALERT, age=44 * HOUR, audience="owner")
        enqueue.assert_awaited_once()

    def test_unknown_send_time_alert_tap_is_expired(self):
        self.alert(mid=ALERT, ts=False)
        query, _, enqueue = self.tap(ALERT)
        query.answer.assert_awaited_once_with(messages.OPTIONS_EXPIRED, show_alert=True)
        enqueue.assert_not_awaited()


class TestExpiredCopy(_Fixture):
    def test_both_locales_have_a_polite_expired_answer(self):
        from bridge.i18n import en, ko
        self.assertTrue(en.STRINGS["options_expired"])
        self.assertTrue(ko.STRINGS["options_expired"])
