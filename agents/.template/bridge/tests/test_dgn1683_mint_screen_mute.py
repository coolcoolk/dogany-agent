"""DGN-1683: mint turn mute -- no agent text after the verb sent the screen.

Live defect 2026-09-24 16:23 (dev.44): the mint verb (`open`) pushed the
catalog screen itself and returned screen_delivered=true /
SCREEN_SENT_END_TURN, and the agent still ended the turn with prose
("No active flow, so open a new one.\\n(...)"). The SKILL rule "add nothing"
was model-executed; bridge/mint_gate.py is the machine forcing point.

Under test (all driven through the real _reader_loop + _finalize_result):
- screen-sent verdict -> owner-bound text suppressed in every interim mode,
  nothing streams live, no fold capture, future still resolves;
- N13 -> exactly the approved sentence; N13 + extra -> cut to the sentence;
  N13 paraphrase -> replaced by the sentence; N13 with no text -> nothing;
- turns without a verb call, verb calls with a non-screen result, verb-shaped
  JSON from a NON-verb command, and subagent tool results are untouched;
- the verdict latches (screen outranks N13);
- contract lockstep: every mint_flow_table row, emitted through the real
  mint_flow.emit, maps to the right verdict; N13_SENTENCE equals the SKILL
  OWNER-SAY block.
"""

import pytest

from bridge import sdk_bridge as _locale_bridge
import asyncio
import importlib.util
import json
import re
import unittest
from pathlib import Path
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

from bridge import mint_gate
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState

REPO_ROOT = Path(__file__).resolve().parents[4]
VERB_CMD = "scripts/pack/mint_flow.sh --instance-root /x open --input 'make a health agent'"

SCREEN_JSON = json.dumps({
    "agent_utterance": "none", "note_for_agent": "SCREEN_SENT_END_TURN",
    "screen_delivered": True, "state": "WAIT_CHOICE", "waiting_for": "choice",
}, sort_keys=True)
N13_JSON = json.dumps({
    "agent_utterance": "N13", "note_for_agent": "DELIVERY_FAILED",
    "screen_delivered": False, "state": "DELIVERY_PENDING", "waiting_for": "none",
}, sort_keys=True)
NO_FLOW_JSON = json.dumps({
    "agent_utterance": "none", "note_for_agent": "NO_FLOW",
    "screen_delivered": False, "state": "IDLE", "waiting_for": "none",
}, sort_keys=True)

LEAK = "No active flow, so open a new one."
LEAK_TAIL = "(screen delivered, nothing to add)"


@pytest.fixture(autouse=True)
def _en_instance(monkeypatch):
    # These fixtures narrate in English: pin an en instance so the interim
    # locale gate (sdk_bridge._interim_off_locale) stays out of the interim
    # mechanics under test, whatever LOCALE the shell exports. The ko side
    # is covered by test_interim_locale_gate.py.
    monkeypatch.setattr(_locale_bridge.config, "locale", "en")



def _text(t: str) -> TextBlock:
    return TextBlock(text=t)


def _call(tid: str, command: str = VERB_CMD) -> ToolUseBlock:
    return ToolUseBlock(id=tid, name="Bash", input={"command": command})


def _asst(stop_reason: Optional[str], blocks: List[Any]) -> AssistantMessage:
    return AssistantMessage(
        content=blocks, model="claude-sonnet-4-5",
        stop_reason=stop_reason, parent_tool_use_id=None,
    )


def _tool_result(tid: str, content: Any, parent: Optional[str] = None) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tid, content=content, is_error=False)],
        parent_tool_use_id=parent,
    )


def _result(result: str = "", is_error: bool = False):
    rm = MagicMock(spec=ResultMessage)
    rm.session_id = "sess-1683"
    rm.result = result
    rm.is_error = is_error
    rm.num_turns = 3
    return rm


def run_turn(messages_seq, interim_mode: str = "suppress"):
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
            streaming_handler=handler,
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
                patch("bridge.sdk_bridge.INTERIM_MODE", interim_mode):
            await bridge_obj._reader_loop(1, state)
        response = await asyncio.wait_for(req.future, timeout=1.0)
        return response, handler, req, bridge_obj

    return asyncio.run(_inner())


def _streamed(handler) -> str:
    return "".join(c.args[0] for c in handler.update_if_needed.call_args_list)


