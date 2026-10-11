"""DGN-1941: the dispatch-return result-first latch, bridge half.

Live 2026-10-08 (dev instance, v2.6.3-dev.3): five returns ended on a gate stall.
The transcript shows the model's first follow-up tool running before any
text (a correct deny); the Stop-side fix lives in
routines/tests/test-dgn1941-resume-after-open.py. Three bridge hazards found
on the way are pinned here:

* a second dispatch-return injected while a dispatch-return turn is still
  live re-armed the shared latch: a fresh closed row re-closed a latch the
  delivered result line had already opened;
* _send_dispatch_return_first_text refused silently, so bot.log could not
  tell which guard held the latch closed;
* text held for the Stop gate window (DGN-1850) and released onto the
  owner's screen before a tool call never opened the latch.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic bridge environment
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, TextBlock, ThinkingBlock, ToolUseBlock,
)
from bridge import sdk_bridge as sdk
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState

GATE = Path(__file__).resolve().parents[2] / "routines" / "dispatch-return-result-first-gate.py"
HAS_GATE = GATE.is_file()
needs_gate = pytest.mark.skipif(not HAS_GATE, reason="no routines/ gate beside this bridge")
RESULT_LINE = "Result: v27-t1-fix3 finished; 15 commits, all tests green."
MERGE = {"tool_name": "Bash", "tool_input": {"command": "git merge --no-edit dsp/x 2>&1 | tail -1"}}


def _gate_denies(context):
    env = dict(os.environ)
    env.pop("GATE_DISPATCH_RETURN_RESULT_FIRST", None)
    env.pop("GATE_ALL", None)
    env["DISPATCH_RETURN_CONTEXT_PATH"] = str(context)
    env["DISPATCH_RETURN_GRACE_SECONDS"] = "0.1"
    env["DISPATCH_RETURN_DELIVERY_WAIT_SECONDS"] = "0.2"
    payload = dict(MERGE, cwd=str(context.parent), session_id="sess-1")
    proc = subprocess.run([sys.executable, str(GATE)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return False
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def _live_state(push=None):
    client = MagicMock()
    client.query = AsyncMock()
    state = _UserStreamState(client=client, model=None)
    state.last_chat_id = 11
    state.last_session_id = "sess-1"
    state.proactive_push = push or AsyncMock()
    return state


def _result_message():
    result = MagicMock(spec=ResultMessage)
    result.session_id = "sess-1"
    result.is_error = False
    result.result = ""
    result.num_turns = 1
    return result


# -- (b) a second return into a live turn ------------------------------------

def test_second_return_does_not_reclose_an_open_latch():
    """16:00 shape: the first return's result line was pushed (latch open);
    a second [dispatch-return] lands before the live turn's Result."""
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        state = _live_state()
        bridge = SdkBridge()
        bridge._streams[7] = state
        assert asyncio.run(bridge.inject_background_turn(7, "[dispatch-return] first done"))
        first_turn = state.dispatch_return_turn_id
        assert asyncio.run(bridge._send_dispatch_return_first_text(7, state, RESULT_LINE))
        assert json.loads(context.read_text())["visible"] is True

        assert asyncio.run(bridge.inject_background_turn(7, "[dispatch-return] second done"))

        row = json.loads(context.read_text())
        assert row["visible"] is True and row["turn_id"] == first_turn
        assert state.dispatch_return_turn_id == first_turn
        assert state.dispatch_return_result_sent is True
        # The second return still carries the result-first instruction.
        assert "[bridge:result-first]" in state.client.query.call_args[0][0]
        if HAS_GATE:
            assert not _gate_denies(context)


def test_second_return_shares_a_closed_latch_and_one_line_opens_it():
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        state = _live_state()
        bridge = SdkBridge()
        bridge._streams[7] = state
        assert asyncio.run(bridge.inject_background_turn(7, "[dispatch-return] first done"))
        first_turn = state.dispatch_return_turn_id
        assert asyncio.run(bridge.inject_background_turn(7, "[dispatch-return] second done"))
        assert state.dispatch_return_turn_id == first_turn
        assert json.loads(context.read_text())["turn_id"] == first_turn

        assert asyncio.run(bridge._send_dispatch_return_first_text(7, state, RESULT_LINE))
        assert json.loads(context.read_text())["visible"] is True
        # The live turn's Result closes the one shared latch.
        asyncio.run(bridge._handle_proactive_message(7, state, _result_message()))
        assert not context.exists()
        assert state.dispatch_return_turn_id is None


def test_second_return_query_failure_keeps_the_live_latch():
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        state = _live_state()
        bridge = SdkBridge()
        bridge._streams[7] = state
        assert asyncio.run(bridge.inject_background_turn(7, "[dispatch-return] first done"))
        first_turn = state.dispatch_return_turn_id
        state.client.query = AsyncMock(side_effect=RuntimeError("stream closed"))
        with pytest.raises(RuntimeError):
            asyncio.run(bridge.inject_background_turn(7, "[dispatch-return] second done"))
        assert state.dispatch_return_turn_id == first_turn
        assert json.loads(context.read_text())["turn_id"] == first_turn


