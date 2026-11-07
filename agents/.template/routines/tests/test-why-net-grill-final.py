#!/usr/bin/env python3
# test-lane: fast
# test-covers: agents/.template/routines/lib/why_net/morning.py
"""v2.7 T1 Why net -- fixtures for the five blockers of the final real-code
grill (FIX-FIRST), one per blocker repro, plus the small non-blocking fixes.

  B1  default OFF writes and prints nothing (verbs, the dispatch path,
      derive); nothing from the off period surfaces once it is turned on.
  B2  the bare 07:00 push is opt-in (WHY_MORNING_FALLBACK, default off).
  B3  the retro hands out ONE item per turn, with a stop button, at most
      WHY_RETRO_MAX (3) per retro day across calls; one signal per readings
      bubble; a read signal gets no T-corr ask in the same retro.
  B4  free text after a retro bubble is its answer (a `free` probe, so a
      `prb-N` signal).
  B5  a before-work ask the owner already answered (or a later gate
      covered) is never re-asked in the retro.
  NB  store lock bounded for the hook; private store modes; T-corr <= 3.

Runs on the AMEND-1 rev 3 harness (test-why-net-followup.Base): live-shaped
owner turns, real transcript chain.  Synthetic data only.

Run: python3 routines/tests/test-why-net-grill-final.py
"""

import fcntl
import importlib.util
import io
import json
import os
import stat
import subprocess
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FU = _load("_final_followup", os.path.join(HERE, "test-why-net-followup.py"))
AM = FU.AM

from legacy_why_fixtures import historical_queue

from why_net import ask, corrections, events, morning, retro, store, ups  # noqa: E402
from why_net import nodes as N  # noqa: E402

T0, SID, CHAT = AM.T0, AM.SID, AM.CHAT
YES, ESC, SPEED, STABLE, COST = AM.YES, AM.ESC, AM.SPEED, AM.STABLE, FU.COST
NOT, STOP = FU.NOT, FU.STOP
RETRO_AT = FU.RETRO_AT                   # 21:00 KST
AT_1400 = T0 + 4 * 3600                  # 14:00 KST
AT_0700 = T0 - 3 * 3600                  # 07:00 KST
DAY = 86400


class Base(FU.Base):
    def conf_off(self, extra=""):
        """The minted conf: no WHY_* line at all."""
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write("AGENT_LANG=ko\nDECISION_LEDGER_PATH=%s\n%s" % (AM.LEDGER, extra))

    def conf_on(self, extra=""):
        self.write_conf("DECISION_LEDGER_PATH=%s\n%s" % (AM.LEDGER, extra))

    def goto(self, ts):
        self.t = ts
        self.at(ts)

    def sender(self):
        sent = []

        def send(ctx, text, buttons):
            sent.append(text)
            return 0, 5000 + len(sent), CHAT
        return sent, send

    def bubble(self, mid):
        """The retro slot's turn: the next bubble, sent and recorded."""
        bubbles = retro.section(self.ctx)["bubbles"]
        self.assertLessEqual(len(bubbles), 1)
        if not bubbles:
            return None
        b = bubbles[0]
        rc, _ = self.cli("retro-shown", "--ask", b["ask"], "--message-id", str(mid),
                         "--chat-id", str(CHAT), "--text", b["text"])
        self.assertEqual(rc, 0)
        return b

    def tap(self, label, mid):
        self.tick()
        self.ups(label, source="callback", message_id=mid)


# ---------------------------------------------------------------------------
# B1: default OFF
# ---------------------------------------------------------------------------

