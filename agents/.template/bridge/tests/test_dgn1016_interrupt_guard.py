"""DGN-1016 auto-interrupt background guard unit tests.

A remote (SDK control) interrupt aborts the CLI's SESSION-wide abort tree and
kills every in-session background subagent (measured, DGN-991 rev3). The
DGN-911 in-flight debounce handler used to fire that interrupt whenever the
owner simply kept talking mid-turn -- silently killing running subagents.

Guard pinned here:
  1. no live background tasks -> auto-interrupt fires exactly as before
     (trigger="auto" tagged).
  2. live background tasks -> auto-interrupt is NOT sent; the buffered
     message is delivered via the legacy DGN-616 coalescing drain when the
     turn ends naturally (zero loss).
  3. /stop stays UNGATED: it interrupts (trigger="stop") regardless of live
     background tasks -- owner intent wins.
  4. deferral cap: once the first deferred expiry is older than
     BRIDGE_INFLIGHT_DEFER_CAP_S, the next expiry interrupts despite live
     tasks (sustained owner input eventually wins; a phantom registry entry
     cannot gate forever). Cap=0 disables the guard entirely.
  5. sdk_bridge lifecycle tracking: task_started adds, terminal status from
     EITHER task_notification or task_updated removes, non-terminal keeps;
     live_task_count is 0 without a stream.
  6. SDK floor degradation: the typed task-lifecycle messages postdate the
     declared claude-agent-sdk floor, so sdk_bridge imports them defensively.
     Absent -> tracker no-ops, live_task_count stays 0, auto-interrupt behaves
     exactly as it did before this ticket (status quo, never an import crash).
"""

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage

from bridge import bot as bot_mod
from bridge import messages
from bridge import sdk_bridge as sdk_bridge_mod

# The typed task-lifecycle messages postdate this bridge's declared SDK floor
# (requirements.txt: claude-agent-sdk>=0.1.72), so sdk_bridge imports them
# defensively and re-exports whatever it got. Source them from there: on an
# SDK that has them these are the real classes; on one that does not, the
# lifecycle tests below skip and the degradation test takes over.
TaskNotificationMessage = sdk_bridge_mod.TaskNotificationMessage
TaskStartedMessage = sdk_bridge_mod.TaskStartedMessage
TaskUpdatedMessage = sdk_bridge_mod.TaskUpdatedMessage

requires_task_lifecycle = pytest.mark.skipif(
    not sdk_bridge_mod.TASK_LIFECYCLE_AVAILABLE,
    reason="installed claude-agent-sdk has no task-lifecycle messages",
)


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


def _patch_common(monkeypatch, window=0.05, cap=300.0):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    monkeypatch.setattr(bot_mod, "BRIDGE_INFLIGHT_DEBOUNCE_S", window)
    monkeypatch.setattr(bot_mod, "BRIDGE_INFLIGHT_DEFER_CAP_S", cap)


def _mock_interrupt(monkeypatch, barrier=None, result=True):
    """Record (uid, trigger) pairs; optionally release the mock turn."""
    calls = []

    async def fake_interrupt(uid, **kwargs):
        calls.append((uid, kwargs.get("trigger")))
        if barrier is not None:
            barrier.set()
        return result

    monkeypatch.setattr(sdk_bridge_mod.sdk_bridge, "interrupt", fake_interrupt)
    return calls


def _mock_live_tasks(monkeypatch, count):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "live_task_count", lambda uid: count
    )


# ---------------------------------------------------------------------------
# sdk_bridge lifecycle tracking (acceptance 5)
# ---------------------------------------------------------------------------


def _mk_state():
    return sdk_bridge_mod._UserStreamState(client=SimpleNamespace(), model=None)


def _started(task_id):
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description="bg agent",
        uuid="u-" + task_id,
        session_id="sid",
    )


def _notification(task_id, status):
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file="",
        summary="",
        uuid="u-" + task_id,
        session_id="sid",
    )


