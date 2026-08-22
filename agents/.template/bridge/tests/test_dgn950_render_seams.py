"""Regression tests for DGN-950 -- Warg session bridge render seams.

SEAM 1: hangul display-name byte cut. dashboard._tail_cut used a raw UTF-16
byte slice (encode -> [:N*2] -> decode ignore) that could land inside a
surrogate pair, corrupting the display name sitting on the cut boundary.
The fix slices at codepoint boundaries against the UTF-16 unit budget, so a
multibyte char (3-byte hangul, emoji surrogate pair) is either kept whole or
dropped whole -- never split, never a U+FFFD / lone surrogate.

SEAM 2: queue-before-timer race. CountdownDriver._tick started countdowns on
the 3s poll with no turn_active() deferral, so a rest-timer send could land
BEFORE the in-flight model reply (order inversion). DashboardSync already
defers on turn_active(); the driver now mirrors that: hold while the owner's
turn is in flight, flush (start) on the first tick after it ends.

No live Telegram, token, or network.
"""

import asyncio
import json
import tempfile
import types
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bridge import ownership
from bridge.countdown import CountdownDriver
from bridge.dashboard import DashboardSync, MAX_UTF16_UNITS, _tail_cut, _utf16_len

OWNER_ID = 42

# "baek-seu-kwo-teu" (back squat), the reported artifact name; each syllable
# is 3 bytes in UTF-8 and 1 UTF-16 unit (BMP). ASCII-escaped per repo rule.
HANGUL_NAME = "\ubc31\uc2a4\ucffc\ud2b8"
EMOJI = "\U0001f4aa"  # non-BMP -> UTF-16 surrogate pair (2 units)


def _no_broken_codepoints(text: str) -> bool:
    """True when text has no U+FFFD and no lone UTF-16 surrogate."""
    if "\ufffd" in text:
        return False
    # A lone surrogate cannot survive a strict utf-16 round trip.
    try:
        text.encode("utf-16-le").decode("utf-16-le")
    except UnicodeError:
        return False
    return not any(0xD800 <= ord(c) <= 0xDFFF for c in text)


# ---------------------------------------------------------------------------
# SEAM 1 -- codepoint-safe tail cut
# ---------------------------------------------------------------------------

