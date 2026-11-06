#!/usr/bin/env python3
# test-lane: fast    # pure lib calls + a few adapter subprocess runs
"""DGN-2049 -- output_gate WHAT_SOLO_PICK: a WHAT choice decided alone.

Canon (rules/hot.framework.md Problem solving): HOW (method, order, tooling,
verification) = the agent decides, never a menu; WHAT (wording, look,
emoji/glyphs, layout, names, tone) = 2-7 rendered candidates + one
recommendation, the user picks.  Incident 2026-10-13 06:49-06:51: the owner
called three emoji candidates ugly ("실화냐"); the agent decided alone, twice
("📏로 갈게요", then "🎯로 갈게요").  Owner: "이건 what의 영역인데 왜 알아서
정하냐고, 몇 개 보여주고 고르게 해야지."

Locks:
  (a) the two incident phrases fire, verbatim, after the owner's reaction;
      so do a quoted name, a What noun and a "this one" pick of a What;
  (b) How near misses stay silent: tool names, an order, a method, a
      storage choice, a Hangul word that merely contains a What syllable;
  (c) exemptions: a candidate list in THIS turn ([[OPTIONS]] or 2+ numbered
      lines), the user's own pick or delegation, a quoted / code / blockquote
      mention of the phrase, a question;
  (d) a candidate list from an EARLIER turn does not exempt (the incident);
  (e) the adapter ladder: record (default) writes one row and never blocks;
      block writes the row FIRST, then WHAT_SOLO_PICK_BLOCK, then blocks;
      no block on a re-prompt or for a How phrase; off writes nothing; the
      switch slug output-gate-what-pick turns the block into a notice.
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

OWNER_REACTION = "실화냐"
INCIDENT_1 = "📏로 갈게요."
INCIDENT_2 = "🎯로 갈게요. 줄자보다 과녁이 정의 줄에 더 맞아요."

FIRES = [
    INCIDENT_1,
    INCIDENT_2,
    "그럼 📏 로 갈게요",
    "제목은 '아침 루틴'으로 정했어요.",
    "버튼 문구는 이걸로 갈게요.",
    "이모지는 ✅로 바꿀게요.",
    "카드 이름은 \"오늘의 한 줄\"로 하겠습니다.",
]

HOW_NEAR_MISSES = [
    "pytest로 갈게요.",
    "이 순서로 갈게요: 백업 먼저, 그다음 이관.",
    "rsync 방식으로 갈게요.",
    "worktree로 갈게요.",
    "SQLite로 정했어요.",
    "검색으로 갈게요.",
    "2번 방법으로 갈게요.",
    "테스트 문구 검증은 pytest로 갈게요.",
    "dispatch로 돌릴게요.",
    "이 구조로 할게요.",
]


class LibFires(unittest.TestCase):
    def test_incident_phrases_fire(self):
        for t in (INCIDENT_1, INCIDENT_2):
            hit = og.what_solo_pick(t, None, OWNER_REACTION)
            self.assertIsNotNone(hit, t)
            self.assertEqual("emoji", hit["signal"], t)

    def test_what_decisions_fire(self):
        for t in FIRES:
            self.assertIsNotNone(og.what_solo_pick(t, None, "제목 어떻게 해"),
                                 t)

    def test_signals(self):
        self.assertEqual("quoted", og.what_solo_pick(
            "제목은 '아침 루틴'으로 정했어요.", None, "음")["signal"])
        self.assertEqual("pick-ref", og.what_solo_pick(
            "버튼 문구는 이걸로 갈게요.", None, "음")["signal"])

    def test_check_view_carries_excerpt(self):
        v = og._check_what_solo_pick(INCIDENT_1, None, OWNER_REACTION)
        self.assertEqual(og.WHAT_SOLO_PICK_RULE_ID, v.rule_id)
        self.assertEqual(og.SEVERITY_DETECT, v.severity)
        self.assertIn("📏", v.excerpt)
        self.assertEqual("emoji", v.metrics["signal"])

    def test_block_reason_is_english_and_names_the_fix(self):
        r = og.what_solo_pick_block_reason({"phrase": "📏로 갈게요"})
        self.assertIn("WHAT_SOLO_PICK", r)
        self.assertIn("2-7 concrete candidates", r)
        self.assertIn("NEW", r)


class LibHowStaysSilent(unittest.TestCase):
    def test_how_near_misses(self):
        for t in HOW_NEAR_MISSES:
            self.assertIsNone(og.what_solo_pick(t, None, OWNER_REACTION), t)

    def test_plain_report_is_silent(self):
        self.assertIsNone(og.what_solo_pick(
            "커밋했고 테스트 3개 모두 통과했습니다.", None, "어떻게 됐어"))


class LibExemptions(unittest.TestCase):
    def test_numbered_candidates_this_turn(self):
        t = ("후보입니다.\n1. 📏 정의\n2. 🎯 정의\n3. 🧭 정의\n"
             "추천은 1번, 📏로 갈게요.")
        self.assertIsNone(og.what_solo_pick(t, None, OWNER_REACTION))

    def test_blockquote_candidates_this_turn(self):
        t = "> 1. 📏 정의\n> 2. 🎯 정의\n\n📏로 갈게요."
        self.assertIsNone(og.what_solo_pick(t, None, OWNER_REACTION))

    def test_options_menu_this_turn(self):
        t = "📏로 갈게요?\n[[OPTIONS]]\n1. 📏\n2. 🎯\n[[/OPTIONS]]"
        self.assertIsNone(og.what_solo_pick(t, None, OWNER_REACTION))

    def test_candidates_in_an_earlier_message_of_this_turn(self):
        turn = ["1. 📏 정의\n2. 🎯 정의", INCIDENT_1]
        self.assertIsNone(og.what_solo_pick(INCIDENT_1, turn, OWNER_REACTION))

    def test_user_picked_or_delegated(self):
        for prompt in ("📏로 해줘", "2번", "2번으로", "그걸로 가자",
                       "알아서 골라줘", "좋아 그걸로", "네가 정해"):
            self.assertIsNone(og.what_solo_pick(INCIDENT_1, None, prompt),
                              prompt)

    def test_negative_reaction_is_not_a_pick(self):
        # A reaction to the candidates means NEW candidates, never a pick.
        for prompt in ("실화냐", "별로야", "못생겼다", "안 좋아",
                       "좋아 보이지 않아", "그래서 뭘로 할 건데"):
            self.assertIsNotNone(og.what_solo_pick(INCIDENT_1, None, prompt),
                                 prompt)

    def test_quoted_code_blockquote_question(self):
        for t in ('앞서 "📏로 갈게요"라고 한 건 잘못이었습니다.',
                  "로그: `📏로 갈게요`",
                  "> 📏로 갈게요",
                  "📏로 갈까요?",
                  "```\n📏로 갈게요\n```"):
            self.assertIsNone(og.what_solo_pick(t, None, OWNER_REACTION), t)

    def test_rejected_candidates_from_an_earlier_turn_do_not_exempt(self):
        # The adapter passes only THIS turn's texts: the incident's list was
        # one turn back and the owner had rejected it.
        self.assertIsNotNone(og.what_solo_pick(
            INCIDENT_2, [INCIDENT_2], OWNER_REACTION))

    def test_empty_inputs(self):
        self.assertIsNone(og.what_solo_pick("", None, None))
        self.assertIsNone(og.what_solo_pick(None, None, None))


def user_entry(text):
    return {"type": "user", "message": {"content": [{"type": "text",
                                                      "text": text}]}}


def assistant_text(text, mid="m1"):
    return {"type": "assistant",
            "message": {"id": mid, "content": [{"type": "text",
                                                "text": text}]}}




if __name__ == "__main__":
    unittest.main()
