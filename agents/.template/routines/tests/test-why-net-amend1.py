#!/usr/bin/env python3
# test-lane: fast
# test-tier: 1
# test-component: why-net words and actions
# promise: original ask stems preserve provisional, contest and settle behavior
"""v2.7 T1 Why net AMEND-1 -- words are provisional, decisions/actions
confirm (dec-335, dec-336).  Fixture group GS (Golden spiral) GS1-GS48.

Spec: docs/design/v27-t1-why-net-AMEND-1.md (sections 1, 2, 4, 5.2, 7).
The shared library's own fixtures (CM1-CM12) are
tests/test-confirm-mechanics.py; the LOCKED fixtures whose results changed
(5.1) are edited in place in the floor / elicit / p3 / rest / grill3 suites.

Live-shaped turns on the elicitation harness (test-why-net-elicit.Base):
every owner turn publishes a fresh bridge inbound-context and runs `ups`
with the session's transcript; the agent's replies are appended to the
same transcript, so the turn ledger, the resolver and the act floors see a
real main chain.  Synthetic data only.

Run: python3 routines/tests/test-why-net-amend1.py
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EL = _load("_amend1_elicit", os.path.join(HERE, "test-why-net-elicit.py"))
REST = _load("_amend1_rest", os.path.join(HERE, "test-why-net-rest.py"))

from legacy_why_fixtures import historical_queue

from why_net import acts, ask, derive, events, morning, store, surfaces, ups  # noqa: E402
from why_net import nodes as N  # noqa: E402
import why_owner_turn  # noqa: E402

T0, SID, CHAT = EL.T0, EL.SID, EL.CHAT
DAY = 86400
ESC = "\uc9c0\uae08\uc740 \ud328\uc2a4"        # why.ask.escape (ko)
YES = "\uadf8\uac70\uc57c"                      # why.ask.confirm_yes (ko)
REC = "(\ucd94\ucc9c)"                          # bridge option_rec_marker (ko)
SPEED = "\ubc30\ud3ec \uc18d\ub3c4"             # "release speed"
STABLE = "\uc6b4\uc601 \uc548\uc815\uc131"      # "operational stability"
CORR = "\uc544\ub2c8 \uadf8\uac8c \uc544\ub2c8\ub77c \ub85c\uc9c1\uc744 \uace0\uccd0\uc57c \ud55c\ub2e4\ub294 \uac70\uc57c"
MERGE = "\uba38\uc9c0\ud574"                    # "merge it"
LEDGER = "worklog/_DECISIONS.md"


class Base(EL.Base):
    def historical_question(self, trigger, w, other=None, shape="split"):
        # Derivation no longer solicits. Seed a pre-retirement row only to
        # keep historical owner-answer / audit behavior under independent test.
        self.assertEqual(self.queued(trigger), [])
        aid = historical_queue(self.ctx, "why", trigger, w,
                               [w, other] if other else [w], shape=shape)[0]
        return self.asks()[aid]

    def setUp(self):
        super().setUp()
        self.t = T0
        self.write_conf("DECISION_LEDGER_PATH=%s\n" % LEDGER)
        os.makedirs(os.path.join(self.root, "worklog"), exist_ok=True)
        self.decs = []

    # -- clock / turns ------------------------------------------------------
    def tick(self, dt=30):
        self.t += dt
        self.at(self.t)

    def turn(self, text="\uc9c4\ud589\ud574\uc918 \uc774\uac70 \uba3c\uc800", **kw):
        self.tick()
        self.ups(text, **kw)
        return self.last_otl()

    def last_otl(self):
        rows = [r for r in store.read_rows(store.TURN_LEDGER) if not r.get("skip")]
        return rows[-1]["id"] if rows else None

    def last_req(self):
        return store.read_rows(store.TURN_LEDGER)[-1]["request_id"]

    # -- nodes ----------------------------------------------------------------
    def why(self, text, work=None):
        return events.propose(text, "framework", work=work)["why"]

    def provisional(self, w, other, topic="DGN-1001"):
        """An inline fork ask; the owner taps `w` (a Why ask pick = words)."""
        self.tick(31 * 60)  # past the inline gap of any earlier ask
        self.turn()
        self.tick(5)
        rc, out = self.historical_inline([w, other], topic=topic)
        self.assertEqual(rc, 0, out)
        self.tick()
        self.ups(self.node(w)["text"], source="callback")
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)
        return self.node(w)["anchor"]

    def settle_direct(self, w):
        n = self.node(w)
        with store.locked():
            events.settle(w, ["act-0001", "act-0002"], n["anchor_evt"])

    # -- reveal menus (A1) ------------------------------------------------------
    def reveal(self, opts, pick, topic, rec=None, voice=False, typed=False):
        """The agent's own [[OPTIONS]] menu with a reveal map; the owner
        picks `pick`.  opts = [(label, why | None)].  rec = the label the
        reply recommends (numbered body run with the rec marker)."""
        self.turn("\ub2e4\uc74c \ub2e8\uacc4 \uc5b4\ub5bb\uac8c \ud560\uae4c")
        self.tick(5)
        args = ["ask", "--reveal", "--topic", topic]
        for label, w in opts:
            args += ["--opt", "%s=%s" % (label, w or "")]
        rc, out = self.historical_cli(*args)
        self.assertEqual((rc, out), (0, ""))
        labels = [l for l, _ in opts]
        body = ["\uc5b4\ub290 \ucabd\uc73c\ub85c \uac08\uae4c\uc694?"]
        if rec is not None:
            body += ["%d. %s%s" % (i, l, " " + REC if l == rec else "")
                     for i, l in enumerate(labels, 1)]
        self.reply("\n".join(body + ["[[OPTIONS: %s]]" % " | ".join(labels)]))
        self.tick()
        if voice:
            self.ups(pick, source="message", voice=True)
        elif typed:
            self.ups(pick, source="message")
        else:
            self.ups(pick, source="callback")
        return self.last_otl()

    def pair(self, w, other, pick_w, topic, rec=None):
        """Reveal menu {w, other} + escape; pick w (win for w) or other."""
        a, b = self.node(w)["text"] + "!", self.node(other)["text"] + "!"
        return self.reveal([(a, w), (b, other), (ESC, None)],
                           a if pick_w else b, topic,
                           rec=(a if rec == "w" else b if rec == "other" else rec))

    # -- store reads ------------------------------------------------------------
    def acts(self, wid=None):
        rows = acts.act_rows()
        return [r for r in rows if wid is None or r["why"] == wid]

    def evs(self, kind=None):
        return [e for e in store.read_rows(store.EVENTS) if kind is None or e["ev"] == kind]

    def derive(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            derive.run(self.ctx)
        self.assertEqual(buf.getvalue(), "")  # N8: derive prints nothing
        return buf

    def queued(self, trigger):
        return [a for a in self.asks().values()
                if a.get("trigger") == trigger and a.get("state") == "queued"]

    # -- decision ledger (A2) ----------------------------------------------------
    def ledger_entry(self, n, labels, req=None, state="confirmed"):
        self.decs.append((n, labels, req, state))
        lines = ["# Decisions", ""]
        for num, labs, rq, st in self.decs:
            lines += ["## dec-%d -- pick (2026-10-08) [confirmed 2026-10-08 10:00: \"1\"]" % num,
                      "<!-- decision-ledger id=dec-%d state=%s%s -->" % (
                          num, st, " req=%s" % rq if rq else ""),
                      "- Options: %s" % " / ".join("%d. %s" % (i, l)
                                                   for i, l in enumerate(labs, 1)),
                      "- Recommended: ", ""]
        with open(os.path.join(self.root, LEDGER), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return "dec-%d" % n

    def menu_answer(self, work, labels=("\uce74\ub098\ub9ac", "\uc804\uccb4"), n=5):
        """A decision menu naming `work`, answered by a typed label; the
        DGN-1952 hook confirmed it with req= (the owner turn's request id)."""
        self.turn()
        self.tick(5)
        self.reply("%s \ubc30\ud3ec \ubc29\uc2dd?\n[[OPTIONS: %s]]" % (work, " | ".join(labels)))
        self.tick()
        self.ups(labels[0], source="message")
        return self.ledger_entry(n, list(labels), req=self.last_req())

    def act_cli(self, *args):
        return self.cli("act", *args)

    # -- tickets (A4 / A5 / A6) --------------------------------------------------
    def ticket(self, tid, status="wip", **fm):
        p = os.path.join(self.root, "worklog", "%s-t.md" % tid)
        lines = ["---", "id: %s" % tid, "status: %s" % status,
                 "display_title.ko: \ud300 \ucd08\ub300 \uae30\ub2a5"]
        lines += ["%s: %s" % (k, v) for k, v in fm.items()]
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines + ["---", "merge_sha: abcdef1234", ""]))
        return p

    def machine(self, kind, path, session=SID, env=None):
        return acts.record_machine(self.ctx, kind, path, session, env=env)


