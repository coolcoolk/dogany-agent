"""DGN-1842: a Stop-blocked answer in a no-pending turn was delivered twice.

Measured 2026-10-04 07:35 (v2.6.0 rehearsal): after /claim the bridge injected
the first-contact turn (inject_background_turn -> no pending request), so every
message went through _handle_proactive_message. The opener tripped the
onboarding Stop gate; the CLI re-prompted ("Stop hook feedback:" user message)
and the model regenerated. The DGN-1703 supersede lived only on the pending
path; the proactive path ignored the re-prompt and flushed the blocked draft
and the regeneration together -- the owner got both.

Fix under test: the proactive path applies the same retract-on-replacement
semantics -- the re-prompt marks the step it closes; replacement text drops it;
a text-less regeneration leaves the earlier answer standing.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic bridge environment
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from bridge import sdk_bridge as sdk
from bridge.sdk_bridge import SdkBridge, _UserStreamState

BLOCKED = "Blocked opener draft."
REGEN = "Clean regenerated opener."
FEEDBACK = "Stop hook feedback:\nFirst-contact gate: the opener addresses the owner."

# ASCII fixtures; the register guard is not under test.
_guard_patch = patch.object(sdk, "_register_guard", lambda text: text)


def setUpModule():
    _guard_patch.start()


def tearDownModule():
    _guard_patch.stop()


def _text(text, stop=None):
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-opus-5-5",
                            stop_reason=stop, parent_tool_use_id=None)


def _tool_call(tid):
    return AssistantMessage(content=[ToolUseBlock(id=tid, name="Read", input={})],
                            model="claude-opus-5-5", stop_reason=None,
                            parent_tool_use_id=None)


def _tool_result(tid):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="ok")])


def _feedback():
    return UserMessage(content=[TextBlock(text=FEEDBACK)])


def _result():
    result = MagicMock(spec=ResultMessage)
    result.session_id = "sess-1"
    result.is_error = False
    result.result = ""
    result.num_turns = 1
    return result


def _run(seq, dispatch_turn=None, root=None):
    push = AsyncMock()
    state = _UserStreamState(client=MagicMock(), model=None)
    state.last_chat_id = 11
    state.last_session_id = "sess-1"
    state.proactive_push = push
    bridge = SdkBridge()

    async def _go():
        for msg in seq:
            await bridge._handle_proactive_message(7, state, msg)

    if dispatch_turn:
        state.dispatch_return_turn_id = dispatch_turn
        with patch.object(sdk, "PROJECT_ROOT", Path(root)):
            sdk._write_dispatch_return_context(dispatch_turn, 7, "sess-1")
            asyncio.run(_go())
    else:
        asyncio.run(_go())
    return state, [c.args[1] for c in push.await_args_list]


class TestProactiveStopBlock(unittest.TestCase):
    def test_regeneration_replaces_the_blocked_draft(self):
        with self.assertLogs(sdk.logger, "INFO") as logs:
            state, pushed = _run([_text(BLOCKED), _feedback(), _text(REGEN), _result()])
        self.assertEqual(pushed, [REGEN])
        self.assertTrue(any("retracted the superseded proactive segment" in line
                                for line in logs.output))
        self.assertIsNone(state.proactive_superseded)
        self.assertEqual(state.proactive_step_start, 0)

    def test_regeneration_after_tools_replaces_the_blocked_draft(self):
        _, pushed = _run([
            _tool_call("t1"), _tool_result("t1"), _text(BLOCKED), _feedback(),
            _tool_call("t2"), _tool_result("t2"), _text(REGEN), _result(),
        ])
        self.assertEqual(pushed, [REGEN])

    def test_textless_regeneration_keeps_the_answer(self):
        _, pushed = _run([_text(BLOCKED), _feedback(), _tool_call("t1"),
                          _tool_result("t1"), _result()])
        self.assertEqual(pushed, [BLOCKED])

    def test_earlier_step_narration_survives(self):
        _, pushed = _run([
            _text("Narration."), _tool_call("t1"), _tool_result("t1"),
            _text(BLOCKED), _feedback(), _text(REGEN), _result(),
        ])
        self.assertEqual(len(pushed), 1)
        self.assertIn("Narration.", pushed[0])
        self.assertIn(REGEN, pushed[0])
        self.assertNotIn(BLOCKED, pushed[0])

    def test_ordinary_injected_turn_unchanged(self):
        _, pushed = _run([_text("One."), _tool_call("t1"), _tool_result("t1"),
                          _text("Two."), _result()])
        self.assertEqual(len(pushed), 1)
        self.assertIn("One.", pushed[0])
        self.assertIn("Two.", pushed[0])

    def test_marks_do_not_leak_past_the_turn(self):
        state, pushed = _run([_text(BLOCKED), _feedback(), _result()])
        self.assertEqual(pushed, [BLOCKED])
        self.assertIsNone(state.proactive_superseded)
        self.assertEqual(state.proactive_step_start, 0)
        state, pushed = _run([_text(BLOCKED), _feedback(), _result(),
                              _text("Next turn."), _result()])
        self.assertEqual(pushed, [BLOCKED, "Next turn."])

    def test_dispatch_return_first_text_is_never_resent(self):
        with tempfile.TemporaryDirectory() as root:
            _, pushed = _run(
                [_text(BLOCKED), _feedback(), _text(REGEN), _result()],
                dispatch_turn="turn-1842", root=root,
            )
        # The first text went out directly (result-first, cannot be
        # retracted); the regeneration is the turn's only other push.
        self.assertEqual(pushed[0], BLOCKED)
        self.assertEqual(pushed[1:], [REGEN])

    def test_dispatch_return_path_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            _, pushed = _run(
                [_text(BLOCKED), _tool_call("t1"), _tool_result("t1"),
                 _text("Follow-up."), _result()],
                dispatch_turn="turn-1842b", root=root,
            )
        self.assertEqual(pushed, [BLOCKED, "Follow-up."])


if __name__ == "__main__":
    unittest.main()
