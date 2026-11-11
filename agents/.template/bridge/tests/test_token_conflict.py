# test-component: bridge token_conflict (contended bot token: Conflict-streak notice, local holder lookup, torn-down-application message path)
# test-tier: 0
# test-prereq: tmp-files
# test-new-component: token_conflict.py is a new module with no existing suite; its cases also cross the bot.py handler seam, so they stay together rather than split into the polling or mint suites
"""DGN-2147: a fresh install whose bot token is contended.

Live: every message answered "'NoneType' object has no attribute 'bot'". The
Conflict retry cycle sets TelegramBot.application = None between polling
lifecycles, and _process_user_message_text reached the bot through
self.application (via _turn_kwargs).

Promises (each case <= 1 s, fixtures only):
  P1 a message handled while application is None never shows a raw Python
     error to the user.
  P2 that message is kept and answered once the application is back.
  P3 no user-facing error copy interpolates exception text (generic_error,
     processing_failed, resume_failed, both locales).
  P4 a sustained Conflict streak yields exactly ONE owner notice per streak,
     naming another local agent on the same token when one is running.
  P5 other local holders of a token are found by instance lock + .env.
"""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import telegram

from bridge import bot as bot_mod
from bridge import messages, token_conflict
from bridge.i18n import en, ko

TOKEN = "123456:" + "A" * 35


def _update(text="hi"):
    bot = telegram.Bot(TOKEN)  # never initialised: a torn-down application's bot
    return telegram.Update.de_json({
        "update_id": 1,
        "message": {"message_id": 5, "date": 0, "text": text,
                    "chat": {"id": 1, "type": "private"},
                    "from": {"id": 1, "is_bot": False, "first_name": "a"}},
    }, bot)


def _handler_env(bot, sent):
    """Patches that let _process_user_message_text run without a session store."""
    sm = SimpleNamespace(
        get_session=AsyncMock(return_value={"model": "m"}),
        should_start_new_session=AsyncMock(return_value=False),
        update_session=AsyncMock(), set_last_user_message_at=AsyncMock())

    async def fake_process(user_message, **kw):
        sent.append(("turn", user_message, kw["bot"]))
        return SimpleNamespace(content="ok", has_options=False, streamed=False,
                               draft_message_ids=None)

    async def fake_reply(self, message, text, **kw):
        sent.append(("reply", text))

    async def settle(self, **kw):
        return kw["response"]

    T = type(bot)
    return [
        patch.object(bot_mod, "session_manager", sm),
        patch.object(bot_mod.sdk_bridge, "process_message", side_effect=fake_process),
        patch.object(bot_mod.model_state, "take_start_notice", return_value=None),
        patch.object(bot_mod.limit_gate, "active_until", return_value=None),
        patch.object(T, "_get_real_model", lambda s, x: "m", create=True),
        patch.object(T, "_effective_session_id", lambda s, u, x: None, create=True),
        patch.object(T, "_permission_callback", None, create=True),
        patch.object(T, "_proactive_push", None, create=True),
        patch.object(T, "_with_outbound_context", lambda s, t, c, m: t, create=True),
        patch.object(T, "_settle_turn", settle, create=True),
        patch.object(T, "_reply_smart", fake_reply, create=True),
        patch.object(bot_mod, "framework_notice_send", AsyncMock(side_effect=lambda *a, **k: sent.append(("notice", a)))),
    ]


async def _run(bot, update, sent):
    from contextlib import ExitStack
    with ExitStack() as st:
        for p in _handler_env(bot, sent):
            st.enter_context(p)
        await bot._process_user_message_text(update, 1, "hi")


@pytest.mark.asyncio
async def test_message_while_application_none_shows_no_raw_error():
    # promise: P1 no AttributeError text reaches the user while application is None
    b = bot_mod.TelegramBot()
    b.application = None
    sent = []
    await _run(b, _update(), sent)
    assert sent == []  # no error notice, no turn started yet
    assert len(b._deferred_updates) == 1


