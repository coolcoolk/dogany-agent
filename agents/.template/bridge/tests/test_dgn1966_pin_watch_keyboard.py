"""DGN-1966: one-tap standing-watch toggles on the workbench pin.

Bridge half of the contract:
  * DashboardSync attaches the dashboard-actions.json keyboard on edit, send
    and recreate -- only when its text_sha matches the dashboard.md text it
    read; a stale / foreign / malformed file means no keyboard, never a
    blocked pin.  No keyboard -> no reply_markup kwarg (pre-DGN-1966 calls).
  * The rotation digest covers the keyboard.
  * wb:watch:<id>:on|off taps are owner-gated (_check_access), forwarded to
    workbench_tap.toggle_watch, then the board refresh; they never expire
    by age and never touch the DGN-1934 live-menu registry.
Offline: fake bot, temp files, patched owner resolution.
"""

import asyncio
import hashlib
import json
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardMarkup

import bridge.tests.conftest  # noqa: F401
from bridge import bot as bot_mod
from bridge import ownership, workbench_tap
from bridge.dashboard import (
    ACTIONS_FILE_NAME,
    DashboardSync,
    _digest,
    _load_pin_rows,
    _view_key,
)

OWNER_ID = 4242
BOARD = "Workbench (updated 10:00)\n\n[Standing watches]\n- PO reply watch: on\n"
ROWS = [[{"text": "Turn off PO reply watch",
          "callback_data": "wb:watch:po-reply:off"}]]


def _write(dirpath, text=BOARD, rows=ROWS, sha=None):
    (dirpath / "dashboard.md").write_text(text, encoding="utf-8")
    (dirpath / ACTIONS_FILE_NAME).write_text(json.dumps({
        "version": 1,
        "text_sha": sha or hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "rows": rows,
    }), encoding="utf-8")


def _bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=77))
    bot.pin_chat_message = AsyncMock()
    bot.unpin_chat_message = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


def _tick(ds, dirpath):
    cfg = types.SimpleNamespace(dashboard_enabled=True,
                                allowed_user_ids=[OWNER_ID],
                                bot_data_dir=dirpath)
    with patch("bridge.dashboard.ownership.resolve_owner",
               return_value=(ownership.MODE_AUTHORITATIVE, None)), \
         patch("bridge.dashboard.config", cfg):
        asyncio.run(ds._tick())


def _markup_data(kwargs):
    markup = kwargs.get("reply_markup")
    assert isinstance(markup, InlineKeyboardMarkup)
    return [[(b.text, b.callback_data) for b in row]
            for row in markup.inline_keyboard]


# --- the actions file --------------------------------------------------------

def test_rows_bound_to_text_sha(tmp_path):
    _write(tmp_path)
    path = tmp_path / ACTIONS_FILE_NAME
    assert _load_pin_rows(path, BOARD) == [[("Turn off PO reply watch",
                                            "wb:watch:po-reply:off")]]
    assert _load_pin_rows(path, BOARD + "x") == []          # other board
    _write(tmp_path, rows=[[{"text": "x", "callback_data": "opt:1"}]])
    assert _load_pin_rows(path, BOARD) == []                # not a pin button
    _write(tmp_path, rows=[[{"text": "x", "callback_data": "wb:" + "a" * 70}]])
    assert _load_pin_rows(path, BOARD) == []                # over 64 bytes
    path.write_text("{broken", encoding="utf-8")
    assert _load_pin_rows(path, BOARD) == []
    assert _load_pin_rows(tmp_path / "absent.json", BOARD) == []


def test_digest_covers_the_keyboard():
    rows = [[("a", "wb:watch:x:off")]]
    assert _view_key(BOARD, []) == BOARD                    # legacy digest kept
    assert _digest(_view_key(BOARD, rows)) != _digest(BOARD)


# --- DashboardSync: edit / send carry the keyboard ---------------------------

def test_recreate_and_edit_attach_the_keyboard(tmp_path):
    bot = _bot()
    ds = DashboardSync(bot=bot, turn_active=lambda uid: False,
                       dashboard_path=tmp_path / "dashboard.md",
                       state_path=tmp_path / "state.json")
    _write(tmp_path)
    _tick(ds, tmp_path)
    send_kwargs = bot.send_message.await_args.kwargs
    assert _markup_data(send_kwargs) == [[("Turn off PO reply watch",
                                          "wb:watch:po-reply:off")]]
    assert ds._digest == _digest(_view_key(
        BOARD, [[("Turn off PO reply watch", "wb:watch:po-reply:off")]]))

    ds._edit_guard = type(ds._edit_guard)()  # no min-interval wait
    text2 = BOARD.replace(": on", ": off")
    rows2 = [[{"text": "Turn on PO reply watch",
               "callback_data": "wb:watch:po-reply:on"}]]
    _write(tmp_path, text=text2, rows=rows2)
    ds._last_synced_mtime = None
    _tick(ds, tmp_path)
    edit_kwargs = bot.edit_message_text.await_args.kwargs
    assert edit_kwargs["message_id"] == 77
    assert _markup_data(edit_kwargs) == [[("Turn on PO reply watch",
                                          "wb:watch:po-reply:on")]]


