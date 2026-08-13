"""DGN-192 queue-bundling unit tests.

Success criteria (spec):
  (a) 3 texts buffered while in-flight -> merged into 1 new turn after
  (b) 1 in-flight + 2 pending -> in-flight runs alone, pending 2 merged
  (c) opt:/slash callbacks go through _enqueue_user_task, never touch pending buffer
  (d) existing serialization invariant (_enqueue_user_task behavior) unchanged
"""

import asyncio
from datetime import datetime, timezone

import pytest

from bridge import bot as bot_mod
from bridge import messages
from bridge import sdk_bridge as sdk_bridge_mod


# --- shared fakes ---


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))


class _FakeApp:
    def __init__(self, fake_bot):
        self.bot = fake_bot


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id

    async def send_action(self, *a, **k):
        pass


class _FakeMessage:
    def __init__(self, chat, date=None):
        self.chat = chat
        self.date = date or datetime.now(timezone.utc)
        self.caption = None

    async def reply_text(self, *a, **k):
        pass


class _FakeUpdate:
    def __init__(self, chat_id=1, date=None):
        chat = _FakeChat(chat_id)
        self.effective_chat = chat
        self.message = _FakeMessage(chat, date=date)


def _make_bot():
    b = bot_mod.TelegramBot()
    b.application = _FakeApp(_FakeBot())
    return b


def _upd(ts=None, chat_id=1):
    return _FakeUpdate(chat_id=chat_id, date=ts or datetime.now(timezone.utc))


async def _await_all_tasks(b, user_id, max_rounds=10):
    """Drain all tasks, including those spawned by drain callbacks."""
    seen = set()
    for _ in range(max_rounds):
        tasks = [t for t in b._user_run_tasks.get(user_id, set()) if t not in seen]
        if not tasks:
            break
        seen.update(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)


# --- (a) 3 texts buffered while in-flight -> 1 merged turn ---


@pytest.mark.asyncio
async def test_three_buffered_texts_merge_into_one_turn(monkeypatch):
    """Three texts sitting in the pending buffer are dispatched as a single merged turn."""
    b = _make_bot()
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    user_id = 100
    calls = []

    async def mock_process(update, uid, text, **kwargs):
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts1 = datetime(2026, 7, 14, 7, 0, 1, tzinfo=timezone.utc)
    ts2 = datetime(2026, 7, 14, 7, 0, 2, tzinfo=timezone.utc)
    ts3 = datetime(2026, 7, 14, 7, 0, 3, tzinfo=timezone.utc)

    # Manually populate pending buffer (simulates 3 texts arriving while in-flight)
    b._user_pending_texts[user_id] = [
        ("first", ts1, _upd(ts1)),
        ("second", ts2, _upd(ts2)),
        ("third", ts3, _upd(ts3)),
    ]

    # Drain: should produce exactly 1 merged turn
    await b._drain_pending_texts(user_id)
    await _await_all_tasks(b, user_id)

    assert len(calls) == 1
    merged = calls[0]
    assert "[07:00:01] first" in merged
    assert "[07:00:02] second" in merged
    assert "[07:00:03] third" in merged
    assert "---" in merged
    assert b._user_pending_texts.get(user_id, []) == []


# --- (b) 1 in-flight + 2 pending -> in-flight runs alone, pending 2 merged ---


@pytest.mark.asyncio
async def test_inflight_runs_alone_pending_two_merged(monkeypatch):
    """In-flight turn processes its text; pending 2 are merged into one new turn after."""
    b = _make_bot()
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    user_id = 101
    calls = []
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "inflight":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 7, 14, 7, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 7, 14, 7, 0, 1, tzinfo=timezone.utc)
    ts2 = datetime(2026, 7, 14, 7, 0, 2, tzinfo=timezone.utc)

    # First text: dispatched immediately (no in-flight)
    await b._enqueue_text_task(user_id, "inflight", ts0, _upd(ts0))
    # Two more arrive while first is blocking on the barrier
    await b._enqueue_text_task(user_id, "pending1", ts1, _upd(ts1))
    await b._enqueue_text_task(user_id, "pending2", ts2, _upd(ts2))

    assert len(b._user_pending_texts.get(user_id, [])) == 2

    # Release in-flight; it will call _drain_pending_texts on completion
    barrier.set()
    await _await_all_tasks(b, user_id)

    # In-flight ran alone; pending 2 merged into 1 turn
    assert len(calls) == 2
    assert calls[0] == "inflight"
    assert "[07:00:01] pending1" in calls[1]
    assert "[07:00:02] pending2" in calls[1]
    assert "---" in calls[1]


