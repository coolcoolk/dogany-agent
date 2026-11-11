#!/usr/bin/env python3
# test-lane: fast
# test-new-component: Leader-frame PM routing and same-day bend bundles are a separate consumer boundary from core Why-net events.
# test-component: why-net-pm-side
# test-tier: 1
# test-prereq: tmp-files
# test-covers: agents/.template/routines/lib/why_net/pm_side.py agents/.template/routines/why-net.py
# promise: a Why open in the leader's current frame routes the PM question via the leader
# promise: a Why not in the leader's frame, or whose ask is closed, routes direct
# promise: only the named leader session's frame counts
# promise: a Why linked to the work item of an open frame ask is live
# promise: with the net off the route is direct and the store is untouched
# promise: a Why bend yields one same-day bundle listing every affected ticket with impact, evidence, proposal and done
# promise: the bundle carries the pending target, owner and next check line until the owner answers
# promise: a ticket outside the bent Why and a change of another day stay out of the bundle
# promise: an ended Why proposes cancel and a missing new done is reported as needed
"""M1-4 G10-G11 PM-side reads on a tmp why-net store (scenes S9, S10).
Run: python3 routines/tests/test-why-net-pm-side.py"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROUTINES, "lib"))

from why_net import budget, citations, events, pm_side, store  # noqa: E402

LEADER, OTHER = "sess-leader", "sess-other"
DEFINE = dict(pointed_line="dec-509 M1-4", how="Reuse events", what="Bundle",
              done="Bundle lists every ticket")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="why-pm-side-")
        self.root = self.tmp.name
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_"))}
        env["DOGANY_ROOT"] = self.root
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        os.makedirs(os.path.join(self.root, "config"))
        os.makedirs(os.path.join(self.root, "tickets"))
        self.conf("WHY_NET=on\n")
        self.n = 0

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def conf(self, text):
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write(text)

    def ctx(self):
        return budget.Ctx(root=self.root)

    def turn(self, session):
        self.n += 1
        row = {"id": "otl-%06d" % self.n, "session_id": session, "sha": "x",
               "request_id": "r%d" % self.n}
        store.append_row(store.TURN_LEDGER, row)
        return row["id"]

    def frame_ask(self, otl, why, ev="create", topic="DGN-1"):
        aid = "ask-%06d" % (self.n + 100)
        store.append_row(store.ASKS, {"ask": aid, "ev": "create", "trigger": "turn",
                                      "frame_otl": otl, "topic": topic, "at": store.now_iso(),
                                      "shown": [{"slot": 1, "why": why}]})
        if ev != "create":
            store.append_row(store.ASKS, {"ask": aid, "ev": ev, "at": store.now_iso()})
        return aid


class Route(Base):
    def test_open_frame_ask_on_the_why_routes_via_the_leader(self):
        w = events.propose("Ship the PM path", "framework")["why"]
        aid = self.frame_ask(self.turn(LEADER), w)
        r = pm_side.route(self.ctx(), w, LEADER)
        self.assertEqual((r["live"], r["via"], r["asks"]), (True, "leader", [aid]))

    def test_why_outside_the_frame_or_closed_ask_routes_direct(self):
        w = events.propose("Ship the PM path", "framework")["why"]
        x = events.propose("Unrelated", "framework")["why"]
        otl = self.turn(LEADER)
        self.frame_ask(otl, x)
        self.assertEqual(pm_side.route(self.ctx(), w, LEADER)["via"], "direct")
        self.frame_ask(otl, w, ev="answered")
        r = pm_side.route(self.ctx(), w, LEADER)
        self.assertEqual((r["live"], r["via"]), (False, "direct"))

    def test_only_the_named_leader_session_counts(self):
        w = events.propose("Ship the PM path", "framework")["why"]
        self.frame_ask(self.turn(OTHER), w)
        self.turn(LEADER)
        self.assertEqual(pm_side.route(self.ctx(), w, LEADER)["via"], "direct")
        self.assertEqual(pm_side.route(self.ctx(), w, OTHER)["via"], "leader")

    def test_work_linked_why_is_live_through_the_ask_topic(self):
        w = events.propose("Ship the PM path", "framework", work="DGN-7")["why"]
        x = events.propose("Other", "framework")["why"]
        self.frame_ask(self.turn(LEADER), x, topic="DGN-7")
        self.assertEqual(pm_side.route(self.ctx(), w, LEADER)["via"], "leader")

    def test_net_off_routes_direct_and_writes_nothing(self):
        w = events.propose("Ship the PM path", "framework")["why"]
        self.frame_ask(self.turn(LEADER), w)
        self.conf("WHY_NET=off\n")
        before = {n: store.read_rows(n) for n in (store.EVENTS, store.ASKS, store.TURN_LEDGER)}
        r = pm_side.route(self.ctx(), w, LEADER)
        self.assertEqual((r["via"], r["basis"]), ("direct", "net-off"))
        self.assertEqual(before, {n: store.read_rows(n) for n in before})


class Bundle(Base):
    def setUp(self):
        super().setUp()
        self.top = events.propose("Top Why", "framework")["why"]
        self.other = events.propose("Side Why", "framework")["why"]

    def ticket(self, tid, whys, target):
        p = os.path.join(self.root, "tickets", tid + ".md")
        with open(p, "w") as f:
            f.write("---\nid: %s\nproject: sample\nstatus: open\n---\n" % tid)
        citations.record_ticket(p, DEFINE, target, "v1", "none", "owner:dec-509",
                                whys, root=self.root)

    def change(self, why, kind):
        store.append_row(store.PROBES, {"id": "prb-%06d" % (self.n + 1), "verbatim": "Reword",
                                        "shown": [{"why": why}]})
        self.n += 1
        events.revise(why, "Top Why bent", "prb-%06d" % self.n, change_kind=kind)

    def day(self):
        return store.now_iso()[:10]

    def test_bend_yields_one_bundle_with_every_affected_ticket(self):
        self.ticket("APP-1", [self.top], "sample/a")
        self.ticket("APP-2", [self.top], "sample/b")
        self.change(self.top, "bend")
        b = pm_side.bundle(self.day(), {"APP-1": "New done one"}, self.root)
        self.assertEqual([t["ticket"] for t in b["tickets"]], ["APP-1", "APP-2"])
        t = b["tickets"][0]
        self.assertEqual((t["proposal"], t["done"], t["new_done"]),
                         ("carry-over", DEFINE["done"], "New done one"))
        self.assertEqual(t["impact"][0]["why"], self.top)
        self.assertEqual(b["tickets"][1]["new_done"], "needed")
        self.assertIn("evidence", t)

    def test_pending_line_names_target_owner_next_check_until_answered(self):
        self.ticket("APP-1", [self.top], "sample/a")
        self.change(self.top, "bend")
        b = pm_side.bundle(self.day(), root=self.root)
        self.assertEqual(len(b["pending"]), 1)
        self.assertIn("target=sample/a", b["pending"][0])
        self.assertIn("owner=owner", b["pending"][0])
        self.assertIn("next=owner answer on " + self.top, b["pending"][0])
        store.append_row(store.EVENTS, {"ev": "confirm", "id": "evt-900001", "why": self.top,
                                        "at": store.now_iso(), "actor": "agent", "data": {}})
        with patch.object(events, "audit_confirms", return_value=[]):
            self.assertEqual(pm_side.bundle(self.day(), root=self.root)["pending"], [])

    def test_unaffected_ticket_and_other_day_stay_out(self):
        self.ticket("APP-1", [self.top], "sample/a")
        self.ticket("APP-2", [self.other], "sample/b")
        self.change(self.top, "bend")
        b = pm_side.bundle(self.day(), root=self.root)
        self.assertEqual([t["ticket"] for t in b["tickets"]], ["APP-1"])
        self.assertEqual(pm_side.bundle("2000-01-01", root=self.root)["tickets"], [])

    def test_end_proposes_cancel(self):
        self.ticket("APP-1", [self.top], "sample/a")
        self.change(self.top, "end")
        t = pm_side.bundle(self.day(), root=self.root)["tickets"][0]
        self.assertEqual((t["proposal"], t["new_done"]), ("cancel", "needed"))


if __name__ == "__main__":
    unittest.main()
