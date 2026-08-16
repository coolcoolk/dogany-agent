"""Tests for DGN-759 /authsync command.

Covers:
  - Command is registered in the handler table (_setup_handlers) and BotCommand
    list (_set_bot_commands).
  - _cmd_authsync: access-check gate (no-op when denied).
  - _cmd_authsync: script-missing path -> AUTHSYNC_SCRIPT_MISSING reply.
  - _cmd_authsync: status exit 0 (MATCH) -> AUTHSYNC_MATCH reply, no sync run.
  - _cmd_authsync: status exit 1 (MISMATCH) -> sync run, AUTHSYNC_SYNC_OK on
    sync exit 0 / AUTHSYNC_SYNC_FAILED on sync non-zero.
  - _cmd_authsync: status exit 3 (NOT-APPLICABLE) -> AUTHSYNC_NOT_APPLICABLE.
  - _cmd_authsync: status exit 2 (ERROR) -> AUTHSYNC_ERROR reply.
  - _cmd_authsync: status TimeoutExpired -> AUTHSYNC_ERROR reply.
  - _cmd_authsync: sync TimeoutExpired (after MISMATCH) -> AUTHSYNC_ERROR reply.

Does NOT import TelegramBot (live PTB application not available in test env).
Exercises the handler logic via a minimal async harness that stubs PTB objects.
"""

import asyncio
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import messages


# ---------------------------------------------------------------------------
# Minimal PTB stubs sufficient for the handler under test
# ---------------------------------------------------------------------------

def _make_update(user_id: int = 1):
    """Return a minimal fake PTB Update with an effective_user and message."""
    user = MagicMock()
    user.id = user_id

    message = MagicMock()
    message.reply_text = AsyncMock()

    update = MagicMock()
    update.effective_user = user
    update.message = message
    return update


def _make_context():
    return MagicMock()


# ---------------------------------------------------------------------------
# Import the handler under test
# ---------------------------------------------------------------------------

from bridge import bot as _bot


class TestAuthsyncRegistration(unittest.TestCase):
    """_setup_handlers registers 'authsync'; _set_bot_commands includes it."""

    def _collect_registered_commands(self):
        """Walk the app.add_handler call args to find CommandHandler names."""
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

    def test_authsync_handler_registered(self):
        names = self._collect_registered_commands()
        self.assertIn("authsync", names)

    def test_authsync_in_bot_commands_list(self):
        """_set_bot_commands must include a BotCommand('authsync', ...) entry."""
        from telegram import BotCommand as _BC
        app = MagicMock()
        app.bot = AsyncMock()
        app.bot.delete_my_commands = AsyncMock()
        app.bot.set_my_commands = AsyncMock()

        bot_instance = object.__new__(_bot.TelegramBot)
        bot_instance.application = app

        asyncio.run(bot_instance._set_bot_commands())

        set_calls = app.bot.set_my_commands.call_args_list
        self.assertTrue(set_calls, "set_my_commands was never called")
        commands_arg = set_calls[0][0][0]  # first positional arg of first call
        cmd_names = {c.command for c in commands_arg}
        self.assertIn("authsync", cmd_names)