class DefaultOff(Base):
    def test_B1_off_verbs_dispatch_and_derive_write_nothing(self):
        self.conf_off()
        self.goto(AT_1400)
        self.turn()                                       # sentinel ledger row only
        before = {f: store.read_rows(f) for f in (store.EVENTS, store.ASKS, store.PROBES)}
        runs = [
            ("propose", "--text", "\uc18d\ub3c4 \uc6b0\uc120", "--scope", "framework",
             "--work", "DGN-1201"),
            ("ask", "--trigger", "fork", "--topic", "DGN-1201", "--shape", "contrast",
             "--cand", "why-0001", "--cand", "why-0002"),
            ("gate", "--work", "DGN-1201", "--before", "irreversible"),
            ("enqueue", "--kind", "why", "--trigger", "new-work", "--topic", "DGN-1300"),
            ("read", "--signal", "cor-0001", "--why", "why-0001"),
            ("retro-shown", "--ask", "ask-0001", "--message-id", "1"),
            ("act", "--kind", "decision", "--ref", "dec-001", "--why", "why-0001",
             "--direction", "consistent"),
            ("confirm", "--why", "why-0001", "--quote", "x"),
            ("lint-ticket", os.path.join(self.root, "worklog", "DGN-1201.md")),
        ]
        for argv in runs:
            rc, out = self.cli(*argv)
            self.assertEqual(rc, 4, argv)
            self.assertEqual(out.strip(), "signal::WHY_REFUSED reason=off", argv)
            self.assertNotIn("[[OPTIONS", out)
        # the dispatch path, as dispatch-detached.sh runs it (no agent action)
        fn = subprocess.run(["sed", "-n", "/^resolve_dispatch_why() {/,/^}/p",
                             os.path.join(ROUTINES, "dispatch-detached.sh")],
                            capture_output=True, text=True).stdout
        self.assertNotIn("enqueue", fn)
        env = dict(os.environ, DOGANY_ROOT=self.root)
        p = subprocess.run(["bash", "-c", fn + '\nSCRIPT_DIR="$1" TICKET=DGN-1300 WHY_ID=""'
                            '\nresolve_dispatch_why; echo "$DISPATCH_WHY"', "_", ROUTINES],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.stdout.strip(), "none", p.stderr)
        # derive: the audit runs, nothing is derived (the store dir exists)
        self.assertTrue(os.path.isdir(self.ddir))
        rc, out = self.cli("derive", "--no-model")
        self.assertEqual(rc, 0)
        rc, out = self.cli("derive", "--audit", "--no-model")
        self.assertIn("WHY_AUDIT_OK", out)
        self.assertFalse(os.path.exists(os.path.join(self.ddir, "derived")))
        after = {f: store.read_rows(f) for f in before}
        self.assertEqual(after, before)
        self.assertEqual(after[store.ASKS], [])
        # read-only verbs still serve
        self.assertEqual(self.cli("status")[0], 0)
        self.assertEqual(self.cli("lint-ticket", "--resolve", "DGN-1201")[0], 0)
        # -- then the owner turns the net on: nothing from before surfaces --
        self.conf_on("WHY_MORNING_FALLBACK=on\n")
        self.goto(RETRO_AT)
        self.assertEqual(retro.section(self.ctx)["bubbles"], [])
        sent, send = self.sender()
        self.goto(AT_0700 + DAY)
        self.assertEqual(morning.run(self.ctx, sender=send, out=io.StringIO()), 0)
        self.assertEqual(sent, [])

    def test_B1_on_verbs_still_work(self):
        rc, out = self.cli("propose", "--text", COST, "--scope", "framework")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("enqueue", "--kind", "why", "--trigger", "new-work",
                           "--topic", "DGN-1300")
        self.assertEqual(rc, 3, out)
        self.assertIn("retired", out)


# ---------------------------------------------------------------------------
# B2: the 07:00 push is opt-in
# ---------------------------------------------------------------------------

class MorningOptIn(Base):
    def test_B2_net_on_alone_pushes_nothing_at_0700(self):
        w = self.why(COST)
        aid = historical_queue(self.ctx, "why", "ripe", w, [w], shape="confirm")[0]
        sent, send = self.sender()
        self.goto(AT_0700)
        out = io.StringIO()
        self.assertEqual(morning.run(self.ctx, sender=send, out=out), 0)
        self.assertEqual(sent, [])
        self.assertIn("reason=fallback-off", out.getvalue())
        self.assertEqual(self.asks()[aid]["state"], "queued")   # it waits
        # DGN-2028 AMEND-2 P3.4: even the fallback flag pushes nothing now
        # (gated with the view-only retro; the ask returns inline)
        self.conf_on("WHY_MORNING_FALLBACK=on\n")
        out = io.StringIO()
        morning.run(self.ctx, sender=send, out=out)
        self.assertEqual(sent, [])
        self.assertIn("reason=retro-off", out.getvalue())

    def test_B2_shipped_plist_says_opt_in(self):
        with open(os.path.join(ROUTINES, "com.telegram-skill-bot.telegram-agent."
                               "why-morning.plist"), encoding="utf-8") as fh:
            self.assertIn("WHY_MORNING_FALLBACK=on", fh.read())


