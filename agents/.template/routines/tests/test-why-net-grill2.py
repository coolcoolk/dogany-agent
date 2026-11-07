#!/usr/bin/env python3
# test-lane: fast    # subprocess CLI + ups against a temp HOME / store, ~15 s
"""v2.7 T1 Why net -- grill round 2 regression (every BREAK, as a fixture).

Source: the grill-2 verdict BLOCK of 2026-10-08 (findings 1-9, attack
inputs N1-N14 from the grill's own attacks.py).  Same black-box harness as
test-why-net-grill.py (public surface only: `why-net.py` verbs, the `ups`
hook stdin, the bridge inbound-context, a Claude Code transcript).
WHY_NET_ROUTINES=<routines dir> replays the attacks against another tree;
on dsp/v27-t1-fix1-b every test here fails.

The attacker is modelled at full strength: it computes the bridge's
text_sha itself, chains forged transcript lines to the newest entry, and
writes ledger rows with a valid `prev`.  Agent-writable files alone must
never be enough: the live confirm refuses where it can, and the nightly
`derive --audit` flags every forged row it cannot refuse live.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("why_grill", HERE / "test-why-net-grill.py")
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)
MAIN, SPEED, COST, HOW, ESCAPE = g.MAIN, g.SPEED, g.COST, g.HOW, g.ESCAPE


class R2(g.Grill):
    def forged_ups(self, prompt, rid="agent-forged-1", payload_path=None, **inb):
        """The agent, from its own Bash tool mid-turn: a fresh inbound-context
        (with the right text_sha -- anyone can hash) piped into `why-net.py
        ups`, in the same env as its other CLI calls."""
        ctx = dict({"schema": 1, "request_id": rid, "source": "message", "chat_id": 4242,
                    "message_id": None, "text_sha": g.sha(prompt), "ts": time.time()}, **inb)
        with open(os.path.join(self.bot, "inbound-context.json"), "w") as fh:
            json.dump(ctx, fh)
        payload = {"prompt": prompt, "session_id": MAIN, "cwd": self.ws,
                   "transcript_path": payload_path or self.transcript(MAIN),
                   "hook_event_name": "UserPromptSubmit"}
        subprocess.run([sys.executable, str(g.CLI), "ups"], input=json.dumps(payload),
                       capture_output=True, text=True, env=self.env, cwd=self.ws, timeout=60)
        self.tool_call()

    def audit(self):
        rc, out = self.cli("derive", "--audit")
        self.assertEqual(rc, 0, out)
        return out

    def assertAuditFlags(self, *needles):
        out = self.audit()
        self.assertNotIn("WHY_AUDIT_OK", out)
        for n in needles:
            self.assertIn(n, out)
        return out


class Finding1AgentRunsUps(R2):
    LINE = "ship faster than quality"

    def test_N1_forged_ups_plus_forged_line_refuses_and_is_flagged(self):
        self.turn("오늘 계획 정리해줘")  # real owner turn
        w = self.propose(self.LINE)
        self.forged_ups(self.LINE)
        self.user_line(self.LINE)            # Bash `>>` onto the real transcript
        rc, out = self.cli("confirm", "--why", w, "--quote", self.LINE, "--session", MAIN)
        self.assertEqual(rc, 3, out)
        self.assertIn("turn-off-chain", out)
        self.assertFalse(self.confirmed(w))
        self.assertAuditFlags("WHY_AUDIT_UNBACKED_TURN", "reason=no-real-turn")

    def test_N1_forge_and_confirm_in_one_bash_call_is_flagged(self):
        # Live, the forged line is still the newest entry (the residual the
        # resolver documents); the audit sees the dead branch.
        self.turn("오늘 계획 정리해줘")
        w = self.propose(self.LINE)
        self.forged_ups(self.LINE)
        self.user_line(self.LINE, logged=False)
        self.cli("confirm", "--why", w, "--quote", self.LINE, "--session", MAIN)
        out = self.assertAuditFlags("WHY_AUDIT_UNBACKED_TURN")
        if self.confirmed(w):
            self.assertIn("WHY_AUDIT_UNBACKED evt=", out)

    def test_control_real_turn_confirms_and_audits_ok(self):
        said = "속도가 먼저야."
        self.turn(said)
        w = self.propose(said)
        rc, out = self.cli("confirm", "--why", w, "--quote", said, "--session", MAIN)
        self.assertEqual(rc, 0, out)
        self.assertIn("WHY_AUDIT_OK", self.audit())


class Finding2ForgedTap(R2):
    def test_N2_forged_tap_is_flagged_by_the_audit(self):
        a, b, rc, out = self.open_fork_ask()
        self.assertEqual(rc, 0, out)
        self.forged_ups(SPEED, source="callback", message_id=self.last_bubble, keyboard=[SPEED, COST, ESCAPE],
                        option_index=1, callback_data="opt:1")
        self.assertAuditFlags("WHY_AUDIT_UNBACKED_TURN")
        if self.confirmed(a):
            self.assertAuditFlags("WHY_AUDIT_UNBACKED evt=")

    def test_N3_config_dir_redirect_refuses(self):
        self.turn("오늘 계획")
        line = "growth over everything"
        w = self.propose(line)
        self.forged_ups(line, rid="agent-forged-3")
        fake = os.path.join(self.tmp, "fakecfg")
        proj = os.path.join(fake, "projects", g.enc(self.ws))
        self.user_line(line, MAIN, proj)
        env = dict(self.env, CLAUDE_CONFIG_DIR=fake)
        rc, out = self.cli("confirm", "--why", w, "--quote", line, "--session", MAIN, env=env)
        self.assertEqual(rc, 3, out)
        self.assertIn("transcript-mismatch", out)
        self.assertAuditFlags("WHY_AUDIT_UNBACKED_TURN")


class Finding3BashLedgerRow(R2):
    def test_N4_bash_appended_ledger_row_refuses_and_is_flagged(self):
        self.turn("오늘 계획")
        line = "retention is the only goal"
        w = self.propose(line)
        self.hand_append("turn-ledger.jsonl", {
            "id": "otl-000099", "at": g.iso(time.time()), "session_id": MAIN,
            "sha": g.sha(line), "len": len(line), "request_id": "x", "source": "message",
            "message_id": None, "transcript_path": self.transcript()})
        self.user_line(line)
        rc, out = self.cli("confirm", "--why", w, "--quote", line, "--session", MAIN)
        self.assertEqual(rc, 3, out)
        self.assertFalse(self.confirmed(w))
        self.assertAuditFlags("row=otl-000099")


class Finding7CronClaimsRequestId(R2):
    CRON = "Nightly sweep: goal is zero open tickets"

    def cron_ups(self, prompt, session="sess-cron"):
        """A `claude -p` cron session in this project, inside the 300 s
        freshness window of the owner's inbound-context."""
        payload = {"prompt": prompt, "session_id": session, "cwd": self.ws,
                   "transcript_path": self.transcript(session)}
        subprocess.run([sys.executable, str(g.CLI), "ups"], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60, cwd=self.ws,
                       env=dict(self.env, CLAUDE_CODE_SESSION_ID=session))

    def owned_rows(self, session):
        return [r for r in self.rows("turn-ledger.jsonl")
                if r.get("session_id") == session and not r.get("skip")]

    def test_N10_skipped_owner_turn_leaves_nothing_claimable(self):
        self.turn("[DGN-1234] \uc774\uac70 \uc624\ub298 \ub05d\ub0b4\uc918")
        self.assertFalse(self.owned_rows(MAIN))
        self.cron_ups(self.CRON)
        self.assertFalse(self.owned_rows("sess-cron"))
        self.user_line(self.CRON, "sess-cron")
        w = self.propose("goal is zero open tickets")
        rc, out = self.cli("confirm", "--why", w, "--quote", "goal is zero open tickets",
                           "--session", "sess-cron",
                           env=dict(self.env, CLAUDE_CODE_SESSION_ID="sess-cron"))
        self.assertEqual(rc, 3, out)

    def test_request_id_is_single_use_even_for_the_same_text(self):
        # A forwarded owner turn is skipped but uses its request_id: a cron
        # session submitting the very same text cannot claim it.
        text = "\ubaa9\uc801\uc740 \ub9e4\ucd9c 3\ubc30 \ud655\ub300"
        self.turn(text, forwarded=True)
        self.cron_ups(text)
        self.assertFalse(self.owned_rows("sess-cron"))
        [row] = self.rows("turn-ledger.jsonl")
        self.assertEqual(row["skip"], "forwarded")


