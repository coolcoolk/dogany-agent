"""The 20-min STALE gate must never drop a tap SILENTLY (btnstale, 2026-09-10).

Defect: _check_access dropped any update whose message was older than
STALE_MESSAGE_SECONDS with a bare `return False` -- no log line, and for a
callback no query.answer() either. A notification button tapped ~6 h after the
push therefore did nothing at all on the owner's screen and left zero
server-side trace; the incident had to be reconstructed from ABSENT evidence
(DGN-841 confirmed the same cause in 2026-08 and the general path was left
open).

Pinned here, both directions:

  EXPIRED  a stale owner tap gets an alert (show_alert=True) whose text carries
           the button label to retype, and a WARNING log line. The handler
           still does NOT run (no edit_message_text, no model turn).
  FRESH    a fresh tap is byte-for-byte the old behavior: answer() with no
           args (spinner dismiss) and the opt: branch executes.
  QUIET    a stale tap from a NON-owner is still totally silent (no API call
           at all) -- the pre-gate alert must not become an existence probe.
"""

import logging
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages
from bridge import options as options_mod


OWNER_ID = 4242
STRANGER_ID = 9999
LABEL = "watchdog 작업 실패 원인 봐줘"


# --- harness ---------------------------------------------------------------

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, *a, **k):
        self.sent.append((chat_id, text))
        msg = MagicMock()
        msg.message_id = 9999
        return msg


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, chat, date, reply_markup=None):
        self.chat = chat
        self.date = date
        self.message_id = 1
        self.caption = None
        self.reply_markup = reply_markup
        self.replies = []

    async def reply_text(self, text, *a, **k):
        self.replies.append(text)



# Rendered copy (owner 2026-10-02): the window in minutes, no raw slots.
_EXPIRED = messages.STALE_CALLBACK_EXPIRED.format(choice="", minutes=20)
_EXPIRED_NOLABEL = messages.STALE_CALLBACK_EXPIRED_NOLABEL.format(minutes=20)
assert "20" in _EXPIRED and "{" not in _EXPIRED

def _keyboard(*pairs):
    """Minimal inline_keyboard stand-in: (text, callback_data) rows."""
    rows = [[SimpleNamespace(text=t, callback_data=cb)] for t, cb in pairs]
    return SimpleNamespace(inline_keyboard=rows)


def _cb_update(user_id=OWNER_ID, age_minutes=360, data="opt:1", markup=None):
    chat = _FakeChat(user_id)
    date = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    if markup is None:
        markup = _keyboard((LABEL, "opt:1"))
    msg = _FakeMessage(chat, date, reply_markup=markup)
    query = MagicMock()
    query.data = data
    query.message = msg
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    upd = SimpleNamespace(
        effective_chat=chat,
        effective_user=SimpleNamespace(id=user_id),
        message=None,
        callback_query=query,
    )
    return upd, query


def _text_update(user_id=OWNER_ID, age_minutes=360):
    chat = _FakeChat(user_id)
    date = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    msg = _FakeMessage(chat, date)
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=SimpleNamespace(id=user_id),
        message=msg,
        callback_query=None,
    )


def _make_bot():
    b = bot_mod.TelegramBot()
    b.application = SimpleNamespace(bot=_FakeBot())
    return b


def _as_owner():
    """Ownership context in which OWNER_ID is the established owner."""
    return patch.object(bot_mod.config, "allowed_user_ids", [OWNER_ID])


# --- EXPIRED: the drop is announced ----------------------------------------

@pytest.mark.asyncio
async def test_expired_owner_tap_gets_alert_and_log(caplog):
    """The core fix: a 6-h-old owner tap raises an alert and a WARNING line."""
    b = _make_bot()
    upd, query = _cb_update(age_minutes=357)  # the measured 5 h 57 m

    with _as_owner(), caplog.at_level(logging.WARNING, logger="bridge.bot"):
        allowed = await b._check_access(upd)

    assert allowed is False, "the gate itself must still deny the stale tap"

    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    assert kwargs.get("show_alert") is True, "a toast-only reply is missable"
    text = args[0]
    # 확정 문구(오너 2026-09-10 07:42)는 만료 사실 하나만 말한다. 라벨을 본문에
    # 싣던 초안은 폐기됐다 -- 재타이핑 방법은 학습되는 조작이라 매번 재설명하지
    # 않는다. 여기서 지키는 불변식은 "침묵하지 않는다" 이지 "라벨을 담는다" 가 아니다.
    assert text == _EXPIRED
    assert text.strip(), "an empty alert is the silence this fix removes"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a dropped tap must leave a server-side trace"
    line = warnings[0].getMessage()
    assert "STALE" in line and "opt:1" in line and str(OWNER_ID) in line


@pytest.mark.asyncio
async def test_expired_tap_still_does_not_run_the_handler():
    """Visibility only -- the tap must NOT execute the action it carried."""
    b = _make_bot()
    upd, query = _cb_update()

    with _as_owner(), patch.object(
        bot_mod.sdk_bridge, "process_message", new=AsyncMock()
    ) as pm:
        await b._handle_callback(upd, MagicMock())

    query.edit_message_text.assert_not_awaited()
    pm.assert_not_awaited()
    # Exactly one answer() -- the expiry alert, not the handler's spinner
    # dismiss (the handler was never reached).
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_expired_message_logs_without_replying(caplog):
    """A stale plain MESSAGE is logged (INFO) but never answered.

    Its drop is not an owner act that visibly died, and a polling
    re-establish can replay a burst of them -- WARNING + a reply per message
    would be noise, silence would be the old defect. INFO is the middle.
    """
    b = _make_bot()
    upd = _text_update()

    with _as_owner(), caplog.at_level(logging.INFO, logger="bridge.bot"):
        allowed = await b._check_access(upd)

    assert allowed is False
    assert upd.message.replies == []
    assert any(
        "STALE" in r.getMessage() and r.levelno == logging.INFO
        for r in caplog.records
    )