def verb_turn(result_json: str, final_text: str, pre_text: str = LEAK,
              interim_mode: str = "suppress"):
    """The live incident shape: narration + verb call, result, final prose."""
    pre = [_text(pre_text)] if pre_text else []
    return run_turn([
        _asst("tool_use", pre + [_call("t1")]),
        _tool_result("t1", result_json + "\n"),
        _asst("end_turn", [_text(final_text)] if final_text else []),
        _result(result=final_text),
    ], interim_mode=interim_mode)


class TestScreenSentSuppressed(unittest.TestCase):

    def test_live_incident_suppressed_every_interim_mode(self):
        for mode in ("suppress", "inline", "fold"):
            with self.subTest(mode=mode):
                with self.assertLogs("bridge.sdk_bridge", level="INFO") as cm:
                    resp, handler, req, bridge_obj = verb_turn(
                        SCREEN_JSON, LEAK + "\n" + LEAK_TAIL, interim_mode=mode)
                self.assertEqual(resp.content, "")
                self.assertTrue(resp.success)
                self.assertEqual(req.mint_mute, mint_gate.MUTE_SCREEN)
                self.assertNotIn(LEAK, _streamed(handler))
                self.assertNotIn(LEAK_TAIL, _streamed(handler))
                bridge_obj._fold_dispatch.assert_not_called()
                self.assertTrue(any("mint turn mute for user" in l
                                    for l in cm.output))

    def test_msg_result_fallback_cannot_bypass(self):
        # Final message carries no text block, but the CLI result string does.
        resp, _, _, _ = run_turn([
            _asst("tool_use", [_call("t1")]),
            _tool_result("t1", SCREEN_JSON),
            _asst("end_turn", []),
            _result(result=LEAK_TAIL),
        ])
        self.assertEqual(resp.content, "")

    def test_list_shaped_tool_result_with_stderr_noise(self):
        content = [{"type": "text", "text": "debug: lock ok\n" + SCREEN_JSON}]
        resp, _, req, _ = run_turn([
            _asst("tool_use", [_call("t1")]),
            _tool_result("t1", content),
            _asst("end_turn", [_text(LEAK_TAIL)]),
            _result(result=LEAK_TAIL),
        ])
        self.assertEqual(req.mint_mute, mint_gate.MUTE_SCREEN)
        self.assertEqual(resp.content, "")

    def test_screen_after_no_flow_in_same_turn(self):
        # status -> NO_FLOW, then open -> screen sent: the whole tail is muted.
        resp, _, _, _ = run_turn([
            _asst("tool_use", [_call("t1", "scripts/pack/mint_flow.sh status")]),
            _tool_result("t1", NO_FLOW_JSON),
            _asst("tool_use", [_text(LEAK), _call("t2")]),
            _tool_result("t2", SCREEN_JSON),
            _asst("end_turn", [_text(LEAK_TAIL)]),
            _result(result=LEAK_TAIL),
        ])
        self.assertEqual(resp.content, "")


class TestN13(unittest.TestCase):

    def test_exact_sentence_passes(self):
        resp, handler, _, _ = verb_turn(N13_JSON, mint_gate.N13_SENTENCE, pre_text="")
        self.assertEqual(resp.content, mint_gate.N13_SENTENCE)
        self.assertEqual(_streamed(handler), "")

    def test_sentence_plus_extra_is_cut_to_the_sentence(self):
        resp, _, _, _ = verb_turn(
            N13_JSON, LEAK + "\n" + mint_gate.N13_SENTENCE + "\n" + LEAK_TAIL,
            interim_mode="inline")
        self.assertEqual(resp.content, mint_gate.N13_SENTENCE)

    def test_paraphrase_is_replaced_by_the_sentence(self):
        resp, _, _, _ = verb_turn(N13_JSON, "Delivery failed, please retry later.")
        self.assertEqual(resp.content, mint_gate.N13_SENTENCE)

    def test_no_agent_text_sends_nothing(self):
        resp, _, _, _ = run_turn([
            _asst("tool_use", [_call("t1")]),
            _tool_result("t1", N13_JSON),
            _asst("end_turn", []),
            _result(result=""),
        ])
        self.assertEqual(resp.content, "")

    def test_screen_outranks_n13(self):
        for first, second in ((N13_JSON, SCREEN_JSON), (SCREEN_JSON, N13_JSON)):
            with self.subTest(first=first[:40]):
                resp, _, req, _ = run_turn([
                    _asst("tool_use", [_call("t1")]),
                    _tool_result("t1", first),
                    _asst("tool_use", [_call("t2")]),
                    _tool_result("t2", second),
                    _asst("end_turn", [_text(mint_gate.N13_SENTENCE)]),
                    _result(result=mint_gate.N13_SENTENCE),
                ])
                self.assertEqual(req.mint_mute, mint_gate.MUTE_SCREEN)
                self.assertEqual(resp.content, "")