class Finding4Voice(R2):
    HALL = "\uc2dc\uccad\ud574 \uc8fc\uc154\uc11c \uac10\uc0ac\ud569\ub2c8\ub2e4"  # whisper hallucination

    def test_N7_voice_turn_never_confirms_by_quote(self):
        self.turn(self.HALL, voice=True)
        w = self.propose(self.HALL)          # a hypothesis may still be seeded
        rc, out = self.cli("confirm", "--why", w, "--quote", self.HALL, "--session", MAIN)
        self.assertEqual(rc, 3, out)
        self.assertIn("quote-voice", out)
        self.assertFalse(self.confirmed(w))
        self.assertEqual(self.rows("turn-ledger.jsonl")[-1]["source"], "voice")

    def test_voice_label_answer_records_a_probe_but_no_confirm(self):
        a, b, rc, out = self.open_fork_ask()
        self.assertEqual(rc, 0, out)
        self.turn(SPEED, voice=True)
        [p] = self.probes()
        self.assertEqual((p["verdict"], p["source"]["inbound_source"]), ("pick", "voice"))
        self.assertFalse(self.confirmed(a))

    def test_control_typed_words_confirm(self):
        self.turn(self.HALL)
        w = self.propose(self.HALL)
        rc, out = self.cli("confirm", "--why", w, "--quote", self.HALL, "--session", MAIN)
        self.assertEqual(rc, 0, out)