def _updated(task_id, status):
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={},
        task_id=task_id,
        patch={"status": status} if status else {},
        status=status,
    )


@requires_task_lifecycle
def test_lifecycle_add_and_terminal_removal():
    state = _mk_state()
    track = sdk_bridge_mod.SdkBridge._track_task_lifecycle

    track(state, _started("t1"))
    track(state, _started("t2"))
    track(state, _started("t3"))
    assert set(state.active_tasks) == {"t1", "t2", "t3"}

    # Terminal via task_notification (completed) removes.
    track(state, _notification("t1", "completed"))
    # Terminal via task_updated (raw "killed" vocabulary) removes.
    track(state, _updated("t2", "killed"))
    assert set(state.active_tasks) == {"t3"}

    # Non-terminal task_updated (running) keeps the entry.
    track(state, _updated("t3", "running"))
    assert set(state.active_tasks) == {"t3"}
    # Patch without status (end_time-only) keeps the entry (status=None).
    track(state, _updated("t3", None))
    assert set(state.active_tasks) == {"t3"}

    # Terminal for an unknown id is a harmless no-op.
    track(state, _notification("ghost", "stopped"))
    assert set(state.active_tasks) == {"t3"}

    # Unrelated message types are ignored (never raise).
    track(
        state,
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid",
            total_cost_usd=0.0,
        ),
    )
    assert set(state.active_tasks) == {"t3"}


@requires_task_lifecycle
@pytest.mark.asyncio
async def test_reader_loop_tracks_lifecycle_on_proactive_branch():
    """End-to-end: lifecycle events flow through _reader_loop even with NO
    pending request (background completions routinely arrive on the proactive
    branch), and the registry reflects add + terminal removal."""
    from unittest.mock import MagicMock

    bridge = sdk_bridge_mod.SdkBridge()
    state = sdk_bridge_mod._UserStreamState(client=MagicMock(), model=None)

    async def fake_receive():
        yield _started("bg1")
        yield _started("bg2")
        yield _updated("bg1", "killed")

    state.client.receive_messages = fake_receive
    bridge._streams[888] = state
    try:
        await bridge._reader_loop(888, state)
        assert set(state.active_tasks) == {"bg2"}
        assert bridge.live_task_count(888) == 1
    finally:
        bridge._streams.pop(888, None)


def test_live_task_count_no_stream_is_zero():
    bridge = sdk_bridge_mod.SdkBridge()
    assert bridge.live_task_count(12345) == 0


def test_live_task_count_reads_stream_state():
    bridge = sdk_bridge_mod.SdkBridge()
    state = _mk_state()
    state.active_tasks = {"t1": 0.0, "t2": 0.0}
    bridge._streams[777] = state
    try:
        assert bridge.live_task_count(777) == 2
    finally:
        bridge._streams.pop(777, None)


# ---------------------------------------------------------------------------
# (a) no live background tasks -> interrupt fires as before (acceptance 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_live_tasks_interrupts_with_auto_trigger(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05)
    user_id = 500
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch, barrier=barrier)
    _mock_live_tasks(monkeypatch, 0)

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 8, 22, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))
    await b._enqueue_text_task(user_id, "typed", ts1, _upd(ts1))

    await asyncio.sleep(0.2)
    await _await_all_tasks(b, user_id)

    assert interrupt_calls == [(user_id, "auto")]
    assert calls == ["anchor", "typed"]
    assert b._interrupt_deferred_since.get(user_id) is None


