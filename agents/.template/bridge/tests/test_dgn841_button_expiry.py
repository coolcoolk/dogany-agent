"""DGN-841 / dec-272: inline buttons do not expire by default.

Owner decision (2026-10-07): drop button expiry; make it a variable so only
the buttons that truly need one get one.

Pinned here:

  LATE      a button tapped hours after it was shown executes (opt: runs its
            handler; every no-TTL kind passes the gate).
  DECLARED  a kind with a TTL in CALLBACK_TTL_SECONDS still expires, with the
            confirmed alert copy whose {minutes} is that kind's TTL.
  PLAIN     a replayed plain message older than STALE_MESSAGE_SECONDS is still
            dropped (the gate's real purpose: polling re-establish replay).
  REGISTRY  every callback kind _handle_callback dispatches on is listed in
            CALLBACK_TTL_SECONDS -- a new kind must make an explicit decision.
"""

import inspect
import re
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages, model_picker, voice_setup
from bridge.countdown import CDN_DONE_PREFIX


OWNER_ID = 8410
LABEL = "check the watchdog failure"


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, *a, **k):
        self.sent.append((chat_id, text))
        return MagicMock(message_id=9999)


class _FakeMessage:
    def __init__(self, chat, date, reply_markup=None, text=None):
        self.chat = chat
        self.date = date
        self.message_id = 1
        self.caption = None
        self.text = text
        self.reply_markup = reply_markup
        self.replies = []

    async def reply_text(self, text, *a, **k):
        self.replies.append(text)


def _keyboard(*pairs):
    rows = [[SimpleNamespace(text=t, callback_data=cb)] for t, cb in pairs]
    return SimpleNamespace(inline_keyboard=rows)


def _cb_update(data, *, age, markup=None):
    chat = SimpleNamespace(id=OWNER_ID)
    msg = _FakeMessage(chat, datetime.now(timezone.utc) - age, reply_markup=markup)
    query = MagicMock()
    query.data = data
    query.message = msg
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    upd = SimpleNamespace(
        effective_chat=chat,
        effective_user=SimpleNamespace(id=OWNER_ID),
        message=None,
        callback_query=query,
    )
    return upd, query


def _text_update(*, age):
    chat = SimpleNamespace(id=OWNER_ID)
    msg = _FakeMessage(chat, datetime.now(timezone.utc) - age, text="hi")
    return SimpleNamespace(
        effective_chat=chat,
        effective_user=SimpleNamespace(id=OWNER_ID),
        message=msg,
        callback_query=None,
    )


def _make_bot():
    b = bot_mod.TelegramBot()
    b.application = SimpleNamespace(bot=_FakeBot())
    return b


def _as_owner():
    return patch.object(bot_mod.config, "allowed_user_ids", [OWNER_ID])


# --- LATE: default buttons execute however late ----------------------------

@pytest.mark.asyncio
async def test_late_opt_tap_executes():
    """The DGN-841 incident shape: an opt: button tapped ~6 h later runs."""
    b = _make_bot()
    upd, query = _cb_update(
        "opt:1", age=timedelta(hours=6), markup=_keyboard((LABEL, "opt:1"))
    )

    with _as_owner(), patch.object(
        bot_mod.sdk_bridge, "process_message", new=AsyncMock()
    ), patch.object(b, "_enqueue_user_task", new=AsyncMock()) as enqueue:
        await b._handle_callback(upd, MagicMock())

    query.answer.assert_awaited_once_with()  # spinner dismiss, no alert
    query.edit_message_text.assert_awaited_once_with(
        messages.SELECTED.format(choice=LABEL)
    )
    enqueue.assert_awaited_once()  # the choice is dispatched to the model


_NO_TTL_SAMPLES = [
    "opt:2",
    "resume:tok",
    "retry:tok",
    # ESTATE:BEGIN estate callback kinds (update ledger, usage-defer retry, drilldown)
    "upd:n:2.6.0",
    "upd:a:abcd1234:1:0",
    "upd:c:abcd1234:1:0",
    "usageretry:job-1",
    "idrill:arm1:1:x",
    # ESTATE:END
    CDN_DONE_PREFIX + "42",
    model_picker.CB_MODEL + "opus",
    model_picker.CB_VENDOR + "anthropic",
    model_picker.CB_FOREIGN + "openai:gpt",
    "authsync:restart",  # retired kind, unknown prefix -> default None
]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", _NO_TTL_SAMPLES)
async def test_no_ttl_kinds_pass_the_gate_days_later(data):
    b = _make_bot()
    upd, query = _cb_update(data, age=timedelta(days=3))

    with _as_owner():
        allowed = await b._check_access(upd)

    assert allowed is True, data
    query.answer.assert_not_awaited()