# ---------------------------------------------------------------------------
# B3 + B4: one item per turn, stop, cap, free text
# ---------------------------------------------------------------------------

class RetroTurns(Base):
    def setup_evening(self):
        """2 corrections on DGN-1301 read by the agent, a T-corr ask on the
        same item, and 3 new-work confirms (the grill's repro shape)."""
        self.goto(T0)
        a = self.why(COST, work="DGN-1301")
        b = self.why(SPEED, work="DGN-1301")
        c = self.why(STABLE, work="DGN-1301")
        d = self.why("\uc0ac\uc6a9\uc790 \uacbd\ud5d8 \uac1c\uc120", work="DGN-1301")
        self.turn()
        self.correction("cor-0001", "\uc544\ub2c8 \uadf8\uac8c \uc544\ub2c8\ub77c DGN-1301 "
                        "\uc54c\ub9bc \ube7c\uc918", "DGN-1301")
        self.correction("cor-0002", "\uc544\ub2c8 \uadf8\ub807\uac8c \ub9d0\uace0 DGN-1301 "
                        "\ub9d0\ud22c \ubc14\uafd4", "DGN-1301")
        for sig in ("cor-0001", "cor-0002"):
            self.assertEqual(self.cli("read", "--signal", sig, "--why", d)[0], 0)
        tcorr = historical_queue(self.ctx, "why", "correction", "DGN-1301", [a, b, c],
                            work="DGN-1301")[0]
        nw = []
        for i in range(3):
            h = self.why("\uc774\uc720 %d" % i, work="DGN-%d" % (1302 + i))
            nw.append(historical_queue(self.ctx, "why", "new-work", "DGN-%d" % (1302 + i), [h],
                                  work="DGN-%d" % (1302 + i))[0])
        self.goto(RETRO_AT)
        return tcorr, nw

    def test_B3_view_only_retro_hands_out_nothing(self):
        """DGN-2028 AMEND-2 P3.4 (replaces the one-item-per-turn walk): the
        retro hands out no bubble, however often the slot calls; the queued
        asks wait for an inline pull; the nightly derive drops the T-corr
        ask whose signals the agent already reads (final grill B3)."""
        tcorr, nw = self.setup_evening()
        self.assertIsNone(self.bubble(9101))
        self.assertIsNone(self.bubble(9102))
        self.assertTrue(all(self.asks()[x]["state"] == "queued" for x in nw))
        self.derive()
        self.assertEqual(self.asks()[tcorr]["state"], "not_shown")
        self.assertEqual(self.asks()[tcorr]["dropped"], "read")
        self.assertFalse([r for r in store.read_rows(store.ASKS)
                          if r.get("ev") == "shown" and r.get("via") == "retro"])

    def test_B4_free_text_after_the_retro_binds_nothing(self):
        """DGN-2028 AMEND-2 P3.4: retro_open is gone -- a typed message is
        never bound as a retro answer."""
        self.setup_evening()
        self.assertIsNone(self.bubble(9301))
        n = len(self.probes())
        self.tick()
        self.ups("\uc14b \ub2e4 \uc6d0\uc778\uc774 \ub2ec\ub77c")
        self.assertEqual(len(self.probes()), n)
        self.assertFalse(hasattr(ups, "retro_open"))
        self.assertFalse(hasattr(ups, "RETRO_NEXT"))

    def test_B4_open_retro_asks_are_closed_once(self):
        """F23 / FR24: the one-shot migration closes an open retro ask
        (ignored, retro-view-only) and keeps its probes."""
        self.setup_evening()
        self.assertIsNone(retro.readings_bubble(self.ctx))
        # Seed a pre-retirement reading ask to test the real migration.
        rid = historical_queue(self.ctx, "why", "readings", "DGN-1301", [], shape="scatter")[0]
        self.derive()
        a = self.asks()[rid]
        self.assertEqual((a["state"], a.get("reason")), ("ignored", "retro-view-only"))
        self.derive()
        self.assertEqual(self.asks()[rid]["events"].count("ignored"), 1)


# ---------------------------------------------------------------------------
# B5: an answered before-work ask is never re-asked
# ---------------------------------------------------------------------------

