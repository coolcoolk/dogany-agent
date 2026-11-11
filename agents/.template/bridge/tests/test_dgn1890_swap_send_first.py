"""DGN-1890: a draft swap sends the new message FIRST, then removes the draft.

Owner 2026-10-06: "the answer pops out, vanishes, then comes back a few
seconds later" -- a formatted final (quote / code / bold / buttons) that is swapped instead of
edited in place deleted the live draft and only then sent the replacement,
so the chat showed an empty gap of several seconds (an agent's long turns hit the
DGN-555 latency reply link -> DGN-1720 rich swap on nearly every answer).

Every swap branch of _reply_smart and _send_smart must order the calls
send -> delete, and a send that fails outright leaves the draft standing
(the text never vanishes).
"""

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")


def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


def _run(coro):
    return asyncio.run(coro)


def _record_delete(events):
    async def _delete(chat_id=None, message_id=None, *a, **kw):
        mid = message_id if message_id is not None else (a[0] if a else None)
        events.append(("delete", mid))
    return _delete


# ---------------------------------------------------------------------------
# _reply_smart (model-turn rail)
# ---------------------------------------------------------------------------

def _reply_rig(bot_mod, events, last_incoming=None, age_seconds=0,
               edit_error=None, send_error=None):
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    bot._last_incoming_mid = dict(last_incoming or {})
    bot._send_content_artifacts = AsyncMock()
    msg = MagicMock()
    msg.chat.id = 42
    msg.message_id = 100
    msg.date = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)

    async def _reply_text(text, **kw):
        if send_error is not None:
            raise send_error
        events.append(("send", text))
        return MagicMock(message_id=500 + len(events))

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    tg = MagicMock()
    msg.get_bot.return_value = tg
    tg.delete_message = AsyncMock(side_effect=_record_delete(events))
    tg.edit_message_text = AsyncMock(side_effect=edit_error)
    return bot, msg


def _assert_send_then_delete(events, deleted):
    kinds = [k for k, _ in events]
    assert "send" in kinds, events
    assert sorted(m for k, m in events if k == "delete") == sorted(deleted)
    first_delete = kinds.index("delete")
    last_send = len(kinds) - 1 - kinds[::-1].index("send")
    assert last_send < first_delete, (
        "draft removed before the replacement landed: %r" % (events,))


def test_reply_rich_quote_with_link_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot, msg = _reply_rig(bot_mod, events, last_incoming={42: 101})
    _run(bot._reply_smart(msg, "intro **bold**\n> quoted line",
                          streamed=True, draft_message_ids=[7]))
    _assert_send_then_delete(events, [7])


def test_reply_latency_link_rich_final_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot, msg = _reply_rig(bot_mod, events, age_seconds=9999)
    _run(bot._reply_smart(msg, "a **bold** answer",
                          streamed=True, draft_message_ids=[7]))
    _assert_send_then_delete(events, [7])


def test_reply_fenced_code_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot, msg = _reply_rig(bot_mod, events, age_seconds=1)
    _run(bot._reply_smart(msg, "before\n```\nprint(1)\n```\nafter",
                          streamed=True, draft_message_ids=[7]))
    _assert_send_then_delete(events, [7])
    assert sum(1 for k, _ in events if k == "send") == 3


def test_reply_consumed_options_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot, msg = _reply_rig(bot_mod, events, age_seconds=1)
    _run(bot._reply_smart(msg, "Which one?\n1. Alpha\n2. Beta\n[[OPTIONS]]",
                          force_options=True, streamed=True,
                          draft_message_ids=[7]))
    _assert_send_then_delete(events, [7])


def test_reply_edit_failure_fallback_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot, msg = _reply_rig(bot_mod, events, age_seconds=1,
                          edit_error=RuntimeError("Bad Request: boom"))
    _run(bot._reply_smart(msg, "plain answer kept", streamed=True,
                          draft_message_ids=[7], assembled=True))
    _assert_send_then_delete(events, [7])


def test_reply_multi_bubble_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot, msg = _reply_rig(bot_mod, events, last_incoming={42: 101})
    _run(bot._reply_smart(msg, "plain but overflowed",
                          streamed=True, draft_message_ids=[7, 8]))
    _assert_send_then_delete(events, [7, 8])


def test_reply_send_failure_keeps_draft():
    bot_mod = _load_bot()
    events = []
    bot, msg = _reply_rig(bot_mod, events, last_incoming={42: 101},
                          send_error=RuntimeError("network down"))
    with pytest.raises(RuntimeError):
        _run(bot._reply_smart(msg, "intro\n> quoted line",
                              streamed=True, draft_message_ids=[7]))
    assert events == [], "draft deleted although nothing replaced it"


# ---------------------------------------------------------------------------
# _send_smart (bare-chat rail)
# ---------------------------------------------------------------------------

def _chat_rig(bot_mod, events, edit_error=None):
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    tg = bot.application.bot

    async def _send_message(chat_id, text, **kw):
        events.append(("send", text))
        return MagicMock(message_id=500 + len(events))

    tg.send_message = AsyncMock(side_effect=_send_message)
    tg.delete_message = AsyncMock(side_effect=_record_delete(events))
    tg.edit_message_text = AsyncMock(side_effect=edit_error)
    bot._send_content_artifacts = AsyncMock()
    return bot


def test_chat_fenced_code_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot = _chat_rig(bot_mod, events)
    _run(bot._send_smart(42, "before\n```\nprint(1)\n```\nafter",
                         streamed=True, draft_message_ids=[7]))
    _assert_send_then_delete(events, [7])


def test_chat_consumed_options_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot = _chat_rig(bot_mod, events)
    _run(bot._send_smart(42, "Which one?\n1. Alpha\n2. Beta\n[[OPTIONS]]",
                         force_options=True, streamed=True,
                         draft_message_ids=[7]))
    _assert_send_then_delete(events, [7])


def test_chat_edit_failure_fallback_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot = _chat_rig(bot_mod, events,
                    edit_error=RuntimeError("Bad Request: boom"))
    _run(bot._send_smart(42, "plain answer kept", streamed=True,
                         draft_message_ids=[7], assembled=True))
    _assert_send_then_delete(events, [7])


def test_chat_multi_bubble_sends_before_delete():
    bot_mod = _load_bot()
    events = []
    bot = _chat_rig(bot_mod, events)
    _run(bot._send_smart(42, "plain but overflowed",
                         streamed=True, draft_message_ids=[7, 8]))
    _assert_send_then_delete(events, [7, 8])