# --- QUIET: no existence leak ----------------------------------------------

@pytest.mark.asyncio
async def test_expired_tap_from_stranger_stays_silent():
    """The alert fires BEFORE the ownership branch -- it must be owner-gated."""
    b = _make_bot()
    upd, query = _cb_update(user_id=STRANGER_ID)

    with _as_owner():
        allowed = await b._check_access(upd)

    assert allowed is False
    # A stranger's stale tap must get nothing at all -- not even NO_PERMISSION.
    query.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_tap_silent_while_unclaimed():
    """Born-locked (claim mode): never reveal the bot exists, not even here."""
    b = _make_bot()
    upd, query = _cb_update()

    with patch.object(bot_mod.config, "allowed_user_ids", []):
        allowed = await b._check_access(upd)

    assert allowed is False
    query.answer.assert_not_awaited()


# --- alert wording degrade paths -------------------------------------------

@pytest.mark.asyncio
async def test_alert_degrades_when_label_is_unrecoverable():
    """Keyboard gone -> the label-free wording, never a quoted callback id."""
    b = _make_bot()
    upd, query = _cb_update(markup=SimpleNamespace(inline_keyboard=None))

    with _as_owner():
        await b._check_access(upd)

    text = query.answer.await_args.args[0]
    assert text == _EXPIRED_NOLABEL
    # 확정 문구는 라벨 유무로 갈리지 않는다. 지켜야 할 것은 콜백 id 가 새어나가지
    # 않는 것 하나다.
    assert "opt:1" not in text


@pytest.mark.asyncio
async def test_alert_degrades_on_number_handle_label():
    """DGN-881 overflow labels ("3번") are useless to retype -> label-free."""
    b = _make_bot()
    handle = options_mod._number_handle_label(3)
    upd, query = _cb_update(data="opt:3", markup=_keyboard((handle, "opt:3")))

    with _as_owner():
        await b._check_access(upd)

    assert query.answer.await_args.args[0] == _EXPIRED_NOLABEL


@pytest.mark.asyncio
async def test_alert_is_label_independent():
    """확정 문구는 버튼 라벨 모양과 무관하다 -- 라벨이 토스트로 새지 않는다.

    초안 시절 이 테스트는 "N. label" 접두를 벗겨 라벨을 토스트에 싣는 것을 지켰다.
    확정(2026-09-10 07:42)으로 라벨은 토스트에 안 실린다. 남는 불변식은 라벨 문자열이
    어떤 모양이든 토스트가 같은 값이라는 것이다.
    """
    b = _make_bot()
    upd, query = _cb_update(
        data="opt:2", markup=_keyboard(("2. 로그부터 보여줘", "opt:2"))
    )

    with _as_owner():
        await b._check_access(upd)

    text = query.answer.await_args.args[0]
    assert text == _EXPIRED
    assert "로그부터" not in text, "라벨이 토스트로 새면 안 된다"


@pytest.mark.asyncio
async def test_alert_fits_telegram_alert_limit():
    """answerCallbackQuery text caps at 200 chars -- a long label must not
    push the alert past it (Telegram would reject the whole call)."""
    b = _make_bot()
    long_label = "가" * 300
    upd, query = _cb_update(markup=_keyboard((long_label, "opt:1")))

    with _as_owner():
        await b._check_access(upd)

    assert len(query.answer.await_args.args[0]) <= 200


@pytest.mark.asyncio
async def test_alert_failure_never_breaks_the_gate(caplog):
    """A Telegram blip on the alert must not turn a drop into an exception."""
    b = _make_bot()
    upd, query = _cb_update()
    query.answer = AsyncMock(side_effect=RuntimeError("Telegram down"))

    with _as_owner(), caplog.at_level(logging.WARNING, logger="bridge.bot"):
        allowed = await b._check_access(upd)

    assert allowed is False
    assert any("alert failed" in r.getMessage() for r in caplog.records)


# --- FRESH: nothing changed ------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_tap_keeps_the_original_behavior():
    """A fresh opt: tap: bare answer() spinner dismiss + the opt: branch runs."""
    b = _make_bot()
    upd, query = _cb_update(age_minutes=1)

    with _as_owner(), patch.object(
        bot_mod.sdk_bridge, "process_message", new=AsyncMock()
    ):
        await b._handle_callback(upd, MagicMock())

    query.answer.assert_awaited_once_with()  # no text, no show_alert
    query.edit_message_text.assert_awaited_once_with(
        messages.SELECTED.format(choice=LABEL)
    )


@pytest.mark.asyncio
async def test_fresh_message_passes_the_gate_unannounced(caplog):
    """A fresh plain message still passes and logs nothing new."""
    b = _make_bot()
    upd = _text_update(age_minutes=0)

    with _as_owner(), caplog.at_level(logging.INFO, logger="bridge.bot"):
        allowed = await b._check_access(upd)

    assert allowed is True
    assert not any("STALE" in r.getMessage() for r in caplog.records)