class TestUntouched(unittest.TestCase):

    def test_turn_without_verb_call(self):
        resp, handler, req, _ = run_turn([
            _asst("tool_use", [_text("checking"), _call("t1", "ls -la")]),
            _tool_result("t1", "total 0\n"),
            _asst("end_turn", [_text("final answer")]),
            _result(result="final answer"),
        ], interim_mode="inline")
        self.assertEqual(resp.content, "final answer")
        self.assertIsNone(req.mint_mute)
        self.assertIn("checking", _streamed(handler))

    def test_verb_call_without_screen(self):
        resp, _, req, _ = verb_turn(NO_FLOW_JSON, "final answer")
        self.assertIsNone(req.mint_mute)
        self.assertEqual(resp.content, "final answer")

    def test_verb_shaped_json_from_non_verb_command(self):
        resp, _, req, _ = run_turn([
            _asst("tool_use", [_call("t1", "cat /tmp/last-result.json")]),
            _tool_result("t1", SCREEN_JSON),
            _asst("end_turn", [_text("final answer")]),
            _result(result="final answer"),
        ])
        self.assertIsNone(req.mint_mute)
        self.assertEqual(resp.content, "final answer")

    def test_subagent_tool_result_ignored(self):
        resp, _, req, _ = run_turn([
            _asst("tool_use", [_call("t1")]),
            _tool_result("t1", SCREEN_JSON, parent="task_9"),
            _asst("end_turn", [_text("final answer")]),
            _result(result="final answer"),
        ])
        self.assertIsNone(req.mint_mute)
        self.assertEqual(resp.content, "final answer")

    def test_error_result_keeps_error_path(self):
        resp, _, _, _ = run_turn([
            _asst("tool_use", [_call("t1")]),
            _tool_result("t1", SCREEN_JSON),
            _result(result="API Error: overloaded_error", is_error=True),
        ])
        self.assertFalse(resp.success)


class TestGateUnits(unittest.TestCase):

    def test_is_verb_call(self):
        yes = (VERB_CMD, "bash scripts/pack/mint_flow.sh status",
               "cd /r && ./mint_flow.sh next --input x",
               "python3 scripts/pack/lib/mint_flow.py status")
        no = ("ls", "cat mint_flow.sh.bak", "grep -n x scripts/pack/mint_flow_table.json")
        for c in yes:
            self.assertTrue(mint_gate.is_verb_call("Bash", {"command": c}), c)
        for c in no:
            self.assertFalse(mint_gate.is_verb_call("Bash", {"command": c}), c)
        self.assertFalse(mint_gate.is_verb_call("Read", {"command": VERB_CMD}))

    def test_prose_never_arms(self):
        self.assertIsNone(mint_gate.verdict_from_result(
            "screen_delivered true, SCREEN_SENT_END_TURN, N13"))
        self.assertIsNone(mint_gate.verdict_from_result('{"screen_delivered": true}'))


# ESTATE:BEGIN the mint verb library is canonical scripts/pack machinery the public bridge does not ship
def _load_mint_flow():
    path = REPO_ROOT / "scripts" / "pack" / "lib" / "mint_flow.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_dgn1683_mint_flow", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
# ESTATE:END


