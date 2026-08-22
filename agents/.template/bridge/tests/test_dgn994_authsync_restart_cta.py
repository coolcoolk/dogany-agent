"""Tests for DGN-994: /authsync sync-ok restart CTA button + callback wiring.

Covers:
  - Sync-ok branch (MISMATCH -> sync exit 0): reply carries exactly ONE inline
    button, label == messages.AUTHSYNC_RESTART_BUTTON, callback_data ==
    bot.AUTHSYNC_RESTART_CB.
  - MATCH / NOT-APPLICABLE / ERROR / SYNC_FAILED / SCRIPT_MISSING branches:
    NO reply_markup on any reply (CTA is sync-ok exclusive).
  - Button label lexicon: ko "재시작" / en "Restart" -- short token, no
    separators/descriptions (bridge.md label contract, DGN-990 lexicon).
  - _handle_callback: non-owner (access denied) -> no-op, restart script never
    invoked.
  - _handle_callback: owner tap -> self_restart.sh invoked with
    --trigger user (idle-guard bypass; owner tap = explicit restart command),
    keyboard cleared before launch.
  - Callback handler: duplicate tap inside the latch window -> single launch.
  - Callback handler: script missing -> AUTHSYNC_ERROR reply, no launch.
  - Callback handler: launcher non-zero exit -> AUTHSYNC_ERROR reply and the
    latch resets (a later tap may retry).

Does NOT import TelegramBot.__init__ (live PTB application not available in
the test env) -- same object.__new__ harness as test_dgn759.
"""

import asyncio
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import messages
from bridge import bot as _bot
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


def _make_callback_update(data: str = _bot.AUTHSYNC_RESTART_CB):
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


async def _call_authsync(bot_instance, update, *, access=True):
    with patch.object(
        bot_instance, "_check_access", new=AsyncMock(return_value=access)
    ):
        await bot_instance._cmd_authsync(update, MagicMock())


def _reply_calls(update):
    return update.message.reply_text.call_args_list


def _markup_of(call):
    if "reply_markup" in call.kwargs:
        return call.kwargs["reply_markup"]
    return None


@contextmanager
def _package_dir(*, with_script: bool, executable: bool = True):
    """Patch bridge.bot.PACKAGE_DIR to a temp dir, optionally holding a real
    executable self_restart.sh stub."""
    with tempfile.TemporaryDirectory(prefix="dgn994-pkg-") as d:
        pkg = Path(d)
        script = pkg / "self_restart.sh"
        if with_script:
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755 if executable else 0o644)
        with patch.object(_bot, "PACKAGE_DIR", pkg):
            yield pkg, script


# ---------------------------------------------------------------------------
# Command-side: which branches carry the CTA keyboard
# ---------------------------------------------------------------------------

class TestAuthsyncSyncOkButton(unittest.IsolatedAsyncioTestCase):

    async def _run_branches(self, results):
        """Run _cmd_authsync with asyncio.to_thread yielding `results` in
        order; script resolution stubbed present."""
        bot = _make_bot()
        update = _make_update()
        with patch("pathlib.Path.is_file", return_value=True), \
             patch("asyncio.to_thread", new=AsyncMock(side_effect=results)):
            await _call_authsync(bot, update)
        return update

    async def test_sync_ok_has_exactly_one_restart_button(self):
        mismatch = MagicMock(returncode=1, stdout="MISMATCH\n", stderr="")
        sync_ok = MagicMock(returncode=0, stdout="MATCH\n", stderr="")
        update = await self._run_branches([mismatch, sync_ok])

        ok_calls = [
            c for c in _reply_calls(update)
            if c.args and c.args[0] == messages.AUTHSYNC_SYNC_OK
        ]
        self.assertEqual(len(ok_calls), 1, "sync-ok reply not sent exactly once")
        markup = _markup_of(ok_calls[0])
        self.assertIsNotNone(markup, "sync-ok reply carries no inline keyboard")
        rows = markup.inline_keyboard
        buttons = [b for row in rows for b in row]
        self.assertEqual(len(buttons), 1, "CTA must be exactly one button")
        self.assertEqual(buttons[0].text, messages.AUTHSYNC_RESTART_BUTTON)
        self.assertEqual(buttons[0].callback_data, _bot.AUTHSYNC_RESTART_CB)

    async def _assert_no_markup_anywhere(self, update):
        for c in _reply_calls(update):
            self.assertIsNone(
                _markup_of(c),
                f"unexpected reply_markup on reply {c.args[:1]}",
            )

    async def test_match_branch_has_no_button(self):
        match = MagicMock(returncode=0, stdout="MATCH\n", stderr="")
        update = await self._run_branches([match])
        calls = [c.args[0] for c in _reply_calls(update)]
        self.assertIn(messages.AUTHSYNC_MATCH, calls)
        await self._assert_no_markup_anywhere(update)

    async def test_not_applicable_branch_has_no_button(self):
        na = MagicMock(returncode=3, stdout="NOT-APPLICABLE\n", stderr="")
        update = await self._run_branches([na])
        calls = [c.args[0] for c in _reply_calls(update)]
        self.assertIn(messages.AUTHSYNC_NOT_APPLICABLE, calls)
        await self._assert_no_markup_anywhere(update)

    async def test_error_branch_has_no_button(self):
        err = MagicMock(returncode=2, stdout="", stderr="ERROR: boom\n")
        update = await self._run_branches([err])
        await self._assert_no_markup_anywhere(update)

    async def test_sync_failed_branch_has_no_button(self):
        mismatch = MagicMock(returncode=1, stdout="MISMATCH\n", stderr="")
        fail = MagicMock(returncode=1, stdout="", stderr="sync failed\n")
        update = await self._run_branches([mismatch, fail])
        calls = [c.args[0] for c in _reply_calls(update)]
        self.assertIn(messages.AUTHSYNC_SYNC_FAILED, calls)
        await self._assert_no_markup_anywhere(update)

    async def test_script_missing_branch_has_no_button(self):
        bot = _make_bot()
        update = _make_update()
        with patch("pathlib.Path.is_file", return_value=False):
            await _call_authsync(bot, update)
        calls = [c.args[0] for c in _reply_calls(update)]
        self.assertIn(messages.AUTHSYNC_SCRIPT_MISSING, calls)
        await self._assert_no_markup_anywhere(update)