def test_a_new_turn_after_the_result_still_arms_its_own_latch():
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        state = _live_state()
        bridge = SdkBridge()
        bridge._streams[7] = state
        assert asyncio.run(bridge.inject_background_turn(7, "[dispatch-return] first done"))
        first_turn = state.dispatch_return_turn_id
        asyncio.run(bridge._handle_proactive_message(7, state, _result_message()))
        assert asyncio.run(bridge.inject_background_turn(7, "[dispatch-return] second done"))
        row = json.loads(context.read_text())
        assert row["turn_id"] != first_turn and row["visible"] is False


# -- (a) every refusal names its guard -----------------------------------------

@pytest.mark.parametrize("field,reason,level", [
    ("last_chat_id", "no owner chat id", logging.WARNING),
    ("proactive_push", "no push callback", logging.WARNING),
])
def test_missing_delivery_route_is_logged(caplog, field, reason, level):
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        state = _live_state()
        state.dispatch_return_turn_id = "turn-1941"
        sdk._write_dispatch_return_context("turn-1941", 7, "sess-1")
        setattr(state, field, None)
        with caplog.at_level(logging.DEBUG, logger=sdk.logger.name):
            assert not asyncio.run(SdkBridge()._send_dispatch_return_first_text(
                7, state, RESULT_LINE))
        hits = [r for r in caplog.records if reason in r.getMessage()]
        assert hits and hits[0].levelno == level


@pytest.mark.parametrize("setup,reason", [
    (lambda s: setattr(s, "dispatch_return_turn_id", None), "no dispatch-return turn"),
    (lambda s: setattr(s, "dispatch_return_result_sent", True), "latch already open"),
])
def test_routine_refusals_are_logged_at_debug(caplog, setup, reason):
    state = _live_state()
    state.dispatch_return_turn_id = "turn-1941"
    setup(state)
    with caplog.at_level(logging.DEBUG, logger=sdk.logger.name):
        assert not asyncio.run(SdkBridge()._send_dispatch_return_first_text(
            7, state, RESULT_LINE))
    hits = [r for r in caplog.records if reason in r.getMessage()]
    assert hits and hits[0].levelno == logging.DEBUG


def test_successful_push_is_logged(caplog):
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        state = _live_state()
        state.dispatch_return_turn_id = "turn-1941"
        with caplog.at_level(logging.INFO, logger=sdk.logger.name):
            assert asyncio.run(SdkBridge()._send_dispatch_return_first_text(
                7, state, RESULT_LINE))
        assert any("pushed as first text" in r.getMessage() for r in caplog.records)


# -- a held result line released onto the screen opens the latch -----------

class _HeldThenToolClient:
    """Result line in a text-only message (held inside the DGN-1850 gated
    window), then a tool-call message: the CLI runs PreToolUse right then."""

    def __init__(self, context):
        self.context = context
        self.denials = []

    async def receive_messages(self):
        yield AssistantMessage(content=[TextBlock(text=RESULT_LINE)],
                               model="claude-opus-5-5", stop_reason="end_turn",
                               parent_tool_use_id=None)
        yield AssistantMessage(
            content=[ToolUseBlock(id="tu-1", name="Bash", input=MERGE["tool_input"])],
            model="claude-opus-5-5", stop_reason="tool_use", parent_tool_use_id=None)
        if HAS_GATE:
            self.denials.append(_gate_denies(self.context))
        else:
            self.denials.append(json.loads(self.context.read_text())["visible"] is not True)
        yield _result_message()


def _held_request(handler):
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    loop.close()
    req = _PendingRequest(
        user_id=7, chat_id=11, model=None, requested_session_id=None,
        permission_callback=None, typing_callback=None, future=future,
        user_message="ok",
    )
    req.sent = True
    req.hold_terminal = True
    req.streaming_handler = handler
    return req


def _run_held_turn(on_screen):
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        client = _HeldThenToolClient(context)
        # The first-text push cannot land (Telegram down), so the line is held.
        state = _live_state(AsyncMock(side_effect=RuntimeError("Bad Gateway")))
        state.client = client
        state.dispatch_return_turn_id = "turn-1941"
        sdk._write_dispatch_return_context("turn-1941", 7, "sess-1")
        handler = MagicMock()
        handler.update_if_needed = AsyncMock(return_value=on_screen)
        handler.drafts = [MagicMock()] if on_screen else []
        handler.accumulated_text = RESULT_LINE
        handler.seal_segment = AsyncMock()
        handler.finalize_all = AsyncMock()
        state.pending.append(_held_request(handler))
        bridge = SdkBridge()
        with patch.object(bridge, "_finalize_result", AsyncMock(return_value=False)), \
                patch.object(sdk, "_effective_interim_mode", return_value="inline"), \
                patch.object(sdk, "_interim_off_locale", return_value=False):
            asyncio.run(bridge._reader_loop(7, state))
        return client.denials, handler


