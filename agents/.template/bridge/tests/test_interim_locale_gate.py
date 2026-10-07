"""Interim locale gate: English step notes never reach a ko owner live.

Live rehearsal 2026-10-04 (ko instance, claude-sonnet-5-5, default interim
mode): the model's internal step notes, each streamed as a text-only event
before a tool call, reached the owner as their own bubbles:
- 21:22 mint turn: "Run open without --fit first to get packs." (before Bash)
- 21:36 onboarding turn: "Record the name, then send q2." (between Read and
  Edit)
Both are below the DGN-686 80-char drop floor. The gate in
SdkBridge._route_live holds an INTERIM block with no Hangul and Latin prose
off every live surface (stream, fold capture, fold bubble) on a ko instance.
Terminal text, Hangul / mixed blocks and en instances are unchanged.
"""

import asyncio
import json
import unittest
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from bridge import sdk_bridge
from bridge.sdk_bridge import (
    SdkBridge,
    _PendingRequest,
    _UserStreamState,
    _interim_off_locale,
)

MODES = ("suppress", "inline", "fold")
MINT_NOTE = "Run open without --fit first to get packs."
ONBOARD_NOTE = "Record the name, then send q2."
VERB_CMD = "scripts/pack/mint_flow.sh --instance-root /x open --input 'make a health agent'"
SCREEN_JSON = json.dumps({
    "agent_utterance": "none", "note_for_agent": "SCREEN_SENT_END_TURN",
    "screen_delivered": True, "state": "WAIT_CHOICE", "waiting_for": "choice",
}, sort_keys=True)
FIT_REQUIRED_JSON = json.dumps({
    "agent_utterance": "none", "note_for_agent": "FIT_REQUIRED",
    "screen_delivered": False, "state": "IDLE", "waiting_for": "none",
}, sort_keys=True)
KO_FINAL = "\uc774\ub984\uc744 \uae30\ub85d\ud588\uc5b4\uc694. \ub2e4\uc74c \uc9c8\ubb38\uc73c\ub85c \ub118\uc5b4\uac08\uac8c\uc694."


def _text(t: str) -> TextBlock:
    return TextBlock(text=t)


def _tool(tid: str, name: str, tin: dict) -> ToolUseBlock:
    return ToolUseBlock(id=tid, name=name, input=tin)


def _split(stop_reason: Optional[str], blocks: List[Any], mid: str) -> AssistantMessage:
    """One content block of API message `mid`, as the CLI streams it."""
    return AssistantMessage(
        content=blocks, model="claude-sonnet-5-5", stop_reason=stop_reason,
        parent_tool_use_id=None, message_id=mid,
    )


def _tool_result(tid: str, content: Any) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tid, content=content, is_error=False)],
        parent_tool_use_id=None,
    )


def _result(result: str = ""):
    rm = MagicMock(spec=ResultMessage)
    rm.session_id = "sess-interim-locale"
    rm.result = result
    rm.is_error = False
    rm.num_turns = 3
    return rm


def run_turn(messages_seq, interim_mode: str, locale: str = "ko",
             hold_terminal: bool = False):
    async def _inner():
        handler = MagicMock()
        handler.drafts = []
        handler.accumulated_text = ""
        handler.begin_message = MagicMock(return_value=False)
        handler.update_if_needed = AsyncMock(return_value=True)
        handler.finalize_all = AsyncMock(return_value=True)
        handler.seal_segment = AsyncMock(return_value=None)

        bridge_obj = SdkBridge()
        bridge_obj._fold_dispatch = AsyncMock()
        future = asyncio.get_event_loop().create_future()
        req = _PendingRequest(
            user_id=1, chat_id=1, model=None, requested_session_id=None,
            permission_callback=None, typing_callback=None, future=future,
            streaming_handler=handler, hold_terminal=hold_terminal,
        )
        state = _UserStreamState(client=MagicMock(), model=None)
        state.pending.append(req)
        req.sent = True

        async def fake_receive():
            for m in messages_seq:
                yield m

        state.client.receive_messages = fake_receive
        bridge_obj._streams[1] = state
        with patch("bridge.sdk_bridge.STREAM_INTERIM", interim_mode == "inline"), \
                patch("bridge.sdk_bridge.INTERIM_MODE", interim_mode), \
                patch.object(sdk_bridge.config, "locale", locale):
            await bridge_obj._reader_loop(1, state)
        response = await asyncio.wait_for(req.future, timeout=1.0)
        return response, handler, req, bridge_obj

    return asyncio.run(_inner())


def _owner_live(handler, req, bridge_obj) -> str:
    """Every char that reached a live owner surface: the draft stream, the
    fold capture and the fold bubble dispatch."""
    streamed = "".join(c.args[0] for c in handler.update_if_needed.call_args_list)
    folded = "".join(req.interim_texts)
    dispatched = "".join(c.args[1] for c in bridge_obj._fold_dispatch.call_args_list)
    return streamed + folded + dispatched


def mint_turn():
    # The 21:22 shape: skill load, text-only event, verb call of the same API
    # message, FIT_REQUIRED, second verb, screen sent.
    return [
        _split("tool_use", [_tool("s1", "Skill", {"skill": "any-skill"})], "a"),
        _tool_result("s1", "Launching skill: any-skill"),
        _split(None, [_text(MINT_NOTE)], "b"),
        _split("tool_use", [_tool("t1", "Bash", {"command": VERB_CMD})], "b"),
        _tool_result("t1", FIT_REQUIRED_JSON + "\n"),
        _split("tool_use", [_tool(
            "t2", "Bash", {"command": VERB_CMD + " --fit pack_health --lead 'x'"})], "c"),
        _tool_result("t2", SCREEN_JSON + "\n"),
        _split("end_turn", [], "d"),
        _result(result=""),
    ]


