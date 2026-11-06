#!/usr/bin/env python3
# test-lane: fast    # pure lib calls + one adapter subprocess, under 2s
"""DGN-1645 -- output_gate FENCE_CJK_GRID: a CJK label column inside a fence.

Rule (rules/cold.framework.text.md, Tables): Tier 1 fences are values-only;
CJK never aligns in a code block.  The incident (2026-09-23 16:09 / 16:30):
two consecutive replies put a Korean label column, space-aligned, inside a
fence -- with the rule hot-injected and four tickets on it done.  The bridge's
DGN-1889 reshaping covers prose tables only; nothing looked inside fences.

Locks:
  (a) the incident shape (space-aligned) and its pipe / box-bar variants fire;
  (b) the near misses stay silent: a values-only Tier-1 table, a Korean relay
      message, a code fence with Korean comments, a CJK table in prose, a
      single CJK row;
  (c) check_message carries the violation with metrics and a short excerpt;
  (d) output-gate-stop.py logs FENCE_CJK_GRID with no adapter change and
      never blocks on it (detect-only).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ADAPTER = os.path.join(ROOT, "output-gate-stop.py")
sys.path.insert(0, os.path.join(ROOT, "lib"))

import output_gate as og  # noqa: E402

# (a) the incident shape: Korean label column, space-aligned, plain fence.
INCIDENT = (
    "현황입니다.\n"
    "```\n"
    "항목          상태      비고\n"
    "브리지        정상      재시작 없음\n"
    "메모리 엔진   지연      12분 밀림\n"
    "백업          정상      03:00 완료\n"
    "```")
PIPE_GRID = (
    "```\n"
    "| 단계 | 담당 | 기한 |\n"
    "|------|------|------|\n"
    "| 설계 | Ann | 10/12 |\n"
    "| 구현 | Bo | 10/14 |\n"
    "```")
BOX_GRID = (
    "```text\n"
    "단계 │ 결과\n"
    "────┼────\n"
    "설치 │ 통과\n"
    "검증 │ 실패\n"
    "```")
TILDE_GRID = (
    "~~~\n"
    "이름    점수\n"
    "가      10\n"
    "나      12\n"
    "~~~")

# (b) near misses.
TIER1_VALUES = (
    "Disk usage\n"
    "```\n"
    "host     used   free\n"
    "a01      71%    120G\n"
    "a02      12%    800G\n"
    "```")
RELAY = (
    "아래를 그대로 보내 주세요.\n"
    "```\n"
    "안녕하세요. 내일 회의는 오후 3시로 옮겼습니다.\n"
    "자료는 오늘 저녁까지 공유드릴게요.\n"
    "```")
CODE_WITH_KO_COMMENTS = (
    "```python\n"
    "x = load()    # 설정 읽기\n"
    "y = parse(x)  # 파싱\n"
    "```")
PROSE_TABLE = (
    "| 단계 | 담당 |\n"
    "|---|---|\n"
    "| 설계 | Ann |\n"
    "| 구현 | Bo |")
SINGLE_ROW = (
    "```\n"
    "상태    정상\n"
    "```")


def rules(text):
    return [v.rule_id for v in og.check_message(text)]


class TestFires(unittest.TestCase):
    def test_incident_space_aligned(self):
        grids = og.fence_cjk_grids(INCIDENT)
        self.assertEqual(1, len(grids))
        self.assertEqual(4, grids[0]["rows"])

    def test_pipe_grid(self):
        self.assertEqual(3, og.fence_cjk_grids(PIPE_GRID)[0]["rows"])

    def test_box_bar_grid_in_text_fence(self):
        self.assertEqual(3, og.fence_cjk_grids(BOX_GRID)[0]["rows"])

    def test_tilde_fence(self):
        self.assertEqual(3, og.fence_cjk_grids(TILDE_GRID)[0]["rows"])

    def test_two_fences_counted(self):
        v = [x for x in og.check_message(INCIDENT + "\n\n" + PIPE_GRID)
             if x.rule_id == og.FENCE_CJK_GRID_RULE_ID]
        self.assertEqual(1, len(v))
        self.assertEqual({"fences": 2, "rows": 7}, v[0].metrics)

    def test_unterminated_fence_still_scanned(self):
        self.assertTrue(og.fence_cjk_grids(INCIDENT.rsplit("```", 1)[0]))


class TestSilent(unittest.TestCase):
    def test_near_misses(self):
        for name, text in (("tier1", TIER1_VALUES), ("relay", RELAY),
                           ("code", CODE_WITH_KO_COMMENTS),
                           ("prose-table", PROSE_TABLE),
                           ("single-row", SINGLE_ROW), ("empty", ""),
                           ("none", None)):
            with self.subTest(name):
                self.assertEqual([], og.fence_cjk_grids(text))
                self.assertNotIn(og.FENCE_CJK_GRID_RULE_ID,
                                 rules(text or ""))


class TestCheckMessage(unittest.TestCase):
    def test_violation_shape(self):
        v = [x for x in og.check_message(INCIDENT)
             if x.rule_id == og.FENCE_CJK_GRID_RULE_ID]
        self.assertEqual(1, len(v))
        self.assertEqual(og.SEVERITY_DETECT, v[0].severity)
        self.assertEqual({"fences": 1, "rows": 4}, v[0].metrics)
        self.assertTrue(v[0].excerpt.startswith("항목"))
        self.assertLessEqual(len(v[0].excerpt), 60)


class TestAdapterLogs(unittest.TestCase):
    def test_stop_adapter_logs_and_does_not_block(self):
        with tempfile.TemporaryDirectory() as cwd:
            tp = os.path.join(cwd, "session.jsonl")
            log = os.path.join(cwd, "canary.log")
            with open(tp, "w") as fh:
                for e in (
                    {"type": "user", "timestamp": "2026-09-23T07:09:00.000Z",
                     "message": {"content": [{"type": "text",
                                              "text": "status?"}]}},
                    {"type": "assistant",
                     "timestamp": "2026-09-23T07:09:30.000Z",
                     "message": {"id": "m1", "content": [
                         {"type": "text", "text": INCIDENT}]}},
                ):
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            env = dict(os.environ, DOGANY_OUTPUT_GATE_LOG=log)
            p = subprocess.run(
                [sys.executable, ADAPTER],
                input=json.dumps({"hook_event_name": "Stop",
                                  "session_id": "s1", "transcript_path": tp,
                                  "cwd": cwd, "stop_hook_active": False}),
                capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(0, p.returncode, p.stderr)
            self.assertNotIn('"block"', p.stdout)
            with open(log, encoding="utf-8") as fh:
                lines = [ln for ln in fh if "FENCE_CJK_GRID" in ln]
            self.assertEqual(1, len(lines))
            self.assertIn("fences=1 rows=4", lines[0])


if __name__ == "__main__":
    unittest.main()