# ---------------------------------------------------------------------------
# (b) live background tasks -> defer, deliver via coalescing, zero loss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_tasks_defer_interrupt_and_deliver_on_turn_end(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05)
    user_id = 501
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch, barrier=barrier)
    _mock_live_tasks(monkeypatch, 2)

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 8, 22, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))
    await b._enqueue_text_task(user_id, "must-survive", ts1, _upd(ts1))

    await asyncio.sleep(0.2)  # window expires -> guard defers

    # No interrupt fired; message parked in the fail-safe pending buffer;
    # deferral clock started.
    assert interrupt_calls == []
    assert len(b._user_pending_texts.get(user_id, [])) == 1
    assert b._interrupt_deferred_since.get(user_id) is not None
    assert calls == []  # anchor still running, subagents alive

    barrier.set()  # anchor ends naturally -> legacy DGN-616 drain delivers
    await _await_all_tasks(b, user_id)

    assert interrupt_calls == []  # never interrupted
    assert calls == ["anchor", "must-survive"]  # zero loss
    assert b._user_pending_texts.get(user_id, []) == []
    # Drain closed the wait: deferral clock reset for the next wait.
    assert b._interrupt_deferred_since.get(user_id) is None
    # Silent policy: no notice of the deferral.
    assert b.application.bot.sent == []


@pytest.mark.asyncio
async def test_live_task_probe_failure_falls_through_to_interrupt(monkeypatch):
    """A broken probe must never block the interrupt path (fail-open)."""
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05)
    user_id = 502
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch, barrier=barrier)

    def broken_probe(uid):
        raise RuntimeError("probe broken")

    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "live_task_count", broken_probe
    )

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 8, 22, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))
    await b._enqueue_text_task(user_id, "typed", ts1, _upd(ts1))

    await asyncio.sleep(0.2)
    await _await_all_tasks(b, user_id)

    assert interrupt_calls == [(user_id, "auto")]
    assert calls == ["anchor", "typed"]


# ---------------------------------------------------------------------------
# (c) /stop stays ungated (acceptance 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_stop_interrupts_despite_live_tasks(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch)
    user_id = 503

    async def allow_access(update):
        return True

    monkeypatch.setattr(b, "_check_access", allow_access)
    interrupt_calls = _mock_interrupt(monkeypatch, result=True)
    _mock_live_tasks(monkeypatch, 3)  # live subagents present

    # A deferred wait is in progress; /stop must clear its clock too.
    b._interrupt_deferred_since[user_id] = time.monotonic()

    upd = _upd(user_id=user_id)
    await b._cmd_stop(upd, SimpleNamespace(args=[]))

    # /stop interrupted regardless of live tasks, tagged as "stop".
    assert interrupt_calls == [(user_id, "stop")]
    assert upd.message.replies == [
        f"{messages.STOP_INTERRUPTED}\n{messages.STOP_BG_NOTE}"
    ]
    assert b._interrupt_deferred_since.get(user_id) is None


@pytest.mark.asyncio
async def test_cmd_stop_no_live_tasks_unchanged(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch)
    user_id = 504

    async def allow_access(update):
        return True

    monkeypatch.setattr(b, "_check_access", allow_access)
    interrupt_calls = _mock_interrupt(monkeypatch, result=True)
    _mock_live_tasks(monkeypatch, 0)

    upd = _upd(user_id=user_id)
    await b._cmd_stop(upd, SimpleNamespace(args=[]))

    assert interrupt_calls == [(user_id, "stop")]
    assert upd.message.replies == [
        f"{messages.STOP_INTERRUPTED}\n{messages.STOP_BG_NOTE}"
    ]


# ---------------------------------------------------------------------------
# (d) deferral cap (acceptance 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defer_cap_exceeded_interrupts_despite_live_tasks(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05, cap=10.0)
    user_id = 505
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch, barrier=barrier)
    _mock_live_tasks(monkeypatch, 1)

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))

    # Simulate a wait whose first deferral is already past the cap.
    b._interrupt_deferred_since[user_id] = time.monotonic() - 11.0

    ts1 = datetime(2026, 8, 22, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "urgent", ts1, _upd(ts1))

    await asyncio.sleep(0.2)
    await _await_all_tasks(b, user_id)

    # Cap exceeded: interrupt fired despite the live task, clock cleared.
    assert interrupt_calls == [(user_id, "auto")]
    assert calls == ["anchor", "urgent"]
    assert b._interrupt_deferred_since.get(user_id) is None