class GateNoReask(Base):
    def test_B5_refused_gate_then_answered_gate_leaves_nothing(self):
        # (a) v4 section 2: the retired gate cannot create the first ask or a retry.
        w = self.why(COST, work="DGN-1202")
        for delay in (0, 31 * 60):
            self.tick(delay)
            self.turn()
            self.assert_retired("gate", "--work", "DGN-1202", "--before", "trial")
        self.assertEqual(self.queued("before-work"), [])
        self.assertEqual(retro.section(self.ctx)["bubbles"], [])
        self.assertEqual(self.node(w)["status"], "hypothesis")

    def test_B5_covered_queued_ask_is_dropped_in_retro_and_morning(self):
        w, o = self.why(COST, work="DGN-1203"), self.why(SPEED)
        aid = historical_queue(self.ctx, "why", "ripe", w, [w], shape="confirm")[0]
        self.provisional(w, o)                      # answered another way
        self.goto(RETRO_AT)
        self.assertEqual(retro.section(self.ctx)["bubbles"], [])
        # DGN-2028 AMEND-2 P3.4: rendering settles nothing; the nightly
        # derive drops the covered ask
        self.assertEqual(self.asks()[aid]["state"], "queued")
        self.derive()
        a = self.asks()[aid]
        self.assertEqual((a["state"], a["dropped"]), ("not_shown", "covered"))
        # the morning fallback sends nothing either
        w2 = self.why(STABLE, work="DGN-1204")
        historical_queue(self.ctx, "why", "ripe", w2, [w2], shape="confirm")
        self.provisional(w2, o, topic="DGN-1005")
        self.conf_on("WHY_MORNING_FALLBACK=on\n")
        sent, send = self.sender()
        self.goto(AT_0700 + DAY)
        morning.run(self.ctx, sender=send, out=io.StringIO())
        self.assertEqual(sent, [])


# ---------------------------------------------------------------------------
# NB: the non-blocking items fixed on build
# ---------------------------------------------------------------------------

class NonBlocking(Base):
    def test_NB_hook_lock_wait_is_bounded(self):
        self.turn()
        with open(os.path.join(self.ddir, ".lock"), "a+") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            self.rid += 1
            prompt = "\uc774\uac70 \ud574\uc918"
            with open(os.path.join(self.ctx.bot_dir, "inbound-context.json"), "w") as fh:
                json.dump({"schema": 1, "request_id": "req-busy", "source": "message",
                           "message_id": None, "chat_id": CHAT,
                           "text_sha": ups.why_owner_turn.turn_sha(prompt),
                           "ts": self.ctx.now}, fh)
            n = len(store.read_rows(store.TURN_LEDGER))
            t = time.monotonic()
            try:
                rc = ups.main(self.ctx, stdin=io.StringIO(json.dumps(
                    {"prompt": prompt, "session_id": SID,
                     "transcript_path": str(self.transcript())})))
            finally:
                store.LOCK_WAIT = None
            took = time.monotonic() - t
        self.assertEqual(rc, 0)
        self.assertLess(took, 3.0)
        self.assertGreaterEqual(took, ups.LOCK_WAIT)
        self.assertEqual(len(store.read_rows(store.TURN_LEDGER)), n)
        with open(os.path.join(self.root, "logs", "why-net.log"), encoding="utf-8") as fh:
            self.assertIn("store lock busy", fh.read())

    def test_NB_store_files_are_private(self):
        self.turn()
        self.why(COST)
        self.cli("derive", "--no-model")
        for name in (store.EVENTS, store.TURN_LEDGER):
            self.assertEqual(stat.S_IMODE(os.stat(store.path(name)).st_mode), 0o600, name)
        d = os.path.join(self.ddir, "derived")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "nodes.json")).st_mode), 0o600)

    def test_NB_tcorr_offers_at_most_three(self):
        ws = [self.why("\uc774\uc720 %d" % i, work="DGN-1401") for i in range(4)]
        aid = corrections.tcorr_enqueue(self.ctx, {"id": "cor-0001"},
                                        "\uc544\ub2c8 DGN-1401 \ub2e4\uc2dc")
        self.assertIsNone(aid)
        self.assertEqual(self.queued("correction"), [])
        self.assertTrue(all(self.node(w)["status"] == "hypothesis" for w in ws))


if __name__ == "__main__":
    unittest.main(verbosity=1)