# --- DECLARED: opt-in TTL still expires ------------------------------------

@pytest.mark.asyncio
async def test_extsend_declares_twenty_minutes():
    assert bot_mod._callback_ttl("extsend:allow") == 20 * 60
    assert bot_mod._callback_ttl("extsend:deny") == 20 * 60


@pytest.mark.asyncio
async def test_declared_ttl_expires_with_the_confirmed_notice():
    """extsend: (20 min) tapped 25 min later: alert, handler not run."""
    b = _make_bot()
    upd, query = _cb_update("extsend:allow", age=timedelta(minutes=25))

    with _as_owner(), patch.object(
        bot_mod.session_manager, "get_session", new=AsyncMock()
    ) as get_session:
        await b._handle_callback(upd, MagicMock())

    query.answer.assert_awaited_once()
    args, kwargs = query.answer.await_args
    assert kwargs.get("show_alert") is True
    assert args[0] == messages.STALE_CALLBACK_EXPIRED_NOLABEL.format(minutes=20)
    get_session.assert_not_awaited()  # the grant handler never ran
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_declared_ttl_passes_inside_its_window():
    b = _make_bot()
    upd, query = _cb_update("extsend:allow", age=timedelta(minutes=5))

    with _as_owner():
        assert await b._check_access(upd) is True
    query.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_notice_minutes_come_from_the_declaring_kind():
    """A kind declaring 90 min renders 90 in the copy, not the 20-min window."""
    b = _make_bot()
    upd, query = _cb_update("resume:tok", age=timedelta(hours=2))

    with _as_owner(), patch.dict(bot_mod.CALLBACK_TTL_SECONDS, {"resume:": 90 * 60}):
        assert await b._check_access(upd) is False

    assert query.answer.await_args.args[0] == (
        messages.STALE_CALLBACK_EXPIRED_NOLABEL.format(minutes=90)
    )


def test_longest_prefix_wins():
    """A sub-kind can declare its own TTL under a broader no-TTL entry."""
    with patch.dict(bot_mod.CALLBACK_TTL_SECONDS, {"model:x": 600}):
        assert bot_mod._callback_ttl("model:x-large") == 600
        assert bot_mod._callback_ttl("model:opus") is None
    assert bot_mod._callback_ttl(None) is None
    assert bot_mod._callback_ttl("") is None


# --- PLAIN: the replay gate stays ------------------------------------------

@pytest.mark.asyncio
async def test_replayed_plain_message_older_than_window_is_dropped():
    b = _make_bot()
    upd = _text_update(age=timedelta(seconds=bot_mod.STALE_MESSAGE_SECONDS + 60))

    with _as_owner():
        assert await b._check_access(upd) is False
    assert upd.message.replies == []


@pytest.mark.asyncio
async def test_plain_message_inside_window_passes():
    b = _make_bot()
    upd = _text_update(age=timedelta(seconds=bot_mod.STALE_MESSAGE_SECONDS - 60))

    with _as_owner():
        assert await b._check_access(upd) is True


# --- REGISTRY: every dispatched kind made a decision -----------------------

def test_every_dispatched_kind_is_in_the_registry():
    src = inspect.getsource(bot_mod.TelegramBot._handle_callback)
    literal = set(re.findall(r'\bdata\.startswith\("([^"]+)"\)', src))
    named = set(re.findall(r"\bdata\.startswith\(([A-Za-z_][\w.]*)\)", src))
    resolved = {
        "CDN_DONE_PREFIX": CDN_DONE_PREFIX,
        "model_picker.CB_VENDOR": model_picker.CB_VENDOR,
        "model_picker.CB_FOREIGN": model_picker.CB_FOREIGN,
        "voice_setup.PICK_PREFIX": voice_setup.PICK_PREFIX,
    }
    assert named <= set(resolved), named - set(resolved)
    kinds = literal | {resolved[n] for n in named}
    assert {"opt:", "extsend:", "resume:", "retry:"} <= kinds  # sanity
    missing = kinds - set(bot_mod.CALLBACK_TTL_SECONDS)
    assert not missing, (
        "callback kinds without an explicit CALLBACK_TTL_SECONDS decision: %r"
        % sorted(missing)
    )