@pytest.mark.asyncio
async def test_cap_zero_disables_guard(monkeypatch):
    """Cap=0 restores pre-DGN-1016 behavior: interrupt even with live tasks."""
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05, cap=0.0)
    user_id = 506
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch, barrier=barrier)
    probe_calls = []

    def probe(uid):
        probe_calls.append(uid)
        return 5

    monkeypatch.setattr(sdk_bridge_mod.sdk_bridge, "live_task_count", probe)

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    ts1 = datetime(2026, 8, 22, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))
    await b._enqueue_text_task(user_id, "typed", ts1, _upd(ts1))

    await asyncio.sleep(0.2)
    await _await_all_tasks(b, user_id)

    assert probe_calls == []  # guard disabled: probe never consulted
    assert interrupt_calls == [(user_id, "auto")]
    assert calls == ["anchor", "typed"]


# ---------------------------------------------------------------------------
# continued typing during a deferred wait re-arms and re-defers (no loss)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_deferrals_keep_first_clock_and_merge_all(monkeypatch):
    b = _make_bot()
    _patch_common(monkeypatch, window=0.05, cap=300.0)
    user_id = 507
    calls = []
    barrier = asyncio.Event()
    interrupt_calls = _mock_interrupt(monkeypatch, barrier=barrier)
    _mock_live_tasks(monkeypatch, 1)

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    ts0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))

    ts1 = datetime(2026, 8, 22, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "first", ts1, _upd(ts1))
    await asyncio.sleep(0.2)  # first expiry -> defer, clock starts
    first_clock = b._interrupt_deferred_since.get(user_id)
    assert first_clock is not None

    ts2 = datetime(2026, 8, 22, 6, 0, 2, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "second", ts2, _upd(ts2))
    await asyncio.sleep(0.2)  # second expiry -> defer again, clock NOT reset
    assert b._interrupt_deferred_since.get(user_id) == first_clock
    assert interrupt_calls == []

    barrier.set()
    await _await_all_tasks(b, user_id)

    assert interrupt_calls == []
    assert len(calls) == 2
    assert calls[0] == "anchor"
    merged = calls[1]
    assert "first" in merged and "second" in merged
    assert merged.index("first") < merged.index("second")
    assert b._interrupt_deferred_since.get(user_id) is None


# ---------------------------------------------------------------------------
# (e) SDK floor degradation (acceptance 6)
# ---------------------------------------------------------------------------


def test_absent_task_lifecycle_degrades_to_status_quo(monkeypatch):
    """Simulate an SDK that predates the typed task-lifecycle messages: the
    tracker must no-op (never raise, never count) so live_task_count stays 0
    and the auto-interrupt keeps its pre-DGN-1016 behavior."""

    class _Absent:
        pass

    monkeypatch.setattr(sdk_bridge_mod, "TaskStartedMessage", _Absent)
    monkeypatch.setattr(sdk_bridge_mod, "TaskNotificationMessage", _Absent)
    monkeypatch.setattr(sdk_bridge_mod, "TaskUpdatedMessage", _Absent)
    monkeypatch.setattr(sdk_bridge_mod, "TERMINAL_TASK_STATUSES", frozenset())

    bridge = sdk_bridge_mod.SdkBridge()
    state = _mk_state()
    bridge._streams[999] = state
    try:
        # A real task_started payload shape, seen through the sentinel types.
        sdk_bridge_mod.SdkBridge._track_task_lifecycle(
            state, SimpleNamespace(task_id="t1", status="running")
        )
        assert state.active_tasks == {}
        assert bridge.live_task_count(999) == 0
    finally:
        bridge._streams.pop(999, None)


def test_sentinel_types_never_match_a_real_message():
    """The ImportError fallback must be isinstance-safe: sentinels are classes
    (not None), so the isinstance() checks are always-false no-ops rather than
    a TypeError that would kill the reader loop."""
    for name in (
        "TaskStartedMessage",
        "TaskNotificationMessage",
        "TaskUpdatedMessage",
    ):
        assert isinstance(getattr(sdk_bridge_mod, name), type)
