#!/usr/bin/env python3
# test-lane: fast    # subprocess CLI + ups against a temp HOME / store, ~15 s
"""v2.7 T1 Why net -- grill round 3 regression (every in-scope BREAK, as a
fixture).

Source: the grill-3 verdict BLOCK of 2026-10-08 (findings 1, 3-11; attack
inputs A2-A9, T1, B2 from the grill's attacks3*.py).  Finding 2 (a forged
turn spliced into the transcript chain + a recomputed ledger) is out of
scope pending an owner decision and has no fixture here.  Same black-box
harness as test-why-net-grill.py; WHY_NET_ROUTINES=<routines dir> replays
the attacks against another tree (on dsp/v27-t1-fix2 every attack test here
fails; the `control` tests pass on both).
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import unicodedata
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("why_grill", HERE / "test-why-net-grill.py")
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)
MAIN, SPEED, COST, HOW, ESCAPE, DAY = g.MAIN, g.SPEED, g.COST, g.HOW, g.ESCAPE, g.DAY


def text_sha(text):
    """bridge/sdk_bridge.bubble_text_sha: NFC, whitespace removed."""
    return hashlib.sha256("".join(unicodedata.normalize("NFC", text).split())
                          .encode("utf-8")).hexdigest()


class R3(g.Grill):
    def audit(self):
        rc, out = self.cli("derive", "--audit")
        self.assertEqual(rc, 0, out)
        return out

    def node(self, wid):
        rc, out = self.cli("derive")
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.store, "derived", "nodes.json"), encoding="utf-8") as fh:
            return json.load(fh)[wid]

    def confirm(self, wid, quote):
        return self.cli("confirm", "--why", wid, "--quote", quote, "--session", MAIN)


class Finding1Revise(R3):
    SAID = "속도가 먼저야."            # "speed comes first."
    AGENT = "품질은 포기해도 된다"  # "quality may be dropped"

    def test_A2_revise_with_another_asks_probe_refuses(self):
        self.turn(self.SAID)
        w = self.propose(self.SAID)
        rc, out = self.confirm(w, self.SAID)
        self.assertEqual(rc, 0, out)
        self.open_fork_ask()
        self.turn("음 잘 모르겠네")   # free answer to ANOTHER ask
        pid = self.probes()[-1]["id"]
        rc, out = self.cli("revise", "--why", w, "--text", self.AGENT, "--probe", pid)
        self.assertEqual(rc, 3, out)
        self.assertIn("probe-not-about-why", out)
        n = self.node(w)
        # AMEND-1 1.3: a quote confirm lands provisional
        self.assertEqual((n["status"], n["text"]), ("confirmed-provisional", self.SAID))

    def test_A2_revise_of_a_confirmed_node_resets_it(self):
        a, b, rc, out = self.open_fork_ask()
        self.assertEqual(rc, 0, out)
        self.turn(SPEED, source="callback")                   # tap confirms a
        self.assertTrue(self.confirmed(a))
        pid = self.probes()[-1]["id"]                         # the probe of THIS ask
        rc, out = self.cli("revise", "--why", a, "--text", self.AGENT, "--probe", pid)
        self.assertEqual(rc, 0, out)
        n = self.node(a)
        self.assertEqual((n["status"], n["text"], n["worded_by"]),
                         ("hypothesis", self.AGENT, "agent"))
        self.assertIsNone(n["confirm"])
        self.assertIn("WHY_AUDIT_OK", self.audit())


class Finding3SeedActor(R3):
    def test_A3_hand_appended_seed_confirm_is_unbacked(self):
        self.turn("아무 말")
        w = self.propose("고객 수가 유일한 목표다")
        n = len(self.rows("events.jsonl"))
        self.hand_append("events.jsonl", {
            "ev": "confirm", "id": "evt-%06d" % (n + 1), "why": w, "at": g.iso(time.time()),
            "actor": "seed", "data": {
                "quote": "고객 수가 유일한 목표다",
                "worded_by": "owner", "probe": None, "said_at": g.iso(time.time()),
                "source": {"kind": "ledger-seed", "dec": "dec-999"}}})
        out = self.audit()
        self.assertNotIn("WHY_AUDIT_OK", out)
        self.assertIn("WHY_AUDIT_UNBACKED", out)
        self.assertIn("seed-unbacked", out)

    def test_control_ledger_seed_under_another_actor_is_unbacked(self):
        self.turn("아무 말")
        w = self.propose("x")
        n = len(self.rows("events.jsonl"))
        self.hand_append("events.jsonl", {
            "ev": "confirm", "id": "evt-%06d" % (n + 1), "why": w, "at": g.iso(time.time()),
            "actor": "derive", "data": {"quote": "x", "worded_by": "owner", "probe": None,
                                        "source": {"kind": "ledger-seed"}}})
        self.assertNotIn("WHY_AUDIT_OK", self.audit())


class Finding4TapLabelQuote(R3):
    LABEL = "속도 우선으로 진행"   # a push.sh button label

    def test_T1_tap_label_quoted_as_owner_words_refuses(self):
        self.turn(self.LABEL, source="callback", message_id=900,
                  keyboard=[self.LABEL, "나중에"], option_index=1,
                  callback_data="opt:1")
        w = self.propose(self.LABEL)
        rc, out = self.confirm(w, self.LABEL)
        self.assertEqual(rc, 3, out)
        self.assertIn("quote-tap", out)
        self.assertFalse(self.confirmed(w))

    def test_control_same_words_typed_confirm(self):
        self.turn(self.LABEL)
        w = self.propose(self.LABEL)
        rc, out = self.confirm(w, self.LABEL)
        self.assertEqual(rc, 0, out)


class Finding8WholeSentence(R3):
    def refuses(self, said, quote):
        self.turn(said)
        w = self.propose(quote)
        rc, out = self.confirm(w, quote)
        self.assertEqual(rc, 3, out)
        self.assertIn("quote-not-clause", out)
        self.assertFalse(self.confirmed(w))

    def test_A5_reported_speech_after_colon(self):
        # "the client says: speed first. I don't agree."
        self.refuses("고객 말로는: 속도가 먼저. "
                     "난 동의 안 해.", "속도가 먼저.")

    def test_A5b_negation_cut_at_unicode_ellipsis(self):
        self.refuses("속도가 먼저… 는 아니야",
                     "속도가 먼저")

    def test_negation_cut_at_three_dots(self):
        self.refuses("속도가 먼저... 는 아니야",
                     "속도가 먼저.")

    def test_tilde_is_not_a_boundary(self):
        self.refuses("속도가 먼저~ 라고 하더라",
                     "속도가 먼저")

    def test_control_whole_sentences_confirm(self):
        said = "난 동의 안 해. 속도가 먼저야!\n비용은 나중"
        self.turn(said)
        for q in ("속도가 먼저야!", "비용은 나중"):
            w = self.propose(q)
            rc, out = self.confirm(w, q)
            self.assertEqual(rc, 0, out)


class Finding7BridgeWrapper(R3):
    PHOTO = ("사용자가 사진을 보냈습니다. "
             "아래 경로의 이미지 파일을 Read "
             "도구로 열어서 내용을 확인하고 "
             "응답하세요.")
    DOC = ("사용자가 파일을 보냈습니다. "
           "아래 경로의 파일을 Read 도구로 "
           "열어서 내용을 확인하고 응답하세요.")
    CAP = "사용자 캡션: "
    SENT = "사용자가 사진을 보냈습니다."

    def refuses(self, said, quote):
        self.turn(said)
        w = self.propose(quote)
        rc, out = self.confirm(w, quote)
        self.assertEqual(rc, 3, out)
        self.assertIn("quote-wrapper", out)
        self.assertFalse(self.confirmed(w))

    def test_A4_photo_wrapper_sentence_refuses(self):
        self.refuses(self.PHOTO + "\n이미지 경로: /tmp/x.jpg\n"
                     + self.CAP + "이거 봐줘", self.SENT)

    def test_document_wrapper_sentence_refuses(self):
        self.refuses(self.DOC + "\n파일 경로: /tmp/a.pdf",
                     "사용자가 파일을 보냈습니다.")

    def test_english_wrapper_refuses(self):
        self.refuses("The user sent a photo. Open the image file at the path below with "
                     "the Read tool, review it, and respond.\nImage path: /tmp/x.jpg",
                     "The user sent a photo.")

    def test_control_caption_span_confirms(self):
        cap = "속도가 먼저야."
        self.turn(self.PHOTO + "\n이미지 경로: /tmp/x.jpg\n" + self.CAP + cap)
        w = self.propose(cap)
        rc, out = self.confirm(w, cap)
        self.assertEqual(rc, 0, out)


class Finding6PastedAgentLine(R3):
    LINE = "목적은 매출 3배 확대입니다"  # "the goal is 3x revenue"
    # no ':' lead-in, so the sentence rule passes and the echo check decides
    # (the ':' form refuses as an introduced line: Finding8IntroducedLine)
    SAID = "아래 문장 틀렸어\n%s\n이거 아니야"
    SAID_COLON = "아래 문장 틀렸어:\n%s\n이거 아니야"

    def pasted_refuses(self):
        self.turn(self.SAID % self.LINE)
        w = self.propose(self.LINE)
        rc, out = self.confirm(w, self.LINE)
        self.assertEqual(rc, 3, out)
        self.assertIn("quote-echo", out)
        self.assertFalse(self.confirmed(w))

    def test_A6_line_from_a_push_bubble(self):
        os.makedirs(self.bot, exist_ok=True)
        with open(os.path.join(self.bot, "outbound-ledger.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": g.iso(time.time() - 3600), "chat_id": "4242",
                                 "body": "<b>목적은</b> 매출 3배 "
                                         "확대입니다", "audience": "owner"},
                                ensure_ascii=False) + "\n")
        self.pasted_refuses()

    def test_A6_line_from_an_earlier_session(self):
        self.entry({"type": "assistant", "uuid": "old-1", "parentUuid": None,
                    "sessionId": "sess-yesterday", "timestamp": g.iso(time.time() - DAY),
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "정리하면, " + self.LINE}]}},
                   "sess-yesterday")
        self.pasted_refuses()

    def test_control_agent_saying_it_later_is_not_an_echo(self):
        self.turn(self.SAID % self.LINE)
        with open(os.path.join(self.bot, "outbound-ledger.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": g.iso(time.time() + 5), "chat_id": "4242",
                                 "body": self.LINE}, ensure_ascii=False) + "\n")
        w = self.propose(self.LINE)
        rc, out = self.confirm(w, self.LINE)
        self.assertEqual(rc, 0, out)


class Finding9RedefinedButton(R3):
    def ask_reply(self, tail):
        a, b = self.propose(SPEED), self.propose(COST)
        self.turn(HOW)
        rc, out = self.historical_cli("ask", "--trigger", "fork", "--topic", "DGN-7", "--shape",
                           "contrast", "--cand", a, "--cand", b)
        self.assertEqual(rc, 0, out)
        self.reply(tail(out.rstrip("\n")))
        self.turn(SPEED, source="callback")
        return a

    def test_A8_line_after_the_block_redefines_the_button(self):
        a = self.ask_reply(lambda blk: blk + "\n\n참고: '%s'를 누르면 "
                           "버그 수정을 이번 주로 미룬다는 "
                           "뜻입니다." % SPEED)
        self.assertFalse(self.confirmed(a))

    def test_label_named_before_the_block(self):
        a = self.ask_reply(lambda blk: "'%s'는 버그 수정을 미룬다는 "
                           "뜻이에요.\n\n%s" % (SPEED, blk))
        self.assertFalse(self.confirmed(a))

    def test_control_preamble_without_labels_confirms(self):
        a = self.ask_reply(lambda blk: "하나만 확인할게요.\n\n" + blk)
        self.assertTrue(self.confirmed(a))


class Finding10DeclinedWording(R3):
    def declined(self, a, b, topic="DGN-10"):
        """The owner declined an ask on `topic` about a, b two days ago."""
        t = time.time() - 2 * DAY
        shown = [{"slot": 1, "why": a, "label": SPEED}, {"slot": 2, "why": b, "label": COST},
                 {"slot": 3, "why": None, "label": ESCAPE, "escape": True}]
        self.hand_append("asks.jsonl", {"ask": "ask-0001", "ev": "create", "at": g.iso(t),
            "kind": "why", "trigger": "fork", "shape": "contrast", "topic": topic,
            "work_ref": topic, "channel": "inline", "session_id": MAIN,
            "bubble_message_id": None, "shown": shown, "refuse_reason": None})
        self.hand_append("asks.jsonl", {"ask": "ask-0001", "ev": "shown", "at": g.iso(t)})
        self.hand_append("asks.jsonl", {"ask": "ask-0001", "ev": "declined", "at": g.iso(t + 60)})

    def ask(self, topic, *cands):
        args = ["ask", "--trigger", "fork", "--topic", topic, "--shape", "contrast"]
        for c in cands:
            args += ["--cand", c]
        return self.cli(*args)

    def test_B2_same_whys_reproposed_under_fresh_ids(self):
        # (a) v4 section 2: no optional producer, regardless of budget/topic.
        self.declined_topic_rows("DGN-10")
        a, b = self.propose(SPEED), self.propose(COST)
        self.turn(HOW)
        a, b = self.propose(SPEED), self.propose(COST)
        for topic in ("DGN-11", "DGN-12"):
            self.assert_retired("ask", "--trigger", "fork", "--topic", topic, "--shape", "contrast", "--cand", a, "--cand", b)
        self.assert_retired("enqueue", "--kind", "why", "--trigger", "fork", "--topic", "DGN-12", "--cand", a, "--cand", b)

    def test_B2_same_wording_as_literal_cands(self):
        # (a) v4 section 2: no optional producer, regardless of budget/topic.
        self.declined_topic_rows("DGN-10")
        a, b = self.propose(SPEED), self.propose(COST)
        self.turn(HOW)
        for topic in ("DGN-11", "DGN-12"):
            self.assert_retired("ask", "--trigger", "fork", "--topic", topic, "--shape", "contrast", "--cand", a, "--cand", b)
        self.assert_retired("enqueue", "--kind", "why", "--trigger", "fork", "--topic", "DGN-12", "--cand", a, "--cand", b)

    def test_B2_enqueue_door_too(self):
        a, b = self.propose(SPEED), self.propose(COST)
        self.declined(a, b)
        a2, b2 = self.propose(SPEED), self.propose(COST)
        rc, out = self.cli("enqueue", "--kind", "why", "--trigger", "new-work",
                           "--topic", "DGN-13", "--cand", a2, "--cand", b2)
        self.assertEqual(rc, 3, out)
        self.assertIn("retired", out)

    def test_control_other_wording_still_asks(self):
        # (a) v4 section 2: no optional producer, regardless of budget/topic.
        a, b = self.propose(SPEED), self.propose(COST)
        self.turn(HOW)
        for topic in ("DGN-11", "DGN-12"):
            self.assert_retired("ask", "--trigger", "fork", "--topic", topic, "--shape", "contrast", "--cand", a, "--cand", b)
        self.assert_retired("enqueue", "--kind", "why", "--trigger", "fork", "--topic", "DGN-12", "--cand", a, "--cand", b)


class Finding11aOwnerBytes(R3):
    def test_A9_nfd_owner_bytes_are_stored(self):
        said = unicodedata.normalize("NFD", "오늘 정리. 속도가 먼저야.")
        self.turn(said)
        q = "속도가 먼저야."                      # the agent's NFC
        w = self.propose(q)
        rc, out = self.confirm(w, q)
        self.assertEqual(rc, 0, out)
        [ev] = [e for e in self.rows("events.jsonl") if e.get("ev") == "confirm"]
        self.assertEqual(ev["data"]["quote"].encode("utf-8"),
                         unicodedata.normalize("NFD", q).encode("utf-8"))
        self.assertIn("WHY_AUDIT_OK", self.audit())


class Finding11dGuardDataDir(R3):
    def deny(self, path):
        p = subprocess.run([sys.executable, str(g.GUARD)], input=json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}),
            capture_output=True, text=True, timeout=30)
        return p.stdout

    def test_why_net_store_under_any_data_dir_name(self):
        for d in (os.path.join(self.bot, "state", "why-net", "events.jsonl"),
                  "/srv/agent-data/state/why-net/asks.jsonl",
                  "/x/.telegram_bot/state/why-net/probes.jsonl"):
            self.assertIn("Why net store", self.deny(d), d)

    def test_control_other_state_dirs_pass(self):
        self.assertEqual(self.deny("/srv/agent-data/state/other/x.json"), "")
        self.assertEqual(self.deny("/x/ws/docs/why-net.md"), "")


class Finding11bMorningChatMissing(R3):
    def morning(self, chat):
        a, b = self.propose(SPEED), self.propose(COST)
        t = time.time() - 600
        shown = [{"slot": 1, "why": a, "label": SPEED}, {"slot": 2, "why": b, "label": COST}]
        self.hand_append("asks.jsonl", {"ask": "ask-0001", "ev": "create", "at": g.iso(t),
            "kind": "why", "trigger": "ripe", "shape": "contrast", "topic": "DGN-1",
            "work_ref": None, "channel": "morning", "session_id": None,
            "bubble_message_id": 500, "shown": shown, "refuse_reason": None})
        self.hand_append("asks.jsonl", {"ask": "ask-0001", "ev": "shown", "at": g.iso(t),
                                        "bubble_message_id": 500, "bubble_chat_id": chat})
        return a, b

    def test_N14_fallback_bubble_without_chat_id_binds_nothing(self):
        a, b = self.morning(None)                  # push.sh returned no chat_id
        self.turn("2번", source="callback", message_id=500, chat_id=-100999)
        self.assertFalse(self.confirmed(b))
        self.turn("2번", source="callback", message_id=500)
        self.assertFalse(self.confirmed(b))


class Finding5OldBubbleSameLabels(R3):
    def two_bubbles(self):
        self.turn("테스트 순서 정해줘")
        self.reply("뭘 먼저 테스트할까요?\n[[OPTIONS: %s | %s | %s]]"
                   % (SPEED, COST, ESCAPE))                 # a non-Why question
        old = self.last_bubble
        a, b, rc, out = self.open_fork_ask()                # the Why ask, same labels
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(self.last_bubble, old)
        return a, old

    def test_A7_tap_on_the_older_bubble_binds_nothing(self):
        a, old = self.two_bubbles()
        self.turn(SPEED, source="callback", message_id=old)
        self.assertFalse(self.confirmed(a))
        self.assertEqual(self.probes(), [])

    def test_no_bubble_record_binds_nothing(self):
        a, old = self.two_bubbles()
        os.remove(os.path.join(self.bot, "options-bubbles.json"))  # pre-fix bridge
        self.turn(SPEED, source="callback")
        self.assertFalse(self.confirmed(a))

    def test_bubble_in_another_chat_binds_nothing(self):
        a, old = self.two_bubbles()
        self.turn(SPEED, source="callback", chat_id=-100999)
        self.assertFalse(self.confirmed(a))

    def test_control_tap_on_the_asks_own_bubble_confirms(self):
        a, old = self.two_bubbles()
        self.turn(SPEED, source="callback")
        self.assertTrue(self.confirmed(a))


class Finding11cEditedMorningBubble(R3):
    TEXT = "이 작업, 어떤 이유가 더 가까워요?"

    def bubble(self):
        a, b = self.propose(SPEED), self.propose(COST)
        t = time.time() - 600
        shown = [{"slot": 1, "why": a, "label": SPEED}, {"slot": 2, "why": b, "label": COST}]
        self.hand_append("asks.jsonl", {"ask": "ask-0001", "ev": "create", "at": g.iso(t),
            "kind": "why", "trigger": "ripe", "shape": "contrast", "topic": "DGN-1",
            "work_ref": None, "channel": "morning", "session_id": None,
            "bubble_message_id": 600, "shown": shown, "refuse_reason": None})
        self.hand_append("asks.jsonl", {"ask": "ask-0001", "ev": "shown", "at": g.iso(t),
            "bubble_message_id": 600, "bubble_chat_id": 4242,
            "bubble_text_sha": text_sha(self.TEXT)})
        return a, b

    def test_push_edit_rewords_the_bubble_under_its_keyboard(self):
        a, b = self.bubble()
        edited = "버그 수정을 미룰까요?"   # push.sh --edit
        self.turn("2번", source="callback", message_id=600,
                  bubble_text_sha=text_sha(edited))
        self.assertFalse(self.confirmed(b))

    def test_bubble_without_a_text_sha_binds_nothing(self):
        a, b = self.bubble()
        self.turn("2번", source="callback", message_id=600, bubble_text_sha=None)
        self.assertFalse(self.confirmed(b))

    def test_control_unedited_bubble_confirms(self):
        a, b = self.bubble()
        self.turn("2번", source="callback", message_id=600,
                  bubble_text_sha=text_sha(" " + self.TEXT + "\n"))  # spacing differs only
        self.assertTrue(self.confirmed(b))


class Finding8IntroducedLine(R3):
    def test_A5_reported_speech_on_the_next_line(self):
        said = "고객 말로는:\n속도가 먼저.\n난 동의 안 해."
        self.turn(said)
        w = self.propose("속도가 먼저.")
        rc, out = self.confirm(w, "속도가 먼저.")
        self.assertEqual(rc, 3, out)
        self.assertIn("quote-not-clause", out)

    def test_A6_as_sent_line_under_this_is_wrong(self):
        line = Finding6PastedAgentLine.LINE
        self.turn(Finding6PastedAgentLine.SAID_COLON % line)   # no push / session copy at all
        w = self.propose(line)
        rc, out = self.confirm(w, line)
        self.assertEqual(rc, 3, out)
        self.assertFalse(self.confirmed(w))

    def test_control_line_after_a_plain_line_confirms(self):
        self.turn("오늘 정리해보면\n속도가 먼저야")
        w = self.propose("속도가 먼저야")
        rc, out = self.confirm(w, "속도가 먼저야")
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