# ---------------------------------------------------------------------------
# words -> provisional (1.3)
# ---------------------------------------------------------------------------

class Words(Base):
    def test_GS1_careless_pick_is_provisional_only(self):
        ws = [self.why("\uc774\uc720 %d" % i) for i in range(5)]
        self.turn()
        self.tick(5)
        rc, out = self.historical_inline(ws, trigger="new-work", shape="set", topic="DGN-1")
        self.assertEqual(rc, 0, out)
        self.tick()
        self.ups(self.node(ws[3])["text"], source="callback")
        self.derive()
        n = self.node(ws[3])
        self.assertEqual((n["status"], n["settle"]), (N.PROVISIONAL, None))
        self.assertEqual(self.evs("settle"), [])

    def test_GS2_scatter_quote_is_provisional_with_otl(self):
        self.turn()
        self.tick(5)
        rc, out = self.historical_inline([], trigger="new-work", shape="scatter", topic="DGN-2")
        self.assertEqual(rc, 0, out)
        said = "\uadf8\ub0e5 \ub0b4\uac00 \ud3b8\ud558\ub824\uace0"
        otl = self.turn(said)
        w = self.why(said)
        rc, out = self.cli("confirm", "--why", w, "--session", SID, "--quote", said)
        self.assertEqual(rc, 0, out)
        n = self.node(w)
        self.assertEqual((n["status"], n["worded_by"], n["anchor"]),
                         (N.PROVISIONAL, "owner", otl))
        self.assertEqual(self.evs("confirm")[-1]["data"]["otl"], otl)

    def test_GS24_revise_on_provisional_resets(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        pid = self.probes()[-1]["id"]
        rc, out = self.cli("revise", "--why", w, "--text", "\ubc30\ud3ec\ub97c \ube68\ub9ac", "--probe", pid)
        self.assertEqual(rc, 0, out)
        n = self.node(w)
        self.assertEqual((n["status"], n["confirm"], n["anchor"]), ("hypothesis", None, None))

    def test_GS20_no_words_ask_on_provisional(self):
        # (a) v4 sections 1 D2-D4 and 2: no independent ask/reveal policy.
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        self.turn()
        self.assert_retired("ask", "--trigger", "fork", "--topic", "DGN-20", "--shape", "contrast", "--cand", w, "--cand", o)
        self.assert_retired("ask", "--reveal", "--topic", "DGN-20", "--opt", "a=" + w, "--opt", "b=" + o)
        self.assert_retired("enqueue", "--kind", "why", "--trigger", "act-ripe", "--topic", w, "--cand", w)
        self.assertEqual(ask.enqueue(self.ctx, "why", "fork", "DGN-21", [w, o]), (None, "retired"))

    def test_GS21_tcorr_skips_provisional_readings(self):
        from why_net import corrections
        w = self.why(SPEED, work="DGN-21")
        h = self.why(STABLE, work="DGN-21")
        other = self.why("\ube44\uc6a9")
        self.provisional(w, other)
        self.assertEqual(corrections.open_readings("DGN-21", events.nodes(), self.root), [h])
        self.turn("DGN-21 " + CORR)
        self.assertEqual([a for a in self.asks().values() if a.get("trigger") == "correction"], [])


# ---------------------------------------------------------------------------
# A1 reveal acts, settle (1.2, 2.2)
# ---------------------------------------------------------------------------

class Settle(Base):
    def setup_two(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        return w, o

    def test_GS3_two_choice_wins_settle_silently(self):
        w, o = self.setup_two()
        otl = self.pair(w, o, True, "DGN-31", rec="other")
        dec = self.ledger_entry(9, ["x"], req=self.last_req())
        self.derive()
        [a] = [r for r in self.acts(w)]
        self.assertEqual((a["kind"], a["class"], a["link"], a["direction"], a["dec"]),
                         ("decision", "choice", "agent-prior", "consistent", dec))
        self.assertEqual(a["owner_turn"]["otl"], otl)
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)  # one win is not enough
        self.pair(w, o, True, "DGN-32")
        self.derive()
        n = self.node(w)
        self.assertEqual(n["status"], "confirmed")
        [s] = self.evs("settle")
        self.assertEqual((s["actor"], s["data"]["words"], s["data"]["policy"]),
                         ("derive", n["anchor_evt"], "why/2"))
        self.assertEqual(sorted(s["data"]["acts"]),
                         sorted(r["act"] for r in self.acts(w) if r["direction"] == "consistent"))
        self.assertEqual(self.queued("act-ripe") + self.queued("act-contest"), [])

    def test_GS4_same_turn_as_the_words_does_not_count(self):
        # Live, one owner turn answers one ask, so words and a reveal pick
        # cannot share a turn; the floor is pinned with a Why-ask probe whose
        # verbatim IS the reveal pick turn (confirm_tap binds it to that otl).
        w, o = self.why(STABLE), self.why(SPEED)
        a = STABLE + "!"
        otl = self.reveal([(a, w), (SPEED + "!", o)], a, "DGN-41")
        prb = events.write_probe({
            "ask": "ask-0999", "trigger": "fork", "verdict": "pick", "slots": [1],
            "verbatim": a, "shown": [{"slot": 1, "why": w, "label": a}],
            "source": {"session_id": SID, "inbound_source": "callback"}})["id"]
        events.confirm_tap(w, prb)
        self.assertEqual(self.node(w)["anchor"], otl)
        self.pair(w, o, True, "DGN-42")
        self.derive()
        mine = self.acts(w)
        self.assertEqual(len(mine), 2)
        self.assertEqual(mine[0]["owner_turn"]["otl"], otl)  # recorded ...
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)  # ... not counted (I2)

    def test_GS22_derive_twice_one_act_per_key_one_settle(self):
        w, o = self.setup_two()
        self.pair(w, o, True, "DGN-221")
        self.pair(w, o, True, "DGN-222")
        self.derive()
        self.derive()
        keys = [r["dedupe"] for r in self.acts()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(self.acts(w)), 2)
        self.assertEqual(len(self.evs("settle")), 1)

    def test_GS26_settle_and_acts_say_nothing(self):
        w, o = self.setup_two()
        self.pair(w, o, True, "DGN-261")
        before = len(store.read_rows(store.ASKS))
        self.pair(w, o, True, "DGN-262")
        during = len(store.read_rows(store.ASKS))
        self.derive()   # asserts derive printed nothing
        self.assertEqual(self.node(w)["status"], "confirmed")
        new = [r for r in store.read_rows(store.ASKS)[during:]]
        self.assertEqual([r for r in new if r.get("ev") == "create"], [])
        self.assertGreater(during, before)  # only the reveal menu's own rows
        b = surfaces.board_counts(self.root, None, self.ctx.bot_dir)
        self.assertEqual(b["asks"], 0)

    def test_GS31_reveal_pick_is_an_act_not_words(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.pair(w, o, True, "DGN-311")
        self.derive()
        with open(os.path.join(self.ddir, "derived", "nodes.json")) as fh:
            n = json.load(fh)[w]
        self.assertEqual((n["support"], n["words_support"]), (0.0, 0.0))
        [a] = self.acts(w)
        self.assertEqual(a["class"], "choice")
        self.pair(w, o, True, "DGN-312", rec="w")
        self.derive()
        self.assertEqual(self.acts(w)[-1]["class"], "delegated")

    def test_GS36_recommended_slot_is_delegated(self):
        w, o = self.setup_two()
        h, h2 = self.why("\ube44\uc6a9 \uc808\uac10"), self.why("\ud654\uba74 \uba3c\uc800")
        for topic in ("DGN-361", "DGN-362"):
            self.pair(w, o, True, topic, rec="w")
            a, b = self.node(h)["text"] + "!", self.node(h2)["text"] + "!"
            self.reveal([(a, h), (b, h2)], a, topic, rec=a)
        self.derive()
        self.assertEqual({r["class"] for r in self.acts(w)}, {"delegated"})
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)   # no settle
        self.assertEqual(self.queued("act-ripe"), [])  # facts cannot solicit

    def test_GS43_unreadable_reply_is_delegated(self):
        self.assertIsNone(ups.recommended_label(None))
        self.assertIsNone(ups.recommended_label("no menu here"))
        self.assertEqual(ups.recommended_label("[[OPTIONS: a | b]]"), "")
        self.assertEqual(ups.recommended_label("1. a\n2. b %s\n[[OPTIONS: a | b]]" % REC), "b")
        w, o = self.setup_two()
        saved = ups._DL[:]

        class Broken(object):
            def parse_menu(self, text):
                raise RuntimeError("unreadable")
        ups._DL[:] = [Broken()]
        try:
            self.pair(w, o, True, "DGN-43")
        finally:
            ups._DL[:] = saved
        self.assertIsNone(self.probes()[-1]["recommended"])
        self.derive()
        self.assertEqual(self.acts(w)[0]["class"], "delegated")

    def test_GS44_same_topic_twice_does_not_settle(self):
        w, o = self.setup_two()
        self.pair(w, o, True, "DGN-44")
        self.pair(w, o, True, "DGN-44")
        self.derive()
        self.assertEqual(len([r for r in self.acts(w) if r["class"] == "choice"]), 2)
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)

    def test_GS25_voice_reveal_pick_is_no_act(self):
        w, o = self.why(STABLE), self.why(SPEED)
        a, b = STABLE + "!", SPEED + "!"
        self.reveal([(a, w), (b, o)], a, "DGN-25", voice=True)
        self.assertEqual(self.probes()[-1]["source"]["inbound_source"], "voice")
        self.derive()
        self.assertEqual(self.acts(), [])