def onboarding_turn(note: str = ONBOARD_NOTE, final: str = KO_FINAL):
    # The 21:36 shape: Read, text-only event, Edit, Korean answer.
    return [
        _split("tool_use", [_tool("r1", "Read", {"file_path": "USER.md"})], "a"),
        _tool_result("r1", "name: \n"),
        _split(None, [_text(note)], "b"),
        _split("tool_use", [_tool(
            "e1", "Edit", {"file_path": "USER.md", "old_string": "a", "new_string": "b"})], "b"),
        _tool_result("e1", "ok"),
        _split("end_turn", [_text(final)], "c"),
        _result(result=final),
    ]


class TestObservedLeaks(unittest.TestCase):

    def test_mint_note_zero_owner_chars(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                resp, handler, req, b = run_turn(mint_turn(), mode)
                self.assertNotIn(MINT_NOTE, _owner_live(handler, req, b))
                self.assertEqual(resp.content, "")

    def test_mint_note_no_verdict_release_is_gated(self):
        # Mint armed but no verdict: _settle_mint_held releases the parked
        # narration through _route_live -- still gated.
        seq = [
            _split("tool_use", [_tool("s1", "Skill", {"skill": "any-skill"})], "a"),
            _tool_result("s1", "ok"),
            _split(None, [_text(MINT_NOTE)], "b"),
            _split("tool_use", [_tool("t1", "Bash", {"command": "ls"})], "b"),
            _tool_result("t1", "x\n"),
            _split("end_turn", [_text(KO_FINAL)], "c"),
            _result(result=KO_FINAL),
        ]
        for mode in MODES:
            with self.subTest(mode=mode):
                resp, handler, req, b = run_turn(seq, mode)
                self.assertEqual(_owner_live(handler, req, b).count(MINT_NOTE), 0)
                self.assertEqual(resp.content, KO_FINAL)

    def test_onboarding_note_zero_owner_chars(self):
        for hold in (False, True):
            for mode in MODES:
                with self.subTest(mode=mode, hold_terminal=hold):
                    resp, handler, req, b = run_turn(
                        onboarding_turn(), mode, hold_terminal=hold)
                    self.assertNotIn(ONBOARD_NOTE, _owner_live(handler, req, b))
                    self.assertEqual(resp.content, KO_FINAL)

    def test_drop_logged_with_char_count(self):
        with self.assertLogs("bridge.sdk_bridge", level="INFO") as cm:
            run_turn(onboarding_turn(), "inline")
        self.assertTrue(any(
            "Interim locale gate dropped a %d-char" % len(ONBOARD_NOTE) in line
            for line in cm.output
        ))


class TestUnchanged(unittest.TestCase):

    def test_ko_hangul_interim_unchanged(self):
        note = "\uc774\ub984\uc744 \uae30\ub85d\ud560\uac8c\uc694."
        mixed = "USER.md \uc5d0 \uc774\ub984 \uae30\ub85d"
        for text in (note, mixed):
            for mode in MODES:
                with self.subTest(text=text, mode=mode):
                    _, handler, req, b = run_turn(onboarding_turn(note=text), mode)
                    live = _owner_live(handler, req, b)
                    if mode == "inline":
                        self.assertIn(text, live)
                    if mode == "fold":
                        self.assertIn(text, req.interim_texts)

    def test_en_instance_unchanged(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                _, handler, req, b = run_turn(
                    onboarding_turn(final="Done."), mode, locale="en")
                if mode == "inline":
                    self.assertIn(ONBOARD_NOTE, _owner_live(handler, req, b))
                if mode == "fold":
                    self.assertIn(ONBOARD_NOTE, req.interim_texts)

    def test_terminal_english_on_ko_unchanged(self):
        final = "All set."
        for mode in MODES:
            with self.subTest(mode=mode):
                resp, handler, _, _ = run_turn(onboarding_turn(final=final), mode)
                self.assertEqual(resp.content, final)
                streamed = "".join(
                    c.args[0] for c in handler.update_if_needed.call_args_list)
                self.assertIn(final, streamed)

    def test_text_only_english_answer_still_delivered(self):
        # A text-only event (stop_reason None) that turns out to be the
        # answer is kept off the live stream but delivered by finalize.
        seq = [
            _split("tool_use", [_tool("r1", "Read", {"file_path": "x"})], "a"),
            _tool_result("r1", "x"),
            _split(None, [_text("All set.")], "b"),
            _result(result="All set."),
        ]
        for mode in MODES:
            with self.subTest(mode=mode):
                resp, _, _, _ = run_turn(seq, mode)
                self.assertEqual(resp.content, "All set.")


class TestPredicate(unittest.TestCase):

    def _check(self, locale: str, text: str) -> bool:
        with patch.object(sdk_bridge.config, "locale", locale):
            return _interim_off_locale(text)

    def test_ko(self):
        self.assertTrue(self._check("ko", MINT_NOTE))
        self.assertTrue(self._check("ko", ONBOARD_NOTE))
        self.assertTrue(self._check("ko", "scripts/pack/mint_flow.sh open"))
        self.assertFalse(self._check("ko", "\uc774\ub984 \uae30\ub85d \ud6c4 q2 \uc804\uc1a1"))
        self.assertFalse(self._check("ko", "```\nls -la\n```"))
        self.assertFalse(self._check("ko", "`mint_flow.sh open`"))
        self.assertFalse(self._check("ko", "https://example.com/x"))
        self.assertFalse(self._check("ko", "\U0001F44D"))
        self.assertFalse(self._check("ko", "1/3 ..."))
        self.assertFalse(self._check("ko", ""))

    def test_en_noop(self):
        self.assertFalse(self._check("en", MINT_NOTE))


if __name__ == "__main__":
    unittest.main()
