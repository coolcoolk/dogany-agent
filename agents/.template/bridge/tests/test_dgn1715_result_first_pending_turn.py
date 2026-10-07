"""DGN-1715: a delivered result line opens the result-first latch in any path.

Observed on a dev instance: an owner message queued into a running dispatch-return
turn made the turn *pending*, so its result line rendered in the owner's
progress bubble while the reader never opened the DGN-1687 latch -- every
later Bash/Write was denied for the rest of the turn, and the latch outlived
the turn.  Separately, the CLI runs PreToolUse right after a text block,
before the bridge's owner push returns.  These tests replay both shapes and
run the real gate script at the moment the CLI would.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic bridge environment
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from bridge import sdk_bridge as sdk
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState

GATE = Path(__file__).resolve().parents[2] / "routines" / "dispatch-return-result-first-gate.py"
RESULT_LINE = "Result: dgn1702-grill finished; one blocker found, fix dispatch next."
WORK_TOOL = {"tool_name": "Bash", "tool_input": {"command": "routines/dispatch-detached.sh fix"}}
# The gate ships in the agent tree's routines/, not with a standalone bridge
# (the extracted public build); there only the bridge half is checkable.
HAS_GATE = GATE.is_file()
needs_gate = pytest.mark.skipif(not HAS_GATE, reason="no routines/ gate beside this bridge")


def _gate_env(context, grace="0.2"):
    env = dict(os.environ)
    env.pop("GATE_DISPATCH_RETURN_RESULT_FIRST", None)
    env.pop("GATE_ALL", None)
    env["DISPATCH_RETURN_CONTEXT_PATH"] = str(context)
    env["DISPATCH_RETURN_GRACE_SECONDS"] = grace
    return env


def _start_gate(context, grace="0.2"):
    # DGN-1738: the gate acts only for the session that owns the record
    # (_dispatch_return_state writes "sess-1"), as the owning CLI would send.
    payload = dict(WORK_TOOL, cwd=str(context.parent), session_id="sess-1")
    proc = subprocess.Popen(
        [sys.executable, str(GATE)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_gate_env(context, grace),
    )
    proc.stdin.write(json.dumps(payload))
    proc.stdin.close()
    return proc


def _denied(proc):
    out = proc.stdout.read()
    assert proc.wait(timeout=15) == 0, proc.stderr.read()
    if not out.strip():
        return False
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


class _GateProbeClient:
    """Yields the turn, running the real gate where the CLI would."""

    def __init__(self, context):
        self.context = context
        self.denials = []

    async def receive_messages(self):
        yield AssistantMessage(
            content=[TextBlock(text=RESULT_LINE),
                     ToolUseBlock(id="tu-1", name="Bash", input=WORK_TOOL["tool_input"])],
            model="claude-opus-5-5",
            stop_reason="tool_use",
            parent_tool_use_id=None,
        )
        # The CLI evaluates PreToolUse for tu-1 now, before the turn ends.
        if HAS_GATE:
            self.denials.append(_denied(_start_gate(self.context)))
        else:
            self.denials.append(json.loads(self.context.read_text())["visible"] is not True)
        result = MagicMock(spec=ResultMessage)
        result.session_id = "sess-1"
        result.is_error = False
        result.result = ""
        result.num_turns = 1
        yield result


def _owner_request():
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    loop.close()
    request = _PendingRequest(
        user_id=7, chat_id=11, model=None, requested_session_id=None,
        permission_callback=None, typing_callback=None, future=future,
        user_message="끝났어 수정?",
    )
    request.sent = True
    return request


def _dispatch_return_state(push, client=None):
    state = _UserStreamState(client=client or MagicMock(), model=None)
    state.last_chat_id = 11
    state.last_session_id = "sess-1"
    state.proactive_push = push
    state.dispatch_return_turn_id = "turn-1715"
    sdk._write_dispatch_return_context("turn-1715", 7, "sess-1")
    return state


def test_owner_folded_turn_result_line_opens_latch_before_tool():
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        push = AsyncMock()
        client = _GateProbeClient(context)
        state = _dispatch_return_state(push, client)
        state.pending.append(_owner_request())
        bridge = SdkBridge()
        with patch.object(bridge, "_finalize_result", AsyncMock(return_value=False)):
            asyncio.run(bridge._reader_loop(7, state))

        push.assert_awaited_once_with(11, RESULT_LINE, False, False)
        assert client.denials == [False]
        # The latch dies with its turn instead of gating the next ones.
        assert not context.exists()
        assert state.dispatch_return_turn_id is None


@needs_gate
def test_gate_waits_for_the_in_flight_push_then_allows():
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        seen = {}

        async def slow_push(*_args):
            seen["row"] = json.loads(context.read_text())
            seen["gate"] = _start_gate(context)
            await asyncio.sleep(0.6)

        state = _dispatch_return_state(AsyncMock(side_effect=slow_push))
        assert asyncio.run(SdkBridge()._send_dispatch_return_first_text(7, state, RESULT_LINE))

        assert seen["row"]["delivering"] is True and seen["row"]["visible"] is False
        assert not _denied(seen["gate"])
        assert json.loads(context.read_text())["visible"] is True


def _real_push(fail_calls):
    """DGN-1736 F1: the REAL bot._proactive_push -> _send_smart, with the
    Telegram transport (bot.send_message) raising for the first `fail_calls`
    calls. Each owner send tries HTML then a plain fallback, so 2 failures
    fail one delivery. Mocking state.proactive_push to raise tests the wrong
    layer: the real _proactive_push used to swallow the error."""
    import bridge.bot as bot_mod
    import telegram.error

    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    calls, sent = [], []

    async def send_message(chat_id, text, **kwargs):
        calls.append(text)
        if len(calls) <= fail_calls:
            raise telegram.error.NetworkError("Bad Gateway")
        sent.append(text)
        return MagicMock(message_id=len(calls))

    bot.application.bot.send_message = AsyncMock(side_effect=send_message)
    bot.application.bot.delete_message = AsyncMock()
    bot._kick_update_offer = lambda chat_id: None
    return bot._proactive_push, calls, sent


def test_failed_push_keeps_the_latch_closed():
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        push, calls, sent = _real_push(fail_calls=2)
        state = _dispatch_return_state(push)
        assert not asyncio.run(SdkBridge()._send_dispatch_return_first_text(7, state, RESULT_LINE))

        assert len(calls) == 2 and sent == []  # the transport really raised
        row = json.loads(context.read_text())
        assert row["visible"] is False and row["delivering"] is False
        assert state.dispatch_return_result_sent is False
        # Nothing recorded as delivered, so finalize subtracts nothing.
        assert state.dispatch_return_delivered is None
        if HAS_GATE:
            assert _denied(_start_gate(context))


def test_failed_first_text_is_still_delivered_by_the_proactive_flush():
    """No pending request: the failed first text is not popped from the
    proactive buffer, so the turn-end flush delivers it."""
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        push, calls, sent = _real_push(fail_calls=2)
        state = _dispatch_return_state(push)
        bridge = SdkBridge()
        turn = AssistantMessage(content=[TextBlock(text=RESULT_LINE)],
                                model="claude-opus-5-5", parent_tool_use_id=None)
        asyncio.run(bridge._handle_proactive_message(7, state, turn))

        assert len(calls) == 2 and sent == []
        assert state.proactive_texts == [RESULT_LINE]
        assert state.dispatch_return_result_sent is False

        result = MagicMock(spec=ResultMessage)
        result.session_id = "sess-1"
        result.is_error = False
        result.result = ""
        asyncio.run(bridge._handle_proactive_message(7, state, result))

        assert len(sent) == 1 and RESULT_LINE in sent[0]


def test_failed_first_text_is_not_subtracted_from_the_final():
    """Owner request pending: the failed first text stays in the final
    assembly and is not recorded as delivered, so finalize keeps it."""
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        push, calls, sent = _real_push(fail_calls=10**6)
        client = _GateProbeClient(context)
        state = _dispatch_return_state(push, client)
        request = _owner_request()
        state.pending.append(request)
        bridge = SdkBridge()
        at_finalize = {}

        async def finalize(user_id, st, req, *args, **kwargs):
            at_finalize["texts"] = list(req.last_assistant_texts)
            at_finalize["delivered"] = st.dispatch_return_delivered
            return False

        with patch.object(bridge, "_finalize_result", AsyncMock(side_effect=finalize)):
            asyncio.run(bridge._reader_loop(7, state))

        assert calls and sent == []
        # The latch stayed closed, so the gate denied the tool.
        assert client.denials == [True]
        assert RESULT_LINE in at_finalize["texts"]
        assert at_finalize["delivered"] is None