class Finding5PastedAgentLine(R2):
    BUBBLE = "\ubaa9\uc801\uc740 \ub9e4\ucd9c 3\ubc30 \ud655\ub300\uc785\ub2c8\ub2e4"

    def test_N5_owner_pastes_agent_line_to_reject_it(self):
        self.reply(self.BUBBLE)
        # no ':' lead-in (grill-3 fix 8 refuses a ':'-introduced line first)
        self.turn("\uc544\ub798 \ubb38\uc7a5 \ud2c0\ub838\uc5b4\n" + self.BUBBLE
                  + "\n\uc774\uac70 \uc544\ub2c8\uc57c")
        w = self.propose(self.BUBBLE)
        rc, out = self.cli("confirm", "--why", w, "--quote", self.BUBBLE, "--session", MAIN)
        self.assertEqual(rc, 3, out)
        self.assertIn("quote-echo", out)
        self.assertFalse(self.confirmed(w))

    def test_quote_markup_is_refused(self):
        line = "\uc18d\ub3c4\uac00 \uba3c\uc800\uc57c"
        for said in ("> " + line + "\n\uc774\uac74 \uc544\ub2cc \ub4ef",
                     "\uadf8\ub798\uc11c \"" + line + "\" \ub77c\uace0? \ub09c \ubaa8\ub974\uaca0\uc5b4",
                     "\u300c" + line + "\u300d \uc774\uac74 \ud2c0\ub838\uc5b4"):
            with self.subTest(said=said[:12]):
                self.tearDown()
                self.setUp()
                self.turn(said)
                w = self.propose(line)
                rc, out = self.cli("confirm", "--why", w, "--quote", line, "--session", MAIN)
                self.assertEqual(rc, 3, out)
                self.assertIn("quote-markup", out)

    def test_control_agent_restating_after_the_owner_is_fine(self):
        said = "\ube44\uc6a9\ubcf4\ub2e4 \uc18d\ub3c4\uac00 \uc911\uc694\ud574."
        self.turn(said)
        self.reply("\ub124, " + said + " \ub85c \uc774\ud574\ud588\uc5b4\uc694")
        w = self.propose(said)
        rc, out = self.cli("confirm", "--why", w, "--quote", said, "--session", MAIN)
        self.assertEqual(rc, 0, out)


class Finding8Question(R2):
    def test_N6_question_quoted_as_why(self):
        q = "\uc18d\ub3c4\uac00 \uba3c\uc800\uc57c?"
        self.turn(q + " \uc544\ub2c8\uba74 \ube44\uc6a9\uc774 \uba3c\uc800\uc57c?")
        for quote in (q, q[:-1]):           # with and without its "?"
            w = self.propose(quote)
            rc, out = self.cli("confirm", "--why", w, "--quote", quote, "--session", MAIN)
            self.assertEqual(rc, 3, out)
            self.assertIn("quote-question", out)

    def test_interrogative_ending_without_mark(self):
        q = "\uc18d\ub3c4\ubd80\ud130 \ud560\uae4c"
        self.turn(q)
        w = self.propose(q)
        rc, out = self.cli("confirm", "--why", w, "--quote", q, "--session", MAIN)
        self.assertEqual(rc, 3, out)
        self.assertIn("quote-question", out)

    def test_control_statement_confirms(self):
        q = "\uc18d\ub3c4\uac00 \uba3c\uc800\uc57c."
        self.turn("\ube44\uc6a9\uc740? \ub098\uc911\uc5d0. " + q)
        w = self.propose(q)
        rc, out = self.cli("confirm", "--why", w, "--quote", q, "--session", MAIN)
        self.assertEqual(rc, 0, out)


