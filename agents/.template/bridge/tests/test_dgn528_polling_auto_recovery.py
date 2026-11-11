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


DGN-2147 part 2 (pytest cases below the unittest classes; a real PTB Application
over a fake Telegram transport -- wake recovery of the polling lifecycle):

DGN-2147 part 2: a Mac that slept answers after wake, with no user action.

Live (owner, 17:34): a carried MacBook slept a long time; after wake every
message failed and the in-chat /restart did not clear it. Root cause: the
getUpdates-Conflict force path (DGN-330) called Application.shutdown() on a
RUNNING application; PTB refuses that (RuntimeError, swallowed), so the old
updater kept polling while self.application was None. The next lifecycle
then Conflicted with this process's own zombie poller, forever. Nothing
noticed the wake itself either: the heartbeat runs on the monotonic clock,
which pauses in sleep.

The lifecycle runs on a REAL python-telegram-bot Application; only the HTTP
transport is a fake Telegram (no token, no network): getUpdates long-polls,
the newest poller wins and an overlapped older one gets 409 Conflict, and a
"dead" transport hangs like a half-open socket after sleep.

Promises (each case <= 1 s, fixtures only):
  P1 a wake (wall clock jumps past monotonic) resets the polling lifecycle,
     so a message sent after wake is answered.
  P2 after wake + stuck graceful stop + a Conflict, exactly ONE getUpdates
     loop polls; no loop of a torn-down lifecycle survives into the next.
  P3 the Conflict force path stops a running updater before it discards the
     application, and closes both of its HTTP clients.
  P4 a stuck graceful stop closes the getUpdates client at once and keeps the
     general client for an in-flight turn until that turn ends.
  P5 between lifecycles, any leftover getUpdates loop is cancelled.
  P6 a stop request (SIGTERM, /restart) ends a Conflict backoff at once.
  P7 WakeDetector reports a frozen gap once and ordinary passage never.
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
import functools
import json
from unittest.mock import AsyncMock, patch
import pytest
from telegram.ext import Application, MessageHandler, filters
from telegram.request import BaseRequest
from bridge import bot as bot_mod
from bridge import heartbeat


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


TOKEN = "123456:" + "A" * 35
CONFLICT_BODY = json.dumps({
    "ok": False, "error_code": 409,
    "description": "Conflict: terminated by other getUpdates request",
}).encode()


class FakeTelegram:
    """Server side: updates, sent messages, who polled, Conflicts served."""

    def __init__(self):
        self.updates = []
        self.next_id = 1
        self.sent = []
        self.polls = []
        self.polling = set()
        self.conflicts = 0
        self.conflict_next = 0
        self.requests = []

    def push(self, text):
        self.updates.append({
            "update_id": self.next_id,
            "message": {"message_id": self.next_id, "date": 0, "text": text,
                        "chat": {"id": 7, "type": "private"},
                        "from": {"id": 7, "is_bot": False, "first_name": "o"}},
        })
        self.next_id += 1

    def texts(self):
        return [t for _, t in self.sent]


class FakeRequest(BaseRequest):
    def __init__(self, srv, name):
        self.srv, self.name = srv, name
        self.dead = False
        self.closed = False
        self.evicted = False
        srv.requests.append(self)

    @property
    def read_timeout(self):
        return 1.0

    async def initialize(self):
        pass

    async def shutdown(self):
        self.closed = True

    async def do_request(self, url, method, request_data=None, **_):
        if self.dead:  # half-open socket after sleep: hangs until cancelled
            await asyncio.Event().wait()
        method_name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if method_name == "getMe":
            return self._ok({"id": 42, "is_bot": True, "first_name": "b", "username": "b_bot"})
        if method_name == "sendMessage":
            self.srv.sent.append((self.name, params.get("text")))
            return self._ok({"message_id": 900 + len(self.srv.sent), "date": 0,
                             "chat": {"id": params.get("chat_id"), "type": "private"},
                             "text": params.get("text")})
        if method_name == "getUpdates":
            return await self._get_updates(params)
        return self._ok(True)

    async def _get_updates(self, params):
        srv = self.srv
        offset = int(params.get("offset") or 0)
        srv.updates = [u for u in srv.updates if u["update_id"] >= offset]
        if float(params.get("timeout") or 0) > 0:
            srv.polls.append(self.name)
            if srv.conflict_next:
                srv.conflict_next -= 1
                srv.conflicts += 1
                return 409, CONFLICT_BODY
            for other in srv.polling:  # newest poller wins
                if other is not self:
                    other.evicted = True
            srv.polling.add(self)
            try:
                deadline = time.monotonic() + 0.03
                while not srv.updates and not self.evicted and time.monotonic() < deadline:
                    await asyncio.sleep(0.003)
            finally:
                srv.polling.discard(self)
            if self.evicted:
                self.evicted = False
                srv.conflicts += 1
                return 409, CONFLICT_BODY
        return self._ok(list(srv.updates))

    @staticmethod
    def _ok(result):
        return 200, json.dumps({"ok": True, "result": result}).encode()


