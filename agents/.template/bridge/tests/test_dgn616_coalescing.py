"""DGN-616 message-coalescing unit tests.

DGN-911 UPDATE: coalescing is no longer the DEFAULT in-flight path -- it is the
explicit /queue path (coalesce=True on _enqueue_text_task). The in-flight cases
below pass coalesce=True; the default path (debounce-interrupt) is pinned by
test_dgn911_inflight_debounce.py.

Success criteria (task spec):
  (a) messages queued during an in-flight turn coalesce into ONE combined
      turn (order preserved), with no per-message overflow notice.
  (b) an idle single message processes immediately (no buffering).
  (c) a control command during an in-flight turn is NOT buffered (runs its own
      task via _enqueue_user_task, bypassing the coalescing buffer).
  (d) the hard-cap boundary (COALESCE_MAX) surfaces the queue_busy notice; below
      the cap never does.

These complement test_queue_bundling.py (the merge-format + serialization
invariants); this file focuses on the input-side coalescing contract and the
memory-safety cap.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from bridge import bot as bot_mod
from bridge import messages
from bridge import sdk_bridge as sdk_bridge_mod


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


async def _await_all_tasks(b, user_id, max_rounds=12):
    """Drain all tasks, including those spawned by drain callbacks."""
    seen = set()
    for _ in range(max_rounds):
        tasks = [t for t in b._user_run_tasks.get(user_id, set()) if t not in seen]
        if not tasks:
            break
        seen.update(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)


# --- (a) in-flight arrivals coalesce into ONE combined turn, no per-msg notice ---


@pytest.mark.asyncio
async def test_inflight_arrivals_coalesce_no_overflow_notice(monkeypatch):
    b = _make_bot()
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    user_id = 300
    calls = []
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "first":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 7, 28, 5, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 7, 28, 5, 0, 1, tzinfo=timezone.utc)
    ts2 = datetime(2026, 7, 28, 5, 0, 2, tzinfo=timezone.utc)
    ts3 = datetime(2026, 7, 28, 5, 0, 3, tzinfo=timezone.utc)

    # first: idle -> dispatched immediately, blocks on barrier.
    await b._enqueue_text_task(user_id, "first", ts0, _upd(ts0))
    # next three /queue while first is in flight -> buffered, no new turn each.
    await b._enqueue_text_task(user_id, "second", ts1, _upd(ts1), coalesce=True)
    await b._enqueue_text_task(user_id, "third", ts2, _upd(ts2), coalesce=True)
    await b._enqueue_text_task(user_id, "fourth", ts3, _upd(ts3), coalesce=True)

    assert len(b._user_pending_texts.get(user_id, [])) == 3

    barrier.set()
    await _await_all_tasks(b, user_id)

    # Exactly two SDK turns ran: the original + ONE merged follow-up.
    assert len(calls) == 2
    assert calls[0] == "first"
    merged = calls[1]
    assert "[05:00:01] second" in merged
    assert "[05:00:02] third" in merged
    assert "[05:00:03] fourth" in merged
    # Order preserved.
    assert merged.index("second") < merged.index("third") < merged.index("fourth")
    # No QUEUE_BUSY / overflow notice was sent on any of the buffered messages.
    assert b.application.bot.sent == []
    # Buffer fully drained.
    assert b._user_pending_texts.get(user_id, []) == []


# --- (b) idle single message processes immediately (no buffering) ---


@pytest.mark.asyncio
async def test_idle_single_message_immediate(monkeypatch):
    b = _make_bot()
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    user_id = 301
    calls = []

    async def mock_process(update, uid, text, **kwargs):
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts = datetime(2026, 7, 28, 5, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "hi", ts, _upd(ts))
    # Never buffered when idle.
    assert b._user_pending_texts.get(user_id, []) == []
    await _await_all_tasks(b, user_id)

    assert calls == ["hi"]  # verbatim, single-item bundle -> no wrapper


# --- (c) control command during in-flight is NOT buffered ---


@pytest.mark.asyncio
async def test_control_command_not_buffered_during_inflight(monkeypatch):
    b = _make_bot()
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    user_id = 302
    barrier = asyncio.Event()
    order = []

    async def mock_process(update, uid, text, **kwargs):
        if text == "regular":
            await barrier.wait()
        order.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts = datetime(2026, 7, 28, 5, 0, 0, tzinfo=timezone.utc)
    # Regular turn in flight (blocks).
    await b._enqueue_text_task(user_id, "regular", ts, _upd(ts))

    # A control command arrives while regular is in flight. Control commands use
    # _enqueue_user_task directly and must NOT land in the coalescing buffer.
    control_ran = {"ok": False}

    async def control_task():
        control_ran["ok"] = True
        order.append("control")

    async def on_overflow():
        raise AssertionError("control command must not overflow (inflight cap=3)")

    await b._enqueue_user_task(user_id, control_task, on_overflow, chat_id=1)

    # Buffer stays empty: control command bypassed it entirely.
    assert b._user_pending_texts.get(user_id, []) == []
    # Control ran immediately, even though the regular turn is still blocked.
    await asyncio.sleep(0)
    assert control_ran["ok"]

    barrier.set()
    await _await_all_tasks(b, user_id)
    assert "control" in order


# --- (d) hard-cap boundary surfaces the notice; below cap never does ---


@pytest.mark.asyncio
async def test_hard_cap_surfaces_notice(monkeypatch):
    b = _make_bot()
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    user_id = 303
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 7, 28, 5, 0, 0, tzinfo=timezone.utc)
    # Anchor turn holds the in-flight slot so everything else buffers.
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))

    cap = bot_mod.COALESCE_MAX
    # Fill exactly to the cap: no notice yet.
    for i in range(cap):
        await b._enqueue_text_task(user_id, f"m{i}", ts0, _upd(ts0), coalesce=True)
    assert len(b._user_pending_texts.get(user_id, [])) == cap
    assert b.application.bot.sent == []

    # One past the cap: notice fires, message not appended.
    await b._enqueue_text_task(user_id, "overflow", ts0, _upd(ts0), coalesce=True)
    assert len(b._user_pending_texts.get(user_id, [])) == cap
    assert b.application.bot.sent == [(1, messages.QUEUE_BUSY)]

    # Cleanup: release the anchor and drain so no pending task leaks.
    barrier.set()
    await _await_all_tasks(b, user_id)
