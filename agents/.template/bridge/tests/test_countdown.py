"""Unit tests for bridge/countdown.py (DGN-594).

Covers rendering (m:ss + progress bar), the cadence edit loop, natural-end
final edit, cancel() early stop, fail-open on send/edit errors, the
control-file driver (start / delete-cancel / completion cleanup / malformed
skip), and owner-chat targeting (file chat_id ignored).
No live Telegram, token, or network. Loop tests use sub-second durations
with a zero-interval guard so the suite stays fast.
"""

import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import telegram.error

from bridge import messages, ownership
from bridge.countdown import (
    BAR_CELLS,
    BAR_EMPTY,
    BAR_FILLED,
    Countdown,
    CountdownDriver,
    DEFAULT_CADENCE,
    _format_remaining,
    _render_bar,
    render_countdown,
    start_countdown,
)
from bridge.edit_guard import EditRateGuard

OWNER_ID = 42


def _mock_bot(msg_id=5555):
    bot = MagicMock()
    fake_msg = MagicMock()
    fake_msg.message_id = msg_id
    bot.send_message = AsyncMock(return_value=fake_msg)
    bot.edit_message_text = AsyncMock()
    return bot


def _done_text(label):
    return messages.COUNTDOWN_DONE.format(label=label)


def _fake_config(tmpdir):
    return types.SimpleNamespace(
        allowed_user_ids=[OWNER_ID], bot_data_dir=Path(tmpdir)
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestFormatRemaining(unittest.TestCase):
    def test_minutes_and_seconds(self):
        self.assertEqual(_format_remaining(150), "2:30")

    def test_seconds_zero_padded(self):
        self.assertEqual(_format_remaining(61), "1:01")

    def test_under_a_minute(self):
        self.assertEqual(_format_remaining(45), "0:45")

    def test_zero(self):
        self.assertEqual(_format_remaining(0), "0:00")

    def test_fractional_rounds_up(self):
        # ceil: a running countdown never shows 0:00
        self.assertEqual(_format_remaining(149.2), "2:30")
        self.assertEqual(_format_remaining(0.4), "0:01")

    def test_negative_clamps_to_zero(self):
        self.assertEqual(_format_remaining(-3), "0:00")


class TestRenderBar(unittest.TestCase):
    def test_start_all_empty(self):
        self.assertEqual(_render_bar(0.0), BAR_EMPTY * BAR_CELLS)

    def test_full_elapsed(self):
        self.assertEqual(_render_bar(1.0), BAR_FILLED * BAR_CELLS)

    def test_half(self):
        self.assertEqual(
            _render_bar(0.5), BAR_FILLED * 5 + BAR_EMPTY * 5
        )

    def test_proportional(self):
        self.assertEqual(
            _render_bar(0.75), BAR_FILLED * 7 + BAR_EMPTY * 3
        )

    def test_fixed_width_always(self):
        for frac in (0.0, 0.31, 0.5, 0.99, 1.0):
            self.assertEqual(len(_render_bar(frac)), BAR_CELLS)

    def test_clamps_out_of_range(self):
        self.assertEqual(_render_bar(-0.5), BAR_EMPTY * BAR_CELLS)
        self.assertEqual(_render_bar(1.5), BAR_FILLED * BAR_CELLS)


class TestRenderCountdown(unittest.TestCase):
    def test_body_carries_label_time_and_bar(self):
        # 150s remaining of 600s -> 75% elapsed -> 7 filled cells
        text = render_countdown("rest", 150, 600)
        expected = messages.COUNTDOWN_BODY.format(
            label="rest",
            remaining="2:30",
            bar=BAR_FILLED * 7 + BAR_EMPTY * 3,
        )
        self.assertEqual(text, expected)

    def test_at_start_bar_is_empty(self):
        text = render_countdown("rest", 600, 600)
        self.assertIn(BAR_EMPTY * BAR_CELLS, text)
        self.assertIn("10:00", text)

    def test_zero_total_renders_full_bar(self):
        text = render_countdown("rest", 0, 0)  # degenerate: no division crash
        self.assertIn(BAR_FILLED * BAR_CELLS, text)


# ---------------------------------------------------------------------------
# Countdown loop
# ---------------------------------------------------------------------------

class TestCountdownLoop(unittest.TestCase):
    def _start(self, bot, seconds, cadence, label="rest"):
        # Zero-interval guard so sub-second test cadences are not gated by
        # the production 3s minimum.
        return start_countdown(
            bot, OWNER_ID, seconds, label, cadence=cadence,
            guard=EditRateGuard(min_interval=0.0),
        )

    def test_send_is_silent_and_targets_chat(self):
        bot = _mock_bot()

        async def scenario():
            countdown = self._start(bot, seconds=0.05, cadence=0.05)
            await asyncio.wait_for(countdown.task, timeout=2)

        asyncio.run(scenario())
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], OWNER_ID)
        self.assertTrue(kwargs["disable_notification"])

    def test_cadence_edits_advance(self):
        bot = _mock_bot()

        async def scenario():
            countdown = self._start(bot, seconds=0.35, cadence=0.1)
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.finished)
        # At least two periodic edits plus the final done edit.
        self.assertGreaterEqual(bot.edit_message_text.await_count, 3)
        calls = bot.edit_message_text.await_args_list
        for call in calls[:-1]:  # every periodic edit is a body line
            text = call.kwargs["text"]
            self.assertIn("rest", text)
            self.assertRegex(text, r"\d+:\d{2}")  # m:ss remaining
            self.assertTrue(BAR_FILLED in text or BAR_EMPTY in text)
            self.assertNotEqual(text, _done_text("rest"))
            self.assertEqual(call.kwargs["message_id"], 5555)

    def test_natural_end_final_edit(self):
        bot = _mock_bot()

        async def scenario():
            countdown = self._start(bot, seconds=0.05, cadence=0.02)
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.finished)
        last = bot.edit_message_text.await_args_list[-1]
        self.assertEqual(last.kwargs["text"], _done_text("rest"))

    def test_cancel_early_stop(self):
        bot = _mock_bot()

        async def scenario():
            countdown = self._start(bot, seconds=60, cadence=10)
            await asyncio.sleep(0.02)  # let the send land
            countdown.cancel()
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.finished)
        # No periodic edit had fired yet; only the final cleanup edit.
        self.assertEqual(bot.edit_message_text.await_count, 1)
        last = bot.edit_message_text.await_args_list[-1]
        self.assertEqual(last.kwargs["text"], _done_text("rest"))

    def test_edit_error_fails_open(self):
        bot = _mock_bot()
        bot.edit_message_text = AsyncMock(
            side_effect=telegram.error.TelegramError("internal error")
        )

        async def scenario():
            countdown = self._start(bot, seconds=0.3, cadence=0.05)
            await asyncio.wait_for(countdown.task, timeout=2)  # must not raise
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.finished)
        # First failing periodic edit ends the countdown quietly: no final
        # edit attempt, no retry storm.
        self.assertEqual(bot.edit_message_text.await_count, 1)

    def test_send_error_fails_open(self):
        bot = _mock_bot()
        bot.send_message = AsyncMock(
            side_effect=telegram.error.Forbidden("bot was blocked")
        )

        async def scenario():
            countdown = self._start(bot, seconds=0.3, cadence=0.05)
            await asyncio.wait_for(countdown.task, timeout=2)  # must not raise
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.finished)
        bot.edit_message_text.assert_not_awaited()

    def test_flood_backoff_skips_tick(self):
        bot = _mock_bot()

        async def scenario():
            guard = EditRateGuard(min_interval=0.0)
            guard.note_retry_after(999.0)  # flood active for the whole test
            countdown = start_countdown(
                bot, OWNER_ID, 0.2, "rest", cadence=0.05, guard=guard
            )
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.finished)
        # Periodic edits all skipped (guard not ready); only the final edit
        # is attempted (deliberately unconditional) and returns FLOOD.
        self.assertEqual(bot.edit_message_text.await_count, 1)


