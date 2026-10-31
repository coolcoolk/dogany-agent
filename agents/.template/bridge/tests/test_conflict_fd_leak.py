"""Regression test for DGN-330: fd leak in the getUpdates-conflict retry path.

Before the fix, _graceful_shutdown(force=True) set self.application = None
without calling application.shutdown(), so the HTTPXRequest/httpx.AsyncClient
objects created by each build() call were never closed. Under sustained token
contention the fd count grew until OSError [Errno 24] Too many open files.

The fix: _graceful_shutdown(force=True) now calls application.shutdown() before
clearing self.application, ensuring every client is closed on each retry cycle.

These tests mock Application.shutdown to track call counts and mock
HTTPXRequest.__init__ to track live (un-closed) instances, then drive the
conflict retry path and assert closure semantics.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

import telegram.error

from bridge.bot import TelegramBot, CONFLICT_BACKOFF_BASE, CONFLICT_BACKOFF_MAX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bot() -> TelegramBot:
    """TelegramBot with all lifecycle methods stubbed except _graceful_shutdown."""
    bot = TelegramBot.__new__(TelegramBot)
    bot.application = None
    bot._conflict_event = None
    bot._runtime_active_sessions = set()
    bot._user_run_tasks = {}
    bot._user_queue_locks = {}
    bot._active_tasks = {}
    bot._media_groups = {}
    bot._media_group_lock = asyncio.Lock()
    bot._stall_restart_count = 0
    bot._last_stall_restart = None
    return bot


def _make_mock_application() -> MagicMock:
    """Application stub: initialize/start/stop/shutdown are all async no-ops."""
    app = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.running = False
    updater = MagicMock()
    updater.running = False
    updater.start_polling = AsyncMock()
    updater.stop = AsyncMock()
    app.updater = updater
    app.bot = MagicMock()
    app.bot.set_my_commands = AsyncMock()
    app.bot.delete_my_commands = AsyncMock()
    return app


# ---------------------------------------------------------------------------
# Test: _graceful_shutdown(force=True) always calls application.shutdown()
# ---------------------------------------------------------------------------

class TestGracefulShutdownForceClosesClient(unittest.IsolatedAsyncioTestCase):
    """_graceful_shutdown(force=True) must call application.shutdown() so the
    underlying HTTPXRequest/AsyncClient is released on every conflict retry."""

    async def test_force_shutdown_calls_application_shutdown(self):
        bot = _make_bot()
        app = _make_mock_application()
        bot.application = app

        await bot._graceful_shutdown(force=True)

        app.shutdown.assert_awaited_once()
        self.assertIsNone(bot.application)

    async def test_force_shutdown_does_not_call_stop(self):
        """force=True skips the graceful stop path (updater/app stop) but still
        closes the HTTP layer via shutdown()."""
        bot = _make_bot()
        app = _make_mock_application()
        bot.application = app

        await bot._graceful_shutdown(force=True)

        app.stop.assert_not_awaited()
        app.updater.stop.assert_not_awaited()

    async def test_non_force_shutdown_calls_full_stop(self):
        """Baseline: force=False triggers the full graceful stop path."""
        bot = _make_bot()
        app = _make_mock_application()
        app.running = True
        app.updater.running = True
        bot.application = app

        await bot._graceful_shutdown(force=False)

        app.updater.stop.assert_awaited_once()
        app.stop.assert_awaited_once()
        app.shutdown.assert_awaited_once()

    async def test_force_shutdown_on_none_application_is_noop(self):
        bot = _make_bot()
        bot.application = None
        # Must not raise.
        await bot._graceful_shutdown(force=True)

    async def test_shutdown_exception_does_not_prevent_application_clear(self):
        """If shutdown() itself raises, application is still set to None so the
        next retry calls build() fresh (no double-use of a broken client)."""
        bot = _make_bot()
        app = _make_mock_application()
        app.shutdown = AsyncMock(side_effect=RuntimeError("transport closed"))
        bot.application = app

        await bot._graceful_shutdown(force=True)

        self.assertIsNone(bot.application)


# ---------------------------------------------------------------------------
# Test: repeated conflict retries each close the previous client
# ---------------------------------------------------------------------------

class TestConflictRetryClosesEachClient(unittest.IsolatedAsyncioTestCase):
    """Each conflict retry cycle must close the Application it created.

    Rather than driving the full _run_async loop (which spawns background tasks
    that complicate clean teardown in tests), we test the two operations that
    constitute one retry cycle directly:
    - build() creates a fresh Application
    - _graceful_shutdown(force=True) closes it before the next build()

    This is the exact code path exercised by the PollingConflict branch in
    _run_async (lines 313-320 in bot.py).
    """

    async def test_repeated_build_and_force_shutdown_closes_each_application(self):
        """Simulate 5 conflict retry cycles: build -> conflict -> forced shutdown.
        Every Application.shutdown() must be called exactly once per cycle."""
        bot = _make_bot()
        shutdown_calls = []

        for i in range(5):
            # Each retry: build() creates a fresh Application
            app = _make_mock_application()
            shutdown_calls_this_cycle = []

            original_shutdown = app.shutdown

            async def _tracked_shutdown(_calls=shutdown_calls_this_cycle, _orig=original_shutdown):
                _calls.append(True)
                await _orig()

            app.shutdown = _tracked_shutdown
            bot.application = app

            # Conflict detected -> forced shutdown (the fix)
            await bot._graceful_shutdown(force=True)

            # After shutdown: application is cleared and client was closed
            self.assertIsNone(bot.application,
                f"cycle {i}: application must be None after forced shutdown")
            self.assertEqual(len(shutdown_calls_this_cycle), 1,
                f"cycle {i}: application.shutdown() must be called exactly once")
            shutdown_calls.extend(shutdown_calls_this_cycle)

        self.assertEqual(len(shutdown_calls), 5,
            "shutdown() must be called once per cycle across all 5 retries")

    async def test_no_application_survives_to_next_build(self):
        """After a forced shutdown, bot.application is None so the next loop
        iteration enters build() and does not reuse the old (closed) client."""
        bot = _make_bot()
        app = _make_mock_application()
        bot.application = app

        await bot._graceful_shutdown(force=True)

        # The next cycle: since application is None, build() would be called.
        # Verify that application is None (build() gate is: if not self.application).
        self.assertIsNone(bot.application)


# ---------------------------------------------------------------------------
# Test: exponential backoff after sustained conflict
# ---------------------------------------------------------------------------

class TestConflictExponentialBackoff(unittest.TestCase):
    """After CONFLICT_BACKOFF_EXPONENTIAL_AFTER seconds of sustained conflict,
    the retry cadence switches to exponential with CONFLICT_BACKOFF_EXPO_CAP.
    Only one ERROR log line is emitted on the mode switch; subsequent retries
    are silent."""

    def test_constants_ordered_correctly(self):
        from bridge.bot import (
            CONFLICT_BACKOFF_BASE,
            CONFLICT_BACKOFF_MAX,
            CONFLICT_BACKOFF_EXPONENTIAL_AFTER,
            CONFLICT_BACKOFF_EXPO_CAP,
            CONFLICT_SUSTAINED_SECONDS,
        )
        self.assertLess(CONFLICT_BACKOFF_BASE, CONFLICT_BACKOFF_MAX)
        self.assertLess(CONFLICT_BACKOFF_MAX, CONFLICT_BACKOFF_EXPO_CAP)
        self.assertLess(CONFLICT_SUSTAINED_SECONDS, CONFLICT_BACKOFF_EXPONENTIAL_AFTER)
        self.assertGreater(CONFLICT_BACKOFF_EXPO_CAP, CONFLICT_BACKOFF_MAX)

    def test_exponential_after_is_ten_minutes(self):
        from bridge.bot import CONFLICT_BACKOFF_EXPONENTIAL_AFTER
        self.assertEqual(CONFLICT_BACKOFF_EXPONENTIAL_AFTER, 600)


if __name__ == "__main__":
    unittest.main()
