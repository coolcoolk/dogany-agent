"""Tests for DGN-997: /restart -- owner-only explicit bridge restart command.

Fills the missing command-line surface: DGN-994 wired a restart CTA only
inside the /authsync sync-ok reply (a button); there was no direct /restart
slash command, so Metal ended up announcing a non-existent one to the owner
(DGN-997 incident, 2026-08-21 screenshot report).

Covers:
  - Non-owner: access denied -> restart script never invoked, no reply
    beyond the standard NO_PERMISSION handled by _check_access itself.
  - Owner: self_restart.sh invoked with --trigger user (idle-guard bypass;
    an explicit /restart IS the owner's restart command, same convention as
    the DGN-994 CTA tap) and --reason set.
  - Duplicate /restart before the process dies: single launch (latch).
  - Cross-path latch: a DGN-994 authsync CTA tap followed immediately by a
    /restart (self-grill finding) shares ONE latch (_restart_launch_started)
    -- must not double-fire two concurrent self_restart.sh workers.
  - Script missing -> RESTART_ERROR reply, no launch.
  - Launcher non-zero exit -> RESTART_ERROR reply, latch resets (retry works).
  - /help and the BotCommand menu both expose /restart (COMMAND_MENU_SPEC).

Does NOT import TelegramBot.__init__ (live PTB application not available in
the test env) -- same object.__new__ harness as test_dgn759/test_dgn994.
"""

import asyncio
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import messages
from bridge import bot as _bot
from bridge.bot import COMMAND_MENU_SPEC
from bridge.i18n import en as _en, ko as _ko


# ---------------------------------------------------------------------------
# Harness helpers
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


def _reply_calls(update):
    return update.message.reply_text.call_args_list


@contextmanager
def _package_dir(*, with_script: bool, executable: bool = True):
    """Patch bridge.bot.PACKAGE_DIR to a temp dir, optionally holding a real
    executable self_restart.sh stub."""
    with TemporaryDirectory(prefix="dgn997-pkg-") as d:
        pkg = Path(d)
        script = pkg / "self_restart.sh"
        if with_script:
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755 if executable else 0o644)
        with patch.object(_bot, "PACKAGE_DIR", pkg):
            yield pkg, script


async def _dispatch(bot, update, *, access):
    with patch.object(bot, "_check_access", new=AsyncMock(return_value=access)):
        await bot._cmd_restart(update, MagicMock())


# ---------------------------------------------------------------------------
# Access gate + launch wiring
# ---------------------------------------------------------------------------

class TestRestartCommandAccessGate(unittest.IsolatedAsyncioTestCase):

    async def test_non_owner_never_launches_script(self):
        bot = _make_bot()
        update = _make_update()
        captured = []
        with _package_dir(with_script=True), \
             patch("subprocess.run", side_effect=lambda *a, **k: captured.append(a)):
            await _dispatch(bot, update, access=False)
        self.assertEqual(captured, [], "restart script invoked for non-owner")
        # _check_access itself owns the NO_PERMISSION reply (mocked here);
        # the command body must not add any reply of its own on denial.
        update.message.reply_text.assert_not_called()