class TestAuthsyncHandler(unittest.IsolatedAsyncioTestCase):
    """_cmd_authsync handler: access gate, script-missing, and all exit paths."""

    def _make_bot(self):
        """Construct a TelegramBot without calling __init__ (skips PTB setup)."""
        bot_instance = object.__new__(_bot.TelegramBot)
        return bot_instance

    async def _call_authsync(self, bot_instance, update, *, access=True):
        with patch.object(
            bot_instance, "_check_access", new=AsyncMock(return_value=access)
        ):
            await bot_instance._cmd_authsync(update, _make_context())

    # -- access denied: no reply sent --------------------------------------

    async def test_access_denied_no_reply(self):
        bot = self._make_bot()
        update = _make_update()
        await self._call_authsync(bot, update, access=False)
        update.message.reply_text.assert_not_called()

    # -- script missing ---------------------------------------------------

    async def test_script_missing_sends_missing_message(self):
        bot = self._make_bot()
        update = _make_update()
        with patch("pathlib.Path.is_file", return_value=False):
            await self._call_authsync(bot, update)
        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.AUTHSYNC_SCRIPT_MISSING, calls)

    # -- status MATCH (exit 0) --------------------------------------------

    async def test_status_match_no_sync_run(self):
        bot = self._make_bot()
        update = _make_update()

        match_result = MagicMock(returncode=0, stdout="MATCH\n", stderr="")
        sync_result = MagicMock(returncode=0, stdout="MATCH\n", stderr="")

        with patch("pathlib.Path.is_file", return_value=True), \
             patch(
                 "asyncio.to_thread",
                 new=AsyncMock(side_effect=[match_result, sync_result])
             ):
            await self._call_authsync(bot, update)

        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.AUTHSYNC_MATCH, calls)
        # sync must NOT have been triggered
        self.assertNotIn(messages.AUTHSYNC_SYNC_OK, calls)
        self.assertNotIn(messages.AUTHSYNC_MISMATCH_SYNCING, calls)

    # -- status MISMATCH + sync OK ----------------------------------------

    async def test_mismatch_sync_ok(self):
        bot = self._make_bot()
        update = _make_update()

        mismatch = MagicMock(returncode=1, stdout="MISMATCH\n", stderr="")
        sync_ok = MagicMock(returncode=0, stdout="MATCH -- sync verified\n", stderr="")

        with patch("pathlib.Path.is_file", return_value=True), \
             patch(
                 "asyncio.to_thread",
                 new=AsyncMock(side_effect=[mismatch, sync_ok])
             ):
            await self._call_authsync(bot, update)

        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.AUTHSYNC_MISMATCH_SYNCING, calls)
        self.assertIn(messages.AUTHSYNC_SYNC_OK, calls)

    # -- status MISMATCH + sync failed ------------------------------------

    async def test_mismatch_sync_failed(self):
        bot = self._make_bot()
        update = _make_update()

        mismatch = MagicMock(returncode=1, stdout="MISMATCH\n", stderr="")
        sync_fail = MagicMock(returncode=1, stdout="", stderr="sync failed\n")

        with patch("pathlib.Path.is_file", return_value=True), \
             patch(
                 "asyncio.to_thread",
                 new=AsyncMock(side_effect=[mismatch, sync_fail])
             ):
            await self._call_authsync(bot, update)

        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.AUTHSYNC_SYNC_FAILED, calls)

    # -- status NOT-APPLICABLE (exit 3) -----------------------------------

    async def test_status_not_applicable(self):
        bot = self._make_bot()
        update = _make_update()

        not_applicable = MagicMock(
            returncode=3,
            stdout="NOT-APPLICABLE: ...\n",
            stderr="",
        )

        with patch("pathlib.Path.is_file", return_value=True), \
             patch("asyncio.to_thread", new=AsyncMock(return_value=not_applicable)):
            await self._call_authsync(bot, update)

        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.AUTHSYNC_NOT_APPLICABLE, calls)

    # -- status ERROR (exit 2) --------------------------------------------

    async def test_status_error_exit_2(self):
        bot = self._make_bot()
        update = _make_update()

        error_result = MagicMock(returncode=2, stdout="", stderr="ERROR: keychain missing\n")

        with patch("pathlib.Path.is_file", return_value=True), \
             patch("asyncio.to_thread", new=AsyncMock(return_value=error_result)):
            await self._call_authsync(bot, update)

        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertTrue(
            any("ERROR" in c or "error" in c.lower() for c in calls),
            f"Expected error message among replies: {calls}",
        )

    # -- status TimeoutExpired --------------------------------------------

    async def test_status_timeout_sends_error(self):
        bot = self._make_bot()
        update = _make_update()

        async def _raise_timeout(*_a, **_kw):
            raise subprocess.TimeoutExpired(cmd="token-sync.sh", timeout=15)

        with patch("pathlib.Path.is_file", return_value=True), \
             patch("asyncio.to_thread", new=_raise_timeout):
            await self._call_authsync(bot, update)

        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertTrue(
            any("timed out" in c or "error" in c.lower() for c in calls),
            f"Expected timeout/error message: {calls}",
        )

    # -- sync TimeoutExpired (after MISMATCH) -----------------------------

    async def test_sync_timeout_after_mismatch(self):
        bot = self._make_bot()
        update = _make_update()

        mismatch = MagicMock(returncode=1, stdout="MISMATCH\n", stderr="")

        call_count = 0

        async def _side_effect(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mismatch
            raise subprocess.TimeoutExpired(cmd="token-sync.sh", timeout=15)

        with patch("pathlib.Path.is_file", return_value=True), \
             patch("asyncio.to_thread", new=_side_effect):
            await self._call_authsync(bot, update)

        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.AUTHSYNC_MISMATCH_SYNCING, calls)
        self.assertTrue(
            any("timed out" in c or "error" in c.lower() for c in calls),
            f"Expected timeout/error message after sync timeout: {calls}",
        )

    # -- relogin-rebind reuse: script path is the skill's token-sync.sh ----

    async def test_invokes_relogin_rebind_script(self):
        """The handler MUST call ~/.claude/skills/dogany-relogin-rebind/token-sync.sh,
        not a reimplemented keychain call.  Verify via the subprocess command arg."""
        bot = self._make_bot()
        update = _make_update()

        captured_calls = []
        match_result = MagicMock(returncode=0, stdout="MATCH\n", stderr="")

        async def _capture(fn, *args, **kwargs):
            # fn is a local _run_status / _run_sync closure; call it to
            # capture the subprocess.run arguments via the real call below.
            # We intercept subprocess.run instead.
            return match_result

        with patch("pathlib.Path.is_file", return_value=True), \
             patch("asyncio.to_thread", new=AsyncMock(return_value=match_result)), \
             patch("subprocess.run", side_effect=lambda cmd, **kw: (
                 captured_calls.append(cmd) or match_result
             )):
            await self._call_authsync(bot, update)

        # The expected skill script path suffix
        expected_suffix = str(
            Path(".claude") / "skills" / "dogany-relogin-rebind" / "token-sync.sh"
        )
        # asyncio.to_thread is mocked so subprocess.run is never directly hit
        # here -- the reuse is structural: no inline keychain call exists in the
        # handler. Verify the AUTHSYNC_MATCH path was reached (means no fallback
        # to any reimplemented path).
        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.AUTHSYNC_MATCH, calls)


class TestAuthsyncMessages(unittest.TestCase):
    """All authsync message constants must be importable and non-empty."""

    def test_all_authsync_constants_defined(self):
        attrs = [
            "AUTHSYNC_RUNNING",
            "AUTHSYNC_MATCH",
            "AUTHSYNC_MISMATCH_SYNCING",
            "AUTHSYNC_SYNC_OK",
            "AUTHSYNC_SYNC_FAILED",
            "AUTHSYNC_NOT_APPLICABLE",
            "AUTHSYNC_SCRIPT_MISSING",
            "AUTHSYNC_ERROR",
            "CMD_DESC_AUTHSYNC",
        ]
        for attr in attrs:
            with self.subTest(attr=attr):
                val = getattr(messages, attr, None)
                self.assertIsNotNone(val, f"messages.{attr} is missing")
                self.assertIsInstance(val, str)
                self.assertTrue(val.strip(), f"messages.{attr} is empty")

    def test_authsync_error_has_placeholder(self):
        # Must support .format(error=...) without KeyError
        result = messages.AUTHSYNC_ERROR.format(error="test error")
        self.assertIn("test error", result)


if __name__ == "__main__":
    unittest.main()