async def _until(pred, timeout=0.6):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.005)
    return True


def _pollers():
    return [t for t in asyncio.all_tasks()
            if t.get_name() == bot_mod.POLLING_TASK_NAME and not t.done()]


async def _forever(*_a, **_k):
    await asyncio.Event().wait()


class _Forever:
    def __init__(self, *a, **k):
        pass

    async def run(self):
        await asyncio.Event().wait()


@pytest.fixture()
def env(monkeypatch):
    """Fast bounds, a skewable wall clock, background loops parked."""
    clock = {"skew": 0.0}
    monkeypatch.setattr(heartbeat, "WakeDetector", functools.partial(
        heartbeat.WakeDetector, wall=lambda: time.time() + clock["skew"]))
    for name, value in (("POLLING_EXIT_POLL_S", 0.005), ("GRACEFUL_STOP_TIMEOUT", 0.08),
                        ("TEARDOWN_STEP_TIMEOUT", 0.08), ("CONFLICT_BACKOFF_BASE", 0.01),
                        ("CONFLICT_BACKOFF_MAX", 0.02), ("DEFERRED_CLOSE_POLL_S", 0.005)):
        monkeypatch.setattr(bot_mod, name, value)
    monkeypatch.setattr(bot_mod, "polling_watchdog", _forever)
    monkeypatch.setattr(bot_mod, "DashboardSync", _Forever)
    monkeypatch.setattr(bot_mod, "CountdownDriver", _Forever)
    monkeypatch.setattr(bot_mod, "BridgeSessionRuntime", _Forever)
    monkeypatch.setattr(bot_mod.ext, "run_boot_hooks", lambda: None)
    monkeypatch.setattr("bridge.live_surface.maintenance_loop", _forever)
    return clock


def _make_bot(srv, block=None):
    bot = bot_mod.TelegramBot()
    n = {"i": 0}

    async def answer(update, _ctx):
        if block is not None and update.message.text == "slow":
            await block.wait()
        await update.message.reply_text("ok:" + update.message.text)

    def build():
        n["i"] += 1
        gen, poll = FakeRequest(srv, "gen%d" % n["i"]), FakeRequest(srv, "poll%d" % n["i"])
        bot._lifecycle_requests = (gen, poll)
        bot.application = (Application.builder().token(TOKEN).concurrent_updates(True)
                           .request(gen).get_updates_request(poll).build())
        bot.application.add_handler(MessageHandler(filters.TEXT, answer))

    bot.build = build
    for name in ("_on_ready", "_session_inbox_loop", "_restart_backstop_loop",
                 "_update_ledger_sweep", "_maybe_notice_token_conflict",
                 "_notify_outage_recovered"):
        setattr(bot, name, AsyncMock())
    bot._resume_pending_media = lambda: None
    return bot


async def _wake_sequence(clock):
    srv = FakeTelegram()
    bot = _make_bot(srv)
    stop = asyncio.Event()
    run = asyncio.create_task(bot._run_async(stop))
    try:
        srv.push("before")
        assert await _until(lambda: "ok:before" in srv.texts()), srv.texts()
        # The host sleeps: every socket of the live lifecycle is half-open
        # (its graceful stop hangs on them) and Telegram still holds the
        # pre-sleep long poll (the first poll after wake gets 409).
        for r in srv.requests:
            r.dead = True
        srv.conflict_next = 1
        clock["skew"] += 3600
        srv.push("after")
        answered = await _until(lambda: "ok:after" in srv.texts())
        await asyncio.sleep(0.1)
        c0, p0 = srv.conflicts, len(srv.polls)
        await asyncio.sleep(0.12)
        return srv, answered, srv.polls[p0:], srv.conflicts - c0, len(_pollers())
    finally:
        stop.set()
        await asyncio.wait({run}, timeout=0.5)
        run.cancel()