# --- (c) opt:/slash callbacks go through _enqueue_user_task, not pending buffer ---


@pytest.mark.asyncio
async def test_callback_path_does_not_touch_pending_buffer(monkeypatch):
    """_enqueue_user_task (callback/slash path) never adds to _user_pending_texts."""
    b = _make_bot()
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    user_id = 102

    # Pre-populate pending buffer (simulates a buffered text)
    ts = datetime(2026, 7, 14, 7, 0, 1, tzinfo=timezone.utc)
    b._user_pending_texts[user_id] = [("buffered", ts, _upd(ts))]

    snapshot_before = list(b._user_pending_texts[user_id])

    # Simulate a callback-style task via _enqueue_user_task (the callback path)
    ran = {"ok": False}

    async def callback_task():
        # During execution: pending buffer must not have been modified by this path
        pending = b._user_pending_texts.get(user_id, [])
        assert pending == snapshot_before, "callback task must not modify pending buffer"
        ran["ok"] = True

    # Inject a no-op drain so the after-task drain doesn't try to run _process_user_message_text
    async def noop_drain(uid):
        b._user_pending_texts.pop(uid, None)

    monkeypatch.setattr(b, "_drain_pending_texts", noop_drain)

    async def on_overflow():
        raise AssertionError("overflow must not fire")

    await b._enqueue_user_task(user_id, callback_task, on_overflow, chat_id=1)
    tasks = list(b._user_run_tasks.get(user_id, set()))
    await asyncio.gather(*tasks, return_exceptions=True)

    assert ran["ok"]


# --- (d) existing serialization invariant: _enqueue_user_task death-notice unchanged ---


@pytest.mark.asyncio
async def test_enqueue_user_task_death_notice_still_fires(monkeypatch):
    """DGN-163 regression: _enqueue_user_task still emits the turn-death notice."""
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    fake = _FakeBot()
    b = _make_bot()
    b.application = _FakeApp(fake)

    # Noop drain so the death-notice test isn't confused by pending-text dispatch
    monkeypatch.setattr(b, "_drain_pending_texts", lambda uid: asyncio.sleep(0))

    async def boom():
        raise ValueError("turn death")

    async def on_overflow():
        raise AssertionError

    await b._enqueue_user_task(
        999, boom, on_overflow, chat_id=999, failure_message=messages.TURN_FAILED
    )
    tasks = list(b._user_run_tasks.get(999, set()))
    await asyncio.gather(*tasks, return_exceptions=True)

    assert fake.sent == [(999, messages.TURN_FAILED)]


# --- bundle format unit tests ---


def test_bundle_single_item_no_wrapper():
    """A single-item list is returned as-is (no timestamp prefix, no separator)."""
    ts = datetime(2026, 7, 14, 7, 0, 0, tzinfo=timezone.utc)
    upd = _FakeUpdate()
    result = bot_mod.TelegramBot._bundle_texts([("hello", ts, upd)])
    assert result == "hello"


def test_bundle_multiple_items_format():
    """Multiple items are prefixed with timestamps and separated by ---."""
    t1 = datetime(2026, 7, 14, 9, 0, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 14, 9, 0, 5, tzinfo=timezone.utc)
    upd = _FakeUpdate()
    result = bot_mod.TelegramBot._bundle_texts([
        ("msg A", t1, upd),
        ("msg B", t2, upd),
    ])
    assert "[09:00:01] msg A" in result
    assert "[09:00:05] msg B" in result
    assert "\n---\n" in result
    # Order preserved
    assert result.index("msg A") < result.index("msg B")


# --- /stop clears pending buffer ---


@pytest.mark.asyncio
async def test_stop_clears_pending_text_buffer():
    """/stop (_clear_user_queue) also clears the pending text buffer."""
    b = _make_bot()
    user_id = 200
    ts = datetime(2026, 7, 14, 7, 0, 0, tzinfo=timezone.utc)
    b._user_pending_texts[user_id] = [("hello", ts, _upd(ts))]

    b._clear_user_queue(user_id)

    assert b._user_pending_texts.get(user_id) is None
