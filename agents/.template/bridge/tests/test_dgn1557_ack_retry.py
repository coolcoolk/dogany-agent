"""DGN-1557: command acks that follow a REAL committed side effect must
retry, not narrate. Root incident: /new committed its session reset, then
the closing `reply_text(NEW_SESSION)` raised `telegram.error.NetworkError`
on a transient outbound TLS blip; PTB routed the escaped exception to
`_error_handler`, which told the owner "not processed -- please resend" even
though /new HAD processed. Owner decision (2026-09-18 09:22): retry the ack
itself; do NOT invent a second notice variant or a commit-witness map.

Covers, driving the REAL `bot._cmd_new` handler (not a mock of the retry
loop) with a raising fake `update.message.reply_text`:
  (a) first attempt raises NetworkError, second succeeds -> the ack is
      delivered, the committed side effect (session reset) applied exactly
      once, and no exception escapes the handler (so PTB's dispatcher never
      reaches `_error_handler` -- no turn-death notice).
  (b) all `retries` attempts raise -> the handler lets the last exception
      escape (a genuinely dead channel is a dead channel; no bookkeeping
      invents a way around that), and when PTB's real `_error_handler` is
      then driven with that exception, it sends its one pre-existing
      TURN_FAILED notice -- unchanged wording, no traceback, no duplicate.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import telegram.error
from telegram import Update

import bridge.bot as bot_mod
from bridge import messages
from bridge.bot import TelegramBot


async def _instant_sleep(_seconds):
    return None


def _make_bot(monkeypatch):
    bot = object.__new__(TelegramBot)
    bot.application = MagicMock()
    bot._runtime_active_sessions = set()
    monkeypatch.setattr(bot, "_check_access", AsyncMock(return_value=True))
    monkeypatch.setattr(bot_mod.asyncio, "sleep", _instant_sleep)
    return bot


def _make_update(user_id: int, reply_text_mock):
    update = MagicMock(spec=Update)
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_chat = SimpleNamespace(id=user_id)
    update.message = SimpleNamespace(reply_text=reply_text_mock)
    return update


@pytest.fixture
def session_spy(monkeypatch):
    session_manager = SimpleNamespace(
        get_session=AsyncMock(return_value={}),
        update_session=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(bot_mod, "session_manager", session_manager)
    monkeypatch.setattr(
        bot_mod.sdk_bridge, "cancel_user_streaming", AsyncMock(return_value=None)
    )
    return session_manager


@pytest.mark.asyncio
async def test_transient_ack_failure_retries_and_delivers(monkeypatch, session_spy):
    user_id = 4201
    reply_text = AsyncMock(
        side_effect=[telegram.error.NetworkError("connect blip"), None]
    )
    bot = _make_bot(monkeypatch)
    update = _make_update(user_id, reply_text)

    # Must not raise: the transient failure is swallowed by the retry, so no
    # exception escapes to PTB's dispatcher.
    await bot._cmd_new(update, SimpleNamespace(args=[]))

    # (a) the ack was delivered -- exactly one retry, both attempts carried
    # the real ack text.
    assert reply_text.await_count == 2
    for call in reply_text.await_args_list:
        assert call.args == (messages.NEW_SESSION,)

    # (b) the committed side effect (session reset) applied exactly once,
    # not once per send attempt.
    assert session_spy.update_session.await_count == 1
    committed_session = session_spy.update_session.await_args.args[1]
    assert committed_session["session_id"] is None
    assert committed_session["new_session"] is True


@pytest.mark.asyncio
async def test_transient_ack_failure_never_reaches_error_handler(
    monkeypatch, session_spy
):
    user_id = 4202
    reply_text = AsyncMock(
        side_effect=[telegram.error.NetworkError("connect blip"), None]
    )
    bot = _make_bot(monkeypatch)
    update = _make_update(user_id, reply_text)
    error_handler_spy = AsyncMock()
    monkeypatch.setattr(bot, "_error_handler", error_handler_spy)

    await bot._cmd_new(update, SimpleNamespace(args=[]))

    # (c) no turn-death notice: _error_handler is a PTB-dispatcher-only path,
    # only ever reached when a handler raises. Nothing raised here.
    error_handler_spy.assert_not_called()


@pytest.mark.asyncio
async def test_dead_channel_falls_through_to_single_turn_death_notice(
    monkeypatch, session_spy
):
    user_id = 4203
    dead_error = telegram.error.NetworkError("connect blip")
    reply_text = AsyncMock(side_effect=dead_error)
    bot = _make_bot(monkeypatch)
    update = _make_update(user_id, reply_text)

    caught = None
    try:
        await bot._cmd_new(update, SimpleNamespace(args=[]))
    except Exception as e:  # noqa: BLE001 -- asserting exactly what escapes
        caught = e

    # A genuinely dead channel (every attempt fails) is not swallowed into
    # silence: it escapes exactly like an unguarded reply_text always did,
    # so PTB's own dispatcher still gets a chance to react.
    assert caught is dead_error

    # The committed side effect still applied exactly once -- retrying (or
    # failing to retry) the ACK never re-runs the command body.
    assert session_spy.update_session.await_count == 1

    # Drive the REAL _error_handler exactly as PTB would on an escaped
    # exception, on a separate (working) bot channel.
    fake_context_bot = SimpleNamespace(send_message=AsyncMock())
    fake_context = SimpleNamespace(error=caught, bot=fake_context_bot)
    await bot._error_handler(update, fake_context)

    fake_context_bot.send_message.assert_awaited_once_with(
        update.effective_chat.id, messages.TURN_FAILED
    )
    sent_text = fake_context_bot.send_message.await_args.args[1]
    assert "Traceback" not in sent_text
    assert "NetworkError" not in sent_text