class TestContractLockstep(unittest.TestCase):

    # ESTATE:BEGIN lockstep with the canonical mint verb table and the mint SKILL, neither of which ships with the public bridge
    def test_every_table_row_maps_to_its_verdict(self):
        mf = _load_mint_flow()
        if mf is None:
            self.skipTest("canonical mint verb not present in this tree")
        for (st, wf, note), row in mf.TABLE["_index"].items():
            em = mf.emit(st, wf, note)
            got = mint_gate.verdict_from_result(json.dumps(em, sort_keys=True) + "\n")
            if row["screen_delivered"]:
                want = mint_gate.MUTE_SCREEN
            elif row["agent_utterance"] == "N13":
                want = mint_gate.MUTE_N13
            else:
                want = None
            self.assertEqual(got, want, (st, wf, note))

    def test_n13_sentence_matches_skill_owner_say(self):
        skills = sorted((REPO_ROOT / "skills").glob("*mint-agent/SKILL.md"))
        if not skills:
            self.skipTest("mint-agent SKILL.md not present in this tree")
        text = skills[0].read_text(encoding="utf-8")
        m = re.search(r"<!-- OWNER-SAY -->\s*>\s*(.+?)\s*<!-- /OWNER-SAY -->", text, re.S)
        self.assertIsNotNone(m, "OWNER-SAY block missing from the SKILL")
        self.assertEqual(m.group(1).strip(), mint_gate.N13_SENTENCE)

    def test_skill_commands_are_verb_calls(self):
        # DGN-1674: the documented command resolves .dogany-consume/ first; the
        # gate must still recognise every form the SKILL tells the agent to run.
        skills = sorted((REPO_ROOT / "skills").glob("*mint-agent/SKILL.md"))
        if not skills:
            self.skipTest("mint-agent SKILL.md not present in this tree")
        cmds = [ln.strip() for ln in skills[0].read_text(encoding="utf-8").splitlines()
                if "mint_flow.sh" in ln and ln.lstrip().startswith("bash ")]
        self.assertEqual(len(cmds), 4)
        for cmd in cmds:
            self.assertTrue(mint_gate.is_verb_call("Bash", {"command": cmd}), cmd)
    # ESTATE:END

    def test_lead_required_does_not_mute(self):
        # The refusal carries extra keys; nothing was sent, so the turn stays open
        # for the agent's rerun.
        em = {"agent_utterance": "none", "note_for_agent": "LEAD_REQUIRED", "screen_delivered": False,
              "state": "BLOCKED", "waiting_for": "none", "lead_problem": "missing",
              "rerun_with": "--lead \"<one line>\""}
        self.assertIsNone(mint_gate.verdict_from_result(json.dumps(em, sort_keys=True) + "\n"))


PREVERB = "Run open without --fit first to get packs."
FIT_REQUIRED_JSON = json.dumps({
    "agent_utterance": "none", "note_for_agent": "FIT_REQUIRED",
    "screen_delivered": False, "state": "IDLE", "waiting_for": "none",
}, sort_keys=True)


def _skill(tid: str, skill: str = mint_gate.MINT_SKILL) -> ToolUseBlock:
    return ToolUseBlock(id=tid, name="Skill", input={"skill": skill})


def _split(stop_reason: Optional[str], blocks: List[Any], mid: str) -> AssistantMessage:
    """One content block of API message `mid`, as the CLI streams it."""
    return AssistantMessage(
        content=blocks, model="claude-sonnet-5-5", stop_reason=stop_reason,
        parent_tool_use_id=None, message_id=mid,
    )