class TestRestartButtonLabel(unittest.TestCase):
    """DGN-990 lexicon + bridge.md short-token label contract."""

    def test_ko_label_is_restart_lexicon(self):
        self.assertEqual(_ko.STRINGS["authsync_restart_button"], "재시작")

    def test_en_label_is_short_token(self):
        self.assertEqual(_en.STRINGS["authsync_restart_button"], "Restart")

    def test_label_has_no_separators_or_description(self):
        for label in (
            _ko.STRINGS["authsync_restart_button"],
            _en.STRINGS["authsync_restart_button"],
        ):
            for forbidden in ("--", " - ", "—", ":", "(", ")"):
                self.assertNotIn(forbidden, label)
            self.assertLessEqual(len(label), 31)

    def test_ko_label_never_uses_internal_lexicon(self):
        # DGN-990: user-facing must never say the internal term.
        self.assertNotIn("재기동", _ko.STRINGS["authsync_restart_button"])


# ---------------------------------------------------------------------------
# Callback-side: access gate + restart launch wiring
# ---------------------------------------------------------------------------

class TestRestartCallback(unittest.IsolatedAsyncioTestCase):

    async def _dispatch(self, bot, update, *, access):
        with patch.object(
            bot, "_check_access", new=AsyncMock(return_value=access)
        ):
            await bot._handle_callback(update, MagicMock())

    async def test_non_owner_callback_is_noop(self):
        bot = _make_bot()
        update, query = _make_callback_update()
        captured = []
        with _package_dir(with_script=True), \
             patch("subprocess.run", side_effect=lambda *a, **k: captured.append(a)):
            await self._dispatch(bot, update, access=False)
        self.assertEqual(captured, [], "restart script invoked for non-owner")
        query.answer.assert_not_called()
        query.message.reply_text.assert_not_called()
        query.edit_message_reply_markup.assert_not_called()

    async def test_owner_callback_launches_with_trigger_user(self):
        bot = _make_bot()
        update, query = _make_callback_update()
        captured = []

        def _fake_run(cmd, **_kw):
            captured.append(cmd)
            return MagicMock(returncode=0, stdout="detached worker\n", stderr="")

        with _package_dir(with_script=True) as (pkg, script), \
             patch("subprocess.run", side_effect=_fake_run):
            await self._dispatch(bot, update, access=True)

        self.assertEqual(len(captured), 1, "script must be launched exactly once")
        cmd = captured[0]
        self.assertEqual(cmd[0], str(script))
        self.assertIn("--trigger", cmd)
        self.assertEqual(cmd[cmd.index("--trigger") + 1], "user")
        self.assertIn("--reason", cmd)
        # Keyboard cleared so the CTA cannot be re-tapped from the old message.
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        # Success path invents no new copy: no reply_text on success.
        query.message.reply_text.assert_not_called()

    async def test_duplicate_tap_launches_once(self):
        bot = _make_bot()
        captured = []

        def _fake_run(cmd, **_kw):
            captured.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with _package_dir(with_script=True), \
             patch("subprocess.run", side_effect=_fake_run):
            for _ in range(2):
                update, query = _make_callback_update()
                await self._dispatch(bot, update, access=True)

        self.assertEqual(len(captured), 1, "duplicate tap must not double-fire")

    async def test_missing_script_reports_error_without_launch(self):
        bot = _make_bot()
        update, query = _make_callback_update()
        captured = []
        with _package_dir(with_script=False), \
             patch("subprocess.run", side_effect=lambda *a, **k: captured.append(a)):
            await self._dispatch(bot, update, access=True)
        self.assertEqual(captured, [])
        replies = [c.args[0] for c in query.message.reply_text.call_args_list]
        self.assertTrue(
            any("restart script" in r for r in replies),
            f"expected script-missing error reply, got {replies}",
        )

    async def test_launcher_failure_reports_error_and_resets_latch(self):
        bot = _make_bot()
        update, query = _make_callback_update()

        calls = []

        def _fail_then_ok(cmd, **_kw):
            calls.append(cmd)
            rc = 3 if len(calls) == 1 else 0
            return MagicMock(returncode=rc, stdout="", stderr="need --reason\n")

        with _package_dir(with_script=True), \
             patch("subprocess.run", side_effect=_fail_then_ok):
            await self._dispatch(bot, update, access=True)
            replies = [c.args[0] for c in query.message.reply_text.call_args_list]
            self.assertTrue(
                any("need --reason" in r for r in replies),
                f"expected launcher stderr surfaced, got {replies}",
            )
            # Latch must have reset: a retry tap launches again.
            update2, _q2 = _make_callback_update()
            await self._dispatch(bot, update2, access=True)

        self.assertEqual(len(calls), 2, "failed launch must not arm the latch")


if __name__ == "__main__":
    unittest.main()
