"""Tests for DGN-1010: restart terminal-state backstop (layer 2).

Incident (2026-08-22 08:00): the owner tapped the DGN-994 restart CTA; the
restart succeeded but the completion push never arrived -- the detached
self_restart.sh worker was reaped together with the old bridge's launchd
process group (AbandonProcessGroup=false kills the whole group), dying
between the SIGTERM and the push. Layer 1 fixes the detach itself (double
fork + os.setsid in self_restart.sh); THIS file covers layer 2: the new
bridge's _restart_backstop_loop, which guarantees every restart tap ends in
exactly ONE terminal notification.

Covers:
  - Marker present + worker pid dead -> backstop claims the marker and sends
    RESTART_BACKSTOP_NOTICE to the owner; marker removed (terminal-closed).
  - Marker present + worker pid ALIVE -> backstop does NOT claim or push
    (the live worker still owns the terminal push); marker stays.
  - No marker (normal boot / dry-run) -> loop exits, no push.
  - Unreadable/garbled marker -> treated as an orphaned restart (fail toward
    "notify the owner", never toward silence).
  - Claim race: marker vanishes between the pid check and the rename
    (worker claimed it in the same instant) -> no push, no error.
  - Marker copy exposure: messages.RESTART_BACKSTOP_NOTICE exists and is
    non-empty in both catalogs (copy itself is owner-approval pending).
  - Layer-absence probe: self_restart.sh without the marker-arming code
    (the exact v1.40.0 regression class) -> one WARN at backstop start;
    intact script -> no WARN.

Same object.__new__ harness as test_dgn997 (no live PTB application).
"""

import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import messages
from bridge import bot as _bot
from bridge.i18n import en as _en, ko as _ko


def _make_bot():
    bot_instance = object.__new__(_bot.TelegramBot)
    bot_instance.application = MagicMock()
    bot_instance.application.bot.send_message = AsyncMock()
    return bot_instance


def _write_marker(state_dir: Path, worker_pid: int, text: str = None) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "restart-pending.marker"
    if text is None:
        text = (
            "ts=1755817242\nreason=test restart\nold_pid=11111\n"
            f"worker_pid={worker_pid}\n"
        )
    marker.write_text(text)
    return marker


class _BackstopHarness(unittest.IsolatedAsyncioTestCase):
    """Shared tmp bot_data_dir + fast loop constants."""

    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="dgn1010-")
        self.data_dir = Path(self._tmp.name)
        self.state_dir = self.data_dir / "state"
        self._patches = [
            patch.object(_bot, "RESTART_BACKSTOP_INITIAL_S", 0),
            patch.object(_bot, "RESTART_BACKSTOP_POLL_S", 0.01),
            patch.object(_bot.config, "bot_data_dir", self.data_dir),
            patch.object(_bot.config, "allowed_user_ids", [4242]),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patches:
            self.addCleanup(p.stop)


class TestBackstopFires(_BackstopHarness):

    async def test_dead_worker_marker_terminal_closed_with_owner_push(self):
        bot = _make_bot()
        # A pid that is certainly dead: fork a child and reap it.
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        marker = _write_marker(self.state_dir, worker_pid=pid)

        await bot._restart_backstop_loop()

        send = bot.application.bot.send_message
        send.assert_awaited_once()
        kwargs = send.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], 4242)
        self.assertEqual(kwargs["text"], messages.RESTART_BACKSTOP_NOTICE)
        self.assertFalse(marker.exists(), "marker must be claimed (removed)")
        # The transient .claimed.<pid> file must not linger either.
        self.assertEqual(list(self.state_dir.iterdir()), [])

    async def test_garbled_marker_still_terminal_closes(self):
        # Unreadable worker_pid -> fail toward notifying, never silence.
        bot = _make_bot()
        marker = _write_marker(self.state_dir, 0, text="not a marker at all\n")

        await bot._restart_backstop_loop()

        bot.application.bot.send_message.assert_awaited_once()
        self.assertFalse(marker.exists())


class TestBackstopHoldsOff(_BackstopHarness):

    async def test_no_marker_no_push(self):
        bot = _make_bot()
        await bot._restart_backstop_loop()
        bot.application.bot.send_message.assert_not_awaited()

    async def test_live_worker_keeps_ownership(self):
        bot = _make_bot()
        # Our own pid is definitely alive.
        marker = _write_marker(self.state_dir, worker_pid=os.getpid())

        task = asyncio.get_running_loop().create_task(
            bot._restart_backstop_loop()
        )
        await asyncio.sleep(0.15)  # several poll rounds
        self.assertFalse(task.done(), "backstop must keep waiting on a live worker")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        bot.application.bot.send_message.assert_not_awaited()
        self.assertTrue(marker.exists(), "live worker's marker must not be claimed")

    async def test_worker_claims_first_no_duplicate(self):
        # Race: marker disappears between the pid check and the rename --
        # exactly what happens when the worker claims in the same instant.
        bot = _make_bot()
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        marker = _write_marker(self.state_dir, worker_pid=pid)

        real_rename = Path.rename

        def _steal_then_rename(self_path, target):
            if self_path == marker:
                marker.unlink(missing_ok=True)  # the worker's mv wins
            return real_rename(self_path, target)

        with patch.object(Path, "rename", _steal_then_rename):
            await bot._restart_backstop_loop()

        bot.application.bot.send_message.assert_not_awaited()


class TestLayerAbsenceProbe(_BackstopHarness):
    """The 2026-08-22 incident recurred for two days because layer 1 (the
    marker writer in self_restart.sh) was reverted by an update while this
    backstop stayed -- neither layer saw the other was gone. The probe is one
    static read per boot: WARN when the sibling script no longer arms the
    marker."""

    async def test_missing_writer_warns_once(self):
        bot = _make_bot()
        fake_pkg = self.data_dir / "pkg"
        fake_pkg.mkdir()
        (fake_pkg / "self_restart.sh").write_text("#!/bin/bash\n# no marker here\n")
        with patch.object(_bot, "PACKAGE_DIR", fake_pkg):
            with self.assertLogs("bridge.bot", level="WARNING") as captured:
                await bot._restart_backstop_loop()
        self.assertTrue(
            any("layer 1 missing" in line for line in captured.output),
            captured.output,
        )

    async def test_intact_writer_no_warn(self):
        bot = _make_bot()
        fake_pkg = self.data_dir / "pkg"
        fake_pkg.mkdir()
        (fake_pkg / "self_restart.sh").write_text(
            '#!/bin/bash\nRESTART_MARKER="x/state/restart-pending.marker"\n'
        )
        with patch.object(_bot, "PACKAGE_DIR", fake_pkg):
            with self.assertNoLogs("bridge.bot", level="WARNING"):
                await bot._restart_backstop_loop()


class TestBackstopCopyExposure(unittest.TestCase):
    """Copy is owner-approval pending (미확정), but the key must exist in both
    catalogs and be exposed through messages.py so the backstop can never
    KeyError into silence."""

    def test_key_in_both_catalogs(self):
        self.assertTrue(_en.STRINGS["restart_backstop_notice"].strip())
        self.assertTrue(_ko.STRINGS["restart_backstop_notice"].strip())

    def test_messages_module_exposes_notice(self):
        self.assertTrue(messages.RESTART_BACKSTOP_NOTICE)


if __name__ == "__main__":
    unittest.main()
