"""DGN-1906: usage-limit classification, one notice per window, replay."""

import asyncio
import hashlib
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge import messages
from bridge.bot import TelegramBot
from bridge.sdk_bridge import ChatResponse, _classify_error_result
from bridge.usage_limit import LimitGate, parse_reset, is_usage_limit_text

# The four observed strings (two live instances) plus a model-specific one.
OBSERVED = [
    "You've hit your session limit · resets 7:40pm (Asia/Seoul)",
    "You've hit your session limit · resets 7:40pm (Asia/Seoul)",
    "You've hit your weekly limit · resets 1pm (Asia/Seoul)",
    "You've hit your weekly limit · resets 1pm (Asia/Seoul)",
    "You've hit your Opus limit · resets Oct 12, 9am (Asia/Seoul)",
]

# DRAFT copy gate. APPROVED_HASH is None until the owner confirms; once set,
# it must equal the sha256 of the en+ko copy and the marker must be gone.
APPROVED_HASH = None  # set to a set of sha256 hex digests on approval
MARKER = "WORDING PENDING OWNER CONFIRMATION"
BRIDGE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("text", OBSERVED)
def test_classified_usage_limit(text):
    assert is_usage_limit_text(text)
    assert _classify_error_result(text) == "usage_limit"


def test_other_errors_unchanged():
    assert _classify_error_result("API Error: 529 overloaded") == "transient"
    assert _classify_error_result("boom") == "other"
    assert not is_usage_limit_text("Here is how limits work in Python " * 20)


def test_reset_parse_session_and_date():
    now = time.mktime((2026, 9, 20, 18, 1, 6, 0, 0, 0))
    ep, label = parse_reset(OBSERVED[0], now)
    assert label == "7:40pm (Asia/Seoul)"
    assert ep is not None and ep > now
    ep2, label2 = parse_reset(OBSERVED[4], now)
    assert label2.startswith("Oct 12")
    assert ep2 > ep


def test_one_notice_per_window_and_queue():
    g = LimitGate()
    now = 1_000_000.0
    send1, until, _ = g.hit(1, "You've hit your session limit (no time)", "m1", now)
    send2, until2, _ = g.hit(1, "You've hit your session limit", "m2", now + 5)
    assert send1 is True and send2 is False and until == until2
    assert g.active_until(1, now + 10) == until
    assert g.drain(1) == ["m1", "m2"]
    assert g.active_until(1, now + 10) is None


def test_new_window_after_expiry_notifies_again():
    g = LimitGate()
    t = "You've hit your session limit"
    assert g.hit(1, t, "a", 0.0)[0] is True
    assert g.hit(1, t, "b", 4000.0)[0] is True


def _bot():
    bot = TelegramBot.__new__(TelegramBot)
    bot._send_guaranteed = AsyncMock()
    bot.application = MagicMock()
    return bot


def test_bot_notice_once_and_replay_after_reset(monkeypatch):
    import bridge.bot as bot_mod
    gate = LimitGate()
    monkeypatch.setattr(bot_mod, "limit_gate", gate)
    bot = _bot()
    resp = ChatResponse(content="", success=False, error=OBSERVED[0],
                        error_kind="usage_limit")

    async def go():
        await bot._usage_limit_hit(7, 7, "lyrics?", resp)
        await bot._usage_limit_hit(7, 7, "second", resp)
        return None

    asyncio.run(go())
    assert bot._send_guaranteed.await_count == 1
    sent = bot._send_guaranteed.await_args.args[1]
    assert "Asia/Seoul" in sent
    assert "You've hit" not in sent

    # Replay: window passes, queued requests answered in order, no loop.
    answers = []
    ok = ChatResponse(content="answer", success=True)
    monkeypatch.setattr(bot_mod.sdk_bridge, "process_message",
                        AsyncMock(return_value=ok))
    monkeypatch.setattr(bot_mod.session_manager, "get_session",
                        AsyncMock(return_value={}))
    bot._effective_session_id = lambda u, s: None
    bot._send_smart = AsyncMock(side_effect=lambda cid, c, **k: answers.append(c))
    bot._proactive_push = None
    bot._permission_callback = None
    monkeypatch.setattr(bot_mod.asyncio, "sleep", AsyncMock())
    asyncio.run(bot._usage_limit_replay(7, 7, time.time() - 1))
    assert answers == ["answer", "answer"]
    calls = [c.kwargs["user_message"]
             for c in bot_mod.sdk_bridge.process_message.await_args_list]
    assert calls == ["lyrics?", "second"]
    assert gate.active_until(7) is None


def test_window_replace_keeps_queue_and_old_replay_stands_down():
    """DGN-1967 bug 3: a new hit after the window ended (before the replay
    drained it) opens a new window that keeps the earlier queue, and the
    earlier window's replay no longer drains the new one."""
    g = LimitGate()
    t = "You've hit your session limit"
    g.hit(1, t, "a", 0.0)
    old = g.generation(1)
    assert g.hit(1, t, "b", 3602.0)[0] is True
    assert g.generation(1) != old
    assert g.drain(1, old) == []
    assert g.drain(1, g.generation(1)) == ["a", "b"]


