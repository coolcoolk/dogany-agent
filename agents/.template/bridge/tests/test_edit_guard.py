"""Unit tests for bridge/edit_guard.py (DGN-594).

The shared EditRateGuard: ready() gating (flood deadline + min interval),
state notes, and the fail-open async edit() wrapper. No live Telegram.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

import telegram.error

from bridge.edit_guard import (
    MIN_EDIT_INTERVAL,
    EditOutcome,
    EditRateGuard,
    _is_not_modified,
)

CHAT_ID = 42
MSG_ID = 1111


def _mock_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    return bot


class TestIsNotModified(unittest.TestCase):
    def test_true(self):
        self.assertTrue(_is_not_modified(Exception("message is not modified: x")))

    def test_case_insensitive(self):
        self.assertTrue(_is_not_modified(Exception("Message Is Not Modified")))

    def test_false(self):
        self.assertFalse(_is_not_modified(Exception("message to edit not found")))


class TestReady(unittest.TestCase):
    def test_fresh_guard_is_ready(self):
        self.assertTrue(EditRateGuard().ready(now=100.0))

    def test_default_min_interval_is_spec_value(self):
        self.assertEqual(EditRateGuard().min_interval, MIN_EDIT_INTERVAL)
        self.assertEqual(MIN_EDIT_INTERVAL, 3.0)

    def test_flood_deadline_blocks(self):
        guard = EditRateGuard()
        guard.note_retry_after(10.0, now=100.0)
        self.assertFalse(guard.ready(now=105.0))

    def test_flood_deadline_expires(self):
        guard = EditRateGuard()
        guard.note_retry_after(10.0, now=100.0)
        self.assertTrue(guard.ready(now=110.0))

    def test_min_interval_blocks(self):
        guard = EditRateGuard()
        guard.note_edit(now=100.0)
        self.assertFalse(guard.ready(now=100.0 + MIN_EDIT_INTERVAL - 0.5))

    def test_min_interval_elapses(self):
        guard = EditRateGuard()
        guard.note_edit(now=100.0)
        self.assertTrue(guard.ready(now=100.0 + MIN_EDIT_INTERVAL))

    def test_custom_min_interval(self):
        guard = EditRateGuard(min_interval=0.0)
        guard.note_edit(now=100.0)
        self.assertTrue(guard.ready(now=100.0))


class TestEdit(unittest.TestCase):
    def _edit(self, guard, bot, text="body"):
        return asyncio.run(guard.edit(bot, CHAT_ID, MSG_ID, text))

    def test_success_returns_ok_and_notes_edit(self):
        guard = EditRateGuard()
        bot = _mock_bot()
        outcome = self._edit(guard, bot)
        self.assertIs(outcome, EditOutcome.OK)
        self.assertIsNotNone(guard.last_edit)
        bot.edit_message_text.assert_awaited_once_with(
            text="body", chat_id=CHAT_ID, message_id=MSG_ID
        )

    def test_kwargs_forwarded(self):
        guard = EditRateGuard()
        bot = _mock_bot()
        asyncio.run(guard.edit(bot, CHAT_ID, MSG_ID, "body", parse_mode="HTML"))
        self.assertEqual(
            bot.edit_message_text.await_args.kwargs["parse_mode"], "HTML"
        )

    def test_not_modified_is_benign(self):
        guard = EditRateGuard()
        bot = _mock_bot()
        bot.edit_message_text = AsyncMock(
            side_effect=telegram.error.BadRequest("message is not modified: x")
        )
        outcome = self._edit(guard, bot)
        self.assertIs(outcome, EditOutcome.NOT_MODIFIED)
        self.assertIsNotNone(guard.last_edit)  # counts for pacing

    def test_retry_after_arms_flood_and_returns_flood(self):
        guard = EditRateGuard()
        bot = _mock_bot()
        bot.edit_message_text = AsyncMock(
            side_effect=telegram.error.RetryAfter(30)
        )
        outcome = self._edit(guard, bot)
        self.assertIs(outcome, EditOutcome.FLOOD)
        self.assertFalse(guard.ready())  # deadline armed
        self.assertIsNone(guard.last_edit)  # no edit happened

    def test_bad_request_fails_open(self):
        guard = EditRateGuard()
        bot = _mock_bot()
        bot.edit_message_text = AsyncMock(
            side_effect=telegram.error.BadRequest("message to edit not found")
        )
        outcome = self._edit(guard, bot)  # must not raise
        self.assertIs(outcome, EditOutcome.FAILED)

    def test_generic_telegram_error_fails_open(self):
        guard = EditRateGuard()
        bot = _mock_bot()
        bot.edit_message_text = AsyncMock(
            side_effect=telegram.error.TelegramError("internal 500")
        )
        outcome = self._edit(guard, bot)  # must not raise
        self.assertIs(outcome, EditOutcome.FAILED)

    def test_forbidden_fails_open(self):
        guard = EditRateGuard()
        bot = _mock_bot()
        bot.edit_message_text = AsyncMock(
            side_effect=telegram.error.Forbidden("bot was blocked")
        )
        outcome = self._edit(guard, bot)  # must not raise
        self.assertIs(outcome, EditOutcome.FAILED)


if __name__ == "__main__":
    unittest.main()
