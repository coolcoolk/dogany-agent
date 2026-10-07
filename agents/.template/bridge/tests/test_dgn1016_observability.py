"""DGN-1016 observation triple: real-judge coverage.

The ticket's 2026-08-27 scope correction reframed DGN-1016 from "fix the
root" (impossible: the remote interrupt protocol has no scope field -- not
ours) to three small observability items. This file pins each one with a
REAL judge -- the asserted behavior (log line emitted, message composed,
control request sent) runs through the actual bridge code; mocks are
confined to the periphery (SDK client, Telegram transport):

  O1 -- notice switch. BRIDGE_INFLIGHT_INTERRUPT_NOTICE stays default-OFF
     (owner decision 2026-08-17: conversational naturalness), but when
     opted in it now sends the DEDICATED auto-interrupt copy
     (messages.AUTO_INTERRUPT_NOTICE, draft -- owner confirmation
     pending) instead of reusing the /stop copy. Flag ON -> dedicated
     copy goes out; flag OFF (default) -> nothing goes out.

  O2 -- trigger tag. Driving the REAL /stop handler and the REAL debounce
     expiry each through the REAL sdk_bridge.interrupt() leaves
     distinguishable "trigger=stop" / "trigger=auto" tags in the log
     (the 2026-08-22 09:33 death stayed "[추정]" precisely because this
     tag did not exist then).

  O3 -- stop_task. The SDK's per-task stop control request (present since
     at least 0.2.110, client.py:450) is now reachable through
     SdkBridge.stop_task(): positive send, no-stream / disconnected /
     old-SDK degrade paths, eager-clear avoidance (registry cleanup rides
     the CLI's terminal "stopped" notification through
     _track_task_lifecycle), and send-timeout propagation.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from bridge import bot as bot_mod
from bridge import messages
from bridge import sdk_bridge as sdk_bridge_mod
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState

USER_ID = 9301


def _make_client(connected: bool = True) -> MagicMock:
    client = MagicMock()
    client.interrupt = AsyncMock()
    client.stop_task = AsyncMock()
    client._query = object() if connected else None
    return client


def _make_request(sent: bool = True) -> _PendingRequest:
    handler = MagicMock()
    handler.finalize_all = AsyncMock()
    handler.cancel = AsyncMock()
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


@pytest_asyncio.fixture
async def live_stream():
    """Install a live stream state on the MODULE SINGLETON (the instance
    bot.py actually calls), so handler-driven tests exercise the real
    interrupt()/stop_task() code, then clean it up. Async so the pending
    request's future binds to the running test loop."""
    client = _make_client()
    state = _UserStreamState(client=client, model=None)
    state.pending.append(_make_request(sent=True))
    sdk_bridge_mod.sdk_bridge._streams[USER_ID] = state
    yield state
    sdk_bridge_mod.sdk_bridge._streams.pop(USER_ID, None)


def _make_bot():
    b = bot_mod.TelegramBot()
    fake_bot = SimpleNamespace(send_message=AsyncMock())
    b.application = SimpleNamespace(bot=fake_bot)
    return b, fake_bot


def _stop_update(user_id):
    upd = SimpleNamespace()
    upd.effective_user = SimpleNamespace(id=user_id)
    upd.message = SimpleNamespace(reply_text=AsyncMock())
    return upd


def _text_update(user_id, ts):
    chat = SimpleNamespace(id=1, send_action=AsyncMock())
    msg = SimpleNamespace(chat=chat, date=ts, caption=None, replies=[])
    return SimpleNamespace(
        effective_chat=chat,
        message=msg,
        effective_user=SimpleNamespace(id=user_id),
        callback_query=None,
    )


async def _await_all_tasks(b, user_id, max_rounds=12):
    seen = set()
    for _ in range(max_rounds):
        tasks = [t for t in b._user_run_tasks.get(user_id, set()) if t not in seen]
        if not tasks:
            break
        seen.update(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)


async def _drive_auto_interrupt(b, monkeypatch, user_id=USER_ID):
    """End-to-end DGN-911 auto path: anchor turn in flight, one message
    arrives, the debounce window closes, the REAL debounce-expire handler
    runs (interrupt is whatever the singleton currently provides)."""
    from datetime import datetime, timezone

    monkeypatch.setattr(bot_mod, "BRIDGE_INFLIGHT_DEBOUNCE_S", 0.05)
    barrier = asyncio.Event()
    calls = []

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)
    ts0 = datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _text_update(user_id, ts0))
    ts1 = datetime(2026, 8, 27, 6, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "urgent", ts1, _text_update(user_id, ts1))
    await asyncio.sleep(0.2)
    barrier.set()  # whether or not the interrupt already released it
    await _await_all_tasks(b, user_id)
    return calls


# ---------------------------------------------------------------------------
# O2: the two trigger origins leave DIFFERENT tags in the log (real judge:
# actual log records from the actual sdk_bridge.interrupt()).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_path_logs_trigger_stop(live_stream, monkeypatch, caplog):
    b, _ = _make_bot()

    async def allow_access(update):
        return True

    monkeypatch.setattr(b, "_check_access", allow_access)
    with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        await b._cmd_stop(_stop_update(USER_ID), SimpleNamespace(args=[]))

    tagged = [
        r.getMessage()
        for r in caplog.records
        if "Soft-interrupted turn" in r.getMessage()
    ]
    assert tagged, "real interrupt() never ran"
    assert "trigger=stop" in tagged[0]
    live_stream.client.interrupt.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_path_logs_trigger_auto(live_stream, monkeypatch, caplog):
    b, _ = _make_bot()
    with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        calls = await _drive_auto_interrupt(b, monkeypatch)

    tagged = [
        r.getMessage()
        for r in caplog.records
        if "Soft-interrupted turn" in r.getMessage()
    ]
    assert tagged, "real interrupt() never ran"
    assert "trigger=auto" in tagged[0]
    assert "trigger=stop" not in tagged[0]  # the two origins are separable
    live_stream.client.interrupt.assert_awaited_once()
    assert "urgent" in calls  # deferred message was still delivered