def _replay_bot(monkeypatch, responses):
    import bridge.bot as bot_mod
    gate = LimitGate()
    monkeypatch.setattr(bot_mod, "limit_gate", gate)
    bot = _bot()
    monkeypatch.setattr(bot_mod.sdk_bridge, "process_message",
                        AsyncMock(side_effect=list(responses)))
    monkeypatch.setattr(bot_mod.session_manager, "get_session",
                        AsyncMock(return_value={}))
    monkeypatch.setattr(bot_mod.session_manager, "update_session", AsyncMock())
    bot._effective_session_id = lambda u, s: None
    bot._runtime_active_sessions = set()
    bot._proactive_push = None
    bot._permission_callback = None
    bot._send_smart = AsyncMock()
    bot._reply_smart = AsyncMock()
    monkeypatch.setattr(bot_mod.asyncio, "sleep", AsyncMock())
    return bot_mod, gate, bot


def test_transient_retry_hitting_limit_is_queued(monkeypatch):
    """DGN-1967 bug 4: the one transient retry comes back usage_limit with
    empty content -> the request is queued and the notice sent, never an
    empty reply."""
    transient = ChatResponse(content="", success=False, error="API Error: 529",
                             error_kind="transient")
    limited = ChatResponse(content="", success=False, error=OBSERVED[0],
                           error_kind="usage_limit")
    bot_mod, gate, bot = _replay_bot(monkeypatch, [transient, limited])
    monkeypatch.setattr(bot_mod.session_manager, "should_start_new_session",
                        AsyncMock(return_value=False))
    monkeypatch.setattr(bot_mod.session_manager, "set_last_user_message_at",
                        AsyncMock())
    bot._get_real_model = lambda s: None
    bot._message_ts = lambda m: 0.0
    bot._with_outbound_context = lambda text, cid, m: "[ctx]\n" + text
    bot._usage_limit_replay = AsyncMock()
    message = MagicMock()
    message.chat.send_action = AsyncMock()
    message.reply_text = AsyncMock()
    message.message_id = 42
    message.message_thread_id = None
    update = MagicMock()
    update.message = message
    update.effective_chat.id = 7
    monkeypatch.setattr(bot_mod.model_state, "take_start_notice", lambda: None)
    asyncio.run(bot._process_user_message_text(update, 7, "commit",
                                               forwarded=False, voice=False))
    bot._reply_smart.assert_not_awaited()
    assert bot._send_guaranteed.await_count == 1
    assert gate.active_until(7) is not None
    (item,) = gate.drain(7)
    assert item["turn_text"] == "[ctx]\ncommit"
    assert item["inbound"]["message_id"] == 42


def test_replay_sends_turn_text_and_inbound(monkeypatch):
    """DGN-1967 bug 5: the replay re-runs the turn as the owner sent it
    (outbound context + inbound binding), saves the session id, and goes
    through the live path's turn-finish handling."""
    ok = ChatResponse(content="done", success=True, session_id="s-1")
    bot_mod, gate, bot = _replay_bot(monkeypatch, [ok])
    item = {"text": "commit", "turn_text": "[ctx]\ncommit",
            "inbound": {"chat_id": 7, "message_id": 42, "source": "message"}}
    gate.hit(7, OBSERVED[0], item)
    asyncio.run(bot._usage_limit_replay(7, 7, time.time() - 1))
    kw = bot_mod.sdk_bridge.process_message.await_args.kwargs
    assert kw["user_message"] == "[ctx]\ncommit"
    assert kw["inbound"]["message_id"] == 42
    bot_mod.session_manager.update_session.assert_awaited()
    assert 7 in bot._runtime_active_sessions
    assert bot._send_smart.await_args.args[1] == "done"


def test_replay_retry_offer_gets_notice_not_raw_error(monkeypatch):
    """DGN-1967 bug 5: an error result on replay takes the retry notice,
    like the live path, instead of sending the error text as an answer."""
    err = ChatResponse(content="boom", success=False, error="boom",
                       retry_offer=True)
    bot_mod, gate, bot = _replay_bot(monkeypatch, [err])
    bot._send_retry_notice = AsyncMock()
    gate.hit(7, OBSERVED[0], "hi")
    asyncio.run(bot._usage_limit_replay(7, 7, time.time() - 1))
    bot._send_smart.assert_not_awaited()
    assert bot._send_retry_notice.await_args.kwargs["user_message"] == "hi"


def test_draft_marker_pinned_to_approval_hash():
    """Marker may only be removed together with an approval hash."""
    for f in ("en.py", "ko.py"):
        src = (BRIDGE / "i18n" / f).read_text(encoding="utf-8")
        m = re.search(r'"usage_limit_notice":\s*\((.*?)\n    \),', src, re.S)
        assert m, f
        # the comment block directly above the key
        block = src[max(0, m.start() - 400):m.start()] + m.group(0)
        if APPROVED_HASH is None:
            assert MARKER in block, f"{f}: marker removed without approval hash"
        else:
            assert MARKER not in block
            copy = hashlib.sha256(m.group(1).encode()).hexdigest()
            assert copy in APPROVED_HASH, f"{f}: copy changed after approval"
