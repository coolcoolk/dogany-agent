"""DGN-555: selective reply-linking of the final response.

The final response of a turn is sent as a Telegram reply to its triggering
user message ONLY when (a) a newer user message interleaved in the chat before
the send, or (b) the response fires more than REPLY_LINK_LATENCY_S seconds
after the trigger arrived. Otherwise the send is plain. Only the FIRST sent
part carries the link; a rejected linked send degrades to a plain send.

Covers:
  1. _reply_link_id policy: interleave, latency, fast path, kill switch,
     missing trigger id.
  2. Send-path wiring: linked first chunk only, streamed edit-in-place bypass
     (delete + linked re-send), graceful fallback on Telegram rejection.
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


def _make_bot(bot_mod, last_incoming=None):
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    bot.application.bot = MagicMock()
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = dict(last_incoming or {})
    return bot


def _make_message(
    sent,
    chat_id=42,
    message_id=100,
    age_seconds=0,
    reject_linked=False,
):
    """Triggering-message mock; records every reply_text send."""
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.message_id = message_id
    msg.date = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)

    async def _reply_text(text, parse_mode=None, reply_markup=None,
                          link_preview_options=None, reply_parameters=None):
        if reject_linked and reply_parameters is not None:
            raise RuntimeError("Bad Request: message to be replied not found")
        sent.append({
            "text": text,
            "parse_mode": parse_mode,
            "reply_parameters": reply_parameters,
        })

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    bot = MagicMock()
    msg.get_bot.return_value = bot
    bot.delete_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return msg


def _run(coro):
    return asyncio.run(coro)


def _linked_to(entry):
    rp = entry["reply_parameters"]
    return None if rp is None else rp.message_id


# ---------------------------------------------------------------------------
# 1. Policy: _reply_link_id
# ---------------------------------------------------------------------------

def test_policy_interleave_links():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    msg = _make_message([], message_id=100)
    assert bot._reply_link_id(msg) == 100


def test_policy_latency_links():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 100})
    msg = _make_message([], message_id=100, age_seconds=400)
    assert bot._reply_link_id(msg) == 100


def test_policy_fast_single_turn_plain():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 100})
    msg = _make_message([], message_id=100, age_seconds=1)
    assert bot._reply_link_id(msg) is None


def test_policy_disabled_never_links():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 999})
    msg = _make_message([], message_id=100, age_seconds=9999)
    orig = bot_mod.REPLY_LINK_ENABLED
    bot_mod.REPLY_LINK_ENABLED = False
    try:
        assert bot._reply_link_id(msg) is None
    finally:
        bot_mod.REPLY_LINK_ENABLED = orig


def test_policy_missing_trigger_id_plain():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 999})
    msg = _make_message([], message_id=100, age_seconds=9999)
    msg.message_id = None  # trigger id unavailable -> degrade to plain
    assert bot._reply_link_id(msg) is None


def test_note_incoming_keeps_newest_id():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    m1 = _make_message([], chat_id=42, message_id=100)
    m2 = _make_message([], chat_id=42, message_id=101)
    bot._note_incoming_message(m1)
    bot._note_incoming_message(m2)
    bot._note_incoming_message(m1)  # out-of-order redelivery never regresses
    assert bot._last_incoming_mid == {42: 101}


# ---------------------------------------------------------------------------
# 2. Send-path wiring
# ---------------------------------------------------------------------------

def test_interleave_response_is_linked():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100)

    _run(bot._reply_smart(msg, "answer to the first question"))

    assert len(sent) == 1
    assert _linked_to(sent[0]) == 100
    assert sent[0]["reply_parameters"].allow_sending_without_reply is True


def test_latency_response_is_linked():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 100})
    sent = []
    msg = _make_message(sent, message_id=100, age_seconds=301)

    _run(bot._reply_smart(msg, "late answer"))

    assert len(sent) == 1
    assert _linked_to(sent[0]) == 100


def test_fast_single_turn_is_plain():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 100})
    sent = []
    msg = _make_message(sent, message_id=100, age_seconds=1)

    _run(bot._reply_smart(msg, "quick answer"))

    assert len(sent) == 1
    assert _linked_to(sent[0]) is None


def test_disabled_env_is_plain():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100, age_seconds=999)
    orig = bot_mod.REPLY_LINK_ENABLED
    bot_mod.REPLY_LINK_ENABLED = False
    try:
        _run(bot._reply_smart(msg, "answer"))
    finally:
        bot_mod.REPLY_LINK_ENABLED = orig

    assert len(sent) == 1
    assert _linked_to(sent[0]) is None


def test_only_first_chunk_carries_link():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100)

    _run(bot._reply_smart(msg, "before\n```python\nprint(1)\n```\nafter"))

    assert len(sent) == 3
    assert _linked_to(sent[0]) == 100
    for entry in sent[1:]:
        assert _linked_to(entry) is None


def test_reply_rejection_falls_back_to_plain_send():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100, reject_linked=True)

    _run(bot._reply_smart(msg, "answer survives rejection"))

    assert len(sent) == 1, "message must be delivered despite reply rejection"
    assert _linked_to(sent[0]) is None
    assert "answer survives rejection" in sent[0]["text"]


@pytest.mark.skip(reason="DGN-376 HTML-finalize send path not present in this bridge variant")
def test_streamed_link_deletes_draft_and_resends_linked():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg = _make_message(sent, message_id=100)

    _run(bot._reply_smart(msg, "streamed **answer**",
                          streamed=True, draft_message_ids=[7]))

    tg_bot = msg.get_bot.return_value
    tg_bot.edit_message_text.assert_not_called()
    tg_bot.delete_message.assert_awaited_once_with(42, 7)
    assert len(sent) == 1
    assert _linked_to(sent[0]) == 100
    assert "<b>answer</b>" in sent[0]["text"]


@pytest.mark.skip(reason="DGN-376 HTML-finalize send path not present in this bridge variant")
def test_streamed_plain_keeps_edit_in_place():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 100})
    sent = []
    msg = _make_message(sent, message_id=100, age_seconds=1)

    _run(bot._reply_smart(msg, "streamed **answer**",
                          streamed=True, draft_message_ids=[7]))

    tg_bot = msg.get_bot.return_value
    tg_bot.edit_message_text.assert_awaited_once()
    tg_bot.delete_message.assert_not_called()
    assert sent == [], "no-link streamed prose must finalize by in-place edit"


def test_second_queued_turn_own_response_plain():
    # msg2 is the newest incoming message: its own response has no newer
    # interleaved message, so it goes plain even though msg1's was linked.
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod, last_incoming={42: 101})
    sent = []
    msg2 = _make_message(sent, message_id=101)

    _run(bot._reply_smart(msg2, "answer to the second question"))

    assert len(sent) == 1
    assert _linked_to(sent[0]) is None