# ---------------------------------------------------------------------------
# Control-file driver
# ---------------------------------------------------------------------------

class TestDriver(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="countdown-drv-")
        self._data_dir = Path(self._td.name)
        self._dir = self._data_dir / "countdown"
        self._bot = _mock_bot()
        self._driver = CountdownDriver(self._bot, control_dir=self._dir)

    def tearDown(self):
        self._td.cleanup()

    def _write(self, cid, payload):
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{cid}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _patches(self, mode=ownership.MODE_AUTHORITATIVE, owner_id=None):
        cfg = _fake_config(self._data_dir)
        return (
            patch("bridge.countdown.ownership.resolve_owner",
                  return_value=(mode, owner_id)),
            patch("bridge.countdown.config", cfg),
        )

    def _run(self, coro, mode=ownership.MODE_AUTHORITATIVE, owner_id=None):
        p1, p2 = self._patches(mode=mode, owner_id=owner_id)
        with p1, p2:
            return asyncio.run(coro)

    async def _teardown_countdown(self, countdown):
        if countdown.task is not None and not countdown.task.done():
            countdown.task.cancel()
            try:
                await countdown.task
            except asyncio.CancelledError:
                pass

    def test_absent_dir_is_dormant(self):
        self._run(self._driver._tick())  # no dir; must not raise
        self.assertEqual(self._driver._active, {})

    def test_file_starts_countdown_targeting_owner(self):
        # chat_id in the control file MUST be ignored: owner chat only.
        self._write("rest", {"seconds": 60, "label": "rest", "chat_id": 999})

        async def scenario():
            await self._driver._tick()
            self.assertIn("rest", self._driver._active)
            countdown = self._driver._active["rest"]
            await asyncio.sleep(0.02)  # let the send land
            self.assertEqual(countdown._chat_id, OWNER_ID)
            kwargs = self._bot.send_message.await_args.kwargs
            self.assertEqual(kwargs["chat_id"], OWNER_ID)
            self.assertNotEqual(kwargs["chat_id"], 999)
            await self._teardown_countdown(countdown)

        self._run(scenario())

    def test_second_tick_does_not_duplicate(self):
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await self._driver._tick()
            countdown = self._driver._active["rest"]
            await self._driver._tick()
            self.assertIs(self._driver._active["rest"], countdown)
            await self._teardown_countdown(countdown)

        self._run(scenario())

    def test_cadence_defaults_when_absent(self):
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await self._driver._tick()
            countdown = self._driver._active["rest"]
            self.assertEqual(countdown._cadence, float(DEFAULT_CADENCE))
            await self._teardown_countdown(countdown)

        self._run(scenario())

    def test_delete_file_cancels_countdown(self):
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await self._driver._tick()
            countdown = self._driver._active["rest"]
            await asyncio.sleep(0.02)  # send lands, loop parked on cadence
            (self._dir / "rest.json").unlink()
            await self._driver._tick()
            self.assertNotIn("rest", self._driver._active)
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = self._run(scenario())
        self.assertTrue(countdown.finished)
        last = self._bot.edit_message_text.await_args_list[-1]
        self.assertEqual(last.kwargs["text"], _done_text("rest"))

    def test_finished_countdown_reaped_and_file_removed(self):
        self._write("done1", {"seconds": 60, "label": "done1"})
        dummy = types.SimpleNamespace(
            finished=True, task=None, cancel=lambda: None
        )
        self._driver._active["done1"] = dummy
        self._run(self._driver._tick())
        self.assertNotIn("done1", self._driver._active)
        self.assertFalse((self._dir / "done1.json").exists())

    def test_malformed_file_skipped_and_kept(self):
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "bad.json").write_text("{not json", encoding="utf-8")
        self._run(self._driver._tick())
        self.assertEqual(self._driver._active, {})
        # Kept on disk: a partial write must get a second chance.
        self.assertTrue((self._dir / "bad.json").exists())

    def test_no_owner_stays_dormant_file_kept(self):
        self._write("rest", {"seconds": 60, "label": "rest"})
        self._run(self._driver._tick(), mode=ownership.MODE_CLAIM)
        self.assertEqual(self._driver._active, {})
        self.assertTrue((self._dir / "rest.json").exists())

    def test_owner_lock_mode_targets_lock_owner(self):
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await self._driver._tick()
            countdown = self._driver._active["rest"]
            self.assertEqual(countdown._chat_id, 77)
            await self._teardown_countdown(countdown)

        self._run(scenario(), mode=ownership.MODE_OWNER_LOCK, owner_id=77)