@pytest.mark.asyncio
async def test_deferred_message_is_answered_after_reinit():
    # promise: P2 the kept message is processed on the re-initialised bot
    b = bot_mod.TelegramBot()
    b.application = None
    sent = []
    await _run(b, _update("hello"), sent)
    live = SimpleNamespace(bot=telegram.Bot(TOKEN))
    b.application = live
    created = []
    real = asyncio.create_task
    with patch.object(bot_mod.asyncio, "create_task", lambda c: created.append(c) or real(asyncio.sleep(0))):
        b._drain_deferred()
    assert b._deferred_updates == []
    upd = None
    # run the replayed coroutine under the same fixtures
    from contextlib import ExitStack
    with ExitStack() as st:
        for p in _handler_env(b, sent):
            st.enter_context(p)
        await created[0]
    assert ("reply", "ok") in sent
    turn = [e for e in sent if e[0] == "turn"][0]
    assert turn[2] is live.bot


def test_user_facing_error_copy_has_no_exception_slot():
    # promise: P3 no locale interpolates exception text to the user
    for table in (en.STRINGS if hasattr(en, "STRINGS") else vars(en).get("MESSAGES", {}),
                  ko.STRINGS if hasattr(ko, "STRINGS") else vars(ko).get("MESSAGES", {})):
        for key in ("generic_error", "processing_failed", "resume_failed"):
            assert "{error}" not in table[key]
    for const in (messages.GENERIC_ERROR, messages.PROCESSING_FAILED, messages.RESUME_FAILED):
        assert "{" not in const


@pytest.mark.asyncio
async def test_conflict_streak_sends_one_notice_naming_the_other_agent():
    # promise: P4 one notice per streak, naming the local twin, re-armed on recovery
    b = bot_mod.TelegramBot()
    b.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    now = [0.0]
    b._conflict_notice = token_conflict.ConflictNotice(after_s=180, clock=lambda: now[0])
    sent = []
    with patch.object(bot_mod, "framework_notice_send", AsyncMock(side_effect=lambda *a, **k: sent.append(k["text"]))), \
         patch.object(bot_mod.config, "allowed_user_ids", [42]), \
         patch.object(token_conflict, "local_holders", return_value=["OtherAgent"]):
        for t in (0, 60, 179):
            now[0] = t
            await b._maybe_notice_token_conflict()
        assert sent == []
        for t in (180, 240, 900):
            now[0] = t
            await b._maybe_notice_token_conflict()
        assert len(sent) == 1 and "OtherAgent" in sent[0]
        b._conflict_notice.recovered()
        now[0] = 1000
        await b._maybe_notice_token_conflict()
        now[0] = 1200
        await b._maybe_notice_token_conflict()
        assert len(sent) == 2


def test_local_holders_needs_live_lock_and_same_token(tmp_path):
    # promise: P5 only a running agent with the same token is a holder
    def agent(name, token, pid):
        r = tmp_path / name
        (r / ".telegram_bot" / "state").mkdir(parents=True)
        # Explicit roots below exercise token/lock ownership independently
        # of estate discovery; no estate registration file is needed.
        (r / ".telegram_bot" / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % token)
        if pid:
            (r / ".telegram_bot" / "state" / "bridge.lock").write_text(json.dumps({"pid": pid}))
        return r
    me = agent("me", TOKEN, 1)
    twin = agent("twin", TOKEN, 7)
    agent("stopped", TOKEN, None)
    agent("other", "999999:" + "B" * 35, 8)
    agent("crashed", TOKEN, 9)  # stale lock, process gone
    roots = [tmp_path / n for n in ("me", "twin", "stopped", "other", "crashed")]
    assert token_conflict.local_holders(TOKEN, me, roots, alive=lambda p: p in (7, 8)) == ["twin"]
