#!/usr/bin/env python3
# test-lane: fast    # measured below 1s (DGN-1263 marker requirement)
"""DGN-1279 -- IDLE_PROMISE turns from detect-only into a narrow block.

Locks, in order of the DGN-1279 spec:
  1. promise + no matching action -> the adapter emits decision:block
  2. promise + matching action in the same turn -> pass
  3. split turn (B2 shape: the action ran in an EARLIER message of the same
     turn, the promise closes it) -> pass, no false block
  4. B1: every other rule stays detect-only while IDLE_PROMISE blocks
Plus the false-positive classes measured on 2026-09-03..04 transcripts
(contingent future tense, owner gate, owner question, non-whitelisted
promise) and the fail-open paths.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # routines/
ADAPTER = os.path.join(ROOT, "output-gate-stop.py")
sys.path.insert(0, os.path.join(ROOT, "lib"))

from output_gate import (  # noqa: E402
    check_message,
    idle_promise_block_reason,
)

DISPATCH_CMD = (
    "cd /Users/x/agent; bash routines/dispatch-detached.sh "
    "--task files/tmp/m2.md --model opus --label m2"
)


# --- transcript helpers (mirror test-output-gate.py) ------------------------ #

def user_entry(text="hi"):
    return {"type": "user",
            "message": {"content": [{"type": "text", "text": text}]}}


def assistant_text(text, mid="m1"):
    return {"type": "assistant",
            "message": {"id": mid, "content": [{"type": "text", "text": text}]}}


def assistant_tool(name, inp, mid="m0"):
    return {"type": "assistant",
            "message": {"id": mid,
                        "content": [{"type": "tool_use", "id": "t1",
                                     "name": name, "input": inp}]}}


def tool_result_entry():
    """A tool_result user entry -- tool feedback, NOT a turn boundary."""
    return {"type": "user",
            "message": {"content": [{"type": "tool_result",
                                     "tool_use_id": "t1", "content": "ok"}]}}


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = self.tmp.name
        self.transcript = os.path.join(self.cwd, "session.jsonl")
        self.log_path = os.path.join(self.cwd, "canary.log")

    def tearDown(self):
        self.tmp.cleanup()

    def run_adapter(self, entries, stop_hook_active=False):
        with open(self.transcript, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        payload = {"hook_event_name": "Stop", "session_id": "s1",
                   "transcript_path": self.transcript, "cwd": self.cwd,
                   "stop_hook_active": stop_hook_active}
        env = dict(os.environ)
        env["DOGANY_OUTPUT_GATE_LOG"] = self.log_path
        # Keep the run independent of this instance's conf: the gate switch
        # defaults to ON when the key is absent, which is what we assert.
        return subprocess.run([sys.executable, ADAPTER],
                              input=json.dumps(payload), capture_output=True,
                              text=True, timeout=30, env=env)


# --------------------------------------------------------------------------- #
# 1..3: the three spec cases, end to end through the adapter
# --------------------------------------------------------------------------- #

class TestSpecCases(AdapterCase):

    def test_promise_without_action_blocks(self):
        """Case 1 -- promise present, action absent -> block fires."""
        p = self.run_adapter([
            user_entry("가자"),
            assistant_text("티켓에 다 걸었습니다.\n이제 M2 발주로 돌아가겠습니다."),
        ])
        self.assertEqual(0, p.returncode)
        out = json.loads(p.stdout.strip())
        self.assertEqual("block", out.get("decision"),
                         "promise with no dispatch must block")
        self.assertIn("IDLE_PROMISE block (DGN-1279)", out.get("reason", ""))

    def test_promise_with_action_passes(self):
        """Case 2 -- the promised dispatch ran in this turn -> no block."""
        p = self.run_adapter([
            user_entry("가자"),
            assistant_tool("Bash", {"command": DISPATCH_CMD}, mid="m0"),
            tool_result_entry(),
            assistant_text("이제 M2 발주로 돌아가겠습니다.", mid="m1"),
        ])
        self.assertEqual(0, p.returncode)
        self.assertEqual("", p.stdout.strip(),
                         "tense slip after a real dispatch must NOT block")

    def test_split_turn_action_in_earlier_message_passes(self):
        """Case 3 (B2 shape) -- the separation-contract turn.

        The 2026-08-14 SKELETON revert died on exactly this: a turn split
        into several assistant messages, where judging only the LAST message
        loses what the earlier ones did.  The action ran in message 1; the
        promise closes message 3.  Must not block.
        """
        p = self.run_adapter([
            user_entry("가자"),
            assistant_text("먼저 판을 보겠습니다.", mid="m0"),
            assistant_tool("Bash", {"command": DISPATCH_CMD}, mid="m1"),
            tool_result_entry(),
            assistant_text("```\n<코드/표 분리 전송>\n```", mid="m2"),
            assistant_text("정리했습니다.\n이제 M2 발주로 돌아가겠습니다.", mid="m3"),
        ])
        self.assertEqual(0, p.returncode)
        self.assertEqual("", p.stdout.strip(),
                         "split turn: action in an earlier message must NOT block")


# --------------------------------------------------------------------------- #
# 4 (B1): the detect-only contract for every OTHER rule still holds
# --------------------------------------------------------------------------- #

class TestOtherRulesStayDetectOnly(AdapterCase):
    """B1 -- the checked-in detect-only contract was the first of the two
    bugs that reverted the 2026-08-14 block.  Arming IDLE_PROMISE must not
    arm anything else."""

    def _wall_text(self):
        line = ("이것은 벽텍스트 테스트 줄입니다. 매우 긴 산문이 계속됩니다. "
                "추가적인 긴 내용이 있습니다.")
        return "\n".join([line] * 27)

    def test_wall_detects_but_never_blocks(self):
        p = self.run_adapter([user_entry(), assistant_text(self._wall_text())])
        self.assertEqual(0, p.returncode)
        self.assertEqual("", p.stdout.strip(), "WALL must stay detect-only")
        with open(self.log_path) as fh:
            self.assertIn("WALL", fh.read())

    def test_skeleton_detects_but_never_blocks(self):
        text = "1. 옵션 A -- 설명\n2. 옵션 B -- 설명\n3. 옵션 C -- 설명\n[[OPTIONS]]"
        p = self.run_adapter([user_entry(), assistant_text(text)])
        self.assertEqual(0, p.returncode)
        self.assertEqual("", p.stdout.strip(), "SKELETON must stay detect-only")
        with open(self.log_path) as fh:
            self.assertIn("SKELETON", fh.read())

    def test_done_claim_detects_but_never_blocks(self):
        p = self.run_adapter([user_entry(), assistant_text("완료했습니다.")])
        self.assertEqual(0, p.returncode)
        self.assertEqual("", p.stdout.strip(),
                         "DONE_CLAIM must stay detect-only")

    def test_idle_promise_outside_whitelist_detects_but_never_blocks(self):
        """A promise the whitelist does not cover stays canary-only."""
        p = self.run_adapter([
            user_entry(),
            assistant_text("정리했습니다.\n이제 install.sh 수정을 이어가겠습니다."),
        ])
        self.assertEqual(0, p.returncode)
        self.assertEqual("", p.stdout.strip(),
                         "non-whitelisted promise must NOT block")
        with open(self.log_path) as fh:
            self.assertIn("IDLE_PROMISE", fh.read())

    def test_block_never_loops(self):
        """stop_hook_active -> the re-prompted turn is never blocked again."""
        p = self.run_adapter([
            user_entry(),
            assistant_text("이제 M2 발주로 돌아가겠습니다."),
        ], stop_hook_active=True)
        self.assertEqual(0, p.returncode)
        self.assertEqual("", p.stdout.strip())


# --------------------------------------------------------------------------- #
# Signature integrity: a MENTION of the command is not an invocation
# --------------------------------------------------------------------------- #

class TestSignatureIsInvocationNotMention(AdapterCase):
    """Replay #51 (2026-09-04 11:05) passed the first cut of this gate
    because the turn wrote a memo whose text contained
    "routines/dispatch-detached.sh".  Beyond the miss, that is gameable:
    naming the command in any file would disarm the gate."""

    PROMISE = "티켓에 다 반영했습니다.\n이제 M2 발주로 돌아가겠습니다."

    def test_command_named_inside_a_written_file_still_blocks(self):
        p = self.run_adapter([
            user_entry(),
            assistant_tool("Write", {
                "file_path": "/x/worklog/DGN-1.md",
                "content": "부착점은 routines/dispatch-detached.sh 종결부다.",
            }, mid="m0"),
            tool_result_entry(),
            assistant_text(self.PROMISE, mid="m1"),
        ])
        out = json.loads(p.stdout.strip())
        self.assertEqual("block", out.get("decision"),
                         "a Write mentioning the command is not a dispatch")

    def test_command_named_inside_a_heredoc_still_blocks(self):
        p = self.run_adapter([
            user_entry(),
            assistant_tool("Bash", {"command":
                "cd /x; cat >> worklog/DGN-1.md <<'EOF'\n"
                "부착점 = routines/dispatch-detached.sh 종결부\nEOF"}, mid="m0"),
            tool_result_entry(),
            assistant_text(self.PROMISE, mid="m1"),
        ])
        out = json.loads(p.stdout.strip())
        self.assertEqual("block", out.get("decision"),
                         "a heredoc body mentioning the command is not a dispatch")

    def test_unterminated_heredoc_still_blocks(self):
        p = self.run_adapter([
            user_entry(),
            assistant_tool("Bash", {"command":
                "cat > a.md <<'EOF'\nrun routines/dispatch-detached.sh later"},
                mid="m0"),
            tool_result_entry(),
            assistant_text(self.PROMISE, mid="m1"),
        ])
        out = json.loads(p.stdout.strip())
        self.assertEqual("block", out.get("decision"),
                         "an unterminated heredoc must be stripped to end")

    def test_real_invocation_after_a_heredoc_passes(self):
        """The stripper must not eat the rest of the command line."""
        p = self.run_adapter([
            user_entry(),
            assistant_tool("Bash", {"command":
                "cat > t.md <<'EOF'\ntask body\nEOF\n"
                "bash routines/dispatch-detached.sh --task t.md --model opus"},
                mid="m0"),
            tool_result_entry(),
            assistant_text(self.PROMISE, mid="m1"),
        ])
        self.assertEqual("", p.stdout.strip(),
                         "a real invocation after a heredoc must pass")


# --------------------------------------------------------------------------- #
# Measured false-positive classes must not block (library level)
# --------------------------------------------------------------------------- #

class TestMeasuredFalsePositives(unittest.TestCase):
    """Every case below is a verbatim tail from the 2026-09-03..04 canary
    replay that was judged a false positive.  None may block."""

    def assertNoBlock(self, text, blob="", why=""):
        self.assertIsNone(idle_promise_block_reason(text, blob), why)

    def test_contingent_on_dispatch_return(self):
        # replay #37 -- kept in the next turn
        self.assertNoBlock(
            "부재 주장은 탐색 방법의 한계와 함께만 성립합니다.\n"
            "하나 끝나는 대로 다음 걸 발주하겠습니다.",
            why="contingent future tense ('끝나는 대로') must not block")

    def test_contingent_conditional(self):
        # replay #17 -- owner said 맞다, agent dispatched
        self.assertNoBlock(
            "부피의 정체가 규칙 개수가 아니라 근거 서술이었습니다.\n"
            "이 방향이 맞습니까? 맞으면 2차를 이 스펙으로 발주하겠습니다.",
            why="conditional promise ('맞으면') must not block")

    def test_owner_gate(self):
        self.assertNoBlock(
            "트리는 초록입니다.\n형님 승인 주시면 바로 발주하겠습니다.",
            why="owner-gated promise must not block")

    def test_owner_question_turn(self):
        self.assertNoBlock(
            "지금 발주하겠습니다.\n먼저 여쭙겠습니다 -- 이 순서로 갈까요?",
            why="turn closing on an owner question must not block")

    def test_dispatch_as_a_noun_is_not_a_promise(self):
        # replay #22/#45 -- "발주" as an object, no 발주하- promise form
        self.assertNoBlock(
            "오늘 dev에 13커밋 올라갔습니다.\n"
            "지금은 두 발주 리뷰하고 머지부터 하겠습니다.",
            why="'발주' as a noun object is not a dispatch promise")

    def test_copula_is_not_a_promise(self):
        # replay #28 -- "...잡는 겁니다" is explanatory, not a commitment
        self.assertNoBlock(
            "21건이 더 올라와야 합니다.\n"
            "지금 붙어 있는 3건은 잘못 동작 중인 것들부터 잡는 겁니다.",
            why="copula '겁니다' must not reach the block layer")

    def test_contingency_in_a_neighbouring_sentence_does_not_exempt(self):
        """Scope check -- replay #49 had an unrelated '쓰면' one sentence
        before a real violation.  Whole-tail contingency matching would have
        thrown that true positive away."""
        self.assertIsNotNone(idle_promise_block_reason(
            "M2의 '0건' 게이트를 그대로 쓰면 통과 못 합니다.\n"
            "그 조건을 넣어서 M2 발주하겠습니다.", ""))


# --------------------------------------------------------------------------- #
# Fail-open + metric contract
# --------------------------------------------------------------------------- #

class TestFailOpenAndMetrics(unittest.TestCase):

    def test_unreadable_turn_never_blocks(self):
        """blob None (adapter could not read the turn) -> no block, ever."""
        self.assertIsNone(
            idle_promise_block_reason("이제 M2 발주로 돌아가겠습니다.", None))

    def test_empty_text_never_blocks(self):
        self.assertIsNone(idle_promise_block_reason("", ""))
        self.assertIsNone(idle_promise_block_reason(None, ""))

    def test_action_contrast_metrics_logged(self):
        """The canary gains the contrast had_action never carried."""
        vs = check_message("이제 M2 발주로 돌아가겠습니다.", turn_tool_blob="")
        ip = [v for v in vs if v.rule_id == "IDLE_PROMISE"]
        self.assertEqual(1, len(ip))
        self.assertEqual("dispatch", ip[0].metrics["action"])
        self.assertIs(False, ip[0].metrics["action_done"])

    def test_action_contrast_metrics_none_when_not_computable(self):
        vs = check_message("이제 M2 발주로 돌아가겠습니다.")
        ip = [v for v in vs if v.rule_id == "IDLE_PROMISE"]
        self.assertEqual("-", ip[0].metrics["action_done"])

    def test_detection_breadth_unchanged(self):
        """DGN-1081 rev3 breadth is deliberate; the block layer sits on top
        of it and must not narrow it."""
        for text in ("2.0 이어갑니다.", "지금 계속 밀겠습니다.", "바로 하겠습니다."):
            vs = check_message(text, turn_tool_blob="")
            self.assertTrue(any(v.rule_id == "IDLE_PROMISE" for v in vs),
                            "detection narrowed for: %s" % text)
            self.assertIsNone(idle_promise_block_reason(text, ""),
                              "detection-only shape must not block: %s" % text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
