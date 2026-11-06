"""DGN-1385 regression: a message buffered mid-turn must signal receipt.

Before this fix, _enqueue_text_task's in-flight branches (default debounce
buffer AND /queue coalescing buffer) appended the message and returned with
zero feedback -- no send_action("typing") anywhere on that path. The sender
could not distinguish "received, waiting for the current turn to end" from
"silently dropped" (owner-observed, DGN-1385).

Pinned here:
  1. debounce-buffer append (default in-flight path) fires an immediate
     typing action.
  2. /queue coalescing-buffer append fires an immediate typing action too.
  3. the indicator is REFRESHED (not a one-shot) for as long as the message
     waits -- Telegram typing expires after ~5s and a long-running turn can
     leave the buffered message waiting far longer.
  4. the refresh task is cancelled (no leak) once the buffer drains.
  5. the refresh task is cancelled (no leak) when /stop discards the buffer.
  6. a burst of messages inside one window reuses a single refresh task --
     no duplicate spawn per arrival.
  7. a typing-send failure (e.g. 429/network) never kills the refresh loop
     or propagates into the turn.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bridge import bot as bot_mod
from bridge import sdk_bridge as sdk_bridge_mod


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.typing_calls = 0
        self.fail_next = 0

    async def send_action(self, *a, **k):
        self.typing_calls += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("simulated Telegram 429")


class _FakeMessage:
    def __init__(self, chat, date=None):
        self.chat = chat
        self.date = date or datetime.now(timezone.utc)
        self.caption = None
        self.replies = []

    async def reply_text(self, text, *a, **k):
        self.replies.append(text)


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))


class _FakeApp:
    def __init__(self, fake_bot):
        self.bot = fake_bot


class _FakeUpdate:
    def __init__(self, chat_id=1, date=None, user_id=None, chat=None):
        chat = chat or _FakeChat(chat_id)
        self.effective_chat = chat
        self.message = _FakeMessage(chat, date=date)
        self.effective_user = SimpleNamespace(id=user_id if user_id else chat_id)
        self.callback_query = None


def _make_bot():
    b = bot_mod.TelegramBot()
    b.application = _FakeApp(_FakeBot())
    return b


def _upd(ts=None, chat_id=1, user_id=None, chat=None):
    return _FakeUpdate(
        chat_id=chat_id, date=ts or datetime.now(timezone.utc), user_id=user_id, chat=chat
    )


async def _await_all_tasks(b, user_id, max_rounds=12):
    seen = set()
    for _ in range(max_rounds):
        tasks = [t for t in b._user_run_tasks.get(user_id, set()) if t not in seen]
        if not tasks:
            break
        seen.update(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)


def _patch_common(monkeypatch, window=0.05):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    monkeypatch.setattr(bot_mod, "BRIDGE_INFLIGHT_DEBOUNCE_S", window)


# --- 1. default debounce-buffer append signals receipt immediately ---


@pytest.mark.asyncio
async def test_debounce_buffer_append_sends_immediate_typing(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=5.0)  # long window: turn stays in flight
    user_id = 900
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "long-turn":
            await barrier.wait()

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 9, 10, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "long-turn", ts0, _upd(ts0, chat_id=user_id))
    chat = _FakeChat(user_id)
    upd1 = _upd(ts1, chat_id=user_id, chat=chat)
    await b._enqueue_text_task(user_id, "new-input", ts1, upd1)

    # Buffered, not yet interrupted -- but typing already fired at least once.
    assert len(b._debounce_texts.get(user_id, [])) == 1
    assert chat.typing_calls >= 1

    barrier.set()
    await _await_all_tasks(b, user_id)
    b._cancel_typing_refresh(user_id)


# --- 2. /queue coalescing-buffer append signals receipt too ---


@pytest.mark.asyncio
async def test_queue_buffer_append_sends_immediate_typing(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=5.0)
    user_id = 901
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 9, 10, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0, chat_id=user_id))
    chat = _FakeChat(user_id)
    upd1 = _upd(ts1, chat_id=user_id, chat=chat)
    await b._enqueue_text_task(user_id, "queued", ts1, upd1, coalesce=True)

    assert len(b._user_pending_texts.get(user_id, [])) == 1
    assert chat.typing_calls >= 1

    barrier.set()
    await _await_all_tasks(b, user_id)
    b._cancel_typing_refresh(user_id)


# --- 3. the indicator refreshes for as long as the message waits ---


@pytest.mark.asyncio
async def test_typing_refreshes_across_a_long_wait(monkeypatch):
    b = _make_bot()
    monkeypatch.setattr(bot_mod, "TYPING_INTERVAL", 0.02)
    _patch_common(monkeypatch, window=5.0)
    user_id = 902
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "long-turn":
            await barrier.wait()

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 9, 10, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "long-turn", ts0, _upd(ts0, chat_id=user_id))
    chat = _FakeChat(user_id)
    await b._enqueue_text_task(
        user_id, "new-input", ts1, _upd(ts1, chat_id=user_id, chat=chat)
    )

    first_count = chat.typing_calls
    assert first_count >= 1
    await asyncio.sleep(0.12)  # several refresh intervals
    assert chat.typing_calls > first_count  # NOT a one-shot

    barrier.set()
    await _await_all_tasks(b, user_id)


# --- 4. refresh task cancelled (no leak) once the buffer drains ---


@pytest.mark.asyncio
async def test_typing_refresh_cancelled_after_drain(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=30.0)  # long: turn ends naturally first
    user_id = 903
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 9, 10, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0, chat_id=user_id))
    await b._enqueue_text_task(user_id, "typed", ts1, _upd(ts1, chat_id=user_id))

    task = b._typing_refresh_tasks.get(user_id)
    assert task is not None and not task.done()

    barrier.set()  # anchor ends -> drain fires -> refresh must be cancelled
    await _await_all_tasks(b, user_id)
    await asyncio.sleep(0)

    assert b._typing_refresh_tasks.get(user_id) is None
    assert task.cancelled() or task.done()


# --- 5. refresh task cancelled (no leak) when /stop discards the buffer ---


@pytest.mark.asyncio
async def test_typing_refresh_cancelled_on_stop_discard():
    b = _make_bot()
    user_id = 904
    fake_task = asyncio.create_task(asyncio.sleep(60))
    b._typing_refresh_tasks[user_id] = fake_task

    b._clear_inflight_debounce(user_id)
    await asyncio.sleep(0)

    assert b._typing_refresh_tasks.get(user_id) is None
    assert fake_task.cancelled()


@pytest.mark.asyncio
async def test_typing_refresh_cancelled_on_clear_user_queue():
    b = _make_bot()
    user_id = 905
    fake_task = asyncio.create_task(asyncio.sleep(60))
    b._typing_refresh_tasks[user_id] = fake_task

    b._clear_user_queue(user_id)
    await asyncio.sleep(0)

    assert b._typing_refresh_tasks.get(user_id) is None
    assert fake_task.cancelled()


# --- 6. a burst of messages reuses ONE refresh task, no duplicate spawn ---


@pytest.mark.asyncio
async def test_burst_reuses_single_refresh_task(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=5.0)
    user_id = 906
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0, chat_id=user_id))

    for i, sec in enumerate((1, 2, 3)):
        ts = datetime(2026, 9, 10, 6, 0, sec, tzinfo=timezone.utc)
        await b._enqueue_text_task(user_id, f"burst{i}", ts, _upd(ts, chat_id=user_id))

    task_after_burst = b._typing_refresh_tasks.get(user_id)
    assert task_after_burst is not None

    # Re-arming the debounce timer must not have replaced the running task.
    for i, sec in enumerate((4, 5)):
        ts = datetime(2026, 9, 10, 6, 0, sec, tzinfo=timezone.utc)
        await b._enqueue_text_task(user_id, f"burst2-{i}", ts, _upd(ts, chat_id=user_id))
        assert b._typing_refresh_tasks.get(user_id) is task_after_burst

    barrier.set()
    await _await_all_tasks(b, user_id)


# --- 7. a typing-send failure never kills the refresh loop or the turn ---


@pytest.mark.asyncio
async def test_typing_send_failure_is_swallowed(monkeypatch):
    b = _make_bot()
    monkeypatch.setattr(bot_mod, "TYPING_INTERVAL", 0.02)
    _patch_common(monkeypatch, window=5.0)
    user_id = 907
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "long-turn":
            await barrier.wait()

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 9, 10, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 9, 10, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "long-turn", ts0, _upd(ts0, chat_id=user_id))

    chat = _FakeChat(user_id)
    chat.fail_next = 3  # immediate send + first couple refresh ticks all fail
    await b._enqueue_text_task(
        user_id, "new-input", ts1, _upd(ts1, chat_id=user_id, chat=chat)
    )

    await asyncio.sleep(0.15)  # loop must survive multiple failures
    task = b._typing_refresh_tasks.get(user_id)
    assert task is not None and not task.done()
    assert chat.typing_calls >= 4  # kept retrying past the failures

    barrier.set()
    await _await_all_tasks(b, user_id)
