"""Unit tests for bridge/dashboard.py (DGN-506; DGN-214 spec v1 coverage).

Covers helper functions, state file lifecycle, owner resolution,
_tick gate conditions, _sync edit path, _recreate path, the empty-content
delete state machine (DGN-541 S1: debounce, not-found convergence,
fail-open, undeletable-48h convergence, re-appearance), and the run()
disabled-flag early exit.
No live Telegram, token, or network.
"""

import asyncio
import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import telegram.error

from bridge import ownership
from bridge.dashboard import (
    DashboardSync,
    EMPTY_DEBOUNCE_SECS,
    MAX_UTF16_UNITS,
    MIN_EDIT_INTERVAL,
    PIN_CHECK_IDLE_SECS,
    PIN_CHECK_MIN_SECS,
    RECREATE_COOLDOWN,
    ROTATE_REQUEST_NAME,
    STALE_UNPIN_MAX_ATTEMPTS,
    _is_chat_gone,
    _is_message_gone,
    _is_not_modified,
    _is_undeletable,
    _needs_recreate,
    _rotate_mode,
    _tail_cut,
    _utf16_len,
)

OWNER_ID = 42


def _mock_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.pin_chat_message = AsyncMock()
    bot.unpin_chat_message = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


def _fake_config(**kwargs):
    defaults = dict(
        dashboard_enabled=True,
        allowed_user_ids=[OWNER_ID],
        bot_data_dir=Path("/tmp"),
    )
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestUtf16Len(unittest.TestCase):
    def test_ascii_one_unit_per_char(self):
        self.assertEqual(_utf16_len("hello"), 5)

    def test_empty_is_zero(self):
        self.assertEqual(_utf16_len(""), 0)

    def test_bmp_cjk_one_unit(self):
        # U+4E2D is in BMP -> 1 UTF-16 code unit
        self.assertEqual(_utf16_len("中"), 1)

    def test_emoji_surrogate_pair_two_units(self):
        # U+1F600 outside BMP -> surrogate pair -> 2 UTF-16 code units
        self.assertEqual(_utf16_len("\U0001F600"), 2)

    def test_mixed_ascii_and_emoji(self):
        # "A" (1) + emoji (2) = 3
        self.assertEqual(_utf16_len("A\U0001F600"), 3)


class TestTailCut(unittest.TestCase):
    def test_short_text_unchanged(self):
        text = "hello"
        self.assertEqual(_tail_cut(text), text)

    def test_at_limit_unchanged(self):
        text = "x" * MAX_UTF16_UNITS
        self.assertEqual(_tail_cut(text), text)

    def test_oversized_fits_within_limit(self):
        text = "x" * (MAX_UTF16_UNITS + 100)
        result = _tail_cut(text)
        self.assertLessEqual(_utf16_len(result), MAX_UTF16_UNITS)

    def test_cut_preserves_head(self):
        head = "KEEP"
        result = _tail_cut(head + "x" * (MAX_UTF16_UNITS + 100))
        self.assertTrue(result.startswith(head))

    def test_cut_shorter_than_original(self):
        text = "z" * (MAX_UTF16_UNITS + 50)
        self.assertLess(len(_tail_cut(text)), len(text))


class TestErrorClassifiers(unittest.TestCase):
    def _e(self, msg):
        return Exception(msg)

    def test_not_modified_true(self):
        self.assertTrue(_is_not_modified(self._e("message is not modified: spec")))

    def test_not_modified_false(self):
        self.assertFalse(_is_not_modified(self._e("message to edit not found")))

    def test_not_modified_case_insensitive(self):
        self.assertTrue(_is_not_modified(self._e("Message Is Not Modified")))

    def test_needs_recreate_not_found(self):
        self.assertTrue(_needs_recreate(self._e("message to edit not found")))

    def test_needs_recreate_cant_edit(self):
        self.assertTrue(_needs_recreate(self._e("message can't be edited")))

    def test_needs_recreate_false_on_other(self):
        self.assertFalse(_needs_recreate(self._e("message is not modified")))

    def test_chat_gone_true(self):
        self.assertTrue(_is_chat_gone(self._e("chat not found")))

    def test_chat_gone_false(self):
        self.assertFalse(_is_chat_gone(self._e("message not found")))

    def test_chat_gone_case_insensitive(self):
        self.assertTrue(_is_chat_gone(self._e("Chat Not Found")))

    def test_message_gone_true(self):
        self.assertTrue(_is_message_gone(self._e("Message to delete not found")))

    def test_message_gone_false_on_other(self):
        self.assertFalse(_is_message_gone(self._e("message can't be deleted")))

    def test_undeletable_true(self):
        self.assertTrue(_is_undeletable(self._e("Message can't be deleted for everyone")))

    def test_undeletable_false_on_other(self):
        self.assertFalse(_is_undeletable(self._e("message to delete not found")))


# ---------------------------------------------------------------------------
# State file lifecycle
# ---------------------------------------------------------------------------