def test_p1_wake_resets_lifecycle_and_message_is_answered(env):
    # promise: P1 a message sent after wake is answered, no user action
    srv, answered, _, _, _ = asyncio.run(_wake_sequence(env))
    assert answered, srv.texts()
    assert srv.texts().count("ok:after") == 1


def test_p2_exactly_one_poller_after_wake_stuck_stop_and_conflict(env, caplog):
    # promise: P2 exactly one getUpdates loop polls after the reset
    caplog.set_level("WARNING", logger="bridge.bot")
    srv, answered, window, new_conflicts, alive = asyncio.run(_wake_sequence(env))
    assert answered, srv.texts()
    # the sequence really ran: wake seen, graceful stop stuck, a Conflict
    # served, and three lifecycles (pre-sleep, conflicted, final)
    assert "host wake detected" in caplog.text
    assert "Graceful shutdown issue" in caplog.text
    assert srv.conflicts >= 1
    assert len({r.name for r in srv.requests if r.name.startswith("poll")}) >= 3
    assert alive == 1, alive
    assert window and len(set(window)) == 1, window
    assert new_conflicts == 0, new_conflicts


def test_p3_force_path_stops_running_updater_and_closes_clients(env):
    # promise: P3 Conflict force path leaves no poller and no open client
    async def go():
        srv = FakeTelegram()
        bot = _make_bot(srv)
        bot.build()
        app = bot.application
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        assert len(_pollers()) == 1
        bot._reap_orphan_lifecycle_tasks = AsyncMock()  # the force path alone
        await bot._graceful_shutdown(force=True)
        await asyncio.sleep(0.01)
        return bot, srv, len(_pollers())
    bot, srv, alive = asyncio.run(go())
    assert alive == 0
    assert bot.application is None
    assert all(r.closed for r in srv.requests)


def test_p4_stuck_stop_keeps_general_client_for_inflight_turn(env):
    # promise: P4 getUpdates client closed now, general client after the turn
    async def go():
        srv = FakeTelegram()
        bot = _make_bot(srv)
        bot.build()
        app = bot.application
        gen, poll = bot._lifecycle_requests
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        turn = asyncio.create_task(asyncio.Event().wait())
        bot._user_run_tasks = {7: {turn}}
        poll.dead = True  # updater.stop's last getUpdates hangs: stop is stuck
        await bot._graceful_shutdown()
        await asyncio.sleep(0.03)
        during = (poll.closed, gen.closed, len(_pollers()))
        turn.cancel()
        after = await _until(lambda: gen.closed, timeout=0.3)
        return during, after
    (poll_closed, gen_closed, alive), gen_closed_after = asyncio.run(go())
    assert poll_closed and alive == 0
    assert not gen_closed
    assert gen_closed_after


def test_p5_leftover_poller_is_cancelled_between_lifecycles(env):
    # promise: P5 a getUpdates loop of a dead lifecycle is reaped
    async def go():
        bot = _make_bot(FakeTelegram())
        orphan = asyncio.create_task(asyncio.Event().wait(), name=bot_mod.POLLING_TASK_NAME)
        bystander = asyncio.create_task(asyncio.Event().wait(), name="turn")
        await asyncio.sleep(0)
        await bot._reap_orphan_lifecycle_tasks()
        result = (orphan.cancelled(), bystander.done())
        bystander.cancel()
        return result
    assert asyncio.run(go()) == (True, False)


def test_p6_stop_request_ends_conflict_backoff_at_once():
    # promise: P6 SIGTERM does not wait out a 300 s exponential backoff
    async def go():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.02, stop.set)
        t0 = time.monotonic()
        await asyncio.wait_for(
            bot_mod.TelegramBot._sleep_unless_stopped(stop, bot_mod.CONFLICT_BACKOFF_EXPO_CAP), 0.5)
        return time.monotonic() - t0
    assert asyncio.run(go()) < 0.3


def test_p7_wake_detector_reports_frozen_gap_once():
    # promise: P7 a wall-vs-monotonic jump is reported once, passage never
    t = {"w": 1000.0, "m": 50.0}
    d = heartbeat.WakeDetector(60, wall=lambda: t["w"], mono=lambda: t["m"])
    t["w"] += 300
    t["m"] += 300
    assert d.check() is None  # 5 minutes awake: ordinary passage
    t["w"] += 7200
    t["m"] += 1
    assert d.check() == pytest.approx(7199)
    t["w"] += 1
    t["m"] += 1
    assert d.check() is None  # re-anchored: one wake reports once
