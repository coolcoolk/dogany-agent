"""DGN-1689: background-completion wake-up turns.

Claude Code may enqueue a task_notification as an ownerless input after a
background command or subagent completes.  These tests replay the SDK stream
at the reader-loop seam.  DGN-1689 first made the notification-only turn quiet
by default (PUSH opt-in); DGN-1642 reversed that default after it dropped real
reports (see test_dgn1642_task_notification_delivery.py).  A wake-up now
delivers by default and is silenced only by an explicit NO_PUSH.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic bridge environment
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from bridge import sdk_bridge
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState


pytestmark = pytest.mark.skipif(
    not sdk_bridge.TASK_LIFECYCLE_AVAILABLE,
    reason="installed SDK has no task-notification message type",
)


class _FakeClient:
    def __init__(self, messages):
        self.messages = list(messages)

    async def receive_messages(self):
        for message in self.messages:
            yield message


def _assistant(text):
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-5",
        stop_reason="end_turn",
        parent_tool_use_id=None,
    )


def _result():
    message = MagicMock(spec=ResultMessage)
    message.session_id = "sess-1"
    message.is_error = False
    message.result = ""
    message.num_turns = 1
    return message


def _task_notification():
    return sdk_bridge.TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="background-1",
        status="completed",
        output_file="",
        summary="background work completed",
        uuid="notification-1",
        session_id="sess-1",
    )


async def _owner_request():
    return _PendingRequest(
        user_id=1,
        chat_id=111,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=asyncio.get_running_loop().create_future(),
        user_message="owner question",
    )


@pytest.mark.asyncio
async def test_owner_turn_is_still_delivered():
    bridge = SdkBridge()
    request = await _owner_request()
    state = _UserStreamState(
        client=_FakeClient([_assistant("owner result"), _result()]), model=None
    )
    state.pending.append(request)

    await bridge._reader_loop(1, state)

    assert request.future.done()
    assert request.future.result().content == "owner result"


@pytest.mark.asyncio
async def test_task_notification_only_turn_is_quiet_with_no_push():
    push = AsyncMock()
    bridge = SdkBridge()
    state = _UserStreamState(
        client=_FakeClient([
            _task_notification(), _assistant("done\nNO_PUSH"), _result(),
        ]),
        model=None,
    )
    state.last_chat_id = 111
    state.proactive_push = push

    await bridge._reader_loop(1, state)

    push.assert_not_awaited()
    assert state.task_notification_wakeup is False


@pytest.mark.asyncio
async def test_task_notification_turn_with_push_marker_is_delivered():
    push = AsyncMock()
    bridge = SdkBridge()
    state = _UserStreamState(
        client=_FakeClient([
            _task_notification(), _assistant("new result\nPUSH"), _result(),
        ]),
        model=None,
    )
    state.last_chat_id = 111
    state.proactive_push = push

    await bridge._reader_loop(1, state)

    push.assert_awaited_once()
    assert push.await_args.args[1] == "new result"