# ---------------------------------------------------------------------------
# rule D' (F1, F2)
# ---------------------------------------------------------------------------

class RuleD(Base):
    def test_GS6_node_on_every_slot_is_unknown(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        for topic in ("DGN-61", "DGN-62"):
            self.reveal([("a", w), ("b", w), (ESC, None)], "a", topic)
        self.derive()
        self.assertEqual({r["class"] for r in self.acts(w)}, {"unknown"})
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)
        self.assertEqual(self.queued("act-ripe"), [])

    def test_GS35_one_sided_bet_is_unknown(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        h = self.why("\ube44\uc6a9")
        self.reveal([("a", w), ("b", None)], "a", "DGN-351")
        self.reveal([("a", w), ("b", None)], "b", "DGN-352")
        self.reveal([("a", h), ("b", None)], "a", "DGN-353")
        self.reveal([("a", h), ("b", None)], "a", "DGN-354")
        self.derive()
        self.assertEqual([(r["class"], r["direction"]) for r in self.acts(w)],
                         [("unknown", "consistent"), ("unknown", "unknown")])
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)
        self.assertEqual(self.evs("contest"), [])
        self.assertEqual(self.queued("act-ripe"), [])  # h: unknown rows never ripen

    def test_GS40_reveal_validation_and_audit(self):
        w = self.why(STABLE)
        r = self.why("\uc811\uc744 \uc774\uc720")
        events.end("retire", r)
        self.turn()
        for opt, why in (("a=why-9999", "missing"), ("a=" + r, "terminal"),
                         ("\ubc14\ub85c \ud574\uc918=" + w, "action label")):
            rc, out = self.historical_cli("ask", "--reveal", "--topic", "DGN-40", "--opt", opt,
                               "--opt", "b=" + w)
            self.assertEqual(rc, 4, why)
            self.assertIn("reason=reveal-invalid", out)
        self.assertEqual([a for a in self.asks().values() if a.get("trigger") == "reveal"], [])
        store.append_row(store.ASKS, {"ask": "ask-0900", "ev": "create", "trigger": "reveal",
                                      "at": store.iso_at(self.t),
                                      "shown": [{"slot": 1, "why": "why-9999", "label": "z"}]})
        rc, out = self.cli("derive", "--audit", "--no-model")
        self.assertEqual(rc, 0)
        self.assertIn("reason=reveal-unknown-why", out)


