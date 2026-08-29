"""DGN-1015: subagent silent-death visibility.

Root cause (measured, see worklog/DGN-1015): the 2026-08-22 09:33 subagent
death was NOT an unexplained hang. Metal's own transcript and the killed
subagent's own jsonl both timestamp "[Request interrupted by user]" at
00:33:23.722Z -- the DGN-911 in-flight debounce auto-interrupt (later gated
by DGN-1016) fired while a background subagent (agentId aa6710a3027cbfd1d,
"DGN-991 1차 빌드") was live, and the SDK control-request interrupt aborted
the CLI's session-wide abort tree, killing it. No task_notification or
task_updated terminal event was EVER emitted for that agentId anywhere in
the main session -- the kill is invisible to both the DGN-1016 active_tasks
registry (would leak it as a phantom "still live" entry forever, since no
lifecycle event will ever arrive to clear it) and to the owner (no notice of
any kind was sent).

This file covers the fix layered on top of DGN-1016's registry:
  1. sdk_bridge._track_task_lifecycle also records/clears a description per
     task_id (task_descriptions), so a kill notice can name what died.
  2. sdk_bridge.interrupt(): when it succeeds while active_tasks is
     non-empty, it now (a) clears active_tasks/task_descriptions for the
     killed ids -- closing the phantom-entry leak -- and (b) stashes their
     descriptions for the caller to retrieve via pop_interrupt_killed().
  3. pop_interrupt_killed(): read-once getter, [] when nothing died or no
     stream exists.
  4. bot.py wiring: the auto-interrupt path (defer-cap-exceeded kill) and
     the /stop path both fetch pop_interrupt_killed() after a successful
     interrupt and, ONLY when it is non-empty, send
     messages.BG_SUBAGENT_KILLED_NOTICE naming what died. This is
     unconditional (not gated by the silent-by-default
     BRIDGE_INFLIGHT_INTERRUPT_NOTICE flag, which governs a different,
     older fact -- "your message caused an interrupt" -- not "a background
     subagent is now confirmed dead").
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge import bot as bot_mod
from bridge import messages
from bridge import sdk_bridge as sdk_bridge_mod
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState

USER_ID = 9001


def _make_client(connected: bool = True) -> MagicMock:
    client = MagicMock()
    client.interrupt = AsyncMock()
    client._query = object() if connected else None
    return client


def _make_request(sent: bool) -> _PendingRequest:
    handler = MagicMock()
    handler.finalize_all = AsyncMock()
    handler.drafts = []
    return _PendingRequest(
        user_id=USER_ID,
        chat_id=1,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=asyncio.get_event_loop().create_future(),
        user_message="msg",
        sent=sent,
        streaming_handler=handler,
    )


# ---------------------------------------------------------------------------
# (1) description tracking rides task_descriptions alongside active_tasks
# ---------------------------------------------------------------------------

requires_task_lifecycle = pytest.mark.skipif(
    not sdk_bridge_mod.TASK_LIFECYCLE_AVAILABLE,
    reason="installed claude-agent-sdk has no task-lifecycle messages",
)


@requires_task_lifecycle
def test_track_task_lifecycle_records_and_clears_description():
    TaskStartedMessage = sdk_bridge_mod.TaskStartedMessage
    TaskNotificationMessage = sdk_bridge_mod.TaskNotificationMessage
    state = _UserStreamState(client=SimpleNamespace(), model=None)
    track = SdkBridge._track_task_lifecycle

    track(
        state,
        TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="t1",
            description="DGN-991 1차 빌드",
            uuid="u-t1",
            session_id="sid",
        ),
    )
    assert state.active_tasks == {"t1": state.active_tasks["t1"]}
    assert state.task_descriptions == {"t1": "DGN-991 1차 빌드"}

    track(
        state,
        TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="t1",
            status="completed",
            output_file="",
            summary="",
            uuid="u-t1",
            session_id="sid",
        ),
    )
    assert state.active_tasks == {}
    assert state.task_descriptions == {}


@requires_task_lifecycle
def test_track_task_lifecycle_missing_description_leaves_no_entry():
    TaskStartedMessage = sdk_bridge_mod.TaskStartedMessage
    state = _UserStreamState(client=SimpleNamespace(), model=None)
    SdkBridge._track_task_lifecycle(
        state,
        TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="t2",
            description="",
            uuid="u-t2",
            session_id="sid",
        ),
    )
    assert "t2" in state.active_tasks
    assert "t2" not in state.task_descriptions  # falsy description not stored


# ---------------------------------------------------------------------------
# (2)+(3) interrupt() clears active_tasks and stashes killed descriptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_with_live_tasks_clears_registry_and_stashes_killed():
    bridge = SdkBridge()
    client = _make_client()
    state = _UserStreamState(client=client, model=None)
    head = _make_request(sent=True)
    state.pending.append(head)
    state.active_tasks = {"t1": 0.0, "t2": 0.0}
    state.task_descriptions = {"t1": "DGN-991 1차 빌드"}  # t2 has no description
    bridge._streams[USER_ID] = state

    result = await bridge.interrupt(USER_ID, trigger="auto")

    assert result is True
    # DGN-1015: the phantom-entry leak this closes -- no lifecycle event will
    # ever arrive for an interrupt-killed task, so this registry MUST be
    # cleared here or it leaks forever.
    assert state.active_tasks == {}
    assert state.task_descriptions == {}
    killed = bridge.pop_interrupt_killed(USER_ID)
    assert set(killed) == {"DGN-991 1차 빌드", "t2"}  # t2 falls back to its id
    # Read-once: a second pop is empty.
    assert bridge.pop_interrupt_killed(USER_ID) == []


@pytest.mark.asyncio
async def test_interrupt_with_no_live_tasks_reports_nothing_killed():
    bridge = SdkBridge()
    client = _make_client()
    state = _UserStreamState(client=client, model=None)
    head = _make_request(sent=True)
    state.pending.append(head)
    bridge._streams[USER_ID] = state

    result = await bridge.interrupt(USER_ID, trigger="stop")

    assert result is True
    assert bridge.pop_interrupt_killed(USER_ID) == []


def test_pop_interrupt_killed_no_stream_is_empty():
    bridge = SdkBridge()
    assert bridge.pop_interrupt_killed(424242) == []


# ---------------------------------------------------------------------------
# (4) bot.py wiring: /stop path names what died, only when something did
# ---------------------------------------------------------------------------


def _make_bot():
    b = bot_mod.TelegramBot()
    fake_bot = SimpleNamespace(send_message=AsyncMock())
    b.application = SimpleNamespace(bot=fake_bot)
    return b


def _fake_update(user_id):
    upd = SimpleNamespace()
    upd.effective_user = SimpleNamespace(id=user_id)
    upd.message = SimpleNamespace(reply_text=AsyncMock())
    return upd


@pytest.mark.asyncio
async def test_cmd_stop_names_killed_subagent(monkeypatch):
    b = _make_bot()
    user_id = 9101

    async def allow_access(update):
        return True

    monkeypatch.setattr(b, "_check_access", allow_access)
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "interrupt", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge,
        "pop_interrupt_killed",
        lambda uid: ["DGN-991 1차 빌드"],
    )

    upd = _fake_update(user_id)
    await b._cmd_stop(upd, SimpleNamespace(args=[]))

    expected = (
        f"{messages.STOP_INTERRUPTED}\n{messages.STOP_BG_NOTE}\n"
        + messages.BG_SUBAGENT_KILLED_NOTICE.format(names="DGN-991 1차 빌드")
    )
    upd.message.reply_text.assert_awaited_once_with(expected)


@pytest.mark.asyncio
async def test_cmd_stop_silent_when_nothing_killed(monkeypatch):
    b = _make_bot()
    user_id = 9102

    async def allow_access(update):
        return True

    monkeypatch.setattr(b, "_check_access", allow_access)
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "interrupt", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "pop_interrupt_killed", lambda uid: []
    )

    upd = _fake_update(user_id)
    await b._cmd_stop(upd, SimpleNamespace(args=[]))

    # No kill-notice line appended -- unchanged legacy copy.
    upd.message.reply_text.assert_awaited_once_with(
        f"{messages.STOP_INTERRUPTED}\n{messages.STOP_BG_NOTE}"
    )


# ---------------------------------------------------------------------------
# (4) bot.py wiring: auto-interrupt (defer-cap-exceeded kill) names the dead
# ---------------------------------------------------------------------------


async def _await_all_tasks(b, user_id, max_rounds=12):
    seen = set()
    for _ in range(max_rounds):
        tasks = [t for t in b._user_run_tasks.get(user_id, set()) if t not in seen]
        if not tasks:
            break
        seen.update(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_auto_interrupt_cap_exceeded_kill_is_notified(monkeypatch):
    """Mirrors test_dgn1016's cap-exceeded scenario, but this time the mocked
    interrupt() actually "kills" a tracked task (via pop_interrupt_killed),
    and DGN-1015 must surface that -- the exact residual silent-death path
    DGN-1016 left open (its own test asserts silence, but only for the
    no-kill deferral case, never for an actual kill)."""
    import time
    from datetime import datetime, timezone

    b = bot_mod.TelegramBot()
    fake_bot = SimpleNamespace(send_message=AsyncMock())
    b.application = SimpleNamespace(bot=fake_bot)
    user_id = 9103

    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    monkeypatch.setattr(bot_mod, "BRIDGE_INFLIGHT_DEBOUNCE_S", 0.05)
    monkeypatch.setattr(bot_mod, "BRIDGE_INFLIGHT_DEFER_CAP_S", 10.0)
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "live_task_count", lambda uid: 1
    )
    calls = []
    barrier = asyncio.Event()

    async def fake_interrupt(uid, **kwargs):
        barrier.set()  # releases the anchor turn, mirroring a real kill
        return True

    monkeypatch.setattr(sdk_bridge_mod.sdk_bridge, "interrupt", fake_interrupt)
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge,
        "pop_interrupt_killed",
        lambda uid: ["DGN-991 1차 빌드"],
    )

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    def _upd(ts):
        chat = SimpleNamespace(id=1, send_action=AsyncMock())
        msg = SimpleNamespace(chat=chat, date=ts, caption=None, replies=[])
        return SimpleNamespace(
            effective_chat=chat,
            message=msg,
            effective_user=SimpleNamespace(id=user_id),
            callback_query=None,
        )

    ts0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))

    # First expiry defers (cap not yet exceeded); simulate the cap already
    # being blown so the SECOND expiry interrupts anyway.
    b._interrupt_deferred_since[user_id] = time.monotonic() - 11.0
    ts1 = datetime(2026, 8, 22, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "urgent", ts1, _upd(ts1))

    await asyncio.sleep(0.2)
    await _await_all_tasks(b, user_id)

    fake_bot.send_message.assert_awaited_once_with(
        1, messages.BG_SUBAGENT_KILLED_NOTICE.format(names="DGN-991 1차 빌드")
    )
