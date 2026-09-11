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
    RECREATE_COOLDOWN,
    _is_chat_gone,
    _is_message_gone,
    _is_not_modified,
    _is_undeletable,
    _needs_recreate,
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
        ds._save_state()
        data = json.loads(self._state.read_text(encoding="utf-8"))
        self.assertEqual(data, {"chat_id": 1001, "message_id": 9001})

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

    def test_unpin_failure_is_swallowed(self):
        self._fake_send(9999)
        self._bot.unpin_chat_message = AsyncMock(
            side_effect=telegram.error.TelegramError("unpin refused")
        )
        self._recreate(old_msg=1111)  # must not raise
        self.assertFalse(self._ds._dirty)

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