# ---------------------------------------------------------------------------
# contest, split, mixed (2.1, 2.3)
# ---------------------------------------------------------------------------

class Contest(Base):
    def losses(self, w, o, n, start=0):
        for i in range(n):
            self.pair(w, o, False, "DGN-%d" % (500 + start + i))

    def split_ask(self, w, other):
        q = self.historical_question("act-contest", w, other)
        self.assertEqual((q["channel"], q["shape"], q["topic"]), ("morning", "split", w))
        return q

    def answer_bubble(self, q, label, mid):
        self.show_morning(q["ask"], mid, at=self.t)
        self.tick()
        self.ups(label, source="callback", message_id=mid)

    def test_GS5_one_loss_counts_two_losses_contest(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        self.losses(w, o, 1)
        self.derive()
        [a] = self.acts(w)
        self.assertEqual((a["direction"], a["class"]), ("contradicts", "choice"))
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)
        self.assertEqual(self.queued("act-contest"), [])
        self.losses(w, o, 1, start=1)
        self.derive()
        self.assertEqual(self.node(w)["status"], "contested")
        [c] = self.evs("contest")
        self.assertEqual(sorted(c["data"]["acts"]),
                         sorted(r["act"] for r in self.acts(w) if r["direction"] == "contradicts"))
        self.assertNotIn("interp", c["data"])
        q = self.split_ask(w, o)
        # rev 3: "both" first, then said, acted, escape
        self.assertEqual([s.get("why") for s in q["shown"]], [None, w, o, None])
        self.assertEqual(q["shown"][0]["both"], [w, o])
        text = ask.render(self.ctx, "split", q["shown"], events.nodes(), "act-contest")
        self.assertIn(SPEED, text.split("\n")[0])
        self.assertIn(STABLE, text.split("\n")[0])
        self.derive()
        self.assertEqual(len(self.evs("contest")), 1)   # one open contest ask per node
        self.assertEqual(len(self.queued("act-contest")), 1)

    def test_GS18_split_acted_side_settles_in_the_same_pass(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        self.losses(w, o, 2)
        self.derive()
        q = self.split_ask(w, o)
        self.answer_bubble(q, STABLE, 7001)
        [r] = self.evs("resolve")
        self.assertEqual((r["data"]["side"], r["data"]["other"], r["actor"]),
                         ("acted", o, "ups-hook"))
        n_o, n_w = self.node(o), self.node(w)
        self.assertEqual(n_o["status"], "confirmed")    # carried wins settle at once
        [s] = self.evs("settle")
        self.assertEqual((s["why"], s["data"]["words"]), (o, r["id"]))
        self.assertEqual((n_w["status"], n_w["anchor"]), (N.PROVISIONAL, r["data"]["otl"]))
        rc, out = self.cli("derive", "--audit", "--no-model")
        self.assertNotIn("settle-unbacked", out)

    def test_GS19_split_said_side_needs_new_wins(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        self.losses(w, o, 2)
        self.derive()
        self.answer_bubble(self.split_ask(w, o), SPEED, 7002)
        [r] = self.evs("resolve")
        self.assertEqual(r["data"]["side"], "said")
        n = self.node(w)
        self.assertEqual((n["status"], n["anchor"]), (N.PROVISIONAL, r["data"]["otl"]))
        self.assertEqual(self.node(o)["status"], "hypothesis")
        self.derive()
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)  # old losses do not count
        self.pair(w, o, True, "DGN-191")
        self.pair(w, o, True, "DGN-192")
        self.derive()
        self.assertEqual(self.node(w)["status"], "confirmed")

    def test_GS33_mixed_after_two_resolved_contests(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        for k in range(2):
            self.losses(w, o, 2, start=10 * k)
            self.derive()
            self.answer_bubble(self.split_ask(w, o), SPEED, 7100 + k)
        self.losses(w, o, 2, start=30)
        self.derive()
        self.assertEqual(len(self.evs("contest")), 2)          # no third contest
        self.assertEqual(self.queued("act-contest"), [])        # no third split ask
        f = self.historical_question("fork-mixed", w, o, shape="contrast")
        # rev 3 (dec-334 5f): [both] [A] [B] [skip]
        self.assertEqual((f["topic"], [s.get("why") for s in f["shown"]]), (w, [None, w, o, None]))
        self.assertEqual([s["label"] for s in f["shown"]],
                         ["\ub458 \ub2e4", SPEED, STABLE, ESC])
        text = ask.render(self.ctx, f["shape"], f["shown"], events.nodes(), "fork-mixed")
        self.assertEqual(text.split("\n")[0], ask.t(self.ctx, "why.ask.stem.mixed"))
        self.assertNotIn(ask.t(self.ctx, "why.ask.free_hint"), text)
        evs = store.read_rows(store.EVENTS)
        self.assertIn("mixed", acts.flags(self.ctx, self.node(w), self.acts(), evs))
        # declined -> silent until new words
        self.answer_bubble(f, ESC, 7200)
        self.losses(w, o, 2, start=40)
        self.derive()
        self.assertEqual(self.queued("fork-mixed") + self.queued("act-contest"), [])
        self.assertEqual(len(self.evs("contest")), 2)

    def test_GS45_confirmed_node_contested_by_losses(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        self.pair(w, o, True, "DGN-451")
        self.pair(w, o, True, "DGN-452")
        self.derive()
        self.assertEqual(self.node(w)["status"], "confirmed")
        settle = self.evs("settle")
        self.losses(w, o, 2)
        self.derive()
        self.assertEqual(self.node(w)["status"], "contested")
        self.assertEqual(self.evs("settle"), settle)   # history untouched

    def test_GS47_pivot_keeps_basis_and_acts(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        self.losses(w, o, 2)
        self.derive()
        [c] = self.evs("contest")
        said = "\uc774\uc81c\ub294 \uc548\uc815\uc131\uc774 \uba3c\uc800\uc57c."
        self.turn(said)
        rc, out = self.cli("pivot", "--why", w, "--to", o, "--session", SID, "--quote", said)
        self.assertEqual(rc, 0, out)
        p = self.evs("pivot")[-1]
        self.assertEqual(p["data"]["basis"], {"contest": c["id"], "acts": c["data"]["acts"]})
        self.assertEqual(self.node(w)["status"], "pivoted")
        self.assertEqual(len(self.acts(w)), 2)   # acts are never re-pointed
        from why_net import render
        render.write()
        with open(store.path("why-net.md")) as fh:
            md = fh.read()
        self.assertIn("  - pivot: ", md)
        self.assertIn("basis=%s acts=2" % c["id"], md)


# ---------------------------------------------------------------------------
# agent acts (A2, A3) and their floors
# ---------------------------------------------------------------------------

class AgentActs(Base):
    def prov(self, work="DGN-77"):
        w, o = self.why(STABLE, work=work), self.why(SPEED)
        self.provisional(w, o)
        return w, o

    def correction(self, work="DGN-77"):
        self.turn("%s %s" % (work, CORR))
        rec = [r for r in store.read_rows(store.CORRECTIONS) if r.get("id")][-1]
        self.assertEqual((rec["work"], rec["otl"]), (work, self.last_otl()))
        return rec["id"]

    def test_GS7_one_consistent_correction_is_activity(self):
        w, _ = self.prov()
        cor = self.correction()
        rc, out = self.act_cli("--kind", "correction", "--ref", cor, "--why", w,
                               "--direction", "consistent")
        self.assertEqual(rc, 0, out)
        [a] = self.acts(w)
        self.assertEqual((a["class"], a["link"], a["by"]), ("activity", "agent", "agent"))
        self.derive()
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)

    def test_GS8_agent_acts_alone_never_settle(self):
        w, _ = self.prov()
        cor = self.correction()
        self.assertEqual(self.act_cli("--kind", "correction", "--ref", cor, "--why", w,
                                      "--direction", "consistent")[0], 0)
        dec = self.menu_answer("DGN-77")
        rc, out = self.act_cli("--kind", "decision", "--ref", dec, "--why", w,
                               "--direction", "consistent")
        self.assertEqual(rc, 0, out)
        a = self.acts(w)[-1]
        self.assertEqual((a["class"], a["link"], a["dec"]), ("choice", "agent", dec))
        dec2 = self.menu_answer("DGN-77", labels=("\uc9c0\uae08", "\ub2e4\uc74c"), n=6)
        self.assertEqual(self.act_cli("--kind", "decision", "--ref", dec2, "--why", w,
                                      "--direction", "consistent")[0], 0)
        self.derive()
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)   # I5: no A1
        # one A1 win next to them settles (min_prior 1, two turns, two topics)
        self.pair(w, self.why("\ub2e4\ub978 \uc774\uc720"), True, "DGN-81")
        self.derive()
        self.assertEqual(self.node(w)["status"], "confirmed")

    def test_GS9_correction_on_other_work_refused(self):
        w, _ = self.prov(work="DGN-77")
        cor = self.correction(work="DGN-78")
        before = store.read_rows(store.ACTS)
        rc, out = self.act_cli("--kind", "correction", "--ref", cor, "--why", w,
                               "--direction", "contradicts")
        self.assertEqual(rc, 3)
        self.assertIn("reason=act-unlinked", out)
        self.assertEqual(store.read_rows(store.ACTS), before)

    def test_GS10_missing_ref_gate_or_otl_refused(self):
        w, _ = self.prov()
        for args in (("--kind", "correction", "--ref", "cor-9999"),
                     ("--kind", "decision", "--ref", "dec-999")):
            rc, out = self.act_cli(*(args + ("--why", w, "--direction", "consistent")))
            self.assertEqual(rc, 3, args)
            self.assertIn("reason=act-unlinked", out)
        cor = self.correction()
        os.environ[why_owner_turn.DISPATCH_ENV_MARKER] = "1"
        try:
            rc, out = self.act_cli("--kind", "correction", "--ref", cor, "--why", w,
                                   "--direction", "consistent")
        finally:
            os.environ.pop(why_owner_turn.DISPATCH_ENV_MARKER)
        self.assertEqual(rc, 3)
        self.assertIn("reason=act-unlinked", out)
        # a correction row with no owner turn (hand-appended) never links
        store.append_row(store.CORRECTIONS, {"id": "cor-0500", "said_at": "x",
                                             "text": "t", "session_id": SID,
                                             "work": "DGN-77", "otl": None})
        rc, out = self.act_cli("--kind", "correction", "--ref", "cor-0500", "--why", w,
                               "--direction", "consistent")
        self.assertEqual(rc, 3)
        self.assertEqual(self.acts(), [])

    def test_SD_seed_dec_is_words_never_an_act(self):
        # 5.1 SD1/SD2 + F16: seed -> provisional; `act --ref <its dec>` refuses
        self.turn("DGN-90 \uc2dc\uc791")
        req = self.last_req()
        line = "\ub0b4\uac00 \uc9c1\uc811 \uace0\ub974\ub294 \uac8c \uba3c\uc800\uc57c"
        with open(os.path.join(self.root, LEDGER), "w", encoding="utf-8") as fh:
            fh.write("# Decisions\n\n## dec-1 -- q (2026-10-02) [confirmed 2026-10-02 21:11: "
                     "\"%s\"]\n<!-- decision-ledger id=dec-1 state=confirmed req=%s -->\n"
                     "- Options: 1. a / 2. b\n" % (line, req))
        rc, out = self.cli("seed", "--from", LEDGER)
        self.assertEqual(rc, 0, out)
        n = self.node("why-0001")
        self.assertEqual((n["status"], n["anchor"]), (N.PROVISIONAL, self.last_otl()))
        rc, out = self.act_cli("--kind", "decision", "--ref", "dec-1", "--why", "why-0001",
                               "--direction", "consistent")
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=act-unlinked", out)
        self.assertEqual(self.acts(), [])

    def test_GS15_no_supersedes_flag(self):
        rc, _ = self.act_cli("--kind", "reject", "--ref", "DGN-1", "--why", "why-0001",
                             "--direction", "consistent", "--supersedes", "act-0001")
        self.assertEqual(rc, 2)

    def test_GS32_a2_on_a_reveal_turn_is_same_turn(self):
        w, o = self.prov()
        a, b = STABLE + "!", SPEED + "!"
        self.reveal([(a, w), (b, o), (ESC, None)], a, "DGN-77")
        dec = self.ledger_entry(7, [a, b, ESC], req=self.last_req())
        rc, out = self.act_cli("--kind", "decision", "--ref", dec, "--why", w,
                               "--direction", "consistent")
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=act-same-turn", out)
        self.assertEqual(len(self.acts(w)), 1)
        self.assertEqual(self.acts(w)[0]["by"], "derive")

    def test_GS41_a2_needs_a_machine_confirm(self):
        w, _ = self.prov()
        self.menu_answer("DGN-77")
        dec = self.ledger_entry(8, ["\uce74\ub098\ub9ac", "\uc804\uccb4"], req=None)
        rc, out = self.act_cli("--kind", "decision", "--ref", dec, "--why", w,
                               "--direction", "consistent")
        self.assertEqual(rc, 3)
        self.assertIn("reason=act-unlinked", out)

    def test_GS42_a2_on_a_why_ask_menu_refused(self):
        w, o = self.prov()
        h1, h2 = self.why("\ube44\uc6a9", work="DGN-77"), self.why("\ud654\uba74")
        self.tick(31 * 60)
        self.turn()
        self.tick(5)
        rc, out = self.historical_inline([h1, h2], topic="DGN-77")
        self.assertEqual(rc, 0, out)
        self.tick()
        self.ups(self.node(h1)["text"], source="callback")
        labels = [s["label"] for s in self.asks()[max(self.asks())]["shown"]]
        dec = self.ledger_entry(9, labels, req=self.last_req())
        rc, out = self.act_cli("--kind", "decision", "--ref", dec, "--why", w,
                               "--direction", "consistent")
        self.assertEqual(rc, 3)
        self.assertIn("reason=act-unlinked", out)

    def test_GS38_agent_linked_losses_never_contest(self):
        w, _ = self.prov()
        for work in ("DGN-77", "DGN-77"):
            self.tick(120)
            cor = self.correction(work) if not self.acts() else None
            if cor is None:
                self.turn("DGN-77 \uadf8\uac8c \uc544\ub2c8\ub77c \uc774\uac70 \ub2e4\uc2dc")
                recs = [r for r in store.read_rows(store.CORRECTIONS) if r.get("id")]
                cor = recs[-1]["id"]
            rc, out = self.act_cli("--kind", "correction", "--ref", cor, "--why", w,
                                   "--direction", "contradicts")
            self.assertEqual(rc, 0, out)
            self.derive()
            self.assertEqual(self.node(w)["status"], N.PROVISIONAL)
        self.assertEqual([a["class"] for a in self.acts(w)], ["choice", "choice"])
        self.assertEqual(self.evs("contest"), [])

    def test_GS48_note_cap_and_quote_refusal(self):
        w, _ = self.prov()
        cor = self.correction()
        rc, _ = self.act_cli("--kind", "correction", "--ref", cor, "--why", w,
                             "--direction", "consistent", "--note", "x" * 121)
        self.assertEqual(rc, 2)
        for note in ('owner said "fix it"', "note " + CORR[:14]):
            rc, out = self.act_cli("--kind", "correction", "--ref", cor, "--why", w,
                                   "--direction", "consistent", "--note", note)
            self.assertEqual(rc, 3, note)
            self.assertIn("reason=note-quote", out)
        self.assertEqual(self.acts(), [])
        rc, out = self.act_cli("--kind", "correction", "--ref", cor, "--why", w,
                               "--direction", "consistent", "--note", "same work item")
        self.assertEqual(rc, 0, out)


# ---------------------------------------------------------------------------
# machine acts from the ticket lint (A4, A5, A6)
# ---------------------------------------------------------------------------

class MachineActs(Base):
    def test_GS11_ticket_created_in_typed_turn_is_a6(self):
        w = self.why(STABLE)
        p = self.ticket("DGN-111", why=w)
        self.turn("DGN-111 \ud2f0\ucf13 \ub9cc\ub4e4\uc5b4\uc918")
        a = self.machine("initiate", p)
        self.assertEqual((a["kind"], a["class"], a["link"], a["by"], a["ref"]),
                         ("initiate", "activity", "agent", "lint", "DGN-111"))
        self.assertIsNone(self.machine("initiate", p))   # once per ticket

    def test_GS11_lint_hook_emits_the_act(self):
        w = self.why(STABLE)
        self.turn("\ud300 \ucd08\ub300 \uae30\ub2a5 \ud2f0\ucf13 \ub9cc\ub4e4\uc5b4\uc918")
        p = self.ticket("DGN-112", why=w)
        env = dict(os.environ, DOGANY_ROOT=self.root,
                   DOGANY_AGENT_CONF=os.path.join(self.root, "config", "agent.conf"))
        out = subprocess.run([sys.executable, os.path.join(ROUTINES, "ticket-status-enum-lint.py")],
                             input=json.dumps({"tool_name": "Write", "session_id": SID,
                                               "tool_input": {"file_path": p}}),
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("act-", out.stdout)       # silent (I4)
        self.assertEqual([a["kind"] for a in self.acts(w)], ["initiate"])

    def test_GS12_no_act_from_worker_cron_or_callback_turn(self):
        w = self.why(STABLE)
        p = self.ticket("DGN-121", why=w)
        self.turn("DGN-121 \ub9cc\ub4e4\uc5b4")
        self.assertIsNone(self.machine("initiate", p, env={why_owner_turn.DISPATCH_ENV_MARKER: "1"}))
        self.assertIsNone(self.machine("initiate", p, session="cron-session"))
        self.tick()
        self.ups("x", source="callback", keyboard=["x"], option_index=1)
        self.assertEqual(store.read_rows(store.TURN_LEDGER)[-1]["source"], "callback")
        self.assertIsNone(self.machine("initiate", p))
        self.assertEqual(self.acts(), [])

    def test_GS13_done_named_or_go_ahead_is_a4(self):
        w = self.why(STABLE)
        p = self.ticket("DGN-131", status="done", why=w)
        self.turn("\uc624\ub298 \uc810\uc2ec \ubb50 \uba39\uc9c0")        # unrelated
        self.assertIsNone(self.machine("accept", p))
        self.turn("DGN-131 \ub2eb\uc544\uc918")
        a = self.machine("accept", p)
        self.assertEqual((a["class"], a["link"], a["direction"]), ("activity", "machine", "consistent"))
        q = self.ticket("DGN-132", status="done", why=w)
        self.tick()
        self.reply("DGN-132 \uc900\ube44 \ub05d\ub0ac\uc5b4\uc694. \uba38\uc9c0\ud560\uae4c\uc694?")
        self.turn(MERGE)
        self.assertEqual(self.machine("accept", q)["ref"], "DGN-132")
        r = self.ticket("DGN-133", status="done", why=w)
        self.turn("DGN-133 \ub05d")
        self.assertIsNone(self.machine("accept", r, env={why_owner_turn.DISPATCH_ENV_MARKER: "1"}))
        # the lint never hands a done without evidence over (check 2 exits)
        lint = _load("_amend1_lint", os.path.join(ROUTINES, "ticket-status-enum-lint.py"))
        self.assertEqual(lint.act_kinds("wip", "done"), ["accept"])
        self.assertEqual(lint.act_kinds(None, "wip"), ["initiate"])
        self.assertEqual(lint.act_kinds("wip", "dismissed"), ["reject"])
        self.assertEqual(lint.act_kinds("done", "open"), ["reject"])
        self.assertEqual(lint.act_kinds("done", "done"), [])

    def test_GS14_dismissed_is_a5_unknown(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        p = self.ticket("DGN-141", status="dismissed", why=w)
        self.turn("DGN-141 \uc811\uc790")
        a = self.machine("reject", p)
        self.assertEqual((a["class"], a["direction"], a["link"]), ("unknown", "unknown", "machine"))
        self.derive()
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)
        self.assertEqual([x for x in self.asks().values() if x.get("state") == "queued"], [])

    def test_GS37_two_a6_on_provisional_do_not_settle(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        for tid in ("DGN-371", "DGN-372"):
            p = self.ticket(tid, why=w)
            self.turn("%s \ub9cc\ub4e4\uc5b4" % tid)
            self.assertIsNotNone(self.machine("initiate", p))
        self.derive()
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)

    def test_rows_stamp_the_injected_clock(self):
        # DGN-1967 bug 1: events and acts are stamped from ctx.now, the
        # clock acts.flags and the act-ripe window compare them with.
        h = self.why("\ube44\uc6a9 \uc808\uac10")
        self.assertEqual(self.evs("propose")[-1]["at"], store.iso_at(self.t))
        p = self.ticket("DGN-162", status="done", why=h)
        self.turn("DGN-162 \ub9cc\ub4e4\uc5b4\uc918")
        row = self.machine("initiate", p)
        self.assertEqual(row["at"], store.iso_at(self.t))

    def test_GS16_GS17_act_ripe_then_yes(self):
        h = self.why("\ube44\uc6a9 \uc808\uac10")
        p = self.ticket("DGN-161", status="done", why=h)
        self.turn("DGN-161 \ub9cc\ub4e4\uc5b4\uc918")
        self.assertIsNotNone(self.machine("initiate", p))
        self.assertIsNone(self.machine("accept", p))   # one act per (turn, node)
        self.turn("DGN-161 \ub05d\ub0b4\uc918")
        self.assertIsNotNone(self.machine("accept", p))
        # the same rows 31 days later ripen nothing
        saved = self.t
        self.at(self.t + 31 * DAY)
        self.derive()
        self.assertEqual(self.queued("act-ripe"), [])
        self.at(saved)
        self.t = saved
        self.derive()
        q = self.historical_question("act-ripe", h, shape="act-confirm")
        self.assertEqual((q["shape"], q["channel"], q["topic"]), ("act-confirm", "morning", h))
        self.assertEqual(self.node(h)["status"], "hypothesis")
        text = ask.render(self.ctx, q["shape"], q["shown"], events.nodes(), q["trigger"])
        self.assertEqual(text.split("\n")[0], ask.t(self.ctx, "why.ask.stem.act_confirm").replace(
            "{text}", self.node(h)["text"]))
        # GS17: yes -> provisional, anchored at the answer, no settle
        self.show_morning(q["ask"], 8100, at=self.t)
        self.tick()
        self.ups(YES, source="callback", message_id=8100)
        n = self.node(h)
        self.assertEqual((n["status"], n["anchor"]), (N.PROVISIONAL, self.last_otl()))
        self.derive()
        self.assertEqual(self.node(h)["status"], N.PROVISIONAL)


# ---------------------------------------------------------------------------
# lock, audit, void (F12, F14)
# ---------------------------------------------------------------------------

class Integrity(Base):
    def two_wins(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        self.pair(w, o, True, "DGN-341")
        self.pair(w, o, True, "DGN-342")
        return w, o

    def test_GS34_stale_settle_refused_and_pass_refolds(self):
        w, _ = self.two_wins()
        old = self.node(w)["anchor_evt"]
        real = acts.convert_reveals_unlocked

        def convert_then_new_words(ctx, decs=None):
            out = real(ctx, decs)

            def build(rows):     # a confirm `ups` appends meanwhile
                return {"ev": "confirm", "id": store.next_id(rows, "evt", 6), "why": w,
                        "at": store.iso_at(self.t), "actor": "ups-hook",
                        "data": {"otl": "otl-%06d" % 999, "probe": None}}
            store.append_unlocked(store.EVENTS, build)
            return out
        acts.convert_reveals_unlocked = convert_then_new_words
        try:
            res = acts.act_pass(self.ctx)
        finally:
            acts.convert_reveals_unlocked = real
        self.assertEqual(res["settled"], [])
        self.assertEqual(self.evs("settle"), [])
        with store.locked():
            with self.assertRaises(Exception) as c:
                events.settle(w, [a["act"] for a in self.acts(w)], old)
        self.assertEqual(c.exception.reason, "settle-stale")

    def test_GS23_audit_finds_unbacked_settle_and_act(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        with store.locked():
            events.settle(w, ["act-0077", "act-0078"], self.node(w)["anchor_evt"])
        rc, out = self.cli("derive", "--audit", "--no-model")
        self.assertEqual(rc, 0)
        self.assertIn("reason=settle-unbacked", out)
        x, y = self.why("\ube44\uc6a9"), self.why("\ud654\uba74")
        self.provisional(x, y, topic="DGN-231")
        self.pair(x, y, True, "DGN-232")
        self.derive()
        ref = self.acts(x)[0]["ref"]
        rows = store.read_rows(store.PROBES)
        for r in rows:
            if r.get("id") == ref:
                r["verbatim"] = r["verbatim"] + " (edited)"
        with store.locked():
            store.rewrite_chain(store.PROBES, rows)
        rc, out = self.cli("derive", "--audit", "--no-model")
        self.assertIn("reason=act-unbacked", out)
        self.assertEqual(rc, 0)
        self.pair(x, y, True, "DGN-233")
        self.derive()
        self.assertEqual(self.node(x)["status"], N.PROVISIONAL)   # dropped from the count

    def test_GS39_void_drops_a_win(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        self.pair(w, o, True, "DGN-391")
        self.derive()
        [a] = self.acts(w)
        rc, out = self.cli("act-void", "--act", a["act"])
        self.assertEqual(rc, 0, out)
        self.pair(w, o, True, "DGN-392")
        self.derive()
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)
        self.assertEqual(self.evs("settle"), [])
        # owner-request erasure: chain rebuilt, still valid
        rc, out = self.cli("act-void", "--act", a["act"], "--erase")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(a["act"], [r["act"] for r in self.acts()])
        self.assertEqual([b for b in store.audit() if b["file"] == store.ACTS], [])


# ---------------------------------------------------------------------------
# surfaces (2.6, 4, 5.1)
# ---------------------------------------------------------------------------

class Surfaces(Base):
    def test_GS27_head_order(self):
        c, p, h = self.why("root reason"), None, None
        o = self.why(SPEED)
        self.provisional(c, o)
        self.settle_direct(c)
        p = events.propose("prov mid", "framework", parents=[c])["why"]
        self.tick(31 * 60)
        h = events.propose("leaf hyp", "framework", parents=[p])["why"]
        self.provisional(p, o, topic="DGN-271")
        lines = N.head_lines(events.nodes(), [h])
        self.assertEqual(lines, ["- root reason", N.PROV_TAG + " prov mid",
                                 N.HYP_TAG + " leaf hyp"])
        self.assertLessEqual(len(lines), 5)

    def test_GS28_board_counts(self):
        a, b, c = self.why("A"), self.why("B"), self.why("C")
        self.provisional(a, b)
        p1 = self.ticket("DGN-281", why=a)
        p2 = self.ticket("DGN-282", why=c)
        p3 = self.ticket("DGN-283")
        for p in (p1, p2, p3):
            surfaces.lint_ticket(p, self.root, os.path.dirname(p), enqueue=None)
        counts = surfaces.board_counts(self.root, None, self.ctx.bot_dir)
        self.assertEqual((counts["none"], counts["hypothesis"], counts["provisional"],
                          counts["contested"]), (1, 1, 1, 0))
        self.assertEqual(counts["fold"], ["DGN-283"])

    def test_GS29_render_and_contract(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        self.pair(w, o, True, "DGN-291")
        self.derive()
        from why_net import render
        render.write()
        with open(store.path("why-net.md")) as fh:
            md = fh.read()
        self.assertIn("## confirmed-provisional\n", md)
        [a] = self.acts(w)
        self.assertIn("  - act: decision %s choice consistent" % a["ref"], md)
        self.assertNotIn("note", md)
        v = REST.read_why_view(self.ctx.bot_dir)
        self.assertEqual([n["id"] for n in v["by_status"][N.PROVISIONAL]], [w])

    def test_GS30_morning_priority(self):
        x, y = self.why("X"), self.why("Y")
        z, r, q, f = self.why("Z"), self.why("R"), self.why("Q"), self.why("F")
        self.at(T0)
        events.contest(z, None, {"v": 2})   # a contested node for the words contest
        ids = {}
        ids["ripe"] = historical_queue(self.ctx, "why", "ripe", r, [r], shape="confirm")[0]
        ids["act-ripe"] = historical_queue(self.ctx, "why", "act-ripe", q, [q], shape="act-confirm")[0]
        ids["contest"] = historical_queue(self.ctx, "why", "contest", z, [z], shape="confirm")[0]
        ids["fork-mixed"] = historical_queue(self.ctx, "why", "fork-mixed", f, [f, y],
                                        shape="contrast")[0]
        ids["act-contest"] = historical_queue(self.ctx, "why", "act-contest", x, [x, y],
                                         shape="split")[0]
        self.assertTrue(all(ids.values()), ids)
        sent = []

        def sender(ctx, text, buttons):
            sent.append(text)
            return 0, 1000 + len(sent), CHAT
        self.at(T0 - 3 * 3600)   # 07:00 KST
        self.write_conf("DECISION_LEDGER_PATH=%s\nWHY_MORNING_FALLBACK=on\n" % LEDGER)
        self.assertEqual(morning.run(self.ctx, sender=sender, out=io.StringIO()), 0)
        # DGN-2028 AMEND-2 P3.4: nothing is pushed any more; the priority
        # orders what a `gate` pulls inline
        self.assertEqual(sent, [])
        order = [x.get("trigger") for x in derive.morning_order(self.asks())]
        self.assertEqual(order[:3], ["act-contest", "fork-mixed", "contest"])

    def test_GS46_stale_flag_only(self):
        w, o = self.why(STABLE), self.why(SPEED)
        self.provisional(w, o)
        self.at(self.t + 91 * DAY)
        self.derive()
        n = self.node(w)
        self.assertEqual(n["status"], N.PROVISIONAL)
        evs = store.read_rows(store.EVENTS)
        self.assertEqual(acts.flags(self.ctx, n, self.acts(), evs), ["stale"])
        with open(os.path.join(self.ddir, "derived", "nodes.json")) as fh:
            self.assertEqual(json.load(fh)[w]["flags"], ["stale"])
        self.assertEqual([x for x in self.asks().values() if x.get("state") == "queued"], [])
        self.assertNotIn("stale", "\n".join(surfaces.head_lines(self.root, None, [], None)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