# The public build carries no mint skill name (mint_gate ESTATE region), so
# the skill-arming half of the hold has no subject there.
@unittest.skipUnless(mint_gate.MINT_SKILL, "no mint skill in this build")
class TestPreVerbSplitMessage(unittest.TestCase):
    """Live 2026-10-04 21:22: the CLI streamed the narration and the verb
    call of ONE API message as two events, so the same-message hold never
    saw the call and the narration reached the owner as its own bubble."""

    def _live_incident(self, mode: str):
        return run_turn([
            _split("tool_use", [_skill("s1")], "msg_a"),
            _tool_result("s1", "Launching skill: " + mint_gate.MINT_SKILL),
            _split(None, [_text(PREVERB)], "msg_b"),
            _split("tool_use", [_call("t1")], "msg_b"),
            _tool_result("t1", FIT_REQUIRED_JSON + "\n"),
            _split("tool_use", [_call(
                "t2", VERB_CMD + " --fit pack_health --lead 'x'")], "msg_c"),
            _tool_result("t2", SCREEN_JSON + "\n"),
            _split("end_turn", [], "msg_d"),
            _result(result=""),
        ], interim_mode=mode)

    def test_split_narration_never_reaches_owner(self):
        for mode in ("suppress", "inline", "fold"):
            with self.subTest(mode=mode):
                resp, handler, req, bridge_obj = self._live_incident(mode)
                self.assertEqual(req.mint_mute, mint_gate.MUTE_SCREEN)
                self.assertEqual(resp.content, "")
                self.assertNotIn(PREVERB, _streamed(handler))
                bridge_obj._fold_dispatch.assert_not_called()
                self.assertEqual(req.mint_held, [])

    def test_namespaced_skill_arms(self):
        resp, handler, _, bridge_obj = run_turn([
            _split("tool_use", [_skill("s1", "ns:" + mint_gate.MINT_SKILL)], "a"),
            _tool_result("s1", "ok"),
            _split(None, [_text(PREVERB)], "b"),
            _split("tool_use", [_call("t1")], "b"),
            _tool_result("t1", SCREEN_JSON),
            _split("end_turn", [], "c"),
            _result(result=""),
        ], interim_mode="inline")
        self.assertEqual(resp.content, "")
        self.assertNotIn(PREVERB, _streamed(handler))

    def test_skill_loaded_no_verb_delivers_normally(self):
        # No verdict: the owner gets exactly what the same turn without the
        # skill load gets -- the held narration is released, nothing lost.
        tail = [
            _split(None, [_text("let me check")], "b"),
            _split("tool_use", [_call("t1", "ls -la")], "b"),
            _tool_result("t1", "total 0\n"),
            _split("end_turn", [_text("final answer")], "c"),
            _result(result="final answer"),
        ]
        head = [
            _split("tool_use", [_skill("s1")], "a"),
            _tool_result("s1", "Launching skill: " + mint_gate.MINT_SKILL),
        ]
        for mode in ("suppress", "inline", "fold"):
            with self.subTest(mode=mode):
                resp, handler, req, _ = run_turn(head + tail, interim_mode=mode)
                base, base_handler, base_req, _ = run_turn(tail, interim_mode=mode)
                self.assertTrue(req.mint_armed)
                self.assertIsNone(req.mint_mute)
                self.assertIn("final answer", resp.content)
                self.assertEqual(resp.content, base.content)
                self.assertEqual(req.interim_texts, base_req.interim_texts)
                if mode == "inline":
                    self.assertIn("let me check", _streamed(handler))
                if mode == "fold":
                    self.assertIn("let me check", req.interim_texts)

    def test_verb_without_verdict_answer_delivered(self):
        # Consult-style turn: the verb answers with no mute verdict and the
        # agent's answer is the turn's normal reply.
        resp, _, req, _ = run_turn([
            _split("tool_use", [_skill("s1")], "a"),
            _tool_result("s1", "ok"),
            _split("tool_use", [_call("t1", "scripts/pack/mint_flow.sh status")], "b"),
            _tool_result("t1", NO_FLOW_JSON),
            _split(None, [_text("final answer")], "c"),
            _result(result="final answer"),
        ])
        self.assertIsNone(req.mint_mute)
        self.assertEqual(resp.content, "final answer")

    def test_other_skill_does_not_arm(self):
        resp, handler, req, _ = run_turn([
            _split("tool_use", [_skill("s1", mint_gate.MINT_SKILL + "-notes")], "a"),
            _tool_result("s1", "ok"),
            _split("end_turn", [_text("final answer")], "b"),
            _result(result="final answer"),
        ], interim_mode="inline")
        self.assertFalse(req.mint_armed)
        self.assertIn("final answer", _streamed(handler))
        self.assertEqual(resp.content, "final answer")

    def test_is_mint_skill_call_units(self):
        for skill in (mint_gate.MINT_SKILL, "ns:" + mint_gate.MINT_SKILL,
                      "plugin:x:" + mint_gate.MINT_SKILL):
            self.assertTrue(mint_gate.is_mint_skill_call("Skill", {"skill": skill}))
        for name, tin in (("Skill", {"skill": mint_gate.MINT_SKILL[:-len("-agent")]}),
                          ("Skill", {"skill": "x-" + mint_gate.MINT_SKILL}),
                          ("Read", {"skill": mint_gate.MINT_SKILL}),
                          ("Skill", None)):
            self.assertFalse(mint_gate.is_mint_skill_call(name, tin))


if __name__ == "__main__":
    unittest.main()
