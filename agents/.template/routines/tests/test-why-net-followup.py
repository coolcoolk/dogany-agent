#!/usr/bin/env python3
# test-lane: fast
# test-tier: 1
# test-component: why-net multiple reasons and follow-ups
# promise: original ask stems preserve both-choice and follow-up behavior
"""v2.7 T1 Why net AMEND-1 rev 3 -- the user's 07:42-08:20 decisions.

Spec: docs/design/v27-t1-why-net-AMEND-1.md section 10 (rev 3).
  MW  multiple Whys: "both" is a first-class answer, co-Whys are not rivals,
      a ticket may name several Whys, `mixed` is not a defect.
  TM  timing: `gate` asks inline before irreversible / trial-and-error work;
      the rest goes to the daily retro slot through `retro.section`, with
      the morning push as a flagged fallback (WHY_RETRO_SLOT).
  TR  trial-run rules: no umbrella confirm, one line per signal, tap only
      the wrong ones, follow-up with <= 3 candidates + free text.
  CP  copy: the dec-334 list confirmed (5f rewritten), new strings pending.

Runs on the AMEND-1 harness (test-why-net-amend1.Base): live-shaped owner
turns, real transcript chain.  Synthetic data only.

Run: python3 routines/tests/test-why-net-followup.py
"""

import importlib.util
import io
import json
import os
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AM = _load("_followup_amend1", os.path.join(HERE, "test-why-net-amend1.py"))

from legacy_why_fixtures import historical_queue

from why_net import acts, ask, events, morning, retro, store, surfaces  # noqa: E402
from why_net import nodes as N  # noqa: E402

T0, SID, CHAT = AM.T0, AM.SID, AM.CHAT
ESC, YES, SPEED, STABLE = AM.ESC, AM.YES, AM.SPEED, AM.STABLE
BOTH = "\ub458 \ub2e4"                                          # why.ask.both (ko)
NOT = "\uc544\ub2d8"                                            # why.retro.wrong (ko)
STOP = "\uc624\ub298\uc740 \uc5ec\uae30\uae4c\uc9c0"                 # why.retro.stop (ko)
STEM_5F = ("\uc774 \uc791\uc5c5\uc5d0\ub294 Why\uac00 \ub450 \uac00\uc9c0 "
           "\uc788\ub294 \uac83 \uac19\uc544\uc694. \ub458 \ub2e4 \ub9de\uc73c\uba74 "
           "'\ub458 \ub2e4'\ub97c, \uacbd\uc6b0\uc5d0 \ub530\ub77c \ub2e4\ub974\uba74 "
           "\uc5b4\ub5a4 \uacbd\uc6b0\uc778\uc9c0 \uc801\uc5b4\uc8fc\uc138\uc694.")
RETRO_AT = T0 + 11 * 3600   # 21:00 KST
COST = "\ube44\uc6a9 \uc808\uac10"                             # "cost saving"


class Base(AM.Base):
    def both_tap(self, w, o, topic="DGN-1001"):
        """An inline fork between w and o; the owner taps "both"."""
        self.tick(31 * 60)
        self.turn()
        self.tick(5)
        rc, out = self.historical_inline([w, o], topic=topic)
        self.assertEqual(rc, 0, out)
        self.tick()
        self.ups(BOTH, source="callback")

    def correction(self, cid, text, work):
        store.append_row(store.CORRECTIONS, {"id": cid, "said_at": store.iso_at(self.t),
                                             "text": text, "session_id": SID, "work": work,
                                             "otl": self.last_otl()})

    def gate(self, work, before="irreversible"):
        return self.cli("gate", "--work", work, "--before", before)


# ---------------------------------------------------------------------------
# MW: multiple Whys
# ---------------------------------------------------------------------------