class Finding9TopicAlias(R2):
    def test_N11_topic_switch_after_decline(self):
        # (a) v4 section 2: no optional producer, regardless of budget/topic.
        self.declined_topic_rows("DGN-10")
        a, b = self.propose(SPEED), self.propose(COST)
        self.turn(HOW)
        for topic in ("DGN-0010", "dgn-10", "DGN-11", a):
            self.assert_retired("ask", "--trigger", "fork", "--topic", topic, "--shape", "contrast", "--cand", a, "--cand", b)
        self.assert_retired("enqueue", "--kind", "why", "--trigger", "fork", "--topic", "DGN-12", "--cand", a, "--cand", b)

    def test_decline_on_a_why_covers_its_work_key(self):
        # (a) v4 section 2: no optional producer, regardless of budget/topic.
        self.declined_topic_rows("DGN-10")
        a, b = self.propose(SPEED), self.propose(COST)
        self.turn(HOW)
        for topic in ("DGN-11", "DGN-12"):
            self.assert_retired("ask", "--trigger", "fork", "--topic", topic, "--shape", "contrast", "--cand", a, "--cand", b)
        self.assert_retired("enqueue", "--kind", "why", "--trigger", "fork", "--topic", "DGN-12", "--cand", a, "--cand", b)

    def test_control_unrelated_topic_still_asks(self):
        # (a) v4 section 2: no optional producer, regardless of budget/topic.
        a, b = self.propose(SPEED), self.propose(COST)
        self.turn(HOW)
        for topic in ("DGN-0010", "dgn-10", "DGN-11", a):
            self.assert_retired("ask", "--trigger", "fork", "--topic", topic, "--shape", "contrast", "--cand", a, "--cand", b)
        self.assert_retired("enqueue", "--kind", "why", "--trigger", "fork", "--topic", "DGN-12", "--cand", a, "--cand", b)


class Finding6TapBinding(R2):
    def printed_ask(self):
        a, b = self.propose(SPEED), self.propose(COST)
        self.turn(HOW)
        rc, out = self.historical_cli("ask", "--trigger", "fork", "--topic", "DGN-1", "--shape",
                           "contrast", "--cand", a, "--cand", b)
        self.assertEqual(rc, 0, out)
        return a, b, out.strip()

    def test_N8_same_labels_under_another_question(self):
        a, b, out = self.printed_ask()
        opts = re.search(r"\[\[OPTIONS:.*?\]\]", out, re.S).group(0)
        self.reply("\ubc84\uadf8 \uace0\uce60 \ub54c \ubb50\ubd80\ud130 \ubcfc\uae4c\uc694?\n" + opts)
        self.turn(SPEED, source="callback")
        self.assertFalse(self.confirmed(a))
        self.assertEqual(self.probes(), [])

    def test_N9_second_list_reuses_a_label(self):
        a, b, out = self.printed_ask()
        self.reply(out + "\n\ubc30\ud3ec\ub294?\n[[OPTIONS: " + SPEED + " | \ub0b4\uc77c]]")
        self.turn(SPEED, source="callback",
                  keyboard=[SPEED, "\ub0b4\uc77c"], option_index=1)
        self.assertFalse(self.confirmed(a))

    def test_tap_from_another_keyboard_with_the_same_label(self):
        # the ask was really sent, but the tap came off a different keyboard
        a, b, out = self.printed_ask()
        self.reply(out)
        self.turn(SPEED, source="callback",
                  keyboard=[SPEED, "\ub0b4\uc77c"], option_index=1)
        self.assertFalse(self.confirmed(a))
        [p] = self.probes()
        self.assertEqual(p["verdict"], "free")

    def test_control_tap_on_the_sent_ask_confirms(self):
        a, b, out = self.printed_ask()
        self.reply(out)
        self.turn(SPEED, source="callback")
        self.assertTrue(self.confirmed(a))
        self.assertIn("WHY_AUDIT_OK", self.audit())


class Finding10MorningChat(R2):
    def test_N14_morning_bubble_id_from_another_chat(self):
        a, b = self.propose(SPEED), self.propose(COST)
        t = time.time() - 600
        shown = [{"slot": 1, "why": a, "label": SPEED}, {"slot": 2, "why": b, "label": COST}]
        self.hand_append("asks.jsonl", {"ask": "ask-0001", "ev": "create", "at": g.iso(t),
            "kind": "why", "trigger": "ripe", "shape": "contrast", "topic": "DGN-1",
            "work_ref": None, "channel": "morning", "session_id": None,
            "bubble_message_id": 500, "shown": shown, "refuse_reason": None})
        self.hand_append("asks.jsonl", {"ask": "ask-0001", "ev": "shown", "at": g.iso(t),
                                        "bubble_message_id": 500, "bubble_chat_id": 4242,
                                        "bubble_text_sha": "b500"})
        self.turn("2\ubc88", source="callback", message_id=500, chat_id=-100999)
        self.assertFalse(self.confirmed(b))
        self.turn("2\ubc88", source="callback", message_id=500)     # control: same chat
        self.assertTrue(self.confirmed(b))


if __name__ == "__main__":
    unittest.main(verbosity=2)
