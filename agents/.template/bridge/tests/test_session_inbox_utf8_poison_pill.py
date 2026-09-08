"""session-inbox poison-pill handling (UTF-8 decode failure).

Regression for the incident where routines/ticket-hygiene.sh (run under
launchd with LANG/LC_ALL unset -> C locale) byte-sliced a Korean ticket
title mid-character, writing an invalid-UTF-8 session-inbox file. Before
this fix, TelegramBot._session_inbox_loop's `path.read_text(encoding="utf-8")`
raised UnicodeDecodeError, the file was never removed, and `files[0]`
(sorted glob of *.md) kept re-selecting the SAME undecodable file every
poll tick forever -- 52+ identical "session-inbox read failed" errors in
the log and the report never delivered (also starving any healthy file
sorted after it).

The fix (bridge/bot.py _session_inbox_loop): catch UnicodeDecodeError
specifically, rename the file aside to "<name>.corrupt" (which no longer
matches the "*.md" glob, so it can never be re-picked), and log exactly
once at quarantine time instead of once per 20s poll.

Tests:
  1. test_undecodable_file_quarantined_once_not_looped -- an invalid-UTF-8
     file is quarantined to "<name>.corrupt" within the first tick, is
     never re-selected on later ticks, and logs exactly one error.
  2. test_healthy_file_behind_poison_pill_still_delivered -- a poison file
     that sorts before a healthy file no longer permanently blocks the
     healthy one (files[0] would otherwise wedge on it forever).
  3. test_healthy_file_unaffected -- baseline: a well-formed file is still
     read, injected, and unlinked exactly as before (no regression).
"""

import asyncio
import shutil
import types
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import AsyncMock, MagicMock, patch


class _StopLoop(Exception):
    """Sentinel to break the infinite poll loop after a bounded tick count."""


def _fake_self():
    """Minimal stand-in exposing only what _session_inbox_loop touches."""
    ns = types.SimpleNamespace()
    ns._proactive_push = AsyncMock()
    ns._user_turn_active = lambda _uid: False
    return ns


class TestSessionInboxPoisonPill(unittest.TestCase):
    OWNER_ID = 1

    def _run_loop(self, *, setup, max_ticks):
        """Drive the real loop body against a temp spool dir for max_ticks
        polls, then hand back the inbox dir + recorded injections/log calls.

        `setup(inbox: Path)` populates the spool dir before the loop starts.

        NOTE: uses mkdtemp (not the TemporaryDirectory contextmanager) and
        registers cleanup via addCleanup -- the caller inspects the returned
        `inbox` Path AFTER this method returns (e.g. ".corrupt" file still
        present on disk), which requires the directory to outlive this call.
        """
        from bridge import bot as bot_mod

        results = {"injected": []}

        td = mkdtemp()
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        data_dir = Path(td)
        inbox = data_dir / "session-inbox"
        inbox.mkdir()
        setup(inbox)

        fake_bridge = MagicMock()

        async def ensure_owner_stream(uid, model, chat_id, push):
            return True

        async def inject_background_turn(uid, text):
            results["injected"].append((uid, text))
            return True

        fake_bridge.ensure_owner_stream = AsyncMock(side_effect=ensure_owner_stream)
        fake_bridge.inject_background_turn = AsyncMock(side_effect=inject_background_turn)

        fake_config = MagicMock()
        fake_config.bot_data_dir = data_dir
        fake_config.allowed_user_ids = [self.OWNER_ID]

        fake_sessmgr = MagicMock()
        fake_sessmgr.get_session = AsyncMock(return_value={"model": "sonnet"})

        ticks = {"n": 0}

        async def fake_sleep(_secs):
            ticks["n"] += 1
            if ticks["n"] > max_ticks:
                raise _StopLoop

        fake_logger = MagicMock()
        fake = _fake_self()

        with patch.object(bot_mod, "config", fake_config), \
             patch.object(bot_mod, "sdk_bridge", fake_bridge), \
             patch.object(bot_mod, "session_manager", fake_sessmgr), \
             patch.object(bot_mod, "logger", fake_logger), \
             patch.object(bot_mod.asyncio, "sleep", fake_sleep):
            try:
                asyncio.run(bot_mod.TelegramBot._session_inbox_loop(fake))
            except _StopLoop:
                pass

        return inbox, results, fake_logger

    def test_undecodable_file_quarantined_once_not_looped(self):
        # Byte 0xec alone (lone lead byte of a 3-byte Hangul sequence, no
        # continuation bytes) is invalid UTF-8 -- mirrors the real incident's
        # "byte 0xec ... invalid continuation byte" decode error.
        bad_name = "ticket-hygiene-20260823-180017.md"

        def setup(inbox: Path):
            (inbox / bad_name).write_bytes(b"header text \xec more text")

        inbox, results, fake_logger = self._run_loop(setup=setup, max_ticks=4)

        # Original file gone, quarantined copy present, exactly once.
        self.assertFalse((inbox / bad_name).exists())
        quarantined = inbox / f"{bad_name}.corrupt"
        self.assertTrue(quarantined.exists())
        self.assertEqual(quarantined.read_bytes(), b"header text \xec more text")

        # Never injected (it was never valid text).
        self.assertEqual(results["injected"], [])

        # Exactly one error logged, not one per poll tick (4 ticks ran).
        self.assertEqual(fake_logger.error.call_count, 1)

        # Quarantined file must never resurface as a *.md candidate: glob
        # again by hand the way the loop does, to prove it's excluded.
        remaining_md = sorted(inbox.glob("*.md"))
        self.assertEqual(remaining_md, [])

    def test_healthy_file_behind_poison_pill_still_delivered(self):
        # Poison file sorts first alphabetically; a healthy file sorts after
        # it. Before the fix, files[0] would wedge on the poison file forever
        # and the healthy file would never be reached.
        bad_name = "aaa-poison-20260823-000000.md"
        good_name = "zzz-healthy-20260823-000000.md"

        def setup(inbox: Path):
            (inbox / bad_name).write_bytes(b"\xec broken")
            (inbox / good_name).write_text("[cron-inject] healthy report", encoding="utf-8")

        inbox, results, fake_logger = self._run_loop(setup=setup, max_ticks=3)

        self.assertTrue((inbox / f"{bad_name}.corrupt").exists())
        self.assertFalse((inbox / good_name).exists())  # consumed
        self.assertEqual(
            results["injected"], [(self.OWNER_ID, "[cron-inject] healthy report")]
        )

    def test_healthy_file_unaffected(self):
        # Baseline regression check: a normal well-formed file is still
        # read, injected, and unlinked -- the poison-pill branch never fires.
        good_name = "restart-verify-20260823-000000.md"

        def setup(inbox: Path):
            (inbox / good_name).write_text("[cron-inject] resume + verify", encoding="utf-8")

        inbox, results, fake_logger = self._run_loop(setup=setup, max_ticks=2)

        self.assertFalse((inbox / good_name).exists())
        self.assertEqual(
            results["injected"], [(self.OWNER_ID, "[cron-inject] resume + verify")]
        )
        fake_logger.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
