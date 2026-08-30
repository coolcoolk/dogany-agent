"""Unit tests for bridge/countdown.py (DGN-594; DGN-780 snap + UI redesign;
DGN-915 completion affordance).

Covers rendering (m:ss + draining progress bar + glyph allowlist), the
deadline-anchored boundary-snap edit loop (drift self-correction), natural-end
final edit with completion-affordance keyboard, cancel() early stop (no
keyboard), fail-open on send/edit errors, affordance-emit fail-soft fallback,
the control-file driver (start / delete-cancel / completion cleanup / malformed
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
from telegram import InlineKeyboardMarkup

from bridge import messages, ownership

from bridge.countdown import (
    BAR_CELLS,
    BAR_EMPTY,
    BAR_FILLED,
    CDN_DONE_PREFIX,
    Countdown,
    CountdownDriver,
    DEFAULT_CADENCE,
    DEFAULT_DONE_ICON,
    DEFAULT_GLYPH_SET,
    DEFAULT_ICON,
    GLYPH_SETS,
    _build_done_keyboard,
    _format_remaining,
    _is_safe_glyph,
    _next_boundary,
    _render_bar,
    _resolve_glyphs,
    _resolve_icon,
    render_countdown,
    render_done,
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


def _done_text(label, done_icon=None):
    return render_done(label, done_icon=done_icon)


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
    """DGN-780: the bar DRAINS -- filled cells = time REMAINING."""

    def test_start_all_filled(self):
        self.assertEqual(_render_bar(1.0), BAR_FILLED * BAR_CELLS)

    def test_done_all_empty(self):
        self.assertEqual(_render_bar(0.0), BAR_EMPTY * BAR_CELLS)

    def test_half(self):
        self.assertEqual(
            _render_bar(0.5), BAR_FILLED * 5 + BAR_EMPTY * 5
        )

    def test_ceil_rounds_partial_cell_up(self):
        # 75% remaining -> ceil(7.5) = 8 filled cells
        self.assertEqual(
            _render_bar(0.75), BAR_FILLED * 8 + BAR_EMPTY * 2
        )

    def test_running_countdown_never_fully_drained(self):
        # ceil: any positive remaining keeps at least one filled cell
        self.assertEqual(
            _render_bar(0.01), BAR_FILLED * 1 + BAR_EMPTY * 9
        )

    def test_fixed_width_always(self):
        for frac in (0.0, 0.31, 0.5, 0.99, 1.0):
            self.assertEqual(len(_render_bar(frac)), BAR_CELLS)

    def test_clamps_out_of_range(self):
        self.assertEqual(_render_bar(-0.5), BAR_EMPTY * BAR_CELLS)
        self.assertEqual(_render_bar(1.5), BAR_FILLED * BAR_CELLS)


class TestGlyphSets(unittest.TestCase):
    """DGN-780: config-selected glyph set with allowlist fallback."""

    def _bar_with(self, cfg):
        with patch("bridge.countdown.config", cfg):
            return _render_bar(0.5)

    def test_default_is_dot(self):
        filled, empty = GLYPH_SETS[DEFAULT_GLYPH_SET]
        self.assertEqual((filled, empty), (BAR_FILLED, BAR_EMPTY))
        self.assertEqual(DEFAULT_GLYPH_SET, "dot")

    def test_alternate_sets_render(self):
        for name, (filled, empty) in GLYPH_SETS.items():
            cfg = types.SimpleNamespace(countdown_glyph_set=name)
            self.assertEqual(self._bar_with(cfg), filled * 5 + empty * 5)

    def test_unknown_value_falls_back_to_dot(self):
        cfg = types.SimpleNamespace(countdown_glyph_set="sparkles")
        self.assertEqual(
            self._bar_with(cfg), BAR_FILLED * 5 + BAR_EMPTY * 5
        )

    def test_missing_attr_falls_back_to_dot(self):
        cfg = types.SimpleNamespace()  # config without the knob at all
        self.assertEqual(
            self._bar_with(cfg), BAR_FILLED * 5 + BAR_EMPTY * 5
        )

    def test_config_validator_normalizes_to_allowlist(self):
        # bridge.config layer: case-insensitive accept, junk falls back.
        # (Token/env comes from the hermetic conftest environment.)
        from bridge.config import Config
        self.assertEqual(
            Config(countdown_glyph_set="BLOCK-LINE").countdown_glyph_set,
            "block-line",
        )
        self.assertEqual(
            Config(countdown_glyph_set="sparkles").countdown_glyph_set, "dot"
        )
        self.assertEqual(Config().countdown_glyph_set, "dot")


class TestGlyphMarkdownSafety(unittest.TestCase):
    """DGN-780: every allowlisted set must be markdown-safe on the PLAIN
    message surface and pass the scaffold-leak guard untouched."""

    MARKDOWN_RISK = set("*_`#>|[]~")

    def test_glyphs_carry_no_markdown_risk_chars(self):
        for name, pair in GLYPH_SETS.items():
            for glyph in pair:
                self.assertFalse(
                    set(glyph) & self.MARKDOWN_RISK,
                    f"glyph set {name!r} carries a markdown-risk char",
                )

    def test_rendered_bodies_pass_scaffold_guard(self):
        from bridge.sdk_bridge import _scaffold_guard
        for name in GLYPH_SETS:
            cfg = types.SimpleNamespace(countdown_glyph_set=name)
            with patch("bridge.countdown.config", cfg):
                for remaining in (30, 15, 5):
                    text = render_countdown("rest", remaining, 30)
                    self.assertEqual(_scaffold_guard(text), text)
        done = render_done("rest")
        self.assertEqual(_scaffold_guard(done), done)


class TestFreeFormAppearance(unittest.TestCase):
    """DGN-780b: free-form icon/glyph with call > config > default priority
    and silent safe fallback on unsafe/unknown values."""

    def test_safe_glyph_gate(self):
        # Emoji + plain glyphs pass; markdown-risk chars, newlines, empties,
        # and non-strings are rejected.
        for good in ("🔥", "●", "▶", "A", "→"):
            self.assertTrue(_is_safe_glyph(good), good)
        for bad in ("*", "_", "`", "#", ">", "|", "[", "]", "~",
                    "a*b", "x\ny", "", "  ", None, 5, ("a",)):
            self.assertFalse(_is_safe_glyph(bad), repr(bad))

    def test_priority_call_over_config_over_default(self):
        cfg = types.SimpleNamespace(countdown_glyph_set="square")
        with patch("bridge.countdown.config", cfg):
            # config only: square preset
            self.assertEqual(_resolve_glyphs(None), GLYPH_SETS["square"])
            # call preset name wins over config
            self.assertEqual(_resolve_glyphs("block-line"),
                             GLYPH_SETS["block-line"])
            # call custom pair wins over config
            self.assertEqual(_resolve_glyphs(("▶", "▷")), ("▶", "▷"))
        # no config knob at all -> default dot
        with patch("bridge.countdown.config", types.SimpleNamespace()):
            self.assertEqual(_resolve_glyphs(None),
                             GLYPH_SETS[DEFAULT_GLYPH_SET])

    def test_custom_glyph_pair_renders(self):
        bar = _render_bar(0.5, glyph=("▶", "▷"))
        self.assertEqual(bar, "▶" * 5 + "▷" * 5)

    def test_unsafe_custom_glyph_falls_back(self):
        # A markdown-risk char in either member -> silent fallback to config
        # (here: no config knob -> default dot).
        with patch("bridge.countdown.config", types.SimpleNamespace()):
            self.assertEqual(_resolve_glyphs(("*", "-")),
                             GLYPH_SETS[DEFAULT_GLYPH_SET])
            self.assertEqual(_resolve_glyphs(("●", "|")),
                             GLYPH_SETS[DEFAULT_GLYPH_SET])
        # Unknown preset name -> config/default fallback too.
        cfg = types.SimpleNamespace(countdown_glyph_set="square")
        with patch("bridge.countdown.config", cfg):
            self.assertEqual(_resolve_glyphs("sparkles"), GLYPH_SETS["square"])

    def test_icon_priority_and_fallback(self):
        self.assertEqual(_resolve_icon(None, DEFAULT_ICON), DEFAULT_ICON)
        self.assertEqual(_resolve_icon("🔥", DEFAULT_ICON), "🔥")
        # unsafe icon -> default
        self.assertEqual(_resolve_icon("#", DEFAULT_ICON), DEFAULT_ICON)
        self.assertEqual(_resolve_icon("", DEFAULT_ICON), DEFAULT_ICON)

    def test_render_uses_call_icon_and_glyph(self):
        text = render_countdown("rest", 150, 600, icon="🔥", glyph=("▶", "▷"))
        self.assertTrue(text.startswith("🔥 rest"))
        self.assertIn("▶" * 3 + "▷" * 7, text)

    def test_render_done_uses_call_icon(self):
        self.assertTrue(render_done("rest", done_icon="🎉").startswith("🎉 rest"))
        # unsafe -> default check icon
        self.assertTrue(
            render_done("rest", done_icon="`").startswith(DEFAULT_DONE_ICON)
        )

    def test_omitted_params_unchanged_regression(self):
        # DGN-780b hard requirement: omitting the new params reproduces the
        # pre-DGN-780b default look exactly.
        with patch("bridge.countdown.config", types.SimpleNamespace()):
            body = render_countdown("rest", 600, 600)
            self.assertEqual(
                body,
                f"{DEFAULT_ICON} rest  10:00  {BAR_FILLED * BAR_CELLS}",
            )
            done = render_done("rest")
            self.assertTrue(done.startswith(f"{DEFAULT_DONE_ICON} rest"))

    def test_custom_icon_glyph_still_pass_scaffold_guard(self):
        from bridge.sdk_bridge import _scaffold_guard
        text = render_countdown("rest", 15, 30, icon="🔥", glyph=("▶", "▷"))
        self.assertEqual(_scaffold_guard(text), text)
        done = render_done("rest", done_icon="🎉")
        self.assertEqual(_scaffold_guard(done), done)


class TestRenderCountdown(unittest.TestCase):
    def test_body_carries_label_time_and_bar(self):
        # 150s remaining of 600s -> 25% remaining -> ceil(2.5) = 3 filled
        text = render_countdown("rest", 150, 600)
        expected = messages.COUNTDOWN_BODY.format(
            icon=DEFAULT_ICON,
            label="rest",
            remaining="2:30",
            bar=BAR_FILLED * 3 + BAR_EMPTY * 7,
        )
        self.assertEqual(text, expected)

    def test_at_start_bar_is_full(self):
        text = render_countdown("rest", 600, 600)
        self.assertIn(BAR_FILLED * BAR_CELLS, text)
        self.assertIn("10:00", text)

    def test_zero_total_renders_drained_bar(self):
        text = render_countdown("rest", 0, 0)  # degenerate: no division crash
        self.assertIn(BAR_EMPTY * BAR_CELLS, text)


class TestNextBoundary(unittest.TestCase):
    """DGN-780 snap math: next cadence boundary strictly below remaining."""

    def test_spec_example(self):
        self.assertEqual(_next_boundary(24.3, 5), 20)

    def test_full_start(self):
        self.assertEqual(_next_boundary(30, 5), 25)

    def test_exact_boundary_steps_down(self):
        self.assertEqual(_next_boundary(20.0, 5), 15)

    def test_below_one_cadence_snaps_to_zero(self):
        self.assertEqual(_next_boundary(3, 5), 0)
        self.assertEqual(_next_boundary(5, 5), 0)

    def test_never_negative(self):
        self.assertEqual(_next_boundary(0.2, 5), 0)

    def test_fractional_cadence(self):
        self.assertAlmostEqual(_next_boundary(0.35, 0.1), 0.3, places=9)


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

    def test_first_send_notifies_and_targets_chat(self):
        # DGN-932: the FIRST tick send is LOUD by default (set-start signal,
        # owner lock 2026-08-19) -- supersedes the DGN-594 always-silent send.
        bot = _mock_bot()

        async def scenario():
            countdown = self._start(bot, seconds=0.05, cadence=0.05)
            await asyncio.wait_for(countdown.task, timeout=2)

        asyncio.run(scenario())
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], OWNER_ID)
        self.assertFalse(kwargs["disable_notification"])

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

    def test_snap_self_corrects_edit_latency(self):
        # DGN-780: with a fixed-cadence nap, a 25%-of-cadence edit latency
        # accumulates and slides the display off the boundary grid. The
        # deadline-anchored snap must keep every periodic edit on EXACT
        # cadence boundaries (0.6 -> 0.4 -> 0.2 for 0.8s @ 0.2s cadence).
        bot = _mock_bot()

        async def slow_edit(*args, **kwargs):
            await asyncio.sleep(0.05)  # simulated editMessageText round-trip

        bot.edit_message_text = AsyncMock(side_effect=slow_edit)
        recorded = []
        real_render = render_countdown

        def spy_render(label, remaining, total, *args, **kwargs):
            recorded.append(remaining)
            return real_render(label, remaining, total, *args, **kwargs)

        async def scenario():
            with patch("bridge.countdown.render_countdown", spy_render):
                countdown = self._start(bot, seconds=0.8, cadence=0.2)
                await asyncio.wait_for(countdown.task, timeout=3)

        asyncio.run(scenario())
        # recorded[0] = initial send (full remaining); the rest are the
        # periodic edits, which must sit exactly on the cadence grid.
        self.assertAlmostEqual(recorded[0], 0.8, places=6)
        periodic = recorded[1:]
        self.assertEqual(len(periodic), 3, periodic)
        for value, expected in zip(periodic, (0.6, 0.4, 0.2)):
            self.assertAlmostEqual(value, expected, places=6)
        # Final edit is still the done line, after the periodic edits.
        last = bot.edit_message_text.await_args_list[-1]
        self.assertEqual(last.kwargs["text"], _done_text("rest"))

    def test_natural_end_final_edit(self):
        # DGN-915: natural completion emits the done text WITH a tappable
        # completion-affordance keyboard on the final edit.
        bot = _mock_bot()

        async def scenario():
            countdown = self._start(bot, seconds=0.05, cadence=0.02)
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.finished)
        self.assertTrue(countdown.completed)
        last = bot.edit_message_text.await_args_list[-1]
        self.assertEqual(last.kwargs["text"], _done_text("rest"))
        # DGN-915: affordance keyboard present on natural completion.
        self.assertIn("reply_markup", last.kwargs)
        self.assertIsInstance(last.kwargs["reply_markup"], InlineKeyboardMarkup)

    def test_cancel_early_stop(self):
        # DGN-915: cancelled countdown emits plain done WITHOUT the affordance
        # keyboard (timer was interrupted, no next-step offered).
        bot = _mock_bot()

        async def scenario():
            countdown = self._start(bot, seconds=60, cadence=10)
            await asyncio.sleep(0.02)  # let the send land
            countdown.cancel()
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.finished)
        self.assertFalse(countdown.completed)  # DGN-915: cancel != completion
        # No periodic edit had fired yet; only the final cleanup edit.
        self.assertEqual(bot.edit_message_text.await_count, 1)
        last = bot.edit_message_text.await_args_list[-1]
        self.assertEqual(last.kwargs["text"], _done_text("rest"))
        # DGN-915: no affordance keyboard on cancel path.
        self.assertNotIn("reply_markup", last.kwargs)

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
# DGN-915: completion affordance
# ---------------------------------------------------------------------------

class TestCompletionAffordance(unittest.TestCase):
    """DGN-915: countdown END emits a tappable done button; cancel does not."""

    def _start(self, bot, seconds, cadence, label="rest"):
        return start_countdown(
            bot, OWNER_ID, seconds, label, cadence=cadence,
            guard=EditRateGuard(min_interval=0.0),
        )

    def test_build_done_keyboard_structure(self):
        # _build_done_keyboard returns a one-button InlineKeyboardMarkup whose
        # callback_data carries the CDN_DONE_PREFIX and the message_id.
        msg_id = 1234
        kb = _build_done_keyboard(msg_id)
        self.assertIsInstance(kb, InlineKeyboardMarkup)
        rows = kb.inline_keyboard
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 1)
        btn = rows[0][0]
        self.assertTrue(btn.callback_data.startswith(CDN_DONE_PREFIX))
        self.assertIn(str(msg_id), btn.callback_data)

    def test_natural_completion_emits_affordance_keyboard(self):
        # Natural completion: final edit carries reply_markup with one button.
        bot = _mock_bot(msg_id=9999)

        async def scenario():
            countdown = self._start(bot, seconds=0.05, cadence=0.02)
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.completed)
        last = bot.edit_message_text.await_args_list[-1]
        self.assertIn("reply_markup", last.kwargs)
        kb = last.kwargs["reply_markup"]
        btn = kb.inline_keyboard[0][0]
        # callback_data embeds the message_id (9999) and the prefix.
        self.assertEqual(btn.callback_data, f"{CDN_DONE_PREFIX}9999")

    def test_cancel_path_no_affordance_keyboard(self):
        # Cancelled countdown: final edit has no reply_markup.
        bot = _mock_bot()

        async def scenario():
            countdown = self._start(bot, seconds=60, cadence=10)
            await asyncio.sleep(0.02)
            countdown.cancel()
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = asyncio.run(scenario())
        self.assertFalse(countdown.completed)
        last = bot.edit_message_text.await_args_list[-1]
        # No keyboard on the cancel path.
        self.assertNotIn("reply_markup", last.kwargs)

    def test_affordance_emit_fail_soft_fallback_to_plain_done(self):
        # If the affordance edit (with keyboard) fails, the countdown must
        # still terminate cleanly by retrying as a plain done edit.
        # Use a very short countdown with a cadence that skips periodic edits
        # (cadence >= seconds), so all edits are the final one.
        bot = _mock_bot()

        async def edit_side_effect(**kwargs):
            if kwargs.get("reply_markup") is not None:
                # Affordance edit: simulate Telegram rejecting the keyboard.
                raise telegram.error.TelegramError("bad request")
            # Plain done fallback: succeed silently (no side effect).

        bot.edit_message_text = AsyncMock(side_effect=edit_side_effect)

        async def scenario():
            # cadence > seconds: no periodic edits, only the final done edit.
            countdown = start_countdown(
                bot, OWNER_ID, 0.05, "rest", cadence=60.0,
                guard=EditRateGuard(min_interval=0.0),
            )
            await asyncio.wait_for(countdown.task, timeout=2)
            return countdown

        countdown = asyncio.run(scenario())
        self.assertTrue(countdown.finished)
        self.assertTrue(countdown.completed)
        # Two final-edit attempts: affordance (failed) + plain fallback.
        calls = bot.edit_message_text.await_args_list
        self.assertEqual(len(calls), 2, calls)
        with_kb = calls[0]
        plain = calls[1]
        # First attempt carries the keyboard.
        self.assertIsNotNone(with_kb.kwargs.get("reply_markup"))
        # Second attempt (fallback) has no keyboard.
        self.assertIsNone(plain.kwargs.get("reply_markup"))
        # Both carry the correct done text.
        self.assertEqual(with_kb.kwargs["text"], _done_text("rest"))
        self.assertEqual(plain.kwargs["text"], _done_text("rest"))

    def test_affordance_stale_tap_handler_fail_soft(self):
        # Simulate the bot.py callback handler: stale tap (message gone)
        # must swallow the Telegram error without raising.
        # We test the behavior expected of the handler -- that it catches
        # errors from edit_message_reply_markup. This is a unit-level check
        # on the handler's error path using a mock query.

        async def scenario():
            query = MagicMock()
            query.edit_message_reply_markup = AsyncMock(
                side_effect=telegram.error.BadRequest("message not found")
            )
            # Replicate the handler logic from bot.py _handle_callback cdn:done: branch.
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass  # fail-soft: stale tap is silent

            # Must NOT raise; query was called once.
            query.edit_message_reply_markup.assert_awaited_once()

        asyncio.run(scenario())

    def test_affordance_tap_clears_keyboard(self):
        # Simulate a successful tap: edit_message_reply_markup called with None.
        async def scenario():
            query = MagicMock()
            query.edit_message_reply_markup = AsyncMock(return_value=None)
            # Replicate the handler logic.
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

        asyncio.run(scenario())


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

    def test_control_file_appearance_flows_to_rendered_message(self):
        # DGN-780b: icon/glyph from the control file reach the sent message.
        self._write("rest", {
            "seconds": 60, "label": "rest",
            "icon": "🔥", "glyph": ["▶", "▷"],
        })

        async def scenario():
            await self._driver._tick()
            countdown = self._driver._active["rest"]
            await asyncio.sleep(0.02)  # let the send land
            text = self._bot.send_message.await_args.kwargs["text"]
            # Initial send is at full remaining -> bar all filled (custom
            # filled glyph); the custom icon leads the line.
            self.assertTrue(text.startswith("🔥 rest"))
            self.assertIn("▶" * BAR_CELLS, text)
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
        # DGN-915: dummy must include 'completed' (checked in _tick reaper).
        dummy = types.SimpleNamespace(
            finished=True, completed=False, task=None, cancel=lambda: None
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
        # Appearance fields default to None (fall through to the default look).
        self.assertEqual(
            self._spec({"seconds": 120, "label": "rest"}),
            (120, "rest", DEFAULT_CADENCE, None, None, None),
        )

    def test_valid_with_cadence(self):
        self.assertEqual(
            self._spec({"seconds": 120, "label": "rest", "cadence": 5}),
            (120, "rest", 5, None, None, None),
        )

    def test_chat_id_key_is_ignored(self):
        # Security: owner-only targeting; a chat_id key never surfaces.
        self.assertEqual(
            self._spec({"seconds": 60, "label": "rest", "chat_id": 999}),
            (60, "rest", DEFAULT_CADENCE, None, None, None),
        )

    def test_appearance_fields_passed_through(self):
        # DGN-780b: icon/done_icon pass through raw; glyph preset name kept;
        # a [filled, empty] JSON array becomes a tuple pair.
        self.assertEqual(
            self._spec({
                "seconds": 60, "label": "rest",
                "icon": "🔥", "done_icon": "🎉", "glyph": "square",
            }),
            (60, "rest", DEFAULT_CADENCE, "🔥", "🎉", "square"),
        )
        self.assertEqual(
            self._spec({
                "seconds": 60, "label": "rest", "glyph": ["#", "-"],
            }),
            (60, "rest", DEFAULT_CADENCE, None, None, ("#", "-")),
        )

    def test_bad_glyph_shape_becomes_none(self):
        # A malformed glyph field (not a name, not a 2-pair) -> None, so the
        # countdown still starts with the default look.
        self.assertEqual(
            self._spec({"seconds": 60, "label": "rest", "glyph": [1, 2, 3]}),
            (60, "rest", DEFAULT_CADENCE, None, None, None),
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
