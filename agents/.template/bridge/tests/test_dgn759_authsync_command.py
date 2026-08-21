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
import tempfile
import types
import unittest
from contextlib import contextmanager
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


# ---------------------------------------------------------------------------
# DGN-929: real (non-mocked) workspace/HOME skill-root fixture
#
# Uses actual temp directories and actual files -- no Path.is_file mocking --
# so the fixture exercises the real resolution order, not an assertion about
# it. This is what would have caught the original bug: the old handler
# hardcoded Path.home() only, so a workspace-only script (the standard Dogany
# instance layout) was invisible to it and /authsync always replied
# "script missing" in production despite the script being present and
# executable.
# ---------------------------------------------------------------------------

def _write_script(root: Path) -> Path:
    skill_dir = root / ".claude" / "skills" / "dogany-relogin-rebind"
    skill_dir.mkdir(parents=True)
    script = skill_dir / "token-sync.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return script


@contextmanager
def _real_skill_layout(*, workspace_has_script: bool, home_has_script: bool):
    """Patch bridge.bot.PROJECT_ROOT and Path.home() to two fresh temp dirs,
    optionally populating each with a real dogany-relogin-rebind/token-sync.sh.

    Yields (workspace_root, home_root, expected_script_or_None) where
    expected_script_or_None is the path _resolve_skill_script SHOULD return
    under the workspace-first / HOME-fallback contract (None if neither root
    has the script).
    """
    with tempfile.TemporaryDirectory(prefix="dgn929-ws-") as ws, \
         tempfile.TemporaryDirectory(prefix="dgn929-home-") as home:
        ws_path = Path(ws)
        home_path = Path(home)
        expected = None
        if workspace_has_script:
            expected = _write_script(ws_path)
        if home_has_script:
            home_script = _write_script(home_path)
            if expected is None:
                expected = home_script
        with patch.object(_bot, "PROJECT_ROOT", ws_path), \
             patch("pathlib.Path.home", return_value=home_path):
            yield ws_path, home_path, expected


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
    #
    # DGN-929: this test used to assert the handler MUST call
    # ~/.claude/skills/dogany-relogin-rebind/token-sync.sh (HOME-only) as the
    # correct contract -- that was the bug itself, pinned as a passing test.
    # A standard Dogany instance installs skills under
    # PROJECT_ROOT/.claude/skills (workspace), so the real contract is
    # workspace-first with HOME fallback (matches /skills' _skill_roots()).
    # This test now runs the resolver+subprocess call against a REAL
    # workspace-only script (no HOME copy) and asserts subprocess.run was
    # invoked with THAT resolved path -- something the old HOME-only handler
    # could never satisfy (it would reply AUTHSYNC_SCRIPT_MISSING instead).

    async def test_command_invokes_resolved_workspace_script(self):
        """Script installed only under PROJECT_ROOT/.claude/skills (workspace,
        standard instance layout, no HOME copy). The handler must locate it
        via the workspace-first resolver and invoke subprocess.run against
        that exact path."""
        with _real_skill_layout(workspace_has_script=True, home_has_script=False) as (
            ws, home, expected
        ):
            bot = self._make_bot()
            update = _make_update()
            match_result = MagicMock(returncode=0, stdout="MATCH\n", stderr="")
            captured_calls = []

            def _fake_run(cmd, **_kw):
                captured_calls.append(cmd)
                return match_result

            # asyncio.to_thread runs for real here (not mocked) so the
            # underlying subprocess.run call args are genuinely exercised.
            with patch("subprocess.run", side_effect=_fake_run):
                await self._call_authsync(bot, update)

        self.assertTrue(captured_calls, "subprocess.run was never invoked")
        self.assertEqual(captured_calls[0][0], str(expected))
        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.AUTHSYNC_MATCH, calls)

    async def test_workspace_root_wins_and_old_home_only_contract_would_miss_it(self):
        """DGN-929 regression: workspace-only script must resolve. The OLD
        hardcoded HOME-only path -- the exact contract this test file used to
        assert as correct -- must NOT exist in this fixture, proving the old
        handler would have hit AUTHSYNC_SCRIPT_MISSING here (the production
        bug)."""
        with _real_skill_layout(workspace_has_script=True, home_has_script=False) as (
            ws, home, expected
        ):
            bot = self._make_bot()
            resolved = bot._resolve_skill_script("dogany-relogin-rebind", "token-sync.sh")
            self.assertEqual(resolved, expected)

            old_hardcoded_path = (
                home / ".claude" / "skills" / "dogany-relogin-rebind" / "token-sync.sh"
            )
            self.assertFalse(
                old_hardcoded_path.is_file(),
                "fixture invariant broken: the old HOME-only path must be "
                "absent in this workspace-only scenario for the regression "
                "check to be meaningful",
            )

    async def test_home_fallback_when_workspace_missing(self):
        """No script under PROJECT_ROOT/.claude/skills -- resolver falls back
        to ~/.claude/skills (legacy/global install)."""
        with _real_skill_layout(workspace_has_script=False, home_has_script=True) as (
            ws, home, expected
        ):
            bot = self._make_bot()
            resolved = bot._resolve_skill_script("dogany-relogin-rebind", "token-sync.sh")
            self.assertEqual(resolved, expected)

    async def test_workspace_wins_over_home_when_both_present(self):
        """Both roots have the script -- workspace takes precedence."""
        with _real_skill_layout(workspace_has_script=True, home_has_script=True) as (
            ws, home, expected
        ):
            bot = self._make_bot()
            resolved = bot._resolve_skill_script("dogany-relogin-rebind", "token-sync.sh")
            ws_script = ws / ".claude" / "skills" / "dogany-relogin-rebind" / "token-sync.sh"
            home_script = home / ".claude" / "skills" / "dogany-relogin-rebind" / "token-sync.sh"
            self.assertEqual(resolved, ws_script)
            self.assertNotEqual(resolved, home_script)

    async def test_genuine_missing_script_neither_root_has_it(self):
        """Real (non-mocked) missing-script case: neither workspace nor HOME
        has the script anywhere on disk."""
        with _real_skill_layout(workspace_has_script=False, home_has_script=False):
            bot = self._make_bot()
            update = _make_update()
            await self._call_authsync(bot, update)

        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.AUTHSYNC_SCRIPT_MISSING, calls)


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