# ---------------------------------------------------------------------------
# O1: opt-in notice sends the DEDICATED draft copy; default stays silent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notice_flag_on_sends_dedicated_auto_copy(
    live_stream, monkeypatch
):
    b, fake_bot = _make_bot()
    monkeypatch.setattr(bot_mod, "BRIDGE_INFLIGHT_INTERRUPT_NOTICE", True)
    await _drive_auto_interrupt(b, monkeypatch)

    sent_texts = [c.args[1] for c in fake_bot.send_message.await_args_list]
    assert messages.AUTO_INTERRUPT_NOTICE in sent_texts
    # The /stop copy must no longer ride the auto path.
    assert messages.STOP_INTERRUPTED not in sent_texts


@pytest.mark.asyncio
async def test_notice_flag_off_default_stays_silent(live_stream, monkeypatch):
    # conftest pins BRIDGE_INFLIGHT_INTERRUPT_NOTICE=0 (the shipped
    # default); assert that default here rather than re-forcing it.
    assert bot_mod.BRIDGE_INFLIGHT_INTERRUPT_NOTICE is False
    b, fake_bot = _make_bot()
    await _drive_auto_interrupt(b, monkeypatch)

    sent_texts = [c.args[1] for c in fake_bot.send_message.await_args_list]
    assert messages.AUTO_INTERRUPT_NOTICE not in sent_texts
    assert messages.STOP_INTERRUPTED not in sent_texts


# ---------------------------------------------------------------------------
# O3: SdkBridge.stop_task -- per-task stop reaches the SDK control request.
# ---------------------------------------------------------------------------


def test_sdk_client_exposes_stop_task():
    """Existence check INSIDE the suite (with the import as its own
    positive control): the pinned claude-agent-sdk really has the per-task
    control request this bridge method calls."""
    ClaudeSDKClient = pytest.importorskip("claude_agent_sdk").ClaudeSDKClient
    assert callable(getattr(ClaudeSDKClient, "stop_task", None))


@pytest.mark.asyncio
async def test_stop_task_sends_and_leaves_registry_to_lifecycle():
    bridge = SdkBridge()
    client = _make_client()
    state = _UserStreamState(client=client, model=None)
    state.active_tasks = {"t1": 0.0}
    state.task_descriptions = {"t1": "detached build"}
    bridge._streams[USER_ID] = state

    result = await bridge.stop_task(USER_ID, "t1")

    assert result is True
    client.stop_task.assert_awaited_once_with("t1")
    # NOT eagerly cleared: the CLI's terminal "stopped" notification is the
    # single source of registry truth (eager clearing would fabricate an
    # under-count if the CLI-side stop failed after the send).
    assert "t1" in state.active_tasks


requires_task_lifecycle = pytest.mark.skipif(
    not sdk_bridge_mod.TASK_LIFECYCLE_AVAILABLE,
    reason="installed claude-agent-sdk has no task-lifecycle messages",
)


@requires_task_lifecycle
def test_stopped_status_is_terminal_and_clears_registry():
    """The cleanup contract stop_task relies on: status='stopped' (what the
    CLI emits after a stop_task control request) is terminal for the
    tracker, so the registry entry drains through the reader loop."""
    TaskNotificationMessage = sdk_bridge_mod.TaskNotificationMessage
    state = _UserStreamState(client=SimpleNamespace(), model=None)
    state.active_tasks = {"t1": 0.0}
    state.task_descriptions = {"t1": "detached build"}
    SdkBridge._track_task_lifecycle(
        state,
        TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="t1",
            status="stopped",
            output_file="",
            summary="",
            uuid="u-t1",
            session_id="sid",
        ),
    )
    assert state.active_tasks == {}
    assert state.task_descriptions == {}


@pytest.mark.asyncio
async def test_stop_task_no_stream_returns_false():
    bridge = SdkBridge()
    assert await bridge.stop_task(424242, "t1") is False


@pytest.mark.asyncio
async def test_stop_task_disconnected_query_returns_false():
    bridge = SdkBridge()
    client = _make_client(connected=False)
    bridge._streams[USER_ID] = _UserStreamState(client=client, model=None)

    assert await bridge.stop_task(USER_ID, "t1") is False
    client.stop_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_task_old_sdk_degrades_with_warning(caplog):
    bridge = SdkBridge()
    # SimpleNamespace, not MagicMock: the attribute must be genuinely
    # absent, the way a pre-stop_task SDK client presents.
    client = SimpleNamespace(_query=object())
    bridge._streams[USER_ID] = _UserStreamState(client=client, model=None)

    with caplog.at_level(logging.WARNING, logger="bridge.sdk_bridge"):
        result = await bridge.stop_task(USER_ID, "t1")

    assert result is False
    assert any(
        "stop_task unavailable" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_stop_task_send_timeout_propagates(monkeypatch):
    bridge = SdkBridge()
    client = _make_client()

    async def hang(task_id):
        await asyncio.sleep(5)

    client.stop_task = hang
    bridge._streams[USER_ID] = _UserStreamState(client=client, model=None)
    monkeypatch.setattr(sdk_bridge_mod, "INTERRUPT_SEND_TIMEOUT", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        await bridge.stop_task(USER_ID, "t1")