def test_stale_actions_file_means_no_keyboard(tmp_path):
    bot = _bot()
    ds = DashboardSync(bot=bot, turn_active=lambda uid: False,
                       dashboard_path=tmp_path / "dashboard.md",
                       state_path=tmp_path / "state.json")
    _write(tmp_path, sha="0" * 64)
    _tick(ds, tmp_path)
    assert bot.send_message.await_count == 1
    assert "reply_markup" not in bot.send_message.await_args.kwargs
    assert ds._digest == _digest(BOARD)


# --- the callback ------------------------------------------------------------

def _cb_update(data, user_id=OWNER_ID):
    chat = SimpleNamespace(id=OWNER_ID)
    msg = SimpleNamespace(chat=chat, message_id=77, caption=None, text=BOARD,
                          reply_markup=None,
                          date=datetime.now(timezone.utc) - timedelta(days=4))
    query = MagicMock()
    query.data = data
    query.message = msg
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return SimpleNamespace(effective_chat=chat,
                           effective_user=SimpleNamespace(id=user_id),
                           message=None, callback_query=query), query


def _make_bot():
    b = bot_mod.TelegramBot()
    b.application = SimpleNamespace(bot=MagicMock())
    return b


def test_parse_watch():
    assert workbench_tap.parse_watch("wb:watch:po-reply:off") == ("po-reply", False)
    assert workbench_tap.parse_watch("wb:watch:po-reply:on") == ("po-reply", True)
    for bad in ("wb:watch:PO:on", "wb:watch:x:maybe", "wb:watch::on",
                "wb:watch:" + "a" * 33 + ":on", "opt:1", None):
        assert workbench_tap.parse_watch(bad) is None, bad


def test_pin_buttons_never_expire():
    assert bot_mod._callback_ttl("wb:watch:po-reply:off") is None
    assert "wb:" in bot_mod.CALLBACK_TTL_SECONDS


@pytest.mark.asyncio
async def test_owner_tap_toggles_then_refreshes():
    b = _make_bot()
    upd, query = _cb_update("wb:watch:po-reply:off")
    reg = MagicMock()
    with patch.object(bot_mod.config, "allowed_user_ids", [OWNER_ID]), \
         patch.object(workbench_tap, "toggle_watch",
                      return_value=(0, "po-reply: off")) as toggle, \
         patch.object(workbench_tap, "refresh_board") as refresh, \
         patch.object(b, "_live_menu_registry", return_value=reg):
        await b._handle_callback(upd, MagicMock())
    toggle.assert_called_once_with(bot_mod.PROJECT_ROOT, "po-reply", False)
    refresh.assert_called_once_with(bot_mod.PROJECT_ROOT)
    assert reg.mock_calls == []                 # not a decision menu
    query.edit_message_text.assert_not_awaited()  # the board refresh re-renders


@pytest.mark.asyncio
async def test_stranger_tap_does_nothing():
    b = _make_bot()
    upd, query = _cb_update("wb:watch:po-reply:off", user_id=999)
    with patch.object(bot_mod.config, "allowed_user_ids", [OWNER_ID]), \
         patch.object(workbench_tap, "toggle_watch") as toggle, \
         patch.object(workbench_tap, "refresh_board") as refresh:
        await b._handle_callback(upd, MagicMock())
    toggle.assert_not_called()
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_tap_is_ignored():
    b = _make_bot()
    upd, _ = _cb_update("wb:watch:../../x:on")
    with patch.object(bot_mod.config, "allowed_user_ids", [OWNER_ID]), \
         patch.object(workbench_tap, "toggle_watch") as toggle, \
         patch.object(workbench_tap, "refresh_board") as refresh:
        await b._handle_callback(upd, MagicMock())
    toggle.assert_not_called()
    refresh.assert_not_called()


def test_toggle_runs_the_host_lib(tmp_path):
    assert workbench_tap.toggle_watch(tmp_path, "po-reply", True)[0] == 127
    lib = tmp_path / "routines" / "lib"
    lib.mkdir(parents=True)
    (lib / "watches.py").write_text(
        "import sys; print(' '.join(sys.argv[1:]))\n", encoding="utf-8")
    rc, out = workbench_tap.toggle_watch(tmp_path, "po-reply", True)
    assert (rc, out) == (0, "toggle po-reply on --by owner-tap")
