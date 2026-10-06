"""DGN-1642: a task-notification wake-up delivers by default.

Live failure (dev-instance bot.log 2026-09-27 18:04:47, v2.6.0-dev.58): Claude Code
opened a turn with a <task-notification> for a completed background command
("Run new test post-merge and push dev").  The model wrote a merge report plus
a pending owner question, with no trailing PUSH (it was never told the
wake-up was quiet), and the DGN-1689 quiet default dropped all 129 chars.
Nothing replaced it: the owner learns about it only by asking.

The rule applied (DGN-1642 ticket): a turn that exists is a turn worth
delivering; silence is the author's explicit NO_PUSH, never a default the
author cannot see.  Explicit quiet injections keep their own default.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic bridge environment
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from bridge import sdk_bridge
from bridge.sdk_bridge import SdkBridge, _UserStreamState


pytestmark = pytest.mark.skipif(
    not sdk_bridge.TASK_LIFECYCLE_AVAILABLE,
    reason="installed SDK has no task-notification message type",
)

# The dropped text, verbatim from transcript e4dec9b7 09:04:40Z (129 chars).
INCIDENT_TEXT = (
    "형님, 재시작 안내를 되살린 수정이 저장소에도 올라갔어요. 이제 다음 개발판에 들어가요.\n\n"
    "아까 여쭌 팩 업데이트 안내 첫 줄은 아직 답을 기다리고 있어요. "
    "1번은 팩 이름을 붙이는 거고, 2번은 지금처럼 버전 숫자만 두는 거예요."
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


def _result(is_error=False):
    message = MagicMock(spec=ResultMessage)
    message.session_id = "sess-1"
    message.is_error = is_error
    message.result = ""
    message.num_turns = 1
    return message


def _task_notification():
    return sdk_bridge.TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="bsz268vux",
        status="completed",
        output_file="",
        summary='Background command "Run new test post-merge and push dev" '
        "completed (exit code 0)",
        uuid="notification-1",
        session_id="sess-1",
    )


def _state(messages, push):
    state = _UserStreamState(client=_FakeClient(messages), model=None)
    state.last_chat_id = 111
    state.proactive_push = push
    return state


@pytest.mark.asyncio
async def test_incident_report_from_wakeup_reaches_owner(caplog):
    """Regression: the exact 18:04 text is delivered, not dropped."""
    push = AsyncMock()
    state = _state(
        [_task_notification(), _assistant(INCIDENT_TEXT), _result()], push
    )

    with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        await SdkBridge()._reader_loop(1, state)

    push.assert_awaited_once()
    assert push.await_args.args[0] == 111
    assert push.await_args.args[1] == INCIDENT_TEXT
    assert "Quiet injected turn" not in caplog.text
    # Measurement line: length only, never content.
    assert "Task-notification wake-up for user 1 delivered" in caplog.text
    assert "재시작" not in caplog.text
    assert state.task_notification_wakeup is False


@pytest.mark.asyncio
async def test_wakeup_with_no_push_stays_silent():
    """The silence path DGN-1689 relied on survives: explicit NO_PUSH."""
    push = AsyncMock()
    state = _state(
        [_task_notification(), _assistant("NO_PUSH"), _result()], push
    )

    await SdkBridge()._reader_loop(1, state)

    push.assert_not_awaited()
    assert state.task_notification_wakeup is False


@pytest.mark.asyncio
async def test_wakeup_report_ending_in_no_push_stays_silent():
    push = AsyncMock()
    state = _state(
        [_task_notification(), _assistant("tests passed\nNO_PUSH"), _result()],
        push,
    )

    await SdkBridge()._reader_loop(1, state)

    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_quiet_injection_still_defaults_silent():
    """No re-opened noise: an explicit quiet injection keeps its default,
    even when a task notification lands inside the same turn."""
    push = AsyncMock()
    state = _state(
        [_task_notification(), _assistant("recorded"), _result()], push
    )
    state.injected_turn_mode = "quiet"

    await SdkBridge()._reader_loop(1, state)

    push.assert_not_awaited()
    assert state.injected_turn_mode is None


@pytest.mark.asyncio
async def test_wakeup_does_not_leak_past_turn_boundary():
    """A wake-up turn followed by an explicit quiet turn: the quiet turn is
    still silent and the wake-up still delivered (no state bleed)."""
    push = AsyncMock()
    bridge = SdkBridge()
    state = _state(
        [_task_notification(), _assistant("pushed to dev"), _result()], push
    )
    await bridge._reader_loop(1, state)
    push.assert_awaited_once()

    state.client = _FakeClient([_assistant("recorded"), _result()])
    state.injected_turn_mode = "quiet"
    await bridge._reader_loop(1, state)
    push.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_wakeup_surfaces_failure_notice():
    """A wake-up that ended in error may have held a report; its failure is
    surfaced like any other ownerless turn, not suppressed as quiet."""
    push = AsyncMock()
    state = _state([_task_notification(), _result(is_error=True)], push)

    await SdkBridge()._reader_loop(1, state)

    push.assert_awaited_once()
    assert push.await_args.args[1] == sdk_bridge.messages.PROACTIVE_TURN_FAILED
