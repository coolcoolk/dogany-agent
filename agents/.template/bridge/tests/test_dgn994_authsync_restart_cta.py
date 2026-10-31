"""Tests for the ABSENCE of the DGN-994 /authsync restart CTA (DGN-1050).

History: this file used to assert the sync-ok restart CTA wiring (inline
Restart button on the /authsync sync-ok reply + the "authsync:restart"
callback launching self_restart.sh). DGN-1050 retired the whole /authsync
sync path -- the file -> keychain overwrite it CTA'd for was the estate-wide
auth-death vector -- so per DGN-1050 these tests now assert the ABSENCE of
the CTA surface instead of being deleted wholesale.

Covers:
  - bot module no longer defines AUTHSYNC_RESTART_CB.
  - TelegramBot no longer has _handle_authsync_restart_callback.
  - A leftover "authsync:restart" callback tap (old chat history) is a
    harmless no-op: dispatch completes, NO subprocess is launched, no crash.
  - The CTA button i18n key (authsync_restart_button) is gone from both
    locales.
  - The restart latch survives the retirement under its new name
    (RESTART_LATCH_S) for /restart; the old AUTHSYNC_RESTART_LATCH_S name
    is gone.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import bot as _bot
from bridge.i18n import en as _en, ko as _ko


LEGACY_CTA_DATA = "authsync:restart"  # the retired DGN-994 callback token


def _make_callback_update(data: str = LEGACY_CTA_DATA):
    user = MagicMock()
    user.id = 1
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user = user
    update.effective_chat = MagicMock(id=100)
    update.message = None
    return update, query


def _make_bot():
    bot_instance = object.__new__(_bot.TelegramBot)
    bot_instance.application = MagicMock()
    return bot_instance


class TestCtaSurfaceAbsent(unittest.TestCase):

    def test_callback_token_constant_gone(self):
        self.assertFalse(
            hasattr(_bot, "AUTHSYNC_RESTART_CB"),
            "DGN-1050: AUTHSYNC_RESTART_CB belongs to the retired CTA",
        )

    def test_callback_handler_method_gone(self):
        self.assertFalse(
            hasattr(_bot.TelegramBot, "_handle_authsync_restart_callback"),
            "DGN-1050: the CTA callback handler must be removed",
        )

    def test_button_label_i18n_key_gone_both_locales(self):
        self.assertNotIn("authsync_restart_button", _en.STRINGS)
        self.assertNotIn("authsync_restart_button", _ko.STRINGS)

    def test_latch_renamed_and_kept_for_restart_command(self):
        """The duplicate-launch latch window survives for /restart (DGN-997)
        under RESTART_LATCH_S; the AUTHSYNC_* name is retired."""
        self.assertFalse(hasattr(_bot, "AUTHSYNC_RESTART_LATCH_S"))
        self.assertTrue(hasattr(_bot, "RESTART_LATCH_S"))
        self.assertGreater(_bot.RESTART_LATCH_S, 0)


class TestLegacyCtaTapIsNoop(unittest.IsolatedAsyncioTestCase):
    """An owner tapping a leftover CTA button on an old message must fall
    through every callback branch without launching anything."""

    async def test_legacy_tap_no_subprocess_no_crash(self):
        bot = _make_bot()
        update, query = _make_callback_update()
        captured = []
        with patch.object(
            bot, "_check_access", new=AsyncMock(return_value=True)
        ), patch(
            "subprocess.run", side_effect=lambda *a, **k: captured.append(a)
        ):
            await bot._handle_callback(update, MagicMock())
        self.assertEqual(
            captured, [],
            "DGN-1050: a legacy authsync:restart tap must not launch anything",
        )
        # Spinner is still dismissed (standard callback hygiene), but no
        # reply and no keyboard edit happen on this dead branch.
        query.answer.assert_awaited_once()
        query.message.reply_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