class TestStateFile(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-state-")
        self._dir = Path(self._td.name)
        self._state = self._dir / "state.json"
        self._dash = self._dir / "dashboard.md"

    def tearDown(self):
        self._td.cleanup()

    def _ds(self):
        return DashboardSync(
            bot=_mock_bot(),
            turn_active=lambda uid: False,
            dashboard_path=self._dash,
            state_path=self._state,
        )

    def test_load_valid_state(self):
        self._state.write_text(
            json.dumps({"chat_id": 1001, "message_id": 9001}), encoding="utf-8"
        )
        ds = self._ds()
        self.assertEqual(ds._chat_id, 1001)
        self.assertEqual(ds._message_id, 9001)

    def test_load_absent_gives_none(self):
        ds = self._ds()
        self.assertIsNone(ds._chat_id)
        self.assertIsNone(ds._message_id)

    def test_load_corrupt_json_gives_none(self):
        self._state.write_text("{broken", encoding="utf-8")
        ds = self._ds()  # must not raise
        self.assertIsNone(ds._chat_id)

    def test_load_rejects_bool_type_for_chat_id(self):
        # bool is subclass of int; type() check must reject it (not isinstance)
        self._state.write_text(
            json.dumps({"chat_id": True, "message_id": 1}), encoding="utf-8"
        )
        ds = self._ds()
        self.assertIsNone(ds._chat_id)

    def test_load_rejects_bool_type_for_message_id(self):
        self._state.write_text(
            json.dumps({"chat_id": 1, "message_id": False}), encoding="utf-8"
        )
        ds = self._ds()
        self.assertIsNone(ds._chat_id)

    def test_save_round_trips_correctly(self):
        ds = self._ds()
        ds._chat_id = 1001
        ds._message_id = 9001
        ds._sent_day = "2026-09-28"
        ds._digest = "abc"
        ds._save_state()
        data = json.loads(self._state.read_text(encoding="utf-8"))
        self.assertEqual(data, {
            "chat_id": 1001, "message_id": 9001,
            "sent_day": "2026-09-28", "digest": "abc", "stale_pins": [],
        })
        ds2 = self._ds()
        self.assertEqual(ds2._sent_day, "2026-09-28")
        self.assertEqual(ds2._digest, "abc")

    def test_save_no_tmp_leftover(self):
        ds = self._ds()
        ds._chat_id = 1
        ds._message_id = 2
        ds._save_state()
        tmp = self._state.with_name(self._state.name + ".tmp")
        self.assertFalse(tmp.exists())

    def test_clear_removes_memory(self):
        self._state.write_text(
            json.dumps({"chat_id": 1001, "message_id": 9001}), encoding="utf-8"
        )
        ds = self._ds()
        ds._clear_state()
        self.assertIsNone(ds._chat_id)
        self.assertIsNone(ds._message_id)

    def test_clear_removes_disk_file(self):
        self._state.write_text(
            json.dumps({"chat_id": 1001, "message_id": 9001}), encoding="utf-8"
        )
        ds = self._ds()
        ds._clear_state()
        self.assertFalse(self._state.exists())

    def test_clear_absent_file_does_not_raise(self):
        ds = self._ds()
        ds._clear_state()  # no state file; must not raise


# ---------------------------------------------------------------------------
# Owner resolution
# ---------------------------------------------------------------------------

class TestOwnerChatId(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-owner-")
        self._dir = Path(self._td.name)
        self._ds = DashboardSync(
            bot=_mock_bot(),
            turn_active=lambda uid: False,
            dashboard_path=self._dir / "dashboard.md",
            state_path=self._dir / "state.json",
        )

    def tearDown(self):
        self._td.cleanup()

    def _call(self, mode, owner_id=None, allowed_ids=None):
        if allowed_ids is None:
            allowed_ids = [OWNER_ID] if mode == ownership.MODE_AUTHORITATIVE else []
        cfg = _fake_config(allowed_user_ids=allowed_ids, bot_data_dir=self._dir)
        with patch("bridge.dashboard.ownership.resolve_owner",
                   return_value=(mode, owner_id)), \
             patch("bridge.dashboard.config", cfg):
            return self._ds._owner_chat_id()

    def test_authoritative_returns_first_allowed_id(self):
        self.assertEqual(self._call(ownership.MODE_AUTHORITATIVE), OWNER_ID)

    def test_owner_lock_returns_lock_id(self):
        self.assertEqual(self._call(ownership.MODE_OWNER_LOCK, owner_id=77), 77)

    def test_claim_mode_returns_none(self):
        self.assertIsNone(self._call(ownership.MODE_CLAIM))

    def test_locked_out_returns_none(self):
        self.assertIsNone(self._call(ownership.MODE_LOCKED_OUT))


# ---------------------------------------------------------------------------
# _tick gate conditions
# ---------------------------------------------------------------------------

class TestTick(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-tick-")
        self._dir = Path(self._td.name)
        self._dash = self._dir / "dashboard.md"
        self._state = self._dir / "state.json"

    def tearDown(self):
        self._td.cleanup()

    def _ds(self, turn_active=None):
        return DashboardSync(
            bot=_mock_bot(),
            turn_active=turn_active or (lambda uid: False),
            dashboard_path=self._dash,
            state_path=self._state,
        )

    def _run_tick(self, ds, sync=None, mode=ownership.MODE_AUTHORITATIVE,
                  owner_lock_id=None, allowed_ids=None):
        """Run one _tick with ownership + config patched; returns the sync mock."""
        if allowed_ids is None:
            allowed_ids = [OWNER_ID]
        cfg = _fake_config(allowed_user_ids=allowed_ids, bot_data_dir=self._dir)
        if sync is None:
            sync = AsyncMock()
        with patch("bridge.dashboard.ownership.resolve_owner",
                   return_value=(mode, owner_lock_id)), \
             patch("bridge.dashboard.config", cfg), \
             patch.object(ds, "_sync", sync):
            asyncio.run(ds._tick())
        return sync

    def test_file_absent_stays_clean(self):
        ds = self._ds()
        self._run_tick(ds)
        self.assertFalse(ds._dirty)

    def test_new_mtime_triggers_sync(self):
        ds = self._ds()
        self._dash.write_text("# Dashboard\nStatus: ok\n")
        sync = self._run_tick(ds)
        sync.assert_awaited_once()

    def test_same_mtime_not_dirty_no_sync(self):
        ds = self._ds()
        self._dash.write_text("content")
        mtime = os.path.getmtime(self._dash)
        ds._last_synced_mtime = mtime
        ds._dirty = False
        sync = self._run_tick(ds)
        sync.assert_not_awaited()

    def test_flood_wait_blocks_sync(self):
        ds = self._ds()
        self._dash.write_text("content")
        ds._dirty = True
        ds._flood_until = time.monotonic() + 999.0  # far future
        sync = self._run_tick(ds)
        sync.assert_not_awaited()
        self.assertTrue(ds._dirty)

    def test_min_edit_interval_coalesces(self):
        ds = self._ds()
        self._dash.write_text("content")
        ds._dirty = True
        ds._last_edit = time.monotonic()  # just edited; interval not elapsed
        sync = self._run_tick(ds)
        sync.assert_not_awaited()
        self.assertTrue(ds._dirty)

    def test_no_owner_stays_dormant(self):
        ds = self._ds()
        self._dash.write_text("content")
        ds._dirty = True
        sync = self._run_tick(ds, mode=ownership.MODE_CLAIM, allowed_ids=[])
        sync.assert_not_awaited()

    def test_owner_changed_clears_state_stays_dirty(self):
        ds = self._ds()
        self._dash.write_text("content")
        ds._dirty = True
        ds._chat_id = 999  # different from OWNER_ID
        sync = self._run_tick(ds)  # owner resolves to OWNER_ID=42
        self.assertIsNone(ds._chat_id)
        sync.assert_not_awaited()

    def test_turn_active_defers_sync(self):
        ds = self._ds(turn_active=lambda uid: True)
        self._dash.write_text("content")
        ds._dirty = True
        sync = self._run_tick(ds)
        sync.assert_not_awaited()
        self.assertTrue(ds._dirty)

    def test_empty_content_routes_past_sync(self):
        # v3 (DGN-541 S1): empty content drives the delete state machine,
        # never _sync.  No pinned message here -> converges silently.
        # Delete-path behavior itself is covered in TestEmptyDelete.
        ds = self._ds()
        self._dash.write_text("   \n  ")  # whitespace only
        ds._dirty = True
        sync = self._run_tick(ds)
        sync.assert_not_awaited()

    def test_normal_content_calls_sync(self):
        ds = self._ds()
        self._dash.write_text("# Dashboard\n\nStatus: running\n")
        sync = self._run_tick(ds)
        sync.assert_awaited_once()

    def test_tick_passes_tail_cut_text_to_sync(self):
        ds = self._ds()
        content = "# Dashboard\nStatus: ok\n"
        self._dash.write_text(content)
        sync = AsyncMock()
        self._run_tick(ds, sync=sync)
        # sync called with (chat_id, text, mtime); text is _tail_cut applied
        args = sync.await_args[0]
        self.assertEqual(args[0], OWNER_ID)
        self.assertEqual(args[1], content)  # short enough; unchanged by tail_cut


# ---------------------------------------------------------------------------
# Empty-content delete state machine (DGN-541 S1)
# ---------------------------------------------------------------------------

class TestEmptyDelete(unittest.TestCase):
    """Empty dashboard.md drives the debounced unpin+delete state machine."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-empty-")
        self._dir = Path(self._td.name)
        self._dash = self._dir / "dashboard.md"
        self._state = self._dir / "state.json"
        self._bot = _mock_bot()
        self._ds = DashboardSync(
            bot=self._bot,
            turn_active=lambda uid: False,
            dashboard_path=self._dash,
            state_path=self._state,
        )

    def tearDown(self):
        self._td.cleanup()

    def _run_tick(self):
        cfg = _fake_config(allowed_user_ids=[OWNER_ID], bot_data_dir=self._dir)
        with patch("bridge.dashboard.ownership.resolve_owner",
                   return_value=(ownership.MODE_AUTHORITATIVE, None)), \
             patch("bridge.dashboard.config", cfg):
            asyncio.run(self._ds._tick())

    def test_empty_marks_pending_before_debounce(self):
        # Required case (b): first empty read only MARKS "empty pending" --
        # no delete, message untouched, stays dirty for the next tick.
        self._dash.write_text("")
        self._ds._message_id = 1111
        self._run_tick()
        self._bot.delete_message.assert_not_awaited()
        self._bot.unpin_chat_message.assert_not_awaited()
        self.assertIsNotNone(self._ds._empty_since)
        self.assertEqual(self._ds._message_id, 1111)
        self.assertTrue(self._ds._dirty)

    def test_empty_deletes_after_debounce(self):
        # Required case (a): empty persisted past EMPTY_DEBOUNCE_SECS ->
        # unpin+delete, message_id=None marked synced (no delete loop).
        self._dash.write_text("")
        self._ds._message_id = 1111
        self._ds._empty_since = time.monotonic() - EMPTY_DEBOUNCE_SECS - 1.0
        self._run_tick()
        self._bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=1111
        )
        self._bot.delete_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=1111
        )
        self.assertIsNone(self._ds._message_id)
        self.assertIsNone(self._ds._empty_since)
        self.assertFalse(self._ds._dirty)
        self.assertTrue(self._state.exists())  # converged state persisted

    def test_delete_not_found_converges_as_success(self):
        # Error classification: not-found = success (dead message_id, e.g.
        # bridge restart that lost the post-delete state save).
        self._dash.write_text("")
        self._ds._message_id = 1111
        self._ds._empty_since = time.monotonic() - EMPTY_DEBOUNCE_SECS - 1.0
        self._bot.delete_message = AsyncMock(
            side_effect=telegram.error.BadRequest("message to delete not found")
        )
        self._run_tick()
        self.assertIsNone(self._ds._message_id)
        self.assertFalse(self._ds._dirty)

    def test_delete_failure_fails_open(self):
        # Required case (c): unpin/delete failure -> fail-open (state kept,
        # stays dirty, retried next cycle, no crash).
        self._dash.write_text("")
        self._ds._message_id = 1111
        self._ds._empty_since = time.monotonic() - EMPTY_DEBOUNCE_SECS - 1.0
        self._bot.delete_message = AsyncMock(
            side_effect=telegram.error.TelegramError("internal error")
        )
        self._run_tick()  # must not raise
        self.assertEqual(self._ds._message_id, 1111)
        self.assertTrue(self._ds._dirty)

    def test_undeletable_converges_as_success(self):
        # DGN-541 bug fix: delete_message raises "can't be deleted for everyone"
        # (48h Telegram age limit).  Retrying never succeeds; the best-effort
        # unpin already hid the board.  Converge as success: message_id=None,
        # state saved, empty_since cleared, no further retry (dirty=False).
        self._dash.write_text("")
        self._ds._message_id = 1111
        self._ds._empty_since = time.monotonic() - EMPTY_DEBOUNCE_SECS - 1.0
        self._bot.delete_message = AsyncMock(
            side_effect=telegram.error.TelegramError(
                "Message can't be deleted for everyone"
            )
        )
        self._run_tick()
        # unpin was attempted (best-effort, already done before delete)
        self._bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=1111
        )
        # message_id converged to None -- no more retries
        self.assertIsNone(self._ds._message_id)
        # state persisted so a restart does not re-attempt delete
        self.assertTrue(self._state.exists())
        # empty_since cleared -- debounce window reset
        self.assertIsNone(self._ds._empty_since)
        # dirty cleared -- no retry loop
        self.assertFalse(self._ds._dirty)

    def test_empty_no_pin_converges_silently(self):
        # Empty content with nothing pinned: already converged, no calls.
        self._dash.write_text("")
        self._run_tick()
        self._bot.delete_message.assert_not_awaited()
        self.assertFalse(self._ds._dirty)

    def test_content_return_cancels_pending_delete(self):
        # Flapping guard: content back before the debounce elapses ->
        # pending delete cancelled, normal sync path taken.
        self._dash.write_text("content back")
        self._ds._empty_since = time.monotonic() - 10.0
        sync = AsyncMock()
        cfg = _fake_config(allowed_user_ids=[OWNER_ID], bot_data_dir=self._dir)
        with patch("bridge.dashboard.ownership.resolve_owner",
                   return_value=(ownership.MODE_AUTHORITATIVE, None)), \
             patch("bridge.dashboard.config", cfg), \
             patch.object(self._ds, "_sync", sync):
            asyncio.run(self._ds._tick())
        self.assertIsNone(self._ds._empty_since)
        sync.assert_awaited_once()

    def test_reappearance_send_and_pin(self):
        # Required case (d): after a delete (message_id=None) the board
        # re-appears -> recreate path: SILENT send + silent pin + state save.
        self._dash.write_text("board is back")
        fake_msg = MagicMock()
        fake_msg.message_id = 7777
        self._bot.send_message = AsyncMock(return_value=fake_msg)
        self._run_tick()
        kwargs = self._bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], OWNER_ID)
        self.assertTrue(kwargs["disable_notification"])
        self._bot.pin_chat_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=7777, disable_notification=True
        )
        self.assertEqual(self._ds._message_id, 7777)
        self.assertFalse(self._ds._dirty)


# ---------------------------------------------------------------------------
# _sync edit path
# ---------------------------------------------------------------------------

class TestSync(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-sync-")
        self._dir = Path(self._td.name)
        self._bot = _mock_bot()
        self._ds = DashboardSync(
            bot=self._bot,
            turn_active=lambda uid: False,
            dashboard_path=self._dir / "dashboard.md",
            state_path=self._dir / "state.json",
        )
        self._ds._message_id = 1111  # simulate existing pinned message

    def tearDown(self):
        self._td.cleanup()

    def _sync(self, chat_id=OWNER_ID, text="content", mtime=100.0):
        asyncio.run(self._ds._sync(chat_id, text, mtime))

    def test_edit_success_marks_synced(self):
        self._bot.edit_message_text = AsyncMock(return_value=MagicMock())
        self._sync()
        self.assertFalse(self._ds._dirty)
        self.assertEqual(self._ds._last_synced_mtime, 100.0)

    def test_not_modified_is_normal_flow(self):
        err = telegram.error.BadRequest("message is not modified: content and reply")
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        self._sync()
        self.assertFalse(self._ds._dirty)  # treated as synced, not an error

    def test_needs_recreate_falls_through_to_recreate(self):
        err = telegram.error.BadRequest("message to edit not found")
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        recreate = AsyncMock()
        with patch.object(self._ds, "_recreate", recreate):
            self._sync()
        recreate.assert_awaited_once()

    def test_cant_be_edited_falls_through_to_recreate(self):
        err = telegram.error.BadRequest("message can't be edited")
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        recreate = AsyncMock()
        with patch.object(self._ds, "_recreate", recreate):
            self._sync()
        recreate.assert_awaited_once()

    def test_retry_after_sets_flood_deadline(self):
        err = telegram.error.RetryAfter(10)
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        self._sync()
        self.assertGreater(self._ds._flood_until, time.monotonic())

    def test_chat_gone_clears_state(self):
        err = telegram.error.BadRequest("chat not found")
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        self._ds._chat_id = OWNER_ID
        self._ds._message_id = 1111
        self._sync()
        self.assertIsNone(self._ds._chat_id)
        self.assertIsNone(self._ds._message_id)

    def test_forbidden_clears_state(self):
        err = telegram.error.Forbidden("bot was blocked by the user")
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        self._ds._chat_id = OWNER_ID
        self._sync()
        self.assertIsNone(self._ds._chat_id)

    def test_network_error_stays_dirty_transient(self):
        err = telegram.error.NetworkError("connection reset")
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        self._ds._dirty = True
        self._sync()
        # NetworkError is transient: mark_synced NOT called -> mtime unchanged
        self.assertIsNone(self._ds._last_synced_mtime)

    def test_generic_tg_error_skips_revision(self):
        # Unknown TelegramError: skip the revision to avoid hot-looping the API
        err = telegram.error.TelegramError("unknown 500 internal")
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        self._ds._dirty = True
        self._sync()
        self.assertFalse(self._ds._dirty)

    def test_no_message_id_skips_edit_goes_to_recreate(self):
        self._ds._message_id = None
        recreate = AsyncMock()
        with patch.object(self._ds, "_recreate", recreate):
            self._sync()
        recreate.assert_awaited_once()
        self._bot.edit_message_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# _recreate path
# ---------------------------------------------------------------------------

class TestRecreate(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-recreate-")
        self._dir = Path(self._td.name)
        self._bot = _mock_bot()
        self._ds = DashboardSync(
            bot=self._bot,
            turn_active=lambda uid: False,
            dashboard_path=self._dir / "dashboard.md",
            state_path=self._dir / "state.json",
        )

    def tearDown(self):
        self._td.cleanup()

    def _recreate(self, now=None, old_msg=None):
        if now is None:
            now = time.monotonic()
        if old_msg is not None:
            self._ds._message_id = old_msg
        asyncio.run(self._ds._recreate(OWNER_ID, "text", 100.0, now))
        return now

    def _fake_send(self, msg_id=9999):
        fake_msg = MagicMock()
        fake_msg.message_id = msg_id
        self._bot.send_message = AsyncMock(return_value=fake_msg)
        return fake_msg

    def test_cooldown_blocks_chained_recreate(self):
        now = time.monotonic()
        self._ds._last_recreate = now - 1.0  # only 1s ago; cooldown is 60s
        self._recreate(now=now)
        self._bot.send_message.assert_not_awaited()

    def test_success_saves_state(self):
        self._fake_send(9999)
        self._recreate()
        self.assertEqual(self._ds._chat_id, OWNER_ID)
        self.assertEqual(self._ds._message_id, 9999)
        self.assertTrue((self._dir / "state.json").exists())

    def test_success_marks_synced(self):
        self._fake_send()
        self._recreate()
        self.assertFalse(self._ds._dirty)
        self.assertEqual(self._ds._last_synced_mtime, 100.0)

    def test_success_arms_cooldown(self):
        self._fake_send()
        now = self._recreate()
        self.assertIsNotNone(self._ds._last_recreate)
        self.assertAlmostEqual(self._ds._last_recreate, now, places=1)

    def test_send_is_silent(self):
        # DGN-541: recreate send must not notify (re-appearance alert storm).
        self._fake_send()
        self._recreate()
        kwargs = self._bot.send_message.await_args.kwargs
        self.assertTrue(kwargs["disable_notification"])

    def test_success_pins_with_disable_notification(self):
        self._fake_send(9999)
        self._recreate()
        self._bot.pin_chat_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=9999, disable_notification=True
        )

    def test_unpin_old_message_on_success(self):
        self._fake_send(9999)
        self._recreate(old_msg=1111)
        self._bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=1111
        )

    def test_no_unpin_when_no_old_message(self):
        self._fake_send()
        self._ds._message_id = None
        self._recreate()
        self._bot.unpin_chat_message.assert_not_awaited()

    def test_pin_failure_is_swallowed_sync_still_marks(self):
        self._fake_send(9999)
        self._bot.pin_chat_message = AsyncMock(
            side_effect=telegram.error.TelegramError("pin refused")
        )
        self._recreate()  # must not raise
        self.assertFalse(self._ds._dirty)

    def test_unpin_failure_is_logged_and_queued(self):
        # DGN-1777: no longer swallowed silently -- logged, and the old id
        # stays in the persisted queue for a retry.
        self._fake_send(9999)
        self._bot.unpin_chat_message = AsyncMock(
            side_effect=telegram.error.TelegramError("unpin refused")
        )
        with self.assertLogs("bridge.dashboard", level="WARNING") as logs:
            self._recreate(old_msg=1111)  # must not raise
        self.assertFalse(self._ds._dirty)
        self.assertIn("1111", "\n".join(logs.output))
        self.assertEqual(
            [e["message_id"] for e in self._ds._stale_pins], [1111]
        )

    def test_send_retry_after_sets_flood(self):
        self._bot.send_message = AsyncMock(
            side_effect=telegram.error.RetryAfter(15)
        )
        now = time.monotonic()
        self._recreate(now=now)
        self.assertGreater(self._ds._flood_until, now)

    def test_send_forbidden_clears_state(self):
        self._ds._chat_id = OWNER_ID
        self._ds._message_id = 1111
        self._bot.send_message = AsyncMock(
            side_effect=telegram.error.Forbidden("bot blocked by user")
        )
        self._recreate()
        self.assertIsNone(self._ds._chat_id)

    def test_send_generic_error_stays_dirty(self):
        self._bot.send_message = AsyncMock(
            side_effect=telegram.error.TelegramError("internal error")
        )
        self._ds._dirty = True
        self._recreate()
        # mark_synced NOT called on send failure
        self.assertIsNone(self._ds._last_synced_mtime)

    def test_cooldown_armed_only_on_success(self):
        self._bot.send_message = AsyncMock(
            side_effect=telegram.error.TelegramError("failed")
        )
        now = time.monotonic()
        asyncio.run(self._ds._recreate(OWNER_ID, "text", 100.0, now))
        self.assertIsNone(self._ds._last_recreate)


# ---------------------------------------------------------------------------
# DGN-1768 daily rotation: first real change of a new day -> new pinned message
# ---------------------------------------------------------------------------

DAY1 = "2026-09-27"
DAY2 = "2026-09-28"


class TestDailyRotation(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-rotate-")
        self._dir = Path(self._td.name)
        self._dash = self._dir / "dashboard.md"
        self._state = self._dir / "state.json"
        self._bot = _mock_bot()
        self._next_id = 100
        self._bot.send_message = AsyncMock(side_effect=self._send)
        self._ds = self._new_ds()
        self._mtime = 1000.0

    def tearDown(self):
        self._td.cleanup()

    def _send(self, **kwargs):
        self._next_id += 1
        msg = MagicMock()
        msg.message_id = self._next_id
        return msg

    def _new_ds(self):
        return DashboardSync(
            bot=self._bot,
            turn_active=lambda uid: False,
            dashboard_path=self._dash,
            state_path=self._state,
        )

    def _write(self, text):
        self._dash.write_text(text)
        self._mtime += 10.0
        os.utime(self._dash, (self._mtime, self._mtime))

    def _tick(self, day, ds=None):
        ds = ds or self._ds
        # Clear interval/cooldown gates: the test drives calendar days, not
        # wall-clock seconds.
        ds._last_edit = None
        ds._last_recreate = None
        cfg = _fake_config(allowed_user_ids=[OWNER_ID], bot_data_dir=self._dir)
        with patch("bridge.dashboard.ownership.resolve_owner",
                   return_value=(ownership.MODE_AUTHORITATIVE, None)), \
             patch("bridge.dashboard.config", cfg), \
             patch("bridge.dashboard._local_day", return_value=day):
            asyncio.run(ds._tick())

    def _day1_board(self):
        self._write("board v1")
        self._tick(DAY1)
        self.assertEqual(self._ds._message_id, 101)
        self.assertEqual(self._ds._sent_day, DAY1)
        self._bot.reset_mock()

    def test_day2_first_change_sends_pins_and_deletes_old(self):
        self._day1_board()
        self._write("board v2 (morning brief schedule)")
        self._tick(DAY2)
        self._bot.edit_message_text.assert_not_awaited()
        self._bot.send_message.assert_awaited_once()
        self.assertTrue(
            self._bot.send_message.await_args.kwargs["disable_notification"]
        )
        self._bot.pin_chat_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=102, disable_notification=True
        )
        self._bot.delete_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=101
        )
        self._bot.unpin_chat_message.assert_not_awaited()  # deleted, not orphaned
        self.assertEqual(self._ds._message_id, 102)
        self.assertEqual(self._ds._sent_day, DAY2)
        self.assertFalse(self._ds._dirty)

    def test_same_day_second_change_edits_only(self):
        self._day1_board()
        self._write("board v2")
        self._tick(DAY2)
        self._bot.reset_mock()
        self._write("board v3")
        self._tick(DAY2)
        self._bot.send_message.assert_not_awaited()
        self._bot.delete_message.assert_not_awaited()
        self._bot.pin_chat_message.assert_not_awaited()
        self._bot.edit_message_text.assert_awaited_once()
        self.assertEqual(
            self._bot.edit_message_text.await_args.kwargs["message_id"], 102
        )

    def test_same_day_change_on_send_day_edits(self):
        self._day1_board()
        self._write("board v1b")
        self._tick(DAY1)
        self._bot.send_message.assert_not_awaited()
        self._bot.edit_message_text.assert_awaited_once()

    def test_unchanged_day_sends_nothing(self):
        self._day1_board()
        self._tick(DAY2)  # mtime unchanged: not dirty
        self._tick(DAY2)
        self._bot.send_message.assert_not_awaited()
        self._bot.edit_message_text.assert_not_awaited()
        self._bot.delete_message.assert_not_awaited()

    def test_restart_on_new_day_without_change_does_not_rotate(self):
        # The first tick after a restart is always dirty; same content must
        # not count as the day's first change.
        self._day1_board()
        ds2 = self._new_ds()
        self._tick(DAY2, ds=ds2)
        self._bot.send_message.assert_not_awaited()
        self._bot.delete_message.assert_not_awaited()
        self.assertEqual(ds2._message_id, 101)
        self.assertEqual(ds2._sent_day, DAY1)

    def test_delete_refused_unpins_only_no_crash(self):
        self._day1_board()
        self._bot.delete_message = AsyncMock(
            side_effect=telegram.error.BadRequest(
                "Message can't be deleted for everyone"
            )
        )
        self._write("board v2")
        self._tick(DAY2)  # must not raise
        self._bot.pin_chat_message.assert_awaited_once()
        self._bot.unpin_chat_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=101
        )
        self.assertEqual(self._ds._message_id, 102)
        self.assertFalse(self._ds._dirty)
        # No retry of the delete on the next tick.
        self._tick(DAY2)
        self._bot.delete_message.assert_awaited_once()

    def test_delete_old_already_gone_skips_unpin(self):
        self._day1_board()
        self._bot.delete_message = AsyncMock(
            side_effect=telegram.error.BadRequest("message to delete not found")
        )
        self._write("board v2")
        self._tick(DAY2)
        self._bot.unpin_chat_message.assert_not_awaited()
        self.assertEqual(self._ds._message_id, 102)

    def test_rotation_send_failure_keeps_old_and_retries(self):
        self._day1_board()
        self._bot.send_message = AsyncMock(
            side_effect=telegram.error.TelegramError("internal error")
        )
        self._write("board v2")
        self._tick(DAY2)
        self._bot.delete_message.assert_not_awaited()
        self.assertEqual(self._ds._message_id, 101)
        self.assertTrue(self._ds._dirty)
        self._bot.send_message = AsyncMock(side_effect=self._send)
        self._tick(DAY2)
        self._bot.delete_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=101
        )
        self.assertEqual(self._ds._sent_day, DAY2)

    def test_state_file_carries_send_day_across_restart(self):
        self._day1_board()
        data = json.loads(self._state.read_text())
        self.assertEqual(data["sent_day"], DAY1)
        self.assertEqual(data["message_id"], 101)
        ds2 = self._new_ds()
        self.assertEqual(ds2._sent_day, DAY1)
        self.assertEqual(ds2._message_id, 101)
        # Restarted process, same day: a change edits.
        self._tick(DAY1, ds=ds2)  # restart tick (same content)
        self._write("board v1b")
        self._tick(DAY1, ds=ds2)
        self._bot.send_message.assert_not_awaited()
        # Restarted again, next day: the first change rotates.
        ds3 = self._new_ds()
        self._write("board v2")
        self._tick(DAY2, ds=ds3)
        self._bot.delete_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=101
        )
        self.assertEqual(ds3._message_id, 102)
        self.assertEqual(json.loads(self._state.read_text())["sent_day"], DAY2)

    def test_legacy_state_without_day_rotates_on_next_real_change(self):
        # Old {chat_id, message_id} file: the restart tick edits (no rotation
        # on the restart alone); the next real change rotates.
        self._write("board v1")
        self._state.write_text(json.dumps({"chat_id": OWNER_ID, "message_id": 55}))
        ds = self._new_ds()
        self.assertIsNone(ds._sent_day)
        self._tick(DAY2, ds=ds)
        self._bot.send_message.assert_not_awaited()
        self._bot.edit_message_text.assert_awaited_once()
        self._write("board v2")
        self._tick(DAY2, ds=ds)
        self._bot.send_message.assert_awaited_once()
        self._bot.delete_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=55
        )
        self.assertEqual(ds._sent_day, DAY2)

    def test_empty_delete_clears_day(self):
        self._day1_board()
        self._ds._empty_since = time.monotonic() - EMPTY_DEBOUNCE_SECS - 1.0
        self._write("")
        self._tick(DAY1)
        self.assertIsNone(self._ds._message_id)
        self.assertIsNone(self._ds._sent_day)


# ---------------------------------------------------------------------------
# DGN-1768 r2: explicit fresh request marker + DASHBOARD_ROTATE mode
# ---------------------------------------------------------------------------

class TestFreshRequest(TestDailyRotation):
    """Inherits the rotation fixture; the parent's tests re-run here too,
    with no marker and the default mode (auto stays byte-for-byte r1)."""

    MODE = ""

    def setUp(self):
        super().setUp()
        self._marker = self._dir / ROTATE_REQUEST_NAME

    def _tick(self, day, ds=None, reset_cooldown=True):
        ds = ds or self._ds
        ds._last_edit = None
        if reset_cooldown:
            ds._last_recreate = None
        cfg = _fake_config(allowed_user_ids=[OWNER_ID], bot_data_dir=self._dir,
                           dashboard_rotate=self.MODE)
        with patch("bridge.dashboard.ownership.resolve_owner",
                   return_value=(ownership.MODE_AUTHORITATIVE, None)), \
             patch("bridge.dashboard.config", cfg), \
             patch("bridge.dashboard.AGENT_CONF_FILE", self._dir / "none.conf"), \
             patch("bridge.dashboard._local_day", return_value=day):
            asyncio.run(ds._tick())

    def _request_fresh(self):
        self._marker.write_text("{}")

    def test_marker_rotates_same_day_and_is_consumed(self):
        self._day1_board()
        self._write("board v2 (brief card)")
        self._request_fresh()
        self._ds._last_recreate = time.monotonic()  # cooldown is skipped
        self._tick(DAY1, reset_cooldown=False)
        self._bot.edit_message_text.assert_not_awaited()
        self._bot.send_message.assert_awaited_once()
        self.assertTrue(
            self._bot.send_message.await_args.kwargs["disable_notification"]
        )
        self._bot.pin_chat_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=102, disable_notification=True
        )
        self._bot.delete_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=101
        )
        self.assertFalse(self._marker.exists())
        self.assertEqual(self._ds._message_id, 102)

    def test_marker_consumed_once(self):
        self._day1_board()
        self._write("board v2")
        self._request_fresh()
        self._tick(DAY1)
        self._bot.reset_mock()
        self._write("board v3")
        self._tick(DAY1)
        self._tick(DAY1)
        self._bot.send_message.assert_not_awaited()
        self._bot.edit_message_text.assert_awaited_once()

    def test_marker_rotates_unchanged_content(self):
        self._day1_board()
        self._request_fresh()  # no file change: the marker alone is dirty
        self._tick(DAY1)
        self._bot.send_message.assert_awaited_once()
        self.assertFalse(self._marker.exists())

    def test_marker_kept_when_send_fails(self):
        self._day1_board()
        self._bot.send_message = AsyncMock(
            side_effect=telegram.error.TelegramError("internal error")
        )
        self._write("board v2")
        self._request_fresh()
        self._tick(DAY1)
        self.assertTrue(self._marker.exists())
        self.assertTrue(self._ds._dirty)
        self._bot.send_message = AsyncMock(side_effect=self._send)
        self._tick(DAY1)
        self.assertFalse(self._marker.exists())
        self.assertEqual(self._ds._message_id, 102)

    def test_marker_on_empty_board_is_consumed(self):
        self._day1_board()
        self._write("")
        self._request_fresh()
        self._tick(DAY1)
        self.assertFalse(self._marker.exists())
        self._bot.send_message.assert_not_awaited()

    def test_marker_without_board_sends_fresh(self):
        self._write("board v1")
        self._request_fresh()
        self._tick(DAY1)
        self._bot.send_message.assert_awaited_once()
        self.assertEqual(self._ds._message_id, 101)
        self.assertFalse(self._marker.exists())


class TestFreshRequestExplicit(TestFreshRequest):
    """DASHBOARD_ROTATE=explicit: the parent marker tests hold as-is; the
    inherited r1 day-rotation tests that expect a day-change rotation are
    overridden below with the explicit expectation (edit only)."""

    MODE = "explicit"

    def _assert_day_change_edits(self):
        self._day1_board()
        self._write("board v2 (morning brief schedule)")
        self._tick(DAY2)
        self._bot.send_message.assert_not_awaited()
        self._bot.delete_message.assert_not_awaited()
        self._bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self._ds._message_id, 101)
        self.assertEqual(self._ds._sent_day, DAY1)

    def test_day2_first_change_sends_pins_and_deletes_old(self):
        self._assert_day_change_edits()

    def test_same_day_second_change_edits_only(self):
        self._assert_day_change_edits()

    def test_delete_refused_unpins_only_no_crash(self):
        self._assert_day_change_edits()

    def test_delete_old_already_gone_skips_unpin(self):
        self._assert_day_change_edits()

    def test_rotation_send_failure_keeps_old_and_retries(self):
        self._assert_day_change_edits()

    def test_state_file_carries_send_day_across_restart(self):
        self._assert_day_change_edits()

    def test_legacy_state_without_day_rotates_on_next_real_change(self):
        self._write("board v1")
        self._state.write_text(json.dumps({"chat_id": OWNER_ID, "message_id": 55}))
        ds = self._new_ds()
        self._tick(DAY2, ds=ds)
        self._write("board v2")
        self._tick(DAY2, ds=ds)
        self._bot.send_message.assert_not_awaited()
        self.assertEqual(self._bot.edit_message_text.await_count, 2)

    def test_explicit_clear_of_a_section_edits(self):
        self._day1_board()
        self._write("board v1 minus a cleared section")
        self._tick(DAY2)
        self._bot.send_message.assert_not_awaited()
        self._bot.edit_message_text.assert_awaited_once()

    def test_explicit_fresh_request_still_rotates_on_day_change(self):
        self._day1_board()
        self._write("board v2")
        self._request_fresh()
        self._tick(DAY2)
        self._bot.send_message.assert_awaited_once()
        self._bot.delete_message.assert_awaited_once_with(
            chat_id=OWNER_ID, message_id=101
        )
        self.assertEqual(self._ds._sent_day, DAY2)


class TestRotateMode(unittest.TestCase):
    def _mode(self, env_value, conf_text=None):
        with tempfile.TemporaryDirectory(prefix="dash-mode-") as td:
            conf = Path(td) / "agent.conf"
            if conf_text is not None:
                conf.write_text(conf_text)
            cfg = _fake_config(dashboard_rotate=env_value)
            with patch("bridge.dashboard.config", cfg), \
                 patch("bridge.dashboard.AGENT_CONF_FILE", conf):
                return _rotate_mode()

    def test_default_auto(self):
        self.assertEqual(self._mode(""), "auto")

    def test_env_explicit(self):
        self.assertEqual(self._mode("explicit"), "explicit")

    def test_agent_conf_explicit_quoted(self):
        self.assertEqual(
            self._mode("", 'X=1\nDASHBOARD_ROTATE="explicit"\n'), "explicit"
        )

    def test_env_wins_over_conf(self):
        self.assertEqual(self._mode("auto", "DASHBOARD_ROTATE=explicit\n"), "auto")

    def test_unknown_value_is_auto(self):
        self.assertEqual(self._mode("sometimes"), "auto")


# ---------------------------------------------------------------------------
# DGN-1777: exactly one board pin (fake bot with a real pin set)
# ---------------------------------------------------------------------------

OWNER_PIN = 7  # a message the OWNER pinned; the bridge must never unpin it


class _PinBot:
    """Fake bot that models the chat's pin set.  unpin of an id listed in
    fail_unpin raises; delete of an id listed in undeletable is refused with
    Telegram's 48h age-limit error."""

    def __init__(self):
        self.pinned = {OWNER_PIN}
        self.fail_unpin = set()
        self.undeletable = set()
        self.unpin_calls = []
        self.pin_calls = []
        self.get_chat_calls = 0
        self._next_id = 100
        self.send_message = AsyncMock(side_effect=self._send)
        self.edit_message_text = AsyncMock()
        self.pin_chat_message = AsyncMock(side_effect=self._pin)
        self.unpin_chat_message = AsyncMock(side_effect=self._unpin)
        self.delete_message = AsyncMock(side_effect=self._delete)
        self.get_chat = AsyncMock(side_effect=self._get_chat)

    async def _get_chat(self, chat_id):
        # Bot API: pinned_message = most recent pin BY SENDING DATE, i.e.
        # the highest pinned message id.
        self.get_chat_calls += 1
        top = max(self.pinned) if self.pinned else None
        return types.SimpleNamespace(
            pinned_message=(
                None if top is None else types.SimpleNamespace(message_id=top)
            )
        )

    async def _send(self, **kwargs):
        self._next_id += 1
        msg = MagicMock()
        msg.message_id = self._next_id
        return msg

    async def _pin(self, chat_id, message_id, **kwargs):
        self.pin_calls.append((message_id, kwargs.get("disable_notification")))
        self.pinned.add(message_id)

    async def _unpin(self, chat_id, message_id):
        self.unpin_calls.append(message_id)
        if message_id in self.fail_unpin:
            raise telegram.error.TelegramError("Internal Server Error")
        if message_id not in self.pinned:
            raise telegram.error.BadRequest("Message to unpin not found")
        self.pinned.discard(message_id)

    async def _delete(self, chat_id, message_id):
        if message_id in self.undeletable:
            raise telegram.error.BadRequest(
                "Message can't be deleted for everyone"
            )
        self.pinned.discard(message_id)


class _PinChatCase(unittest.TestCase):
    """Shared fixture: a DashboardSync over the _PinBot pin-set fake."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-onepin-")
        self._dir = Path(self._td.name)
        self._dash = self._dir / "dashboard.md"
        self._state = self._dir / "state.json"
        self._bot = _PinBot()
        self._ds = self._new_ds()
        self._mtime = 1000.0

    def tearDown(self):
        self._td.cleanup()

    def _new_ds(self):
        return DashboardSync(
            bot=self._bot,
            turn_active=lambda uid: False,
            dashboard_path=self._dash,
            state_path=self._state,
        )

    def _write(self, text):
        self._dash.write_text(text)
        self._mtime += 10.0
        os.utime(self._dash, (self._mtime, self._mtime))

    def _tick(self, day, ds=None, sweep_due=False):
        ds = ds or self._ds
        ds._last_edit = None
        ds._last_recreate = None
        if sweep_due:
            ds._next_unpin_sweep = 0.0
        cfg = _fake_config(allowed_user_ids=[OWNER_ID], bot_data_dir=self._dir)
        with patch("bridge.dashboard.ownership.resolve_owner",
                   return_value=(ownership.MODE_AUTHORITATIVE, None)), \
             patch("bridge.dashboard.config", cfg), \
             patch("bridge.dashboard.AGENT_CONF_FILE", self._dir / "none.conf"), \
             patch("bridge.dashboard._local_day", return_value=day):
            asyncio.run(ds._tick())

    def _stale_ids(self, ds=None):
        return [e["message_id"] for e in (ds or self._ds)._stale_pins]

    def _rotate_with_failing_unpin(self):
        """Day-1 board 101, day-2 rotation to 102: delete of 101 refused
        (age limit) and its unpin raises."""
        self._write("board v1")
        self._tick(DAY1)
        self.assertEqual(self._bot.pinned, {OWNER_PIN, 101})
        self._bot.undeletable.add(101)
        self._bot.fail_unpin.add(101)
        self._write("board v2")
        with self.assertLogs("bridge.dashboard", level="WARNING") as logs:
            self._tick(DAY2)
        return logs

class TestOneBoardPin(_PinChatCase):
    def test_failed_unpin_is_logged_not_silent(self):
        logs = self._rotate_with_failing_unpin()
        joined = "\n".join(logs.output)
        self.assertIn("unpin of old board 101 failed", joined)
        self.assertEqual(self._ds._message_id, 102)
        self.assertFalse(self._ds._dirty)
        self.assertEqual(self._stale_ids(), [101])

    def test_state_carries_stale_ids_across_restart_then_converges(self):
        self._rotate_with_failing_unpin()
        data = json.loads(self._state.read_text())
        self.assertEqual(data["message_id"], 102)
        self.assertEqual(
            [e["message_id"] for e in data["stale_pins"]], [101]
        )
        # Restart; Telegram now accepts the unpin.
        self._bot.fail_unpin.clear()
        ds2 = self._new_ds()
        self.assertEqual(self._stale_ids(ds2), [101])
        self._tick(DAY2, ds=ds2)  # a fresh process sweeps on its first tick
        self.assertEqual(self._bot.pinned, {OWNER_PIN, 102})
        self.assertEqual(self._stale_ids(ds2), [])
        data = json.loads(self._state.read_text())
        self.assertEqual(data["stale_pins"], [])

    def test_retry_waits_for_the_interval(self):
        self._rotate_with_failing_unpin()
        calls = len(self._bot.unpin_calls)
        self._tick(DAY2)  # interval not elapsed: no retry
        self.assertEqual(len(self._bot.unpin_calls), calls)
        self._bot.fail_unpin.clear()
        self._tick(DAY2, sweep_due=True)
        self.assertEqual(self._bot.pinned, {OWNER_PIN, 102})

    def test_after_rotations_only_live_board_and_owner_pin_remain(self):
        # Several rotations, some unpins failing transiently.
        self._write("board v1")
        self._tick(DAY1)
        days = ["2026-09-29", "2026-09-30", "2026-10-01"]
        for i, day in enumerate(days):
            live = self._ds._message_id
            self._bot.undeletable.add(live)
            if i % 2 == 0:
                self._bot.fail_unpin.add(live)
            self._write("board day %d" % i)
            with self.assertLogs("bridge.dashboard", level="INFO"):
                self._tick(day)
        self._bot.fail_unpin.clear()
        self._tick(days[-1], sweep_due=True)
        self.assertEqual(self._bot.pinned, {OWNER_PIN, self._ds._message_id})
        self.assertEqual(self._stale_ids(), [])
        self.assertNotIn(OWNER_PIN, self._bot.unpin_calls)
        self.assertNotIn(self._ds._message_id, self._bot.unpin_calls)

    def test_owner_pin_never_touched(self):
        self._rotate_with_failing_unpin()
        self._bot.fail_unpin.clear()
        self._tick(DAY2, sweep_due=True)
        self.assertIn(OWNER_PIN, self._bot.pinned)
        self.assertNotIn(OWNER_PIN, self._bot.unpin_calls)

    def test_gives_up_after_max_attempts_with_warning(self):
        self._rotate_with_failing_unpin()  # attempt 1
        for _ in range(STALE_UNPIN_MAX_ATTEMPTS - 2):
            with self.assertLogs("bridge.dashboard", level="WARNING"):
                self._tick(DAY2, sweep_due=True)
        self.assertEqual(self._stale_ids(), [101])
        with self.assertLogs("bridge.dashboard", level="WARNING") as logs:
            self._tick(DAY2, sweep_due=True)
        self.assertIn("giving up", "\n".join(logs.output))
        self.assertEqual(self._stale_ids(), [])

    def test_already_unpinned_converges(self):
        self._rotate_with_failing_unpin()
        self._bot.fail_unpin.clear()
        self._bot.pinned.discard(101)  # e.g. owner unpinned it by hand
        self._tick(DAY2, sweep_due=True)
        self.assertEqual(self._stale_ids(), [])
        self.assertEqual(self._bot.pinned, {OWNER_PIN, 102})

    def test_live_board_is_never_unpinned_from_queue(self):
        self._write("board v1")
        self._tick(DAY1)
        live = self._ds._message_id
        self._ds._stale_pins = [
            {"chat_id": OWNER_ID, "message_id": live, "attempts": 0}
        ]
        self._tick(DAY1, sweep_due=True)
        self.assertNotIn(live, self._bot.unpin_calls)
        self.assertIn(live, self._bot.pinned)
        self.assertEqual(self._stale_ids(), [])

    def test_empty_path_unpin_failure_on_undeletable_is_queued(self):
        self._write("board v1")
        self._tick(DAY1)
        self._bot.undeletable.add(101)
        self._bot.fail_unpin.add(101)
        self._write("")
        self._tick(DAY1)  # marks empty pending
        self._ds._empty_since = time.monotonic() - EMPTY_DEBOUNCE_SECS - 1
        with self.assertLogs("bridge.dashboard", level="WARNING"):
            self._tick(DAY1)
        self.assertIsNone(self._ds._message_id)
        self.assertEqual(self._stale_ids(), [101])
        self._bot.fail_unpin.clear()
        self._tick(DAY1, sweep_due=True)
        self.assertEqual(self._bot.pinned, {OWNER_PIN})
        self.assertEqual(self._stale_ids(), [])

    def test_clear_state_keeps_the_queue_on_disk(self):
        self._rotate_with_failing_unpin()
        self._ds._clear_state()
        self.assertIsNone(self._ds._message_id)
        data = json.loads(self._state.read_text())
        self.assertIsNone(data["message_id"])
        self.assertEqual(
            [e["message_id"] for e in data["stale_pins"]], [101]
        )
        ds2 = self._new_ds()
        self.assertIsNone(ds2._message_id)
        self.assertEqual(self._stale_ids(ds2), [101])

    def test_malformed_queue_entries_are_dropped_on_load(self):
        self._state.write_text(json.dumps({
            "chat_id": OWNER_ID, "message_id": 5,
            "stale_pins": [
                {"chat_id": OWNER_ID, "message_id": 4, "attempts": 2},
                {"chat_id": True, "message_id": 3},
                {"chat_id": OWNER_ID},
                "junk",
            ],
        }))
        ds = self._new_ds()
        self.assertEqual(
            ds._stale_pins,
            [{"chat_id": OWNER_ID, "message_id": 4, "attempts": 2}],
        )


class TestOwnerUnpinRepin(_PinChatCase):
    """DGN-1782 (dec-201): an owner-unpinned LIVE board is re-pinned silently;
    owner pins and stale board ids are never touched."""

    def _board(self):
        self._write("board v1")
        self._tick(DAY1)  # send + pin 101
        self._bot.pin_calls.clear()
        return self._ds._message_id

    def _age_check(self, secs):
        self._ds._last_pin_check = time.monotonic() - secs

    def test_owner_unpinned_live_board_is_repinned_once(self):
        live = self._board()
        self._bot.pinned.discard(live)  # owner unpins the board
        self._write("board v2")
        self._tick(DAY1)  # real edit
        self.assertEqual(self._bot.pin_calls, [])
        self._age_check(PIN_CHECK_MIN_SECS + 1)
        with self.assertLogs("bridge.dashboard", level="INFO"):
            self._tick(DAY1)  # idle tick after the edit -> check -> re-pin
        self.assertEqual(self._bot.pin_calls, [(live, True)])
        self.assertIn(live, self._bot.pinned)
        # Pinned again: later checks see it on top and do nothing.
        for _ in range(3):
            self._age_check(PIN_CHECK_IDLE_SECS + 1)
            self._tick(DAY1)
        self.assertEqual(self._bot.pin_calls, [(live, True)])

    def test_repins_when_nothing_is_pinned_at_all(self):
        live = self._board()
        self._bot.pinned.clear()
        self._age_check(PIN_CHECK_IDLE_SECS + 1)
        with self.assertLogs("bridge.dashboard", level="INFO"):
            self._tick(DAY1)
        self.assertEqual(self._bot.pin_calls, [(live, True)])

    def test_owner_pin_on_top_no_churn(self):
        live = self._board()
        newer_owner_pin = live + 50
        self._bot.pinned.add(newer_owner_pin)
        for _ in range(3):
            self._write("board edit %f" % self._mtime)
            self._tick(DAY1)
            self._age_check(PIN_CHECK_IDLE_SECS + 1)
            self._tick(DAY1)
        self.assertGreater(self._bot.get_chat_calls, 0)
        self.assertEqual(self._bot.pin_calls, [])
        self.assertEqual(self._bot.unpin_calls, [])
        self.assertEqual(self._bot.pinned, {OWNER_PIN, live, newer_owner_pin})

    def test_older_owner_pin_on_top_means_board_unpinned(self):
        # Only the owner's older pin remains: the board would outrank it if
        # it were pinned, so it is not -> re-pin; the owner pin is untouched.
        live = self._board()
        self._bot.pinned.discard(live)
        self._age_check(PIN_CHECK_IDLE_SECS + 1)
        with self.assertLogs("bridge.dashboard", level="INFO"):
            self._tick(DAY1)
        self.assertEqual(self._bot.pin_calls, [(live, True)])
        self.assertIn(OWNER_PIN, self._bot.pinned)
        self.assertNotIn(OWNER_PIN, self._bot.unpin_calls)

    def test_stale_ids_never_repinned(self):
        # Rotation leaves 101 stuck pinned (queued); owner unpins live 102.
        self._rotate_with_failing_unpin()
        live = self._ds._message_id
        self.assertEqual(self._stale_ids(), [101])
        self._bot.pin_calls.clear()
        self._bot.pinned.discard(live)
        self._age_check(PIN_CHECK_IDLE_SECS + 1)
        with self.assertLogs("bridge.dashboard", level="INFO"):
            self._tick(DAY2)  # top = stale 101 < live -> re-pin live only
        self.assertEqual(self._bot.pin_calls, [(live, True)])
        # Once the stale unpin succeeds it stays unpinned; no pin ever hits it.
        self._bot.fail_unpin.clear()
        self._tick(DAY2, sweep_due=True)
        self._age_check(PIN_CHECK_IDLE_SECS + 1)
        self._tick(DAY2)
        self.assertEqual(self._bot.pinned, {OWNER_PIN, live})
        self.assertNotIn(101, [m for m, _ in self._bot.pin_calls])

    def test_cadence_bounds_get_chat(self):
        self._board()
        calls = self._bot.get_chat_calls
        for _ in range(20):
            self._tick(DAY1)  # idle ticks right after the pinning send
        self.assertEqual(self._bot.get_chat_calls, calls)
        # A real edit alone does not bypass the minimum spacing.
        self._write("board v2")
        self._tick(DAY1)
        self._tick(DAY1)
        self.assertEqual(self._bot.get_chat_calls, calls)
        self._age_check(PIN_CHECK_MIN_SECS + 1)
        self._tick(DAY1)
        self._tick(DAY1)
        self.assertEqual(self._bot.get_chat_calls, calls + 1)
        # No edit: the min spacing is not enough, the idle interval is.
        self._age_check(PIN_CHECK_MIN_SECS + 1)
        self._tick(DAY1)
        self.assertEqual(self._bot.get_chat_calls, calls + 1)
        self._age_check(PIN_CHECK_IDLE_SECS + 1)
        self._tick(DAY1)
        self.assertEqual(self._bot.get_chat_calls, calls + 2)

    def test_turn_active_and_flood_defer_the_check(self):
        live = self._board()
        self._bot.pinned.discard(live)
        busy = DashboardSync(
            bot=self._bot,
            turn_active=lambda uid: True,
            dashboard_path=self._dash,
            state_path=self._state,
        )
        busy._last_synced_mtime = self._mtime
        busy._last_pin_check = time.monotonic() - PIN_CHECK_IDLE_SECS - 1
        calls = self._bot.get_chat_calls
        self._tick(DAY1, ds=busy)
        self.assertEqual(self._bot.get_chat_calls, calls)
        self._age_check(PIN_CHECK_IDLE_SECS + 1)
        self._ds._flood_until = time.monotonic() + 999.0
        cfg = _fake_config(allowed_user_ids=[OWNER_ID], bot_data_dir=self._dir)
        with patch("bridge.dashboard.ownership.resolve_owner",
                   return_value=(ownership.MODE_AUTHORITATIVE, None)), \
             patch("bridge.dashboard.config", cfg):
            asyncio.run(self._ds._tick())
        self.assertEqual(self._bot.get_chat_calls, calls)
        self.assertEqual(self._bot.pin_calls, [])

    def test_repin_flood_wait_arms_guard_and_retries(self):
        live = self._board()
        self._bot.pinned.discard(live)
        self._bot.pin_chat_message.side_effect = telegram.error.RetryAfter(30)
        self._age_check(PIN_CHECK_IDLE_SECS + 1)
        with self.assertLogs("bridge.dashboard", level="INFO"):
            self._tick(DAY1)
        self.assertGreater(self._ds._flood_until, time.monotonic())
        self.assertIsNone(self._ds._last_pin_check)
        self._bot.pin_chat_message.side_effect = self._bot._pin
        self._ds._flood_until = 0.0
        with self.assertLogs("bridge.dashboard", level="INFO"):
            self._tick(DAY1)
        self.assertIn(live, self._bot.pinned)


# ---------------------------------------------------------------------------
# run() disabled-flag early exit
# ---------------------------------------------------------------------------

class TestRunDisabled(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-run-")
        self._dir = Path(self._td.name)
        self._ds = DashboardSync(
            bot=_mock_bot(),
            turn_active=lambda uid: False,
            dashboard_path=self._dir / "dashboard.md",
            state_path=self._dir / "state.json",
        )

    def tearDown(self):
        self._td.cleanup()

    def test_run_exits_immediately_when_disabled(self):
        cfg = _fake_config(dashboard_enabled=False)
        with patch("bridge.dashboard.config", cfg):
            # If this hangs the loop never exits; asyncio.run returns instantly on disable
            asyncio.run(self._ds.run())


if __name__ == "__main__":
    unittest.main()