class TestRestartCommandLaunch(unittest.IsolatedAsyncioTestCase):

    async def test_owner_call_launches_with_trigger_user(self):
        bot = _make_bot()
        update = _make_update()
        captured = []

        def _fake_run(cmd, **_kw):
            captured.append(cmd)
            return MagicMock(returncode=0, stdout="detached worker\n", stderr="")

        with _package_dir(with_script=True) as (pkg, script), \
             patch("subprocess.run", side_effect=_fake_run):
            await _dispatch(bot, update, access=True)

        self.assertEqual(len(captured), 1, "script must be launched exactly once")
        cmd = captured[0]
        self.assertEqual(cmd[0], str(script))
        self.assertIn("--trigger", cmd)
        self.assertEqual(cmd[cmd.index("--trigger") + 1], "user")
        self.assertIn("--reason", cmd)
        reason = cmd[cmd.index("--reason") + 1]
        self.assertTrue(reason.strip(), "--reason must be non-empty")
        # DGN-997 rationale: no --resume-intent -- this command handler has
        # no visibility into what the live SDK session is doing.
        self.assertNotIn("--resume-intent", cmd)
        # dec-094: success path sends exactly one immediate ack (the
        # completion push itself is still owned by self_restart.sh, not
        # duplicated here).
        update.message.reply_text.assert_called_once_with(messages.RESTART_ACK)

    async def test_duplicate_command_launches_once(self):
        bot = _make_bot()
        captured = []
        updates = []

        def _fake_run(cmd, **_kw):
            captured.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with _package_dir(with_script=True), \
             patch("subprocess.run", side_effect=_fake_run):
            for _ in range(2):
                update = _make_update()
                updates.append(update)
                await _dispatch(bot, update, access=True)

        self.assertEqual(len(captured), 1, "duplicate /restart must not double-fire")
        # dec-094: ack fires once (first launch); the latch-blocked second
        # /restart returns before any reply, so no second ack either.
        updates[0].message.reply_text.assert_called_once_with(messages.RESTART_ACK)
        updates[1].message.reply_text.assert_not_called()

    async def test_missing_script_reports_error_without_launch(self):
        bot = _make_bot()
        update = _make_update()
        captured = []
        with _package_dir(with_script=False), \
             patch("subprocess.run", side_effect=lambda *a, **k: captured.append(a)):
            await _dispatch(bot, update, access=True)
        self.assertEqual(captured, [])
        replies = [c.args[0] for c in _reply_calls(update)]
        self.assertTrue(
            any("restart script" in r for r in replies),
            f"expected script-missing error reply, got {replies}",
        )
        # dec-094: failure path must never also send the success ack.
        self.assertNotIn(messages.RESTART_ACK, replies)

    async def test_launcher_failure_reports_error_and_resets_latch(self):
        bot = _make_bot()
        update = _make_update()
        calls = []

        def _fail_then_ok(cmd, **_kw):
            calls.append(cmd)
            rc = 3 if len(calls) == 1 else 0
            return MagicMock(returncode=rc, stdout="", stderr="need --reason\n")

        with _package_dir(with_script=True), \
             patch("subprocess.run", side_effect=_fail_then_ok):
            await _dispatch(bot, update, access=True)
            replies = [c.args[0] for c in _reply_calls(update)]
            self.assertTrue(
                any("need --reason" in r for r in replies),
                f"expected launcher stderr surfaced, got {replies}",
            )
            # dec-094: failure path must never also send the success ack.
            self.assertNotIn(messages.RESTART_ACK, replies)
            # Latch must have reset: a retry launches again.
            update2 = _make_update()
            await _dispatch(bot, update2, access=True)

        self.assertEqual(len(calls), 2, "failed launch must not arm the latch")
        # dec-094: the retry succeeds (rc=0) so it gets the ack.
        update2.message.reply_text.assert_called_once_with(messages.RESTART_ACK)

    async def test_authsync_cta_tap_then_restart_command_shares_latch(self):
        """Self-grill finding: the DGN-994 CTA tap and /restart must not be
        able to double-fire self_restart.sh when triggered back to back --
        they share ONE latch (_restart_launch_started)."""
        bot = _make_bot()
        captured = []

        def _fake_run(cmd, **_kw):
            captured.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        query = MagicMock()
        query.data = _bot.AUTHSYNC_RESTART_CB
        query.answer = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        query.message = MagicMock()
        query.message.reply_text = AsyncMock()

        with _package_dir(with_script=True), \
             patch("subprocess.run", side_effect=_fake_run):
            # First trigger: the authsync CTA callback.
            await bot._handle_authsync_restart_callback(query)
            # Second trigger, immediately after: /restart command.
            update = _make_update()
            await _dispatch(bot, update, access=True)

        self.assertEqual(
            len(captured), 1,
            "CTA tap followed by /restart must not launch the script twice",
        )
        # dec-094: the latch-blocked /restart returns before the ack reply,
        # so /restart's own message never gets the ack here (the CTA tap's
        # own reply path is a separate mechanism, untouched by this ticket).
        update.message.reply_text.assert_not_called()


class TestRestartErrorCopy(unittest.TestCase):
    """RESTART_ERROR is a NEW string (dec-094 owner-confirm gate) -- locked
    parallel to AUTHSYNC_ERROR in shape, restart-scoped in content."""

    def test_ko_error_key_present_and_restart_scoped(self):
        self.assertIn("restart_error", _ko.STRINGS)
        self.assertIn("{error}", _ko.STRINGS["restart_error"])

    def test_en_error_key_present_and_restart_scoped(self):
        self.assertIn("restart_error", _en.STRINGS)
        self.assertIn("{error}", _en.STRINGS["restart_error"])

    def test_messages_module_exposes_restart_error(self):
        self.assertTrue(messages.RESTART_ERROR)


class TestRestartAckCopy(unittest.TestCase):
    """RESTART_ACK is the dec-094 immediate-ack copy (owner-approved,
    2026-08-21) -- locked wording, success-path only."""

    def test_ko_ack_key_present_and_locked_wording(self):
        self.assertIn("restart_ack", _ko.STRINGS)
        self.assertEqual(_ko.STRINGS["restart_ack"], "재시작합니다. 곧 돌아옵니다.")

    def test_en_ack_key_present(self):
        self.assertIn("restart_ack", _en.STRINGS)
        self.assertTrue(_en.STRINGS["restart_ack"].strip())

    def test_messages_module_exposes_restart_ack(self):
        self.assertTrue(messages.RESTART_ACK)


# ---------------------------------------------------------------------------
# Menu / help exposure (DGN-997 requirement 4)
# ---------------------------------------------------------------------------

class TestRestartHelpExposure(unittest.TestCase):

    def test_restart_in_command_menu_spec(self):
        names = [cmd for cmd, _ in COMMAND_MENU_SPEC]
        self.assertIn("restart", names)

    def test_restart_desc_present_in_both_locales(self):
        self.assertIn("cmd_desc_restart", _ko.STRINGS)
        self.assertIn("cmd_desc_restart", _en.STRINGS)
        self.assertTrue(_ko.STRINGS["cmd_desc_restart"].strip())
        self.assertTrue(_en.STRINGS["cmd_desc_restart"].strip())

    def test_help_body_lists_restart(self):
        lines = [messages.HELP_TEXT_HEADER]
        for i, (cmd, desc_fn) in enumerate(COMMAND_MENU_SPEC, start=1):
            lines.append(f"{i}. /{cmd} - {desc_fn()}")
        lines.append("")
        lines.append(messages.HELP_TEXT_FOOTER)
        body = "\n".join(lines)
        self.assertIn("/restart", body)


if __name__ == "__main__":
    unittest.main()
