"""DGN-1720: a PLAIN streamed final is finalized by editing the draft in place.

Owner 2026-09-26: the streamed message must not fade out and be re-sent when
the final answer needs no rich rendering; only a RICH final (buttons,
blockquote / expandable, fenced or inline code, other HTML formatting, ...)
keeps today's swap. The only swap a plain single-bubble final still hit was
the DGN-555 reply-link path (latency / interleave): it now edits in place and
forgoes the link. Edit failure keeps the delete + linked re-send fallback.

Covers:
  1. _streamed_final_rich_reasons classification (plain vs each rich reason).
  2. plain final + reply link -> no delete, no re-send, at most one edit.
  3. rich final (options / quote / inline code / fenced code / bold) + reply
     link -> swap as today.
  4. plain final + edit failure -> delete + linked re-send (text never lost).
  5. multi-bubble (overflow) plain final -> swap as today.
  6. no reply link -> unchanged in-place edit for rich single bubble.
"""

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


def _make_bot(bot_mod, last_incoming=None):
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    bot.application.bot = MagicMock()
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = dict(last_incoming or {})
    bot._send_content_artifacts = AsyncMock()
    return bot


def _make_message(sent, chat_id=42, message_id=100, age_seconds=0,
                  edit_error=None):
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.message_id = message_id
    msg.date = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)

    async def _reply_text(text, parse_mode=None, reply_markup=None,
                          link_preview_options=None, reply_parameters=None):
        sent.append({"text": text, "reply_parameters": reply_parameters})

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    tg = MagicMock()
    msg.get_bot.return_value = tg
    tg.delete_message = AsyncMock()
    tg.edit_message_text = AsyncMock(side_effect=edit_error)
    return msg


def _run(coro):
    return asyncio.run(coro)


def _linked(entry):
    rp = entry["reply_parameters"]
    return None if rp is None else rp.message_id


def _reasons(bot_mod, display, **kw):
    return bot_mod.TelegramBot._streamed_final_rich_reasons(
        kw.get("content", display), display, kw.get("preview", False),
        kw.get("force_options", False), kw.get("notice_meta"),
        kw.get("draft_ids", [7]),
    )


# ---------------------------------------------------------------------------
# 1. Classification
# ---------------------------------------------------------------------------

def test_plain_text_has_no_rich_reason():
    bot_mod = _load_bot()
    assert _reasons(bot_mod, "just a plain answer\n\n- a list\n1. step") == []
    assert _reasons(bot_mod, "a < b & c > d, see https://x.y/?a=1&b=2") == []


def test_each_rich_reason_is_detected():
    bot_mod = _load_bot()
    assert "blockquote" in _reasons(bot_mod, "> quoted")
    assert "blockquote" in _reasons(
        bot_mod, "<blockquote expandable>fold</blockquote>")
    assert "code" in _reasons(bot_mod, "run `ls` now")
    assert "code_block" in _reasons(bot_mod, "x\n```\ncode\n```")
    assert "formatting" in _reasons(bot_mod, "a **bold** word")
    assert "formatting" in _reasons(bot_mod, "[link](https://x.y)")
    assert "buttons" in _reasons(bot_mod, "pick one", force_options=True)
    assert "send_file" in _reasons(
        bot_mod, "here", content="here\nsend_file::files/outbox/a.txt")
    assert "link_preview" in _reasons(bot_mod, "see it", preview=True)
    assert "notice" in _reasons(bot_mod, "hi", notice_meta={"id": "n1"})
    assert "multi_bubble" in _reasons(bot_mod, "hi", draft_ids=[7, 8])
    assert "multi_bubble" in _reasons(bot_mod, "word " * 2000)


# ---------------------------------------------------------------------------
# 2. Plain final + reply link -> in place
# ---------------------------------------------------------------------------

def test_plain_final_with_reply_link_is_not_swapped():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})  # interleave -> link
    sent = []
    msg = _make_message(sent, message_id=100)

    _run(bot._reply_smart(msg, "plain streamed answer",
                          streamed=True, draft_message_ids=[7]))

    tg = msg.get_bot.return_value
    tg.delete_message.assert_not_called()
    assert sent == [], "plain final must not be re-sent"
    # Draft already shows exactly this text -> DGN-376 no-op skip (0 edits).
    assert tg.edit_message_text.await_count <= 1


