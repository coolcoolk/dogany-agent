"""DGN-399: post-restart resume must not stall until an owner message.

Two levels:
  1. Bridge unit -- ensure_owner_stream bootstraps a stream when none exists,
     wires the delivery route, and is an idempotent no-op refresh otherwise
     (no double stream).
  2. Loop integration -- _session_inbox_loop, run against a temp spool dir with
     NO owner message present, bootstraps the stream and consumes the spool file
     within 2 poll ticks (exactly once). Regression: when a stream already
     exists the file is still consumed and no second stream is created.
"""

import asyncio
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from bridge.sdk_bridge import SdkBridge, _UserStreamState


# --- Level 1: bridge ensure_owner_stream --------------------------------------


class TestEnsureOwnerStream(unittest.TestCase):
    def setUp(self):
        self.bridge = SdkBridge()

    def test_bootstrap_creates_stream_and_wires_delivery(self):
        created = _UserStreamState(client=MagicMock(), model=None)
        create = AsyncMock(return_value=created)
        push = AsyncMock()
        with patch.object(self.bridge, "_create_user_stream", create):
            ok = asyncio.run(
                self.bridge.ensure_owner_stream(7, "sonnet", 7, push)
            )
        self.assertTrue(ok)
        # Stream now exists for the owner, with the delivery route wired so the
        # injected turn's output is not dropped by _flush_proactive.
        self.assertIs(self.bridge._streams.get(7), created)
        self.assertEqual(created.last_chat_id, 7)
        self.assertIs(created.proactive_push, push)
        create.assert_awaited_once()

    def test_idempotent_refresh_no_double_stream(self):
        existing = _UserStreamState(client=MagicMock(), model="sonnet")
        # A live (non-stale) reader_task so _get_or_create_stream keeps it.
        existing.reader_task = MagicMock()
        existing.reader_task.done.return_value = False
        self.bridge._streams[7] = existing
        create = AsyncMock()  # must NOT be called
        push = AsyncMock()
        with patch.object(self.bridge, "_create_user_stream", create):
            ok = asyncio.run(
                self.bridge.ensure_owner_stream(7, "sonnet", 7, push)
            )
        self.assertTrue(ok)
        create.assert_not_awaited()
        self.assertIs(self.bridge._streams[7], existing)  # same object, no dupe
        self.assertIs(existing.proactive_push, push)      # route refreshed

    def test_error_returns_false(self):
        create = AsyncMock(side_effect=RuntimeError("connect failed"))
        with patch.object(self.bridge, "_create_user_stream", create):
            ok = asyncio.run(
                self.bridge.ensure_owner_stream(7, None, 7, AsyncMock())
            )
        self.assertFalse(ok)
        self.assertNotIn(7, self.bridge._streams)


# --- Level 2: _session_inbox_loop against a temp spool ------------------------


class _StopLoop(Exception):
    """Sentinel to break the infinite poll loop after a bounded tick count."""


def _fake_self(push):
    """Minimal stand-in exposing only what _session_inbox_loop touches."""
    ns = types.SimpleNamespace()
    ns._proactive_push = push
    ns._user_turn_active = lambda _uid: False
    return ns


class TestSessionInboxLoopBootstrap(unittest.TestCase):
    OWNER_ID = 1  # generic placeholder for template; replace with real user id

    def _run_loop(self, *, stream_exists, max_ticks=2):
        """Drive the real loop body against a temp spool dir.

        Simulates a fresh restart: a spool file is present and NO owner message
        has created a stream. asyncio.sleep is patched to count ticks and raise
        _StopLoop after max_ticks so the infinite loop terminates.
        """
        from bridge import bot as bot_mod

        results = {"created": [], "injected": [], "ensured": []}

        with TemporaryDirectory() as td:
            data_dir = Path(td)
            inbox = data_dir / "session-inbox"
            inbox.mkdir()
            spool = inbox / "restart-verify-20260717-235855.md"
            spool.write_text("[cron-inject] resume + verify", encoding="utf-8")

            # Fake bridge: models "stream missing until bootstrapped".
            fake_bridge = MagicMock()
            state = {"has_stream": stream_exists}

            async def ensure_owner_stream(uid, model, chat_id, push):
                results["ensured"].append((uid, model, chat_id))
                if not state["has_stream"]:
                    state["has_stream"] = True
                    results["created"].append(uid)
                return True

            ticks = {"n": 0}

            async def inject_background_turn(uid, text):
                # Mirrors the real contract: only injects when a stream exists.
                if not state["has_stream"]:
                    return False
                # Record the poll tick on which the spool was actually consumed.
                results["injected"].append((uid, text, ticks["n"]))
                return True

            fake_bridge.ensure_owner_stream = AsyncMock(side_effect=ensure_owner_stream)
            fake_bridge.inject_background_turn = AsyncMock(side_effect=inject_background_turn)

            fake_config = MagicMock()
            fake_config.bot_data_dir = data_dir
            fake_config.allowed_user_ids = [self.OWNER_ID]

            fake_sessmgr = MagicMock()
            fake_sessmgr.get_session = AsyncMock(return_value={"model": "sonnet"})

            async def fake_sleep(_secs):
                ticks["n"] += 1
                if ticks["n"] > max_ticks:
                    raise _StopLoop

            push = AsyncMock()
            fake = _fake_self(push)

            with patch.object(bot_mod, "config", fake_config), \
                 patch.object(bot_mod, "sdk_bridge", fake_bridge), \
                 patch.object(bot_mod, "session_manager", fake_sessmgr), \
                 patch.object(bot_mod.asyncio, "sleep", fake_sleep):
                try:
                    asyncio.run(bot_mod.TelegramBot._session_inbox_loop(fake))
                except _StopLoop:
                    pass

            return results, spool, ticks["n"]

    def test_fresh_start_bootstraps_and_consumes_spool(self):
        # Fresh restart, quiet hour: no stream, spool present, no owner message.
        results, spool, ticks = self._run_loop(stream_exists=False, max_ticks=2)
        # Bootstrap happened, delivery route passed through (chat_id == owner_id).
        self.assertEqual(results["created"], [self.OWNER_ID])
        self.assertTrue(results["ensured"])
        uid, model, chat_id = results["ensured"][0]
        self.assertEqual(uid, self.OWNER_ID)
        self.assertEqual(chat_id, self.OWNER_ID)
        self.assertEqual(model, "sonnet")
        # Spool consumed within 2 ticks, exactly once, and the injection ran.
        self.assertEqual(len(results["injected"]), 1)
        self.assertFalse(spool.exists())
        consumed_at_tick = results["injected"][0][2]
        self.assertLessEqual(consumed_at_tick, 2)

    def test_existing_stream_no_double_bootstrap_regression(self):
        # Normal path: a stream already exists (owner was chatting). The spool is
        # still consumed and NO new stream is created.
        results, spool, ticks = self._run_loop(stream_exists=True, max_ticks=2)
        self.assertEqual(results["created"], [])       # no bootstrap
        self.assertEqual(len(results["injected"]), 1)  # still injected
        self.assertFalse(spool.exists())               # spool consumed once


if __name__ == "__main__":
    unittest.main()
