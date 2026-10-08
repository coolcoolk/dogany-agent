#!/usr/bin/env python3
# test-lane: fast    # adapter subprocess runs, a few seconds total
"""DGN-1308 -- the idle-promise gate checks every promise, not one action.

rev2 blocked only three named shapes (dispatch / ticket / merge) and let
every other promise through by construction.  DGN-1308 inverts the default:
any commitment in the final reply's closing lines blocks unless THIS turn
left a carrier for it in its tool_use blocks (commit, dispatch, handoff,
background job, ticket write, wakeup/cron/monitor).  A conditional promise
needs an ARMED carrier; a deferred separate item needs a QUEUEING carrier.

Locks, in order:
  1. the three 2026-10-02 misses, verbatim -> BLOCK
  2. the same text + the tool_use that keeps it -> PASS
  3. the ticket's own examples ("재고 올리겠습니다", "22:50에 걸겠습니다")
  4. the explicit exemption list (code, quote, owner trigger, owner
     question, decision list) and the non-commitment forms
  5. earlier-turn armed carrier lookback (conditional only, bounded)
  6. the carrier kill switch and the block log row
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # routines/
ADAPTER = os.path.join(ROOT, "output-gate-stop.py")
sys.path.insert(0, os.path.join(ROOT, "lib"))

import output_gate as og  # noqa: E402

DISPATCH_CMD = ("cd /x/agent && bash routines/dispatch-detached.sh "
                "--task files/tmp/t.md --model opus --label t")
COMMIT_CMD = "cd /x/agent && git add -A && git commit -qm 'bridge: fix'"
PUSH_CMD = "cd /x/agent && git push origin dev > /tmp/push.log 2>&1"
HANDOFF_CMD = "bash routines/handoff-send.sh --to peer --subject cleanup"

# --- the three 2026-10-02 misses (dev-instance final replies, closing lines;
# the peer agent's persona name is replaced by "Nova") ----------------------- #
MISS_1311 = (
    "**리허설 뒤 정리:** 리허설 때문에 막아 둔 형님 홈 폴더 권한을 Nova가 "
    "원래대로 돌립니다.\n"
    "1~3번과 4번 안내문 초안은 지금 바로 이어서 하겠습니다. 안내문 초안이 "
    "나오면 보여 드리겠습니다.")
MISS_1543 = (
    "알겠습니다. 설치본이 준비되면 바로 말씀드리겠습니다. 처음부터 다시 "
    "하시려면 리허설 계정에 지금 설치된 에이전트를 먼저 지워야 합니다. "
    "그 정리는 Nova에게 미리 해 두라고 하겠습니다.")
MISS_160X = (
    "- **지금:** 원격에 올리는 중이고, 올라가면 바로 리허설 설치본을 다시 "
    "만들어 알려 드리겠습니다.\n"
    "설치한 뒤 실제로 봇을 켜서 /claim까지 받아 보는 검사는 아직 없습니다. "
    "이 검사를 추가하는 것은 따로 이어서 하겠습니다.")


def _ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


NOW = datetime(2026, 10, 2, 7, 0, 0, tzinfo=timezone.utc)


def user_entry(text="hi", at=None):
    return {"type": "user", "timestamp": _ts(at or NOW),
            "message": {"content": [{"type": "text", "text": text}]}}


def assistant_text(text, mid="m1", at=None):
    return {"type": "assistant", "timestamp": _ts(at or NOW),
            "message": {"id": mid, "content": [{"type": "text", "text": text}]}}


def assistant_tool(name, inp, mid="m0", at=None):
    return {"type": "assistant", "timestamp": _ts(at or NOW),
            "message": {"id": mid,
                        "content": [{"type": "tool_use", "id": "t-" + mid,
                                     "name": name, "input": inp}]}}


def tool_result_entry(at=None):
    return {"type": "user", "timestamp": _ts(at or NOW),
            "message": {"content": [{"type": "tool_result",
                                     "tool_use_id": "t1", "content": "ok"}]}}


def turn(text, *tools):
    """One turn: prompt, the given (name, input) tool calls, closing reply."""
    out = [user_entry("가자")]
    for i, (name, inp) in enumerate(tools):
        out += [assistant_tool(name, inp, mid="m%d" % i), tool_result_entry()]
    out.append(assistant_text(text, mid="mz"))
    return out


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = self.tmp.name
        self.transcript = os.path.join(self.cwd, "session.jsonl")
        self.log_path = os.path.join(self.cwd, "canary.log")
        self.extra_env = {}

    def tearDown(self):
        self.tmp.cleanup()

    def run_adapter(self, entries):
        with open(self.transcript, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        payload = {"hook_event_name": "Stop", "session_id": "s1",
                   "transcript_path": self.transcript, "cwd": self.cwd,
                   "stop_hook_active": False}
        env = dict(os.environ)
        env["DOGANY_OUTPUT_GATE_LOG"] = self.log_path
        env.update(self.extra_env)
        p = subprocess.run([sys.executable, ADAPTER],
                           input=json.dumps(payload), capture_output=True,
                           text=True, timeout=30, env=env)
        self.assertEqual(0, p.returncode, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def assertBlocks(self, entries, msg=""):
        out = self.run_adapter(entries)
        self.assertEqual("block", out.get("decision"), msg)
        return out

    def assertPasses(self, entries, msg=""):
        out = self.run_adapter(entries)
        self.assertEqual({}, out, msg)


# --------------------------------------------------------------------------- #
# 1 + 2: the three misses and their kept twins
# --------------------------------------------------------------------------- #

class TestTodaysMisses(AdapterCase):

    def test_1641_owner_request_before_commitment_passes(self):
        self.assertPasses(turn(
            "리허설에서 문제가 나오면 알려 주세요. 바로 고치겠습니다."))

    def test_1311_continue_now_with_nothing_started_blocks(self):
        out = self.assertBlocks(turn(MISS_1311, ("Bash", {
            "command": "wc -l < /tmp/tl2-results.tsv"})))
        self.assertIn("지금 바로 이어서 하겠습니다", out["reason"])

    def test_1311_fixed_work_started_in_background_passes(self):
        self.assertPasses(turn(MISS_1311, ("Bash", {
            "command": "bash tests/run-slow-lane.sh",
            "run_in_background": True})))

    def test_1311_fixed_work_dispatched_passes(self):
        self.assertPasses(turn(MISS_1311, ("Bash", {"command": DISPATCH_CMD})))

    def test_1543_handoff_promised_never_sent_blocks(self):
        out = self.assertBlocks(turn(MISS_1543))
        self.assertIn("Nova에게 미리 해 두라고", out["reason"])

    def test_1543_fixed_handoff_sent_passes(self):
        self.assertPasses(turn(MISS_1543, ("Bash", {"command": HANDOFF_CMD})))

    def test_1543_fixed_send_message_passes(self):
        self.assertPasses(turn(MISS_1543, ("SendMessage", {
            "to": "nova", "message": "rehearsal account cleanup"})))

    def test_160x_deferred_item_not_carried_by_unrelated_commit(self):
        """The real turn committed a fix and pushed in the background --
        evidence for the work it DID, not for the E2E check it deferred."""
        out = self.assertBlocks(turn(
            MISS_160X,
            ("Bash", {"command": COMMIT_CMD}),
            ("Bash", {"command": PUSH_CMD, "run_in_background": True})))
        self.assertIn("따로 이어서 하겠습니다", out["reason"])
        self.assertIn("defers a separate item", out["reason"])

    def test_160x_fixed_ticket_written_passes(self):
        self.assertPasses(turn(
            MISS_160X,
            ("Bash", {"command": COMMIT_CMD}),
            ("Bash", {"command": PUSH_CMD, "run_in_background": True}),
            ("Write", {"file_path": "/x/agent/worklog/DGN-1832-claim-e2e.md",
                       "content": "---\nid: DGN-1832\n---\n"})))

    def test_160x_fixed_dispatched_passes(self):
        self.assertPasses(turn(
            MISS_160X,
            ("Bash", {"command": COMMIT_CMD}),
            ("Bash", {"command": PUSH_CMD, "run_in_background": True}),
            ("Bash", {"command": DISPATCH_CMD})))


# --------------------------------------------------------------------------- #
# 3: the ticket's own examples (library level)
# --------------------------------------------------------------------------- #

class TestTicketExamples(unittest.TestCase):

    def block(self, text, blob="", armed_earlier=False):
        return og.idle_promise_block_verdict(text, blob, armed_earlier)

    def test_measure_promise_blocks(self):
        self.assertIsNotNone(self.block("재고 올리겠습니다."))

    def test_time_marker_is_not_an_exemption(self):
        self.assertIsNotNone(self.block("22:50에 걸겠습니다."))

    def test_scheduled_twin_passes(self):
        for blob in ("TOOL ScheduleWakeup ", "TOOL CronCreate ",
                     "CMD crontab /tmp/cron.txt"):
            self.assertIsNone(self.block("22:50에 걸겠습니다.", blob), blob)

    def test_crontab_listing_is_not_a_schedule(self):
        self.assertIsNotNone(self.block("22:50에 걸겠습니다.", "CMD crontab -l"))

    def test_conditional_report_needs_an_armed_carrier(self):
        text = "결과 나오면 정리해서 올리겠습니다."
        self.assertIsNotNone(self.block(text))
        self.assertIsNotNone(self.block(text, "CMD " + COMMIT_CMD),
                             "a commit does not bring a result back")
        for blob in ("CMD bash x.sh\nBG Bash", "TOOL Agent \nBG Agent",
                     "TOOL Monitor ", "CMD " + DISPATCH_CMD):
            self.assertIsNone(self.block(text, blob), blob)

    def test_carrier_misattribution_is_the_accepted_residual(self):
        """An unconditional promise passes on ANY carrier in the turn --
        "committed X, promised Y" is the documented residual."""
        self.assertIsNone(self.block("재고 올리겠습니다.", "CMD " + COMMIT_CMD))

    def test_carrier_signatures_are_invocations_not_reads(self):
        for blob in ("CMD git log --oneline -3",
                     "CMD git merge-base main dev",
                     "CMD cat worklog/DGN-1.md",
                     "TOOL Read /x/worklog/DGN-1.md"):
            self.assertIsNotNone(self.block("재고 올리겠습니다.", blob), blob)

    def test_unreadable_turn_never_blocks(self):
        self.assertIsNone(og.idle_promise_block_verdict("재고 올리겠습니다.", None))


# --------------------------------------------------------------------------- #
# 4: the exemption list and the non-commitment forms
# --------------------------------------------------------------------------- #

class TestExemptions(unittest.TestCase):

    def none(self, text, why=""):
        self.assertIsNone(og.idle_promise_block_verdict(text, ""), why or text)

    def test_code_fence(self):
        self.none("예시입니다.\n```\n지금 고치겠습니다.\n```")

    def test_blockquote_owner_text(self):
        self.none("정리했습니다.\n> 형님: 내일 다시 보겠습니다.")

    def test_quoted_span(self):
        self.none('답장 문구는 "곧 확인하겠습니다"로 바꿨습니다.')

    def test_owner_trigger(self):
        for t in ("버튼 누르시면 바로 적용하겠습니다.",
                  "승인 주시면 그때 배포하겠습니다.",
                  "착수하실 거면 다음 발주는 거기로 잡겠습니다."):
            self.none(t)

    def test_immediately_preceding_owner_request(self):
        for request in ("문제를 알려 주세요", "결과를 보내주세요",
                        "재현을 해 주시면", "**문제를 알려 주세요**"):
            for separator in (". ", "! ", "\n"):
                with self.subTest(request=request, separator=separator):
                    self.none(request + separator + "바로 고치겠습니다.")

    def test_owner_request_exempts_only_the_next_sentence(self):
        for text in (
                "알려 주세요. 상태는 그대로입니다. 바로 고치겠습니다.",
                "알려 주세요. 바로 고치겠습니다. 검사도 추가하겠습니다.",
                "바로 고치겠습니다. 알려 주세요."):
            with self.subTest(text=text):
                self.assertIsNotNone(og.idle_promise_block_verdict(text, ""))

    def test_previous_sentence_must_be_a_request(self):
        for previous in (
                "처음부터 다시 하시려면 에이전트를 먼저 지워야 합니다.",
                "결과가 나오면 상태가 바뀝니다.",
                '안내 문구는 "알려 주세요"입니다.',
                "알려 주세요라고 안내했습니다."):
            with self.subTest(previous=previous):
                self.assertIsNotNone(og.idle_promise_block_verdict(
                    previous + " 바로 고치겠습니다.", ""))

    def test_owner_question_in_closing_lines(self):
        self.none("확인하고 말씀드리겠습니다.\n확인 들어갈까요? 태그는 로컬에만 있습니다.")
        self.none("**소비 걸까요?** 재시작 20초 정도고, 돌아와서 이어갑니다.")

    def test_decision_list(self):
        self.none("티켓 열겠습니다.\n[[OPTIONS: 지금 | 나중에]]")

    def test_non_commitments(self):
        for t in ("안 여쭙겠습니다.", "넘겨짚지 않겠습니다.",
                  "지금은 판단 못 하겠습니다.", "모르겠습니다.",
                  "이번엔 통과할 겁니다.", "그래서 만든 겁니다.",
                  "반복 통지 구간에 안 들어갑니다.", "한 가지만 짚고 갑니다.",
                  "2.7 후보로 두겠습니다.", "다섯 건이 2.5에 들어갑니다."):
            self.none(t)

    def test_commitments_still_caught(self):
        for t in ("안내 문구를 고치겠습니다.", "제가 할 겁니다.",
                  "2.0 이어갑니다.", "지금 시작합니다.", "스킬로 박겠습니다."):
            self.assertIsNotNone(og.idle_promise_block_verdict(t, ""), t)

    def test_chained_condition_inherits(self):
        text = ("테스트가 아직 돌고 있습니다. 끝나면 음성 설정을 합칩니다. "
                "그다음 다음 버전으로 봇들에 적용하겠습니다.")
        self.assertIsNone(og.idle_promise_block_verdict(text, "", True))
        self.assertIsNotNone(og.idle_promise_block_verdict(text, ""))

    def test_scope_is_the_closing_lines(self):
        body = "\n".join(["1. 지금 고치겠습니다."] + ["상태 줄 %d" % i
                                                     for i in range(3)])
        self.none(body, "a body line above the closing two is plan narration")


# --------------------------------------------------------------------------- #
# 5: earlier-turn armed carrier lookback (adapter)
# --------------------------------------------------------------------------- #

class TestArmedLookback(AdapterCase):
    COND = "작업 정리했습니다.\n동생 결과 오면 바로 회수하겠습니다."
    UNCOND = "작업 정리했습니다.\n지금 바로 고치겠습니다."

    def earlier(self, hours_ago, tool=("Bash", {"command": DISPATCH_CMD})):
        at = NOW - timedelta(hours=hours_ago)
        return [user_entry("발주해", at=at),
                assistant_tool(tool[0], tool[1], mid="e0", at=at),
                tool_result_entry(at=at),
                assistant_text("발주했습니다.", mid="e1", at=at)]

    def test_conditional_passes_on_dispatch_still_in_window(self):
        self.assertPasses(self.earlier(2) + turn(self.COND))

    def test_conditional_blocks_once_window_ran_out(self):
        self.assertBlocks(self.earlier(7) + turn(self.COND))

    def test_background_job_window_is_shorter(self):
        bg = ("Bash", {"command": "bash t.sh", "run_in_background": True})
        self.assertPasses(self.earlier(0.5, bg) + turn(self.COND))
        self.assertBlocks(self.earlier(2, bg) + turn(self.COND))

    def test_unconditional_never_rides_an_earlier_carrier(self):
        self.assertBlocks(self.earlier(1) + turn(self.UNCOND))

    def test_earlier_commit_is_not_armed(self):
        self.assertBlocks(self.earlier(1, ("Bash", {"command": COMMIT_CMD}))
                          + turn(self.COND))


# --------------------------------------------------------------------------- #
# 6: kill switch + block log row + blob markers
# --------------------------------------------------------------------------- #

class TestSwitchAndLog(AdapterCase):

    def test_carrier_slug_off_emits_notice_not_block(self):
        self.extra_env = {"GATE_STOP_OUTPUT_IDLE_PROMISE_CARRIER": "off"}
        out = self.run_adapter(turn("재고 올리겠습니다."))
        self.assertNotIn("decision", out)
        self.assertIn("output-gate-idle-promise-carrier", out["systemMessage"])

    def test_carrier_slug_off_does_not_disarm_dispatch(self):
        self.extra_env = {"GATE_STOP_OUTPUT_IDLE_PROMISE_CARRIER": "off"}
        self.assertBlocks(turn("이제 M2 발주로 돌아가겠습니다."))

    def test_family_slug_off_suppresses_carrier(self):
        self.extra_env = {"GATE_STOP_OUTPUT_IDLE_PROMISE": "off"}
        out = self.run_adapter(turn("재고 올리겠습니다."))
        self.assertNotIn("decision", out)

    def test_block_writes_a_log_row(self):
        self.assertBlocks(turn("재고 올리겠습니다."))
        with open(self.log_path) as fh:
            self.assertIn("IDLE_PROMISE_BLOCK  kind=carrier", fh.read())

    def test_block_fires_without_the_detect_rule(self):
        """The detect rule reads the last two RAW lines; a trailing quote
        line hides the promise from it but not from the block layer."""
        out = self.assertBlocks(turn("재고 올리겠습니다.\n> 형님: 그래"))
        self.assertIn("재고 올리겠습니다", out["reason"])

    def test_background_flag_reaches_the_blob(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ogs", ADAPTER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        lines = mod._tool_blob_lines({"type": "tool_use", "name": "Bash",
                                      "input": {"command": "x",
                                                "run_in_background": True}})
        self.assertEqual(["CMD x", "BG Bash"], lines)
        lines = mod._tool_blob_lines({"type": "tool_use", "name": "Agent",
                                      "input": {"run_in_background": True}})
        self.assertEqual(["TOOL Agent ", "BG Agent"], lines)


if __name__ == "__main__":
    unittest.main()
