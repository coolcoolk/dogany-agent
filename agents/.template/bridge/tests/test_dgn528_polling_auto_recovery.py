"""DGN-528: polling auto-recovery -- zombie detection and restart signaling.

The bridge keeps the process alive after a network blip; only the polling task
may silently die.  Two-layer watchdog:

  Layer 1 (in-process): heartbeat.stalled() is checked every second in
    _wait_for_polling_exit; a stall raises PollingRestart so the run loop
    re-initializes polling without a full process exit.

  Layer 2 (external): watchdog.sh reads the heartbeat file mtime and
    kickstarts the service after two stale probes.

This test suite covers the Layer-1 path (heartbeat module + polling_watchdog
coroutine) because the Layer-2 shell script test infra is out of scope for the
Python harness.

Covered cases:
  1. heartbeat.stalled() returns False when no beat has been recorded yet.
  2. heartbeat.stalled() returns False for a fresh beat within the threshold.
  3. heartbeat.stalled() returns True after the threshold elapses.
  4. heartbeat.touch() resets the stall clock; a fresh touch is never stalled.
  5. polling_watchdog: a successful API probe resets consecutive_failures to 0.
  6. polling_watchdog: consecutive failures crossing NETWORK_FAILURE_THRESHOLD
     raise PollingRestart.
  7. on_recovery callback: NOT called on a single-probe recovery (below
     RECOVERY_NOTIFY_THRESHOLD); called on multi-probe recovery.
  8. polling_watchdog: skips the API probe when updater.running is False
     (updater already stopped; probing would be misleading).
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bridge.health import (
    NETWORK_FAILURE_THRESHOLD,
    RECOVERY_NOTIFY_THRESHOLD,
    WATCHDOG_INTERVAL,
    PollingRestart,
    polling_watchdog,
)
import bridge.heartbeat as heartbeat_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_updater(running: bool = True) -> MagicMock:
    """Build a standalone updater mock with an awaitable stop()."""
    updater = MagicMock()
    updater.running = running
    updater.stop = AsyncMock()
    return updater


def _make_application(updater_running: bool = True) -> MagicMock:
    """Build an Application-like mock.

    Assign updater as a concrete MagicMock so attribute access always returns
    the same object (MagicMock's auto-creation would give a plain MagicMock
    for every attribute, losing any AsyncMock we set).
    """
    app = MagicMock()
    app.updater = _make_updater(running=updater_running)
    app.bot = MagicMock()
    app.bot.get_me = AsyncMock()
    return app


class TestHeartbeatStalled(unittest.TestCase):
    """heartbeat.stalled() edge cases."""

    def setUp(self):
        # Reset module-level state between tests so they are independent.
        heartbeat_mod._last_beat = None

    def test_no_beat_never_stalled(self):
        """Before any touch(), stalled() returns False regardless of threshold."""
        self.assertFalse(heartbeat_mod.stalled(0))

    def test_fresh_beat_not_stalled(self):
        """A just-recorded beat is well within any reasonable threshold."""
        heartbeat_mod._last_beat = time.monotonic()
        self.assertFalse(heartbeat_mod.stalled(120))

    def test_stale_beat_triggers_stall(self):
        """A beat older than the threshold is stalled."""
        heartbeat_mod._last_beat = time.monotonic() - 200
        self.assertTrue(heartbeat_mod.stalled(120))

    def test_below_threshold_not_stalled(self):
        """A beat 50 seconds old is not stalled against a 120s threshold."""
        heartbeat_mod._last_beat = time.monotonic() - 50
        self.assertFalse(heartbeat_mod.stalled(120))


class TestHeartbeatTouch(unittest.TestCase):
    """heartbeat.touch() resets the stall clock.

    touch() writes to disk (best-effort) AND updates _last_beat in memory.
    We patch the file-write path so tests never touch the real filesystem
    (and never fail due to missing dirs), then verify only the in-memory
    _last_beat effect -- which is what stalled() reads.
    """

    def setUp(self):
        heartbeat_mod._last_beat = None
        heartbeat_mod._last_write = 0.0

    def _touch_no_disk(self):
        """Call touch() with disk I/O neutralized via patching the heartbeat file."""
        mock_path = MagicMock()
        mock_path.parent.mkdir = MagicMock()
        mock_path.name = "poll_heartbeat"
        tmp_mock = MagicMock()
        tmp_mock.write_text = MagicMock()
        mock_path.with_name.return_value = tmp_mock
        with patch("bridge.heartbeat.HEARTBEAT_FILE", mock_path), patch("os.replace"):
            heartbeat_mod.touch()

    def test_touch_clears_stall(self):
        """After touching, a previously stale clock is no longer stalled."""
        heartbeat_mod._last_beat = time.monotonic() - 300
        self.assertTrue(heartbeat_mod.stalled(120))
        self._touch_no_disk()
        self.assertFalse(heartbeat_mod.stalled(120))

    def test_touch_sets_beat_from_none(self):
        """touch() initializes _last_beat when it was None."""
        self.assertIsNone(heartbeat_mod._last_beat)
        self._touch_no_disk()
        self.assertIsNotNone(heartbeat_mod._last_beat)
        self.assertFalse(heartbeat_mod.stalled(120))


# ---------------------------------------------------------------------------
# polling_watchdog tests
#
# Strategy: patch `bridge.health.asyncio.sleep` to a no-op so the watchdog
# loop iterates without real delays, and patch WATCHDOG_INTERVAL /
# NETWORK_FAILURE_THRESHOLD / RECOVERY_NOTIFY_THRESHOLD to control
# threshold arithmetic without waiting seconds.
# ---------------------------------------------------------------------------

_FAST_SLEEP = AsyncMock()  # module-level; reset between tests


class TestPollingWatchdogProbeSuccess(unittest.TestCase):
    """Successful API probe clears the failure counter."""

    def test_success_probe_resets_failure_count(self):
        """After failures then a successful probe, on_recovery fires once."""

        async def scenario():
            app = _make_application(updater_running=True)
            stop = asyncio.Event()
            recovered_calls = []

            async def on_recovery(down_s):
                recovered_calls.append(down_s)

            call_count = 0

            async def fake_get_me():
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise Exception("network down")
                stop.set()

            app.bot.get_me = fake_get_me

            with patch("bridge.health.asyncio.sleep", AsyncMock()), patch(
                "bridge.health.WATCHDOG_INTERVAL", 60
            ), patch("bridge.health.NETWORK_FAILURE_THRESHOLD", 9999):
                try:
                    await polling_watchdog(app, stop, on_recovery=on_recovery)
                except PollingRestart:
                    self.fail("PollingRestart should not fire before threshold")

            # 2 failures >= RECOVERY_NOTIFY_THRESHOLD (2): callback fires once.
            self.assertEqual(len(recovered_calls), 1)

        asyncio.run(scenario())


class TestPollingWatchdogPollingRestart(unittest.TestCase):
    """Consecutive failures past threshold raise PollingRestart."""

    def test_threshold_breach_raises_polling_restart(self):
        """PollingRestart is raised when total_down >= NETWORK_FAILURE_THRESHOLD."""

        async def scenario():
            app = _make_application(updater_running=True)
            stop = asyncio.Event()

            app.bot.get_me = AsyncMock(side_effect=Exception("dns failure"))

            # interval=100s, threshold=150s -> fires after 2 failures (200s >= 150s).
            with patch("bridge.health.asyncio.sleep", AsyncMock()), patch(
                "bridge.health.WATCHDOG_INTERVAL", 100
            ), patch("bridge.health.NETWORK_FAILURE_THRESHOLD", 150):
                with self.assertRaises(PollingRestart):
                    await polling_watchdog(app, stop, on_recovery=None)

            # updater.stop() must have been called before raising PollingRestart.
            app.updater.stop.assert_awaited_once()

        asyncio.run(scenario())


class TestPollingWatchdogRecoveryCallback(unittest.TestCase):
    """on_recovery threshold gate: single-probe blips are not reported."""

    def test_single_failure_recovery_not_reported(self):
        """One failure (< RECOVERY_NOTIFY_THRESHOLD=2) then recovery: no callback."""

        async def scenario():
            app = _make_application(updater_running=True)
            stop = asyncio.Event()
            recovered_calls = []

            async def on_recovery(down_s):
                recovered_calls.append(down_s)

            call_count = 0

            async def fake_get_me():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise Exception("single blip")
                stop.set()

            app.bot.get_me = fake_get_me

            with patch("bridge.health.asyncio.sleep", AsyncMock()), patch(
                "bridge.health.WATCHDOG_INTERVAL", 60
            ), patch("bridge.health.NETWORK_FAILURE_THRESHOLD", 9999), patch(
                "bridge.health.RECOVERY_NOTIFY_THRESHOLD", 2
            ):
                await polling_watchdog(app, stop, on_recovery=on_recovery)

            # 1 failure < threshold of 2: callback must NOT fire.
            self.assertEqual(recovered_calls, [])

        asyncio.run(scenario())

    def test_multi_failure_recovery_reported(self):
        """3 failures (>= RECOVERY_NOTIFY_THRESHOLD=2): callback fires once."""

        async def scenario():
            app = _make_application(updater_running=True)
            stop = asyncio.Event()
            recovered_calls = []

            async def on_recovery(down_s):
                recovered_calls.append(down_s)

            call_count = 0
            n_failures = 3

            async def fake_get_me():
                nonlocal call_count
                call_count += 1
                if call_count <= n_failures:
                    raise Exception("outage")
                stop.set()

            app.bot.get_me = fake_get_me

            fake_interval = 60
            with patch("bridge.health.asyncio.sleep", AsyncMock()), patch(
                "bridge.health.WATCHDOG_INTERVAL", fake_interval
            ), patch("bridge.health.NETWORK_FAILURE_THRESHOLD", 9999), patch(
                "bridge.health.RECOVERY_NOTIFY_THRESHOLD", 2
            ):
                await polling_watchdog(app, stop, on_recovery=on_recovery)

            self.assertEqual(len(recovered_calls), 1)
            self.assertEqual(recovered_calls[0], n_failures * fake_interval)

        asyncio.run(scenario())


class TestPollingWatchdogUpdaterDown(unittest.TestCase):
    """When the updater is not running, probes are skipped."""

    def test_skips_probe_when_updater_not_running(self):
        """updater.running=False -> get_me() is never called."""

        async def scenario():
            app = _make_application(updater_running=False)
            stop = asyncio.Event()
            probe_calls = []

            async def counting_get_me():
                probe_calls.append(1)

            app.bot.get_me = counting_get_me

            # One sleep iteration, then stop.
            sleep_count = 0

            async def controlled_sleep(n):
                nonlocal sleep_count
                sleep_count += 1
                if sleep_count >= 2:
                    stop.set()

            with patch("bridge.health.asyncio.sleep", side_effect=controlled_sleep):
                await polling_watchdog(app, stop, on_recovery=None)

            self.assertEqual(probe_calls, [], "probe must not fire when updater is down")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
