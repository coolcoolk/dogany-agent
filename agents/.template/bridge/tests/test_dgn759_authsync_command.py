"""Tests for the RETIREMENT of the DGN-759 /authsync command (DGN-1050).

History: this file used to assert the /authsync sync behavior (status ->
MISMATCH -> token-sync.sh sync -> keychain overwrite). DGN-1050 established
that exact behavior as the root cause of the estate-wide daily auth deaths:
the credentials FILE is stale by design after CLI runtime token rotations,
so the unconditional file -> keychain overwrite re-injected superseded
refresh tokens and triggered server-side token-family revocation. Per
DGN-1050 the tests now assert the ABSENCE of that surface instead of being
deleted wholesale.

Covers:
  - /authsync is NOT in COMMAND_MENU_SPEC and NOT in the BotCommand menu.
  - A CommandHandler for "authsync" IS still registered: the hidden stub
    must intercept a typed /authsync so the catch-all skill forwarder cannot
    ship it into the SDK session.
  - The stub handler: access gate intact; replies AUTHSYNC_RETIRED and
    NOTHING else; never spawns any subprocess; its source carries no
    subprocess/token-sync wiring at all.
  - The retired copy points at the safe procedure (`claude auth login`).
  - The old sync-era message constants and i18n keys are gone.
  - The DGN-994 restart-CTA surface is gone (see also test_dgn994 file).
"""

import asyncio
import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import messages
from bridge import bot as _bot
from bridge.bot import COMMAND_MENU_SPEC
from bridge.i18n import en as _en, ko as _ko


# ---------------------------------------------------------------------------
# Harness helpers (same object.__new__ harness as before the retirement)
# ---------------------------------------------------------------------------

def _make_update(user_id: int = 1):
    user = MagicMock()
    user.id = user_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_user = user
    update.message = message
    return update


def _make_bot():
    bot_instance = object.__new__(_bot.TelegramBot)
    bot_instance.application = MagicMock()
    return bot_instance


async def _call_authsync(bot_instance, update, *, access=True):
    with patch.object(
        bot_instance, "_check_access", new=AsyncMock(return_value=access)
    ):
        await bot_instance._cmd_authsync(update, MagicMock())


# ---------------------------------------------------------------------------
# Registration surface: off-menu, but the stub handler stays registered
# ---------------------------------------------------------------------------

class TestAuthsyncRetiredRegistration(unittest.TestCase):

    def _collect_registered_commands(self):
        from telegram.ext import CommandHandler as _CH
        app = MagicMock()
        handlers_added = []

        def record_add(handler, **_kw):
            handlers_added.append(handler)

        app.add_handler.side_effect = record_add

        bot_instance = object.__new__(_bot.TelegramBot)
        bot_instance.application = app
        bot_instance._setup_handlers()

        names = set()
        for h in handlers_added:
            if isinstance(h, _CH):
                names.update(h.commands)
        return names

    def test_stub_handler_still_registered(self):
        """DGN-1050: the stub MUST stay registered -- an unregistered
        /authsync would fall through to the catch-all skill forwarder and
        land in the SDK session."""
        names = self._collect_registered_commands()
        self.assertIn("authsync", names)

    def test_authsync_not_in_command_menu_spec(self):
        names = [cmd for cmd, _ in COMMAND_MENU_SPEC]
        self.assertNotIn("authsync", names,
                         "DGN-1050: /authsync is retired and must not "
                         "reappear in COMMAND_MENU_SPEC")

    def test_authsync_not_in_bot_commands_list(self):
        app = MagicMock()
        app.bot = AsyncMock()
        app.bot.delete_my_commands = AsyncMock()
        app.bot.set_my_commands = AsyncMock()

        bot_instance = object.__new__(_bot.TelegramBot)
        bot_instance.application = app

        asyncio.run(bot_instance._set_bot_commands())

        set_calls = app.bot.set_my_commands.call_args_list
        self.assertTrue(set_calls, "set_my_commands was never called")
        commands_arg = set_calls[0][0][0]
        cmd_names = {c.command for c in commands_arg}
        self.assertNotIn("authsync", cmd_names)


# ---------------------------------------------------------------------------
# Stub handler behavior: notice only, zero side effects
# ---------------------------------------------------------------------------

