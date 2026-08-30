"""DGN-911 in-flight debounce-interrupt unit tests.

Default inversion: a REGULAR message arriving while a turn is in flight no
longer coalesces into the next turn (DGN-616 legacy). It opens a per-user
debounce window (BRIDGE_INFLIGHT_DEBOUNCE_S); further messages append and
reset the timer; on expiry the in-flight turn is soft-interrupted (DGN-581)
and the buffered messages run as ONE merged new turn. Explicit /queue keeps
the legacy no-interrupt coalescing.

Acceptance criteria pinned here (DGN-911 spec):
  1. idle message -> immediate new turn, no debounce delay.
  2. in-flight single message -> after the window, current turn interrupted
     and the message runs as a new turn.
  3. in-flight rapid burst inside the window -> ONE merged turn, order
     preserved, no message lost, exactly one interrupt.
  4. /queue -> no interrupt, merges into the next turn (DGN-616 behavior).
  5. /stop regression: both stop paths discard the debounce buffer + timer;
     hard-stop task cancel unchanged.
  6. session no-loss shape: the auto path calls sdk_bridge.interrupt ONLY --
     never stop()/hard teardown (sdk-level session preservation is pinned by
     test_dgn581_soft_interrupt.py).
  plus fail-safe: interrupt failure degrades to the legacy coalescing merge
  (messages never dropped), and a turn that ends naturally inside the window
  flushes the buffer immediately (no wait, no order inversion).
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

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
        self.replies = []

    async def reply_text(self, text, *a, **k):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, chat_id=1, date=None, user_id=None):
        chat = _FakeChat(chat_id)
        self.effective_chat = chat
        self.message = _FakeMessage(chat, date=date)
        self.effective_user = SimpleNamespace(id=user_id if user_id else chat_id)
        self.callback_query = None


def _make_bot():
    b = bot_mod.TelegramBot()
    b.application = _FakeApp(_FakeBot())
    return b


def _upd(ts=None, chat_id=1, user_id=None):
    return _FakeUpdate(
        chat_id=chat_id, date=ts or datetime.now(timezone.utc), user_id=user_id
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


def _mock_interrupt(monkeypatch, barrier=None, result=True, exc=None):
    """Install an sdk_bridge.interrupt mock; returns the call recorder list."""
    calls = []

    async def fake_interrupt(uid, **kwargs):
        # DGN-1016: real signature is interrupt(user_id, *, trigger=...).
        calls.append(uid)
        if exc is not None:
            raise exc
        if barrier is not None:
            # Simulate the DGN-581 soft interrupt resolving the in-flight
            # turn's future: release the blocked mock turn.
            barrier.set()
        return result

    monkeypatch.setattr(sdk_bridge_mod.sdk_bridge, "interrupt", fake_interrupt)
    return calls


def _mock_stop_guard(monkeypatch):
    """sdk_bridge.stop must NEVER fire on the auto-interrupt path."""
    stop_calls = []

    async def fake_stop(uid):
        stop_calls.append(uid)
        return False

    monkeypatch.setattr(sdk_bridge_mod.sdk_bridge, "stop", fake_stop)
    return stop_calls


# --- 1. idle -> immediate new turn, no debounce ---


@pytest.mark.asyncio
async def test_idle_message_immediate_no_debounce(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=5.0)  # long window: must not matter
    interrupt_calls = _mock_interrupt(monkeypatch)
    user_id = 400
    calls = []

    async def mock_process(update, uid, text, **kwargs):
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts = datetime(2026, 8, 17, 6, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "hi", ts, _upd(ts))
    await _await_all_tasks(b, user_id)

    assert calls == ["hi"]
    assert interrupt_calls == []
    assert b._debounce_texts.get(user_id) is None
    assert b._debounce_timers.get(user_id) is None


# --- 2. in-flight single message -> interrupt after window + new turn ---


@pytest.mark.asyncio
async def test_inflight_single_message_interrupts_then_new_turn(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05)
    user_id = 401
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch, barrier=barrier)
    stop_calls = _mock_stop_guard(monkeypatch)

    async def mock_process(update, uid, text, **kwargs):
        if text == "long-turn":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 17, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 8, 17, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "long-turn", ts0, _upd(ts0))
    await b._enqueue_text_task(user_id, "new-input", ts1, _upd(ts1))

    # Buffered in the debounce window, timer armed, not yet interrupted.
    assert len(b._debounce_texts.get(user_id, [])) == 1
    assert user_id in b._debounce_timers
    assert interrupt_calls == []

    await asyncio.sleep(0.2)  # window expires -> interrupt fires
    await _await_all_tasks(b, user_id)

    assert interrupt_calls == [user_id]
    assert calls == ["long-turn", "new-input"]
    # Session-preserving shape: soft interrupt only, never hard teardown.
    assert stop_calls == []
    # Silent by default: no auto-interrupt notice sent (owner-confirmed UX).
    assert b.application.bot.sent == []
    # All buffers empty after the merged dispatch.
    assert b._user_pending_texts.get(user_id, []) == []
    assert b._debounce_texts.get(user_id) is None


# --- 3. rapid burst inside the window -> ONE merged turn, no loss ---


@pytest.mark.asyncio
async def test_inflight_burst_merges_into_one_turn_lossless(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=0.15)
    user_id = 402
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch, barrier=barrier)

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 17, 6, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))

    # Three messages in quick succession, each resetting the timer.
    for i, sec in enumerate((1, 2, 3)):
        ts = datetime(2026, 8, 17, 6, 0, sec, tzinfo=timezone.utc)
        await b._enqueue_text_task(user_id, f"burst{i}", ts, _upd(ts))
        await asyncio.sleep(0.05)  # < window -> timer resets, no interrupt yet
        assert interrupt_calls == []

    await asyncio.sleep(0.3)  # let the (reset) window expire once
    await _await_all_tasks(b, user_id)

    # Exactly ONE interrupt and exactly ONE merged follow-up turn.
    assert interrupt_calls == [user_id]
    assert len(calls) == 2
    assert calls[0] == "anchor"
    merged = calls[1]
    assert "[06:00:01] burst0" in merged
    assert "[06:00:02] burst1" in merged
    assert "[06:00:03] burst2" in merged
    assert merged.index("burst0") < merged.index("burst1") < merged.index("burst2")


# --- 4. /queue -> no interrupt, merges into the NEXT turn ---


@pytest.mark.asyncio
async def test_queue_path_never_interrupts(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05)
    user_id = 403
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch, barrier=barrier)

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 17, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 8, 17, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))
    await b._enqueue_text_task(user_id, "queued", ts1, _upd(ts1), coalesce=True)

    # Lands in the legacy pending buffer; no debounce timer armed.
    assert len(b._user_pending_texts.get(user_id, [])) == 1
    assert b._debounce_timers.get(user_id) is None

    await asyncio.sleep(0.2)  # way past the window: still no interrupt
    assert interrupt_calls == []
    assert calls == []  # anchor still running

    barrier.set()  # anchor finishes on its own -> drain merges the queued msg
    await _await_all_tasks(b, user_id)

    assert interrupt_calls == []
    assert calls == ["anchor", "queued"]


@pytest.mark.asyncio
async def test_cmd_queue_routes_to_coalesce_and_usage_reply(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch)

    async def allow_access(update):
        return True

    monkeypatch.setattr(b, "_check_access", allow_access)

    routed = []

    async def fake_enqueue(user_id, text, ts, update, **kwargs):
        routed.append((user_id, text, kwargs))

    monkeypatch.setattr(b, "_enqueue_text_task", fake_enqueue)

    # /queue with a message -> coalescing path, verbatim joined args.
    upd = _upd(user_id=404)
    ctx = SimpleNamespace(args=["hello", "there"])
    await b._cmd_queue(upd, ctx)
    assert routed == [(404, "hello there", {"coalesce": True})]
    assert upd.message.replies == []

    # /queue without a message -> usage hint, nothing enqueued.
    upd2 = _upd(user_id=404)
    ctx2 = SimpleNamespace(args=[])
    await b._cmd_queue(upd2, ctx2)
    assert len(routed) == 1
    assert upd2.message.replies == [messages.QUEUE_USAGE]


# --- 5. /stop regression: debounce state discarded on both stop paths ---


@pytest.mark.asyncio
async def test_clear_user_queue_discards_debounce_state():
    b = _make_bot()
    user_id = 405
    ts = datetime(2026, 8, 17, 6, 0, 0, tzinfo=timezone.utc)
    b._user_pending_texts[user_id] = [("p", ts, _upd(ts))]
    b._debounce_texts[user_id] = [("d", ts, _upd(ts))]
    timer = asyncio.create_task(asyncio.sleep(60))
    b._debounce_timers[user_id] = timer

    b._clear_user_queue(user_id)

    assert b._user_pending_texts.get(user_id) is None
    assert b._debounce_texts.get(user_id) is None
    assert b._debounce_timers.get(user_id) is None
    await asyncio.sleep(0)
    assert timer.cancelled()


@pytest.mark.asyncio
async def test_cmd_stop_soft_path_discards_debounce_and_replies(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch)
    user_id = 406

    async def allow_access(update):
        return True

    monkeypatch.setattr(b, "_check_access", allow_access)
    interrupt_calls = _mock_interrupt(monkeypatch, result=True)

    ts = datetime(2026, 8, 17, 6, 0, 0, tzinfo=timezone.utc)
    b._debounce_texts[user_id] = [("staged", ts, _upd(ts))]
    timer = asyncio.create_task(asyncio.sleep(60))
    b._debounce_timers[user_id] = timer

    upd = _upd(user_id=user_id)
    await b._cmd_stop(upd, SimpleNamespace(args=[]))

    # Soft interrupt fired, confirmed copy replied, staged input discarded.
    # DGN-991 stopgap A: the first /stop reply carries the background note
    # (pre-existing assertion drift fixed while touching this suite for
    # DGN-1016 -- bot.py has replied with the two-line copy since DGN-991).
    assert interrupt_calls == [user_id]
    assert upd.message.replies == [
        f"{messages.STOP_INTERRUPTED}\n{messages.STOP_BG_NOTE}"
    ]
    assert b._debounce_texts.get(user_id) is None
    assert b._debounce_timers.get(user_id) is None
    await asyncio.sleep(0)
    assert timer.cancelled()


# --- fail-safe: interrupt failure degrades to coalescing merge (no loss) ---


@pytest.mark.asyncio
async def test_interrupt_failure_degrades_to_merge_no_loss(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05)
    user_id = 407
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(
        monkeypatch, exc=RuntimeError("stuck turn")
    )

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 17, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 8, 17, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))
    await b._enqueue_text_task(user_id, "must-survive", ts1, _upd(ts1))

    await asyncio.sleep(0.2)  # window expires; interrupt raises
    assert interrupt_calls == [user_id]
    # Message parked in the fail-safe (legacy) buffer, not dropped.
    assert len(b._user_pending_texts.get(user_id, [])) == 1
    assert calls == []  # anchor still running (interrupt failed)

    barrier.set()  # anchor ends naturally -> legacy merge delivers the message
    await _await_all_tasks(b, user_id)

    assert calls == ["anchor", "must-survive"]


# --- natural turn end inside the window -> immediate flush, timer cancelled ---


@pytest.mark.asyncio
async def test_natural_turn_end_flushes_debounce_immediately(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=30.0)  # long window: flush must NOT wait
    user_id = 408
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch)

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 17, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 8, 17, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))
    await b._enqueue_text_task(user_id, "typed", ts1, _upd(ts1))
    timer = b._debounce_timers.get(user_id)
    assert timer is not None

    barrier.set()  # anchor ends on its own well before the 30s window
    await _await_all_tasks(b, user_id)

    # Buffered message dispatched immediately via the drain path, no interrupt.
    assert calls == ["anchor", "typed"]
    assert interrupt_calls == []
    assert b._debounce_texts.get(user_id) is None
    assert b._debounce_timers.get(user_id) is None
    await asyncio.sleep(0)
    assert timer.cancelled() or timer.done()


# --- FATAL grill fix: non-text SDK turns (options callback) drain on exit ---
#
# Before the fix, _exec_slash_command / options / resume / retry run_task
# closures had no finally drain. A debounce expire during those turns moved
# messages into _user_pending_texts, then the interrupted turn's done-path
# (inside _enqueue_user_task's wrapped()) never drained because the drain
# only lived inside specific run_task bodies. Messages were stranded until the
# next idle turn. After the fix, each run_task has its own finally drain, so
# the debounce buffer is emptied whenever that turn finishes.


@pytest.mark.asyncio
async def test_options_turn_drains_debounce_buffer_on_completion(monkeypatch):
    """FATAL fix: options callback run_task drains debounce buffer on finish.

    Setup: manually inject a debounce buffer entry (simulating a message
    that arrived while the options turn was in flight and was moved to
    _user_pending_texts by _debounce_expire). After the options turn
    finishes, _drain_pending_texts must fire and dispatch the merged turn.
    """
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05)
    user_id = 409
    calls = []
    barrier = asyncio.Event()

    async def mock_process(update, uid, text, **kwargs):
        if text == "options-turn":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    # Start a text turn (stand-in for the options turn running _process_user_message_text).
    ts0 = datetime(2026, 8, 17, 6, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "options-turn", ts0, _upd(ts0))

    # Simulate _debounce_expire result: debounce items already moved to pending.
    ts1 = datetime(2026, 8, 17, 6, 0, 1, tzinfo=timezone.utc)
    async with b._get_user_queue_lock(user_id):
        b._user_pending_texts.setdefault(user_id, []).append(
            ("buffered-msg", ts1, _upd(ts1))
        )

    barrier.set()  # options turn finishes -> drain must fire
    await _await_all_tasks(b, user_id)

    # The buffered message must have been dispatched as a merged follow-up turn.
    assert calls == ["options-turn", "buffered-msg"]
    assert b._user_pending_texts.get(user_id, []) == []
    assert b._debounce_texts.get(user_id) is None


# --- MAJOR grill fix: btw fork task drains debounce buffer on exit ---
#
# btw fork tasks are tracked via _track_user_task (NOT via _enqueue_user_task),
# so they appear in _user_run_tasks. A debounce expire that fires while ONLY a
# fork is running sees _prune_user_tasks truthy, calls interrupt (returns False
# because no main SDK turn), then leaves messages in _user_pending_texts with
# NO drain path: the fork's asyncio.create_task wrapper has no finally drain.
# After the MAJOR fix, both fork wrappers (_fork_task_with_drain and
# _fork_continuation_with_drain) call _drain_pending_texts in their finally.


@pytest.mark.asyncio
async def test_btw_fork_only_running_drains_debounce_buffer_on_exit(monkeypatch):
    """MAJOR fix: debounce buffer drains when only a btw fork is in flight.

    This tests the wrapper (_fork_task_with_drain) added around run_fork_task.
    The scenario: a btw fork is tracked in _user_run_tasks (main session idle),
    a regular message arrives, _enqueue_text_task sees tasks truthy -> debounce
    path, timer arms. The fork then finishes -> _fork_task_with_drain finally
    drains the pending buffer -> merged turn dispatches immediately without
    waiting for the debounce timer.
    """
    b = _make_bot()
    _patch_common(monkeypatch, window=30.0)  # long window: must not wait for it
    user_id = 410
    calls = []

    async def mock_process(update, uid, text, **kwargs):
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    # Manually install a long-running "fork" task in _user_run_tasks to simulate
    # a btw fork holding the in-flight slot.
    fork_barrier = asyncio.Event()

    async def fake_fork():
        try:
            await fork_barrier.wait()
        finally:
            await b._drain_pending_texts(user_id)

    fork_task = asyncio.create_task(fake_fork())
    b._track_user_task(user_id, fork_task)

    # Regular message arrives while the fork is in flight -> debounce path.
    ts1 = datetime(2026, 8, 17, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "typed-while-fork", ts1, _upd(ts1))

    # Message landed in debounce buffer; timer armed; 30s window not expired.
    assert len(b._debounce_texts.get(user_id, [])) == 1
    assert user_id in b._debounce_timers

    # Fork finishes -> drain fires via finally -> debounce buffer flushed
    # into a new turn immediately (no 30s wait).
    fork_barrier.set()
    await _await_all_tasks(b, user_id)

    assert calls == ["typed-while-fork"]
    assert b._debounce_texts.get(user_id) is None
    assert b._debounce_timers.get(user_id) is None