class TestSeam1HangulBoundaryCut(unittest.TestCase):
    def test_under_limit_passthrough(self):
        text = "x" * 100 + HANGUL_NAME
        self.assertEqual(_tail_cut(text), text)

    def test_exact_limit_passthrough(self):
        text = "x" * (MAX_UTF16_UNITS - len(HANGUL_NAME)) + HANGUL_NAME
        self.assertEqual(_utf16_len(text), MAX_UTF16_UNITS)
        self.assertEqual(_tail_cut(text), text)

    def test_hangul_name_crossing_boundary_stays_whole(self):
        # The 4-char hangul name straddles the unit budget: 2 chars fit,
        # 2 cross. Every char that survives must be whole (no U+FFFD).
        filler = "x" * (MAX_UTF16_UNITS - 2)
        text = filler + HANGUL_NAME
        result = _tail_cut(text)
        self.assertTrue(text.startswith(result))  # prefix at codepoint boundary
        self.assertLessEqual(_utf16_len(result), MAX_UTF16_UNITS)
        self.assertTrue(_no_broken_codepoints(result))
        self.assertEqual(result, filler + HANGUL_NAME[:2])

    def test_hangul_every_offset_no_fffd(self):
        # Slide the name across the boundary through every offset a real
        # dashboard body could produce; no offset may yield a broken char.
        for pad in range(len(HANGUL_NAME) + 2):
            body = "x" * (MAX_UTF16_UNITS - pad) + HANGUL_NAME * 2
            result = _tail_cut(body)
            self.assertTrue(body.startswith(result))
            self.assertLessEqual(_utf16_len(result), MAX_UTF16_UNITS)
            self.assertTrue(_no_broken_codepoints(result), f"pad={pad}")

    def test_surrogate_pair_on_boundary_never_split(self):
        # The emoji needs 2 units but only 1 remains in the budget: it must
        # drop WHOLE (old byte slice kept its lone high surrogate).
        text = "x" * (MAX_UTF16_UNITS - 1) + EMOJI + HANGUL_NAME
        result = _tail_cut(text)
        self.assertEqual(result, "x" * (MAX_UTF16_UNITS - 1))
        self.assertTrue(_no_broken_codepoints(result))

    def test_surrogate_pair_fitting_is_kept_whole(self):
        text = "x" * (MAX_UTF16_UNITS - 2) + EMOJI + HANGUL_NAME
        result = _tail_cut(text)
        self.assertEqual(result, "x" * (MAX_UTF16_UNITS - 2) + EMOJI)
        self.assertLessEqual(_utf16_len(result), MAX_UTF16_UNITS)
        self.assertTrue(_no_broken_codepoints(result))

    def test_emoji_heavy_body_budget_respected(self):
        # All-astral body: every char is 2 units; the cut must respect the
        # UNIT budget (not the char count) and stay pair-safe.
        text = EMOJI * MAX_UTF16_UNITS  # 2x over budget in units
        result = _tail_cut(text)
        self.assertLessEqual(_utf16_len(result), MAX_UTF16_UNITS)
        self.assertTrue(_no_broken_codepoints(result))
        self.assertEqual(result, EMOJI * (MAX_UTF16_UNITS // 2))

    def test_combining_sequence_no_broken_codepoint(self):
        # NFD hangul (conjoining jamo) around the boundary: codepoint-level
        # cut may split the grapheme (accepted contract) but must never
        # produce U+FFFD or a lone surrogate.
        nfd = unicodedata.normalize("NFD", "\ud55c")  # conjoining-jamo form
        for pad in range(4):
            body = "x" * (MAX_UTF16_UNITS - pad) + nfd * 3
            result = _tail_cut(body)
            self.assertLessEqual(_utf16_len(result), MAX_UTF16_UNITS)
            self.assertTrue(_no_broken_codepoints(result), f"pad={pad}")


# ---------------------------------------------------------------------------
# SEAM 1 (grill-found gap) -- torn-read U+FFFD frame is never rendered
# ---------------------------------------------------------------------------

class TestSeam1TornReadGuard(unittest.TestCase):
    """A poll can catch the generator mid-write: the byte-cut hangul char
    decodes (errors="replace") to U+FFFD inside the display name. The sync
    must hold that broken frame (stay dirty, no edit) and render the complete
    file on a later tick -- this is the path that materializes the reported
    "\\ubc31\\uc2a4<FFFD>..." artifact for BMP hangul."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-dgn950-")
        self._dir = Path(self._td.name)
        self._dash = self._dir / "dashboard.md"
        self._state = self._dir / "state.json"
        self._ds = DashboardSync(
            bot=_mock_bot(),
            turn_active=lambda uid: False,
            dashboard_path=self._dash,
            state_path=self._state,
        )

    def tearDown(self):
        self._td.cleanup()

    def _run_tick(self, sync=None):
        cfg = types.SimpleNamespace(
            allowed_user_ids=[OWNER_ID], bot_data_dir=self._dir
        )
        if sync is None:
            sync = AsyncMock()
        with patch(
            "bridge.dashboard.ownership.resolve_owner",
            return_value=(ownership.MODE_AUTHORITATIVE, None),
        ), patch("bridge.dashboard.config", cfg), patch.object(
            self._ds, "_sync", sync
        ):
            asyncio.run(self._ds._tick())
        return sync

    def _write_torn(self):
        # Complete head + a hangul name cut after 2 of its 3 UTF-8 bytes:
        # exactly what a reader sees mid-write.
        whole = ("set 3 " + HANGUL_NAME + "\n").encode("utf-8")
        self._dash.write_bytes(whole[:-5])

    def test_torn_read_holds_frame_and_stays_dirty(self):
        self._write_torn()
        sync = self._run_tick()
        sync.assert_not_awaited()
        self.assertTrue(self._ds._dirty)

    def test_completed_write_flushes_whole_name(self):
        self._write_torn()
        self._run_tick()
        # Generator finishes the write; the next tick renders the whole name.
        self._dash.write_text("set 3 " + HANGUL_NAME + "\n", encoding="utf-8")
        sync = self._run_tick()
        sync.assert_awaited_once()
        text = sync.await_args.args[1]
        self.assertIn(HANGUL_NAME, text)
        self.assertNotIn("\ufffd", text)


# ---------------------------------------------------------------------------
# SEAM 2 -- countdown start deferred while the owner's turn is in flight
# ---------------------------------------------------------------------------

def _mock_bot(msg_id=5555):
    bot = MagicMock()
    fake_msg = MagicMock()
    fake_msg.message_id = msg_id
    bot.send_message = AsyncMock(return_value=fake_msg)
    bot.edit_message_text = AsyncMock()
    return bot


def _fake_config(tmpdir):
    return types.SimpleNamespace(
        allowed_user_ids=[OWNER_ID], bot_data_dir=Path(tmpdir)
    )


class TestSeam2QueueBeforeTimer(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="countdown-dgn950-")
        self._data_dir = Path(self._td.name)
        self._dir = self._data_dir / "countdown"
        self._bot = _mock_bot()

    def tearDown(self):
        self._td.cleanup()

    def _write(self, cid, payload):
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{cid}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _run(self, coro):
        cfg = _fake_config(self._data_dir)
        with patch(
            "bridge.countdown.ownership.resolve_owner",
            return_value=(ownership.MODE_AUTHORITATIVE, None),
        ), patch("bridge.countdown.config", cfg):
            return asyncio.run(coro)

    async def _teardown_countdown(self, countdown):
        if countdown.task is not None and not countdown.task.done():
            countdown.task.cancel()
            try:
                await countdown.task
            except asyncio.CancelledError:
                pass

    def test_in_flight_turn_defers_timer_start(self):
        # Turn in flight: the tick must NOT start the countdown, and the
        # control file must stay on disk (flush source for a later tick).
        driver = CountdownDriver(
            self._bot, control_dir=self._dir, turn_active=lambda uid: True
        )
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await driver._tick()
            self.assertEqual(driver._active, {})
            await asyncio.sleep(0.02)
            self._bot.send_message.assert_not_awaited()
            self.assertTrue((self._dir / "rest.json").exists())

        self._run(scenario())

    def test_timer_flushes_on_first_tick_after_turn_ends(self):
        # Hold while in flight, then flush: the countdown starts on the
        # first tick after turn_active flips off (model reply already out).
        in_flight = {"on": True}
        driver = CountdownDriver(
            self._bot, control_dir=self._dir,
            turn_active=lambda uid: in_flight["on"],
        )
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await driver._tick()  # deferred
            self.assertEqual(driver._active, {})
            in_flight["on"] = False
            await driver._tick()  # flush
            self.assertIn("rest", driver._active)
            countdown = driver._active["rest"]
            await asyncio.sleep(0.02)  # let the send land
            self._bot.send_message.assert_awaited()
            kwargs = self._bot.send_message.await_args.kwargs
            self.assertEqual(kwargs["chat_id"], OWNER_ID)
            await self._teardown_countdown(countdown)

        self._run(scenario())

    def test_deferral_gates_on_owner_chat_id(self):
        # The gate must ask about the OWNER (private chat: chat_id ==
        # user_id), the same identity DashboardSync passes.
        seen = []

        def turn_active(uid):
            seen.append(uid)
            return False

        driver = CountdownDriver(
            self._bot, control_dir=self._dir, turn_active=turn_active
        )
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await driver._tick()
            self.assertEqual(seen, [OWNER_ID])
            await self._teardown_countdown(driver._active["rest"])

        self._run(scenario())

    def test_no_turn_active_starts_immediately(self):
        # Back-compat: a driver built without turn_active (standalone/test
        # use) keeps the old immediate-start behavior.
        driver = CountdownDriver(self._bot, control_dir=self._dir)
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await driver._tick()
            self.assertIn("rest", driver._active)
            await self._teardown_countdown(driver._active["rest"])

        self._run(scenario())

    def test_deferred_file_deleted_cancels_cleanly(self):
        # Arm deleted while still deferred: nothing was started, nothing to
        # cancel, no crash, no send.
        driver = CountdownDriver(
            self._bot, control_dir=self._dir, turn_active=lambda uid: True
        )
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await driver._tick()  # deferred
            (self._dir / "rest.json").unlink()
            await driver._tick()  # file gone; must converge silently
            self.assertEqual(driver._active, {})
            await asyncio.sleep(0.02)
            self._bot.send_message.assert_not_awaited()

        self._run(scenario())

    def test_running_countdown_ticks_are_not_deferred(self):
        # The deferral gates only the START; an already-running countdown
        # keeps its own edit loop untouched (timing-anchored edits must not
        # freeze mid-turn -- fail-open discipline unchanged).
        in_flight = {"on": False}
        driver = CountdownDriver(
            self._bot, control_dir=self._dir,
            turn_active=lambda uid: in_flight["on"],
        )
        self._write("rest", {"seconds": 60, "label": "rest"})

        async def scenario():
            await driver._tick()  # starts (idle)
            countdown = driver._active["rest"]
            in_flight["on"] = True
            await driver._tick()  # running countdown untouched by the gate
            self.assertIs(driver._active["rest"], countdown)
            self.assertFalse(countdown.finished)
            await self._teardown_countdown(countdown)

        self._run(scenario())

    def test_bot_wires_turn_active_into_driver(self):
        # Wiring pin: the live construction in bot.py must pass the same
        # turn_active gate DashboardSync gets; a silent revert of the kwarg
        # re-opens the race.
        source = (
            Path(__file__).resolve().parents[1] / "bot.py"
        ).read_text(encoding="utf-8")
        start = source.index("CountdownDriver(")
        call = source[start:start + 200]
        self.assertIn("turn_active=self._user_turn_active", call)


if __name__ == "__main__":
    unittest.main()