class TestAuthsyncRetiredStub(unittest.IsolatedAsyncioTestCase):

    async def test_access_denied_no_reply(self):
        bot = _make_bot()
        update = _make_update()
        await _call_authsync(bot, update, access=False)
        update.message.reply_text.assert_not_called()

    async def test_owner_gets_retirement_notice_only(self):
        bot = _make_bot()
        update = _make_update()
        captured = []
        with patch("subprocess.run", side_effect=lambda *a, **k: captured.append(a)):
            await _call_authsync(bot, update, access=True)
        self.assertEqual(captured, [],
                         "DGN-1050: retired /authsync must never spawn a "
                         "subprocess (that was the keychain-poisoning path)")
        update.message.reply_text.assert_awaited_once_with(
            messages.AUTHSYNC_RETIRED
        )

    def test_stub_source_has_no_sync_wiring(self):
        """The handler body must carry no subprocess / token-sync.sh /
        keychain wiring whatsoever -- absence asserted at source level so a
        partial revert cannot slip past the behavioral mocks above."""
        src = inspect.getsource(_bot.TelegramBot._cmd_authsync)
        for forbidden in ("subprocess.run", "_resolve_skill_script",
                          "to_thread"):
            self.assertNotIn(forbidden, src,
                             f"DGN-1050: '{forbidden}' found in the retired "
                             f"/authsync stub")


# ---------------------------------------------------------------------------
# Copy: the notice must point at the one safe procedure
# ---------------------------------------------------------------------------

class TestAuthsyncRetiredCopy(unittest.TestCase):

    def test_notice_defined_and_nonempty(self):
        self.assertTrue(messages.AUTHSYNC_RETIRED.strip())

    def test_notice_points_to_claude_auth_login_both_locales(self):
        for strings in (_en.STRINGS, _ko.STRINGS):
            self.assertIn("authsync_retired", strings)
            self.assertIn("claude auth login", strings["authsync_retired"])


# ---------------------------------------------------------------------------
# Absence of the old sync-era surface (DGN-1050)
# ---------------------------------------------------------------------------

class TestAuthsyncSyncSurfaceAbsent(unittest.TestCase):

    OLD_MESSAGE_ATTRS = [
        "AUTHSYNC_RUNNING",
        "AUTHSYNC_MATCH",
        "AUTHSYNC_MISMATCH_SYNCING",
        "AUTHSYNC_SYNC_OK",
        "AUTHSYNC_SYNC_FAILED",
        "AUTHSYNC_NOT_APPLICABLE",
        "AUTHSYNC_SCRIPT_MISSING",
        "AUTHSYNC_ERROR",
        "AUTHSYNC_RESTART_BUTTON",
        "CMD_DESC_AUTHSYNC",
    ]

    OLD_I18N_KEYS = [
        "authsync_running",
        "authsync_match",
        "authsync_mismatch_syncing",
        "authsync_sync_ok",
        "authsync_sync_failed",
        "authsync_not_applicable",
        "authsync_script_missing",
        "authsync_error",
        "authsync_restart_button",
        "cmd_desc_authsync",
    ]

    def test_old_message_constants_gone(self):
        for attr in self.OLD_MESSAGE_ATTRS:
            with self.subTest(attr=attr):
                self.assertFalse(
                    hasattr(messages, attr),
                    f"DGN-1050: messages.{attr} belongs to the retired sync "
                    f"path and must not exist",
                )

    def test_old_i18n_keys_gone_both_locales(self):
        for strings, locale in ((_en.STRINGS, "en"), (_ko.STRINGS, "ko")):
            for key in self.OLD_I18N_KEYS:
                with self.subTest(locale=locale, key=key):
                    self.assertNotIn(
                        key, strings,
                        f"DGN-1050: i18n {locale}:{key} belongs to the "
                        f"retired sync path and must not exist",
                    )

    def test_restart_cta_surface_gone(self):
        self.assertFalse(hasattr(_bot, "AUTHSYNC_RESTART_CB"))
        self.assertFalse(
            hasattr(_bot.TelegramBot, "_handle_authsync_restart_callback")
        )


if __name__ == "__main__":
    unittest.main()