class MultipleWhys(Base):
    def test_MW1_two_why_fork_offers_both_first_with_5f(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.turn()
        self.tick(5)
        rc, out = self.historical_inline([w, o])
        self.assertEqual(rc, 0, out)
        lines = out.strip().split("\n")
        self.assertEqual(lines, [STEM_5F, ask.t(self.ctx, "why.ask.gloss.why"),
            "[[OPTIONS: %s | %s | %s | %s]]" % (BOTH, SPEED, STABLE, ESC)])
        # literal-label candidates get no "both" (it would name nothing)
        rc, out = self.historical_inline(["\uac00", "\ub098"], topic="DGN-1002", send=False)
        self.assertNotIn(BOTH, out)

    def test_MW2_both_tap_makes_both_provisional_and_pairs_them(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.both_tap(w, o)
        nw, no = self.node(w), self.node(o)
        self.assertEqual((nw["status"], no["status"]), (N.PROVISIONAL, N.PROVISIONAL))
        self.assertEqual((nw["co"], no["co"]), ([o], [w]))
        [c] = self.evs("cowhy")
        self.assertEqual((c["actor"], c["why"], c["data"]["other"]), ("ups-hook", w, o))
        self.assertEqual(nw["anchor"], c["data"]["otl"])
        self.assertEqual(events.audit_confirms(), [])
        rc, out = self.cli("derive", "--audit", "--no-model")
        self.assertNotIn("cowhy", out)

    def test_MW3_co_whys_are_not_rivals(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.both_tap(w, o)
        # two later reveal picks of o on {w, o} menus: wins for o, and for w
        # they would be losses -- but the owner said both hold
        self.pair(w, o, False, "DGN-301")
        self.pair(w, o, False, "DGN-302")
        self.derive()
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)   # not contested
        self.assertEqual(self.queued("act-contest"), [])
        self.assertEqual(self.node(o)["status"], "confirmed")      # o settled on its wins

    def test_MW4_split_answered_both(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        for i in range(2):
            self.pair(w, o, False, "DGN-%d" % (510 + i))
        self.derive()
        q = self.historical_question("act-contest", w, o)
        self.assertEqual([s["label"] for s in q["shown"]][0], BOTH)
        self.show_morning(q["ask"], 7301, at=self.t)
        self.tick()
        self.ups(BOTH, source="callback", message_id=7301)
        [r] = self.evs("resolve")
        self.assertEqual(r["data"]["side"], "both")
        self.assertEqual(self.node(w)["co"], [o])
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)
        self.assertEqual(self.node(o)["status"], "confirmed")   # the carried wins settle o
        self.pair(w, o, False, "DGN-520")
        self.pair(w, o, False, "DGN-521")
        self.derive()
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)   # no new contest
        self.assertEqual(len(self.evs("contest")), 1)

    def test_MW5_fork_mixed_both_ends_the_back_and_forth(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        for k in range(2):
            for i in range(2):
                self.pair(w, o, False, "DGN-%d" % (600 + 10 * k + i))
            self.derive()
            q = self.historical_question("act-contest", w, o)
            self.show_morning(q["ask"], 7400 + k, at=self.t)
            self.tick()
            self.ups(SPEED, source="callback", message_id=7400 + k)
        for i in range(2):
            self.pair(w, o, False, "DGN-%d" % (650 + i))
        self.derive()
        f = self.historical_question("fork-mixed", w, o, shape="contrast")
        text = ask.render(self.ctx, f["shape"], f["shown"], events.nodes(), "fork-mixed")
        self.assertEqual(text.split("\n")[0], STEM_5F)
        self.show_morning(f["ask"], 7500, at=self.t)
        self.tick()
        self.ups(BOTH, source="callback", message_id=7500)
        self.assertEqual(self.node(w)["co"], [o])
        evs = store.read_rows(store.EVENTS)
        self.assertNotIn("mixed", acts.flags(self.ctx, self.node(w), self.acts(), evs))
        self.assertEqual(acts.resolved_conflicts(evs, w), 0)

    def test_MW6_ticket_with_several_whys(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        p = self.ticket("DGN-61", why="%s, %s" % (w, o))
        r = surfaces.ticket_why(p, events.nodes())
        self.assertEqual((r["whys"], r["why"], r["state"]), ([w, o], w, "confirmed"))
        os.utime(p)
        # a ticket created in a typed owner turn: one A6 per Why
        self.turn("DGN-61 \ub9cc\ub4e4\uc5b4\uc918")
        self.machine("initiate", p)
        self.assertEqual(sorted(a["why"] for a in self.acts() if a["kind"] == "initiate"),
                         sorted([w, o]))

    def test_MW7_two_hows_of_one_why_are_never_a_loss(self):
        w, o = self.why(SPEED), self.why(STABLE)
        self.provisional(w, o)
        # two Hows serving w, plus o: the pick of either How is no loss for w
        self.reveal([("\uce74\ub098\ub9ac", w), ("\ube14\ub8e8\uadf8\ub9b0", w),
                     ("\uc804\uccb4", o), (ESC, None)], "\ube14\ub8e8\uadf8\ub9b0", "DGN-71")
        self.derive()
        self.assertFalse([a for a in self.acts(w) if a["direction"] == "contradicts"])
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)


# ---------------------------------------------------------------------------
# TM: timing -- inline gate, retro provider, morning fallback
# ---------------------------------------------------------------------------

class Timing(Base):
    def test_TM1_gate_clear_when_the_why_is_settled(self):
        # (a) v4 section 2: legacy gate never asks, moves or enqueues.
        w, o = self.why(SPEED, work="DGN-81"), self.why(STABLE)
        self.provisional(w, o)
        self.settle_direct(w)
        self.turn()
        self.assert_retired("gate", "--work", "DGN-81", "--before", "trial")

    def test_TM2_gate_pulls_the_queued_split_inline(self):
        # (a) v4 section 2: legacy gate never asks, moves or enqueues.
        w, o = self.why(SPEED, work="DGN-82"), self.why(STABLE)
        self.provisional(w, o)
        aid = historical_queue(self.ctx, "why", "ripe", w, [w], shape="confirm")[0]
        self.turn()
        self.assert_retired("gate", "--work", "DGN-82", "--before", "trial")
        self.assertEqual(self.asks()[aid]["state"], "queued")

    def test_TM3_gate_builds_a_fresh_confirm_for_a_hypothesis(self):
        # (a) v4 section 2: legacy gate never asks, moves or enqueues.
        w, o = self.why(SPEED, work="DGN-83"), self.why(STABLE)
        self.turn()
        self.assert_retired("gate", "--work", "DGN-83", "--before", "trial")

    def test_TM4_gate_over_budget_leaves_the_queued_ask(self):
        # (a) v4 section 2: legacy gate never asks, moves or enqueues.
        w, o = self.why(SPEED, work="DGN-84"), self.why(STABLE)
        aid = historical_queue(self.ctx, "why", "ripe", w, [w], shape="confirm")[0]
        self.turn()
        self.assert_retired("gate", "--work", "DGN-84", "--before", "trial")
        self.assertEqual(self.asks()[aid]["state"], "queued")

    def test_TM5_gate_on_provisional_with_nothing_queued_does_not_reask(self):
        # (a) v4 section 2: legacy gate never asks, moves or enqueues.
        w, o = self.why(SPEED, work="DGN-85"), self.why(STABLE)
        self.provisional(w, o)
        self.turn()
        self.assert_retired("gate", "--work", "DGN-85", "--before", "trial")

    def test_TM6_usage(self):
        # (a) v4 section 2: legacy gate never asks, moves or enqueues.
        w, o = self.why(SPEED, work="DGN-86"), self.why(STABLE)
        self.turn()
        self.assert_retired("gate", "--work", "DGN-86", "--before", "trial")
        self.assertEqual(self.cli("gate", "--work", "DGN-87", "--before", "soon")[0], 2)

    def test_TM7_morning_is_an_opt_in_fallback(self):
        w = self.why(COST)
        historical_queue(self.ctx, "why", "ripe", w, [w], shape="confirm")
        sent = []

        def sender(ctx, text, buttons):
            sent.append(text)
            return 0, 1000 + len(sent), CHAT
        self.at(T0 - 3 * 3600)    # 07:00 KST
        buf = io.StringIO()       # final grill B2: the default pushes nothing
        self.assertEqual(morning.run(self.ctx, sender=sender, out=buf), 0)
        self.assertEqual(sent, [])
        self.assertIn("reason=fallback-off", buf.getvalue())
        self.write_conf("DECISION_LEDGER_PATH=%s\nWHY_RETRO_SLOT=on\n"
                        "WHY_MORNING_FALLBACK=on\n" % AM.LEDGER)
        buf = io.StringIO()
        self.assertEqual(morning.run(self.ctx, sender=sender, out=buf), 0)
        self.assertEqual(sent, [])
        self.assertIn("reason=retro-slot", buf.getvalue())
        self.write_conf("DECISION_LEDGER_PATH=%s\nWHY_MORNING_FALLBACK=on\n" % AM.LEDGER)
        buf = io.StringIO()
        morning.run(self.ctx, sender=sender, out=buf)
        # DGN-2028 AMEND-2 P3.4: gated with the view-only retro -- nothing
        self.assertEqual(sent, [])
        self.assertIn("reason=retro-off", buf.getvalue())

    def test_TM8_retro_section_is_view_only_and_the_queue_returns_inline(self):
        # (a) v4 section 2: legacy gate never asks, moves or enqueues.
        w, o = self.why(SPEED, work="DGN-88"), self.why(STABLE)
        aid = historical_queue(self.ctx, "why", "ripe", w, [w], shape="confirm")[0]
        self.assertEqual(json.loads(self.cli("retro-section")[1])["bubbles"], [])
        self.turn()
        self.assert_retired("gate", "--work", "DGN-88", "--before", "trial")
        self.assertEqual(self.asks()[aid]["state"], "queued")


# ---------------------------------------------------------------------------
# TR: trial-run rules
# ---------------------------------------------------------------------------

class TrialRun(Base):
    def test_TR1_umbrella_parent_is_never_confirmed(self):
        # (a) v4 D5 + section 2: hierarchy never revives legacy asking.
        top, child = self.why(COST), self.why(SPEED)
        events.attach(child, top)
        self.turn()
        self.assert_retired("ask", "--trigger", "ripe", "--topic", top, "--shape", "confirm", "--cand", top)
        for wid in (top, child):
            self.assertEqual(ask.enqueue(self.ctx, "why", "ripe", wid, [wid], shape="confirm"), (None, "retired"))
            self.assertEqual(self.node(wid)["status"], "hypothesis")

    def test_TR2_umbrella_by_span_of_signals(self):
        w = self.why(COST)
        self.turn()
        for i, work in enumerate(("DGN-91", "DGN-92", "DGN-93"), 1):
            self.correction("cor-%04d" % i, "\uc774\uac74 \uc544\ub2c8\uc57c %d" % i, work)
            self.assertEqual(self.cli("read", "--signal", "cor-%04d" % i, "--why", w)[0], 0)
        self.assertTrue(ask.is_umbrella(self.ctx, w))
        self.assertEqual(ask.enqueue(self.ctx, "why", "ripe", w, [w], shape="confirm")[1],
                         "retired")

    def setup_readings(self):
        a, b = self.why(COST, work="DGN-95"), self.why(SPEED, work="DGN-95")
        c = self.why(STABLE, work="DGN-95")
        self.turn()
        texts = ["\ub85c\uadf8\uac00 \ub108\ubb34 \uae38\uc5b4", "\ubc84\ud2bc\uc774 \uc791\uc544",
                 "\uc54c\ub9bc\uc774 \ub2a6\uc5b4"]
        for i, t in enumerate(texts, 1):
            self.correction("cor-%04d" % i, t, "DGN-95")
        # the agent read all three as the same Why: still three lines
        for i in (1, 2, 3):
            self.cli("read", "--signal", "cor-%04d" % i, "--why", a)
        return a, b, c, texts

    def retro_bubble(self, mid):
        """The slot's turn: the next bubble, sent and recorded."""
        [b] = retro.section(self.ctx)["bubbles"]
        retro.shown(self.ctx, b["ask"], mid, CHAT, b["text"])
        return b

    def test_TR3_readings_are_never_handed_out(self):
        """DGN-2028 AMEND-2 P3.4: the agent's readings stay its own view;
        the view-only retro shows no readings bubble and confirms nothing."""
        a, b, c, texts = self.setup_readings()
        self.at(RETRO_AT)
        self.t = RETRO_AT
        n = len(store.read_rows(store.ASKS))
        self.assertEqual(retro.section(self.ctx)["bubbles"], [])
        self.assertEqual(retro.section(self.ctx)["bubbles"], [])
        self.assertEqual(len(store.read_rows(store.ASKS)), n)
        self.assertEqual(self.evs("confirm") + self.evs("cowhy"), [])
        self.assertEqual(self.node(a)["status"], "hypothesis")
        self.assertEqual(sorted(retro.current()), ["cor-0001", "cor-0002", "cor-0003"])

    def test_TR4_wrong_reading_followup_stays_library_only(self):
        """The on_wrong / candidates library is kept (old rows, `read`);
        its follow-up is never handed out by the view-only retro."""
        a, b, c, texts = self.setup_readings()
        self.at(RETRO_AT)
        self.t = RETRO_AT
        retro.migrate_view_only(self.ctx)
        fid = retro.on_wrong(self.ctx, "cor-0003", [a], "prb-0000")
        self.assertIsNone(fid)
        self.assertEqual(retro.current().get("cor-0003"), None)
        self.assertEqual(retro.section(self.ctx)["bubbles"], [])
        self.assertEqual(store.read_rows(store.PROBES)[-1]["reading_wrong"], "cor-0003")

    def test_TR5_read_verb_unchanged(self):
        """F26: `read` (the agent's reading of one signal) is unchanged."""
        a, b, c, texts = self.setup_readings()
        rc, out = self.cli("read", "--signal", "cor-0003", "--why", c)
        self.assertEqual(rc, 0, out)
        self.assertEqual(retro.current()["cor-0003"]["whys"], [c])
        self.assertEqual(self.node(c)["status"], "hypothesis")

    def test_TR6_no_candidates_is_free_text_only(self):
        w = self.why(COST)
        self.turn()
        self.correction("cor-0001", "\uadf8\ub0e5 \ub290\ub824", None)
        self.cli("read", "--signal", "cor-0001", "--why", w)
        aid = retro.on_wrong(self.ctx, "cor-0001", [w], "prb-0000")
        self.assertIsNone(aid)
        self.assertEqual(store.read_rows(store.PROBES)[-1]["reading_wrong"], "cor-0001")

    def test_TR7_read_floors(self):
        w, o = self.why(COST), self.why(SPEED)
        self.turn()
        self.correction("cor-0001", "\uc544\ub2c8\uc57c", "DGN-97")
        self.assertEqual(self.cli("read", "--signal", "cor-0404", "--why", w)[0], 3)
        events.end("retire", o)
        self.assertEqual(self.cli("read", "--signal", "cor-0001", "--why", o)[0], 3)
        self.assertEqual(self.cli("read", "--signal", "cor-0001")[0], 2)
        w2 = self.why(STABLE)
        rc, out = self.cli("read", "--signal", "cor-0001", "--why", w, "--why", w2)
        self.assertEqual(rc, 0, out)   # one signal may have several Whys
        self.assertEqual(retro.current()["cor-0001"]["whys"], [w, w2])


# ---------------------------------------------------------------------------
# CP: copy
# ---------------------------------------------------------------------------

class Copy(Base):
    def catalog(self, lang):
        p = os.path.join(os.path.dirname(os.path.dirname(HERE)), "config", "i18n", lang + ".json")
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def test_CP1_confirmed_list_and_pending_new_strings(self):
        ko = self.catalog("ko")
        self.assertEqual(ko["why.ask.stem.mixed"], STEM_5F)
        self.assertEqual(ko["why.ask.both"], BOTH)
        self.assertFalse(set(ask.COPY_KEYS_CONFIRMED) & set(ask.COPY_KEYS_PENDING_OWNER_CONFIRM))
        for lang in ("ko", "en"):
            cat = self.catalog(lang)
            for key in ask.COPY_KEYS_PENDING_OWNER_CONFIRM:
                self.assertTrue(cat.get(key, "").strip(), (lang, key))
            self.assertLessEqual(ask.label_width(cat["why.ask.both"]), ask.BUTTON_LABEL_MAX_WIDTH)
            for key in ("why.retro.wrong", "why.retro.stop"):
                self.assertLessEqual(ask.label_width(cat[key]), ask.BUTTON_LABEL_MAX_WIDTH)
            for slot in ("{signal}", "{reading}"):
                self.assertIn(slot, cat["why.retro.line"])
            self.assertIn("{signal}", cat["why.ask.stem.reading"])
            for key in ask.COPY_KEYS_PENDING_OWNER_CONFIRM + ("why.ask.stem.mixed", "why.ask.both"):
                self.assertNotIn("\ud669\uae08\ub098\uc120", cat[key])


if __name__ == "__main__":
    unittest.main(verbosity=1)