class TestReadSpec(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="countdown-spec-")
        self._dir = Path(self._td.name)
        self._driver = CountdownDriver(_mock_bot(), control_dir=self._dir)

    def tearDown(self):
        self._td.cleanup()

    def _spec(self, payload, raw=None):
        path = self._dir / "x.json"
        path.write_text(
            raw if raw is not None else json.dumps(payload), encoding="utf-8"
        )
        return self._driver._read_spec(path)

    def test_valid_minimal(self):
        self.assertEqual(
            self._spec({"seconds": 120, "label": "rest"}),
            (120, "rest", DEFAULT_CADENCE),
        )

    def test_valid_with_cadence(self):
        self.assertEqual(
            self._spec({"seconds": 120, "label": "rest", "cadence": 5}),
            (120, "rest", 5),
        )

    def test_chat_id_key_is_ignored(self):
        # Security: owner-only targeting; a chat_id key never surfaces.
        self.assertEqual(
            self._spec({"seconds": 60, "label": "rest", "chat_id": 999}),
            (60, "rest", DEFAULT_CADENCE),
        )

    def test_rejects_missing_seconds(self):
        self.assertIsNone(self._spec({"label": "rest"}))

    def test_rejects_bool_seconds(self):
        # bool is an int subclass; type() check must reject it.
        self.assertIsNone(self._spec({"seconds": True, "label": "rest"}))

    def test_rejects_zero_and_negative_seconds(self):
        self.assertIsNone(self._spec({"seconds": 0, "label": "rest"}))
        self.assertIsNone(self._spec({"seconds": -5, "label": "rest"}))

    def test_rejects_oversized_seconds(self):
        self.assertIsNone(
            self._spec({"seconds": 24 * 3600 + 1, "label": "rest"})
        )

    def test_rejects_blank_label(self):
        self.assertIsNone(self._spec({"seconds": 60, "label": "   "}))

    def test_rejects_non_string_label(self):
        self.assertIsNone(self._spec({"seconds": 60, "label": 5}))

    def test_rejects_bad_cadence(self):
        self.assertIsNone(
            self._spec({"seconds": 60, "label": "rest", "cadence": 0})
        )
        self.assertIsNone(
            self._spec({"seconds": 60, "label": "rest", "cadence": "10"})
        )

    def test_rejects_non_dict(self):
        self.assertIsNone(self._spec(None, raw="[1, 2]"))

    def test_rejects_broken_json(self):
        self.assertIsNone(self._spec(None, raw="{broken"))


if __name__ == "__main__":
    unittest.main()