def test_plain_final_with_reply_link_forced_edit_is_one_edit():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []
    msg = _make_message(sent, message_id=100, age_seconds=999)  # latency link

    _run(bot._reply_smart(msg, "plain assembled answer", streamed=True,
                          draft_message_ids=[7], assembled=True))

    tg = msg.get_bot.return_value
    tg.delete_message.assert_not_called()
    assert sent == []
    tg.edit_message_text.assert_awaited_once()
    kw = tg.edit_message_text.await_args.kwargs
    assert kw["message_id"] == 7
    assert kw["text"] == "plain assembled answer"


# ---------------------------------------------------------------------------
# 3. Rich final + reply link -> swap as today
# ---------------------------------------------------------------------------

def _assert_swapped(msg, sent):
    tg = msg.get_bot.return_value
    tg.delete_message.assert_awaited_once_with(42, 7)
    tg.edit_message_text.assert_not_called()
    assert sent, "rich final must be re-sent"
    assert _linked(sent[0]) == 100


def test_rich_quote_final_swaps():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100)
    _run(bot._reply_smart(msg, "intro\n> quoted line",
                          streamed=True, draft_message_ids=[7]))
    _assert_swapped(msg, sent)
    assert "<blockquote>" in sent[0]["text"]


def test_rich_inline_code_final_swaps():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100)
    _run(bot._reply_smart(msg, "run `ls -la` here",
                          streamed=True, draft_message_ids=[7]))
    _assert_swapped(msg, sent)
    assert "<code>ls -la</code>" in sent[0]["text"]


def test_rich_fenced_code_final_swaps_even_without_link():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []
    msg = _make_message(sent, message_id=100, age_seconds=1)
    _run(bot._reply_smart(msg, "before\n```\nprint(1)\n```\nafter",
                          streamed=True, draft_message_ids=[7]))
    tg = msg.get_bot.return_value
    tg.delete_message.assert_awaited_once_with(42, 7)
    assert len(sent) == 3


def test_rich_options_final_swaps_even_without_link():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []
    msg = _make_message(sent, message_id=100, age_seconds=1)
    content = "Which one?\n1. Alpha\n2. Beta\n[[OPTIONS]]"
    _run(bot._reply_smart(msg, content, force_options=True,
                          streamed=True, draft_message_ids=[7]))
    tg = msg.get_bot.return_value
    tg.delete_message.assert_awaited_once_with(42, 7)
    assert all("Alpha" not in e["text"] for e in sent)


def test_classifier_options_final_with_link_swaps():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100)
    _run(bot._reply_smart(msg, "Which one?\n1. Alpha\n2. Beta\n[[OPTIONS]]",
                          force_options=True, classifier_injected=True,
                          streamed=True, draft_message_ids=[7]))
    _assert_swapped(msg, sent)


# ---------------------------------------------------------------------------
# 4. Edit failure -> fallback swap, never lose text
# ---------------------------------------------------------------------------

def test_plain_final_edit_failure_falls_back_to_swap():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100,
                        edit_error=RuntimeError("Bad Request: boom"))
    _run(bot._reply_smart(msg, "plain answer kept", streamed=True,
                          draft_message_ids=[7], assembled=True))
    tg = msg.get_bot.return_value
    tg.edit_message_text.assert_awaited_once()
    tg.delete_message.assert_awaited_once_with(42, 7)
    assert len(sent) == 1
    assert "plain answer kept" in sent[0]["text"]
    assert _linked(sent[0]) == 100


# ---------------------------------------------------------------------------
# 5. Multi-bubble overflow -> swap as today
# ---------------------------------------------------------------------------

def test_multi_bubble_plain_final_swaps():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100)
    _run(bot._reply_smart(msg, "plain but overflowed",
                          streamed=True, draft_message_ids=[7, 8]))
    tg = msg.get_bot.return_value
    assert tg.delete_message.await_count == 2
    tg.edit_message_text.assert_not_called()
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# 6. No reply link -> rich single bubble keeps today's in-place HTML edit
# ---------------------------------------------------------------------------

def test_rich_single_bubble_without_link_still_edits_in_place():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []
    msg = _make_message(sent, message_id=100, age_seconds=1)
    _run(bot._reply_smart(msg, "intro\n> quoted line",
                          streamed=True, draft_message_ids=[7]))
    tg = msg.get_bot.return_value
    tg.delete_message.assert_not_called()
    tg.edit_message_text.assert_awaited_once()
    assert sent == []