def test_held_result_line_released_on_screen_opens_latch():
    denials, handler = _run_held_turn(on_screen=True)
    handler.update_if_needed.assert_awaited()
    assert denials == [False]


def test_held_result_line_not_on_screen_keeps_latch_closed():
    denials, _ = _run_held_turn(on_screen=False)
    assert denials == [True]


# -- round 2: the result line arrives as a narration-tagged thinking block ----
#
# Live 16:34:42 (live transcript a3b8e4b7, msg_011CfpPDwAHjMhnsS2BhH3su): the
# owner turn's final was folded at 16:34:36, the return was injected 6 s later,
# and the model's first message after reading out.log was [thinking (empty,
# signature tag "thinking"), thinking (the result line, signature tag
# "narration"), tool_use Bash]. The SDK hands the narration over as a
# ThinkingBlock; the TextBlock-only bridge never pushed it, the latch stayed
# visible=false and both Bash calls were denied.

def _signature(tag):
    import base64
    head = b"\x08\x04\x12\xf8\x07\n\x11\x08\x12\x18\x028\x01B" + bytes([len(tag)]) + tag
    return base64.b64encode(head + b"\x12\x0c" + os.urandom(48)).decode()


THINKING_SIG = _signature(b"thinking")
NARRATION_SIG = _signature(b"narration")


def _am(*blocks):
    return AssistantMessage(content=list(blocks), model="claude-opus-5-5",
                            stop_reason=None, parent_tool_use_id=None)


class _FoldThenInjectedNarrationClient:
    def __init__(self, bridge, state, context):
        self.bridge, self.state, self.context = bridge, state, context
        self.query = AsyncMock()
        self.denials = []

    async def receive_messages(self):
        # Owner request turn: answer + Result (its final folds and the request pops).
        yield _am(TextBlock(text="Owner answer, folded in place."))
        yield _result_message()
        # The dispatch return lands seconds later into the idle stream.
        assert await self.bridge.inject_background_turn(7, "[dispatch-return] dgn1941-latch done")
        yield _am(ToolUseBlock(id="tu-0", name="Read", input={"file_path": "out.log"}))
        yield _am(ThinkingBlock(thinking="", signature=THINKING_SIG))
        yield _am(ThinkingBlock(thinking=RESULT_LINE + "\n\n", signature=NARRATION_SIG))
        yield _am(ToolUseBlock(id="tu-1", name="Bash", input=MERGE["tool_input"]))
        if HAS_GATE:
            self.denials.append(_gate_denies(self.context))
        else:
            self.denials.append(json.loads(self.context.read_text())["visible"] is not True)
        yield _result_message()


def _owner_request():
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    loop.close()
    req = _PendingRequest(
        user_id=7, chat_id=11, model=None, requested_session_id=None,
        permission_callback=None, typing_callback=None, future=future,
        user_message="owner question",
    )
    req.sent = True
    return req


def test_narration_result_line_after_folded_owner_turn_opens_latch():
    with tempfile.TemporaryDirectory() as root, patch.object(sdk, "PROJECT_ROOT", Path(root)):
        context = sdk.dispatch_return_context_path()
        push = AsyncMock()
        state = _live_state(push)
        bridge = SdkBridge()
        bridge._streams[7] = state
        client = _FoldThenInjectedNarrationClient(bridge, state, context)
        state.client = client
        state.pending.append(_owner_request())
        with patch.object(bridge, "_finalize_result", AsyncMock(return_value=False)), \
                patch.object(sdk, "_interim_off_locale", return_value=False):
            asyncio.run(bridge._reader_loop(7, state))
        assert client.denials == [False]
        pushed = [c.args for c in push.await_args_list]
        assert any(RESULT_LINE in " ".join(map(str, a)) for a in pushed), pushed


def test_promote_narration_rewrites_only_narration_blocks():
    real_thinking = ThinkingBlock(thinking="private reasoning", signature=THINKING_SIG)
    empty = ThinkingBlock(thinking="", signature=NARRATION_SIG)
    msg = _am(real_thinking, empty,
              ThinkingBlock(thinking=RESULT_LINE + "\n\n", signature=NARRATION_SIG))
    sdk._promote_narration_blocks(msg)
    assert msg.content[0] is real_thinking and msg.content[1] is empty
    assert isinstance(msg.content[2], TextBlock) and msg.content[2].text == RESULT_LINE
    # Subagent inner output stays untouched (it never reaches the owner).
    inner = AssistantMessage(
        content=[ThinkingBlock(thinking=RESULT_LINE, signature=NARRATION_SIG)],
        model="claude-opus-5-5", parent_tool_use_id="tu-9")
    sdk._promote_narration_blocks(inner)
    assert isinstance(inner.content[0], ThinkingBlock)
    # A garbage signature is never narration.
    assert not sdk._is_narration_block(ThinkingBlock(thinking="x", signature="%%%"))
