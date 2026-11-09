"""DGN-1703: the DGN-1651 retraction never armed on a live stream.

Measured 2026-09-25 (ClaudeSDKClient + a Stop hook that blocks once, on both
SDK 0.2.110/CLI 2.1.191 and SDK 0.2.159/CLI 2.1.281):

    AssistantMessage stop_reason=None  [ThinkingBlock]
    AssistantMessage stop_reason=None  [TextBlock "안녕하세요"]
    UserMessage                        [TextBlock "Stop hook feedback:\\n..."]
    AssistantMessage stop_reason=None  [ThinkingBlock]
    AssistantMessage stop_reason=None  [TextBlock "안녕하세요"]
    ResultMessage    stop_reason='end_turn'

No AssistantMessage is ever terminal on the live stream (end_turn exists only
on the ResultMessage and in the rewritten transcript). DGN-1651's retraction
keys on is_terminal + final_segments, so it never fired; the DGN-1651 tests
fed synthetic stop_reason="end_turn" messages and passed. Live (inline mode,
a domain agent 2026-09-25 09:23) the two identical answers glued into one draft, and
_reply_smart's no-op skip (final content = the last text only, no markdown)
left the glued bubble standing: the owner read the question twice.

Fix under test: the CLI's Stop re-prompt ("Stop hook feedback:" user message)
marks the model step it closes as superseded; the next main-agent message that
carries text retracts that step's span from the live surface (same span cut as
DGN-1651, now armed by the block itself instead of a terminal flag).

The fixtures replay the live trace shape (domain-agent transcript 1deb7843, turn
09:23:14): thinking -> Bash tool -> answer -> Stop block -> thinking -> Read
tool -> identical answer.
"""

import unittest

from claude_agent_sdk import (
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from bridge.sdk_bridge import _is_stop_hook_feedback
from bridge.tests.test_dgn1651_stop_block_duplicate import (
    _asst,
    _count,
    _handler,
    _result,
    run_turn,
)

QUESTION = "제가 어떻게 불러드리면 좋을까요?"
FEEDBACK = (
    "Stop hook feedback:\nOnboarding gate (Stop block, rule R7): this "
    "instance's identity file still carries the ONBOARDING_PENDING marker"
)


def _think():
    return _asst(None, [ThinkingBlock(thinking="...", signature="sig")])


def _tool_call(tid: str, name: str = "Read"):
    return _asst(None, [ToolUseBlock(id=tid, name=name, input={"file_path": "x"})])


def _tool_result(tid: str):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="ok")])


def _feedback():
    return UserMessage(content=[TextBlock(text=FEEDBACK)])


def _text(text: str):
    # The measured live shape: every streamed message has stop_reason=None.
    return _asst(None, [TextBlock(text=text)])


def live_hook_turn(first: str, regenerated, mode: str = "inline"):
    """The measured 09:23 turn, message for message. `regenerated` is the text of
    the post-block answer, or None for a regeneration that emits no text."""
    seq = [
        _think(),
        _tool_call("t1", "Bash"),
        _tool_result("t1"),
        _text(first),
        _feedback(),
        _think(),
        _tool_call("t2"),
        _tool_result("t2"),
    ]
    if regenerated is not None:
        seq.append(_text(regenerated))
    seq.append(_result(result=regenerated or first))
    return run_turn(seq, interim_mode=mode)


class TestFeedbackDetector(unittest.TestCase):
    def test_list_and_str_forms(self):
        self.assertTrue(_is_stop_hook_feedback(_feedback()))
        self.assertTrue(_is_stop_hook_feedback(UserMessage(content=FEEDBACK)))

    def test_tool_result_and_owner_text_are_not_feedback(self):
        self.assertFalse(_is_stop_hook_feedback(_tool_result("t1")))
        self.assertFalse(
            _is_stop_hook_feedback(UserMessage(content="Stop 하고 싶어요"))
        )

    def test_subagent_feedback_is_ignored(self):
        msg = UserMessage(
            content=[TextBlock(text=FEEDBACK)], parent_tool_use_id="toolu_x"
        )
        self.assertFalse(_is_stop_hook_feedback(msg))


class TestSupersedeStepUnit(unittest.TestCase):
    def test_step_span_is_armed_and_cut(self):
        import asyncio

        async def _inner():
            h, _screen = _handler()
            h.begin_message(terminal=False)
            await h.update_if_needed("도구 전 설명")
            h.begin_step()  # tool result closes the narration step
            h.begin_message(terminal=False)
            await h.update_if_needed(QUESTION)
            self.assertTrue(h.supersede_step())
            h.begin_step()
            self.assertTrue(h.begin_message(terminal=False, retract=True))
            # Only the blocked step went; the earlier step's narration stays.
            self.assertEqual(h.accumulated_text, "도구 전 설명")

        asyncio.run(_inner())

    def test_textless_step_arms_nothing(self):
        import asyncio

        async def _inner():
            h, _screen = _handler()
            await h.update_if_needed("앞 단계")
            h.begin_step()
            self.assertFalse(h.supersede_step())
            self.assertFalse(h.begin_message(terminal=False, retract=True))
            self.assertEqual(h.accumulated_text, "앞 단계")

        asyncio.run(_inner())


class TestOwnerSurfaceLiveShape(unittest.TestCase):
    def test_identical_regeneration_leaves_one_message(self):
        # THE ticket. Pre-fix, inline: one bubble reading QUESTION\nQUESTION.
        for mode in ("inline", "fold", "suppress"):
            with self.subTest(mode=mode):
                response, live, final, _screen = live_hook_turn(
                    QUESTION, QUESTION, mode
                )
                self.assertEqual(response.content, QUESTION)
                self.assertEqual(len(final), 1)
                self.assertEqual(_count(final, QUESTION), 1)
                self.assertEqual(_count(live, QUESTION), min(1, len(live)))

    def test_differing_regeneration_replaces(self):
        response, live, final, _screen = live_hook_turn(
            "이름을 알려드릴게요: 하루입니다.", QUESTION, "inline"
        )
        self.assertEqual(final, [QUESTION])
        self.assertEqual(live, [QUESTION])
        self.assertEqual(response.content, QUESTION)

    def test_textless_regeneration_keeps_the_answer(self):
        for mode in ("inline", "fold", "suppress"):
            with self.subTest(mode=mode):
                _response, _live, final, _screen = live_hook_turn(
                    QUESTION, None, mode
                )
                self.assertEqual(_count(final, QUESTION), 1)

    def test_unblocked_multi_step_turn_is_untouched(self):
        # Tool-result boundaries alone never retract: inline narration from an
        # earlier step and the answer both stay.
        _response, live, final, _screen = run_turn(
            [
                _text("파일을 확인할게요"),
                _tool_call("t1"),
                _tool_result("t1"),
                _text(QUESTION),
                _result(result=QUESTION),
            ],
            interim_mode="inline",
        )
        joined = "\n".join(final)
        self.assertIn("파일을 확인할게요", joined)
        self.assertEqual(_count(final, QUESTION), 1)
        self.assertIn("파일을 확인할게요", "\n".join(live))


if __name__ == "__main__":
    unittest.main()
