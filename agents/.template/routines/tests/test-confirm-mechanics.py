#!/usr/bin/env python3
# test-lane: fast
"""Shared confirm-mechanics library (dec-336) -- fixtures CM1-CM12.

Spec: docs/design/v27-t1-why-net-AMEND-1.md section 3 (API 3.2, rules 3.5)
and 5.3.  Pure-function fixtures: plain Evidence dicts in, verdicts out.
Policies: the Why net policy (2.1) and the life kit taste policy (3.3).

Run: python3 routines/tests/test-confirm-mechanics.py
"""

import ast
import builtins
import inspect
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
sys.path.insert(0, LIB)

import confirm_mechanics as cm  # noqa: E402

WHY = {"threshold": 2, "window_n": 4, "distinct_turns": 2, "distinct_topics": 2,
       "min_prior": 1, "ripe_min": 2, "mixed_after": 2}
TASTE = {"threshold": 3, "window_n": 6, "distinct_turns": 3, "distinct_topics": 1,
         "min_prior": 1, "ripe_min": 3, "mixed_after": 2}

_seq = [0]


def ev(order, polarity="win", cls="choice", link="agent-prior", turn=None,
       topic=None, claim="why-0001", void=False, scope="narrow"):
    _seq[0] += 1
    return {"id": "e-%d" % _seq[0], "claim": claim,
            "turn": turn or "otl-%06d" % order,
            "topic": topic if topic is not None else "t%d" % order,
            "order": order, "cls": cls, "link": link, "polarity": polarity,
            "scope": scope, "void": void}


def run(rows, state="provisional", policy=WHY, since=0, resolved=0, claim="why-0001"):
    return cm.verdict(state, cm.tally(rows, claim, since, policy), policy, resolved)


class CM(unittest.TestCase):
    def test_CM1_threshold(self):
        self.assertEqual(run([ev(1)])["verdict"], "none")
        rows = [ev(1), ev(2)]
        v = run(rows)
        self.assertEqual(v["verdict"], "settle")
        self.assertEqual(v["ids"], [r["id"] for r in rows])

    def test_CM2_window(self):
        rows = [ev(1), ev(2), ev(3), ev(4, "loss"), ev(5, "loss"), ev(6, "loss"), ev(7)]
        t = cm.tally(rows, "why-0001", 0, WHY)
        self.assertEqual((t["wins"], t["losses"], t["net"]), (1, 3, -2))
        self.assertEqual(cm.verdict("provisional", t, WHY)["verdict"], "conflict")
        ids = [r["id"] for r in cm.window(rows, "why-0001", 0, WHY)]
        self.assertEqual(ids, [r["id"] for r in rows[3:]])  # old rows ignored

    def test_CM2_since_order(self):
        rows = [ev(1), ev(2), ev(3), ev(4)]
        self.assertEqual(run(rows, since=3)["verdict"], "none")
        self.assertEqual(run(rows, since=2)["verdict"], "settle")

    def test_CM3_distinct_turns(self):
        rows = [ev(1, turn="otl-1"), ev(2, turn="otl-1", topic="other")]
        self.assertEqual(len(cm.window(rows, "why-0001", 0, WHY)), 1)
        self.assertEqual(run(rows)["verdict"], "none")

    def test_CM4_distinct_topics(self):
        rows = [ev(1, topic="a"), ev(2, topic="a")]
        self.assertEqual(run(rows)["verdict"], "none")
        taste = rows + [ev(3, topic="a")]
        self.assertEqual(run(taste, policy=TASTE)["verdict"], "settle")

    def test_CM5_min_prior(self):
        rows = [ev(1, link="agent"), ev(2, link="agent")]
        self.assertEqual(run(rows)["verdict"], "none")
        self.assertEqual(run(rows + [ev(3)])["verdict"], "settle")
        # the loss side: agent-linked losses never conflict alone
        losses = [ev(1, "loss", link="agent"), ev(2, "loss", link="agent")]
        self.assertEqual(run(losses)["verdict"], "none")
        self.assertEqual(run(losses + [ev(3, "loss")])["verdict"], "conflict")

    def test_CM6_delegated_activity_never_settle_or_conflict(self):
        for cls in ("delegated", "activity"):
            wins = [ev(i, cls=cls) for i in range(1, 6)]
            self.assertEqual(run(wins)["verdict"], "none", cls)
            losses = [ev(i, "loss", cls=cls) for i in range(1, 6)]
            self.assertEqual(run(losses)["verdict"], "none", cls)
            self.assertEqual(run(losses, state="confirmed")["verdict"], "none", cls)
            self.assertEqual(run(wins[:2], state="hypothesis")["verdict"], "ripe", cls)
        unknown = [ev(i, cls="unknown") for i in range(1, 6)]
        self.assertEqual(run(unknown)["verdict"], "none")
        self.assertEqual(run(unknown, state="hypothesis")["verdict"], "none")

    def test_CM7_ripe_from_hypothesis(self):
        rows = [ev(1, cls="activity"), ev(2, cls="delegated")]
        v = run(rows, state="hypothesis")
        self.assertEqual(v, {"verdict": "ripe", "ids": [r["id"] for r in rows]})
        self.assertEqual(run(rows[:1], state="hypothesis")["verdict"], "none")
        self.assertEqual(run(rows + [ev(3, "loss")], state="hypothesis")["verdict"], "none")
        # ripe never fires on a provisional / confirmed claim
        self.assertEqual(run([ev(1, cls="activity"), ev(2, cls="activity")])["verdict"], "none")

    def test_CM8_symmetric_conflict(self):
        for state in ("provisional", "confirmed"):
            self.assertEqual(run([ev(1, "loss")], state=state)["verdict"], "none")
            self.assertEqual(run([ev(1, "loss"), ev(2, "loss")], state=state)["verdict"],
                             "conflict")
            # one turn twice is one row
            same = [ev(1, "loss", turn="otl-9"), ev(2, "loss", turn="otl-9")]
            self.assertEqual(run(same, state=state)["verdict"], "none")
        # a confirmed claim never re-settles; a contested one never moves
        self.assertEqual(run([ev(1), ev(2)], state="confirmed")["verdict"], "none")
        for rows in ([ev(1), ev(2)], [ev(1, "loss"), ev(2, "loss")]):
            self.assertEqual(run(rows, state="contested")["verdict"], "none")

    def test_CM9_mixed(self):
        rows = [ev(1, "loss"), ev(2, "loss")]
        self.assertEqual(run(rows, resolved=1)["verdict"], "conflict")
        v = run(rows, resolved=2)
        self.assertEqual(v["verdict"], "mixed")
        self.assertEqual(v["ids"], [r["id"] for r in rows])

    def test_CM10_void_precedence(self):
        rows = [ev(1), ev(2, void=True)]
        self.assertEqual(run(rows)["verdict"], "none")
        agent = ev(5, link="agent", turn="otl-x")
        prior = ev(6, link="agent-prior", turn="otl-x")
        machine = ev(7, link="machine", turn="otl-x")
        got = cm.window([agent, prior, machine], "why-0001", 0, WHY)
        self.assertEqual([r["id"] for r in got], [machine["id"]])
        got = cm.window([agent, prior], "why-0001", 0, WHY)
        self.assertEqual([r["id"] for r in got], [prior["id"]])
        # another claim's rows are never read
        self.assertEqual(cm.window([ev(1, claim="cat:a=b")], "why-0001", 0, WHY), [])
        self.assertEqual(cm.dedupe_key("otl-1", "why-1"), cm.dedupe_key("otl-1", "why-1"))
        self.assertNotEqual(cm.dedupe_key("otl-1", "why-1"), cm.dedupe_key("otl-1", "why-2"))

    def test_CM11_imports_only_stdlib_no_time_argument(self):
        src = inspect.getsource(cm)
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertEqual(names, {"hashlib"})
        for bad in ("why_net", "lifekit", "open(", "time", "datetime", "os"):
            self.assertNotIn("import %s" % bad, src)
        for fn in (cm.tally, cm.verdict, cm.window):
            params = inspect.signature(fn).parameters
            self.assertFalse({"now", "ts", "time", "clock", "at"} & set(params), fn)

    def test_CM12_no_file_no_clock(self):
        def boom(*a, **k):
            raise AssertionError("library touched I/O or the clock")
        saved = (builtins.open, time.time, time.monotonic)
        builtins.open, time.time, time.monotonic = boom, boom, boom
        try:
            rows = [ev(1), ev(2), ev(3, "loss"), ev(4, cls="activity", topic="z")]
            cm.dedupe_key("otl-1", "why-0001")
            cm.window(rows, "why-0001", 0, WHY)
            for state in cm.STATES:
                cm.verdict(state, cm.tally(rows, "why-0001", 0, WHY), WHY, 3)
        finally:
            builtins.open, time.time, time.monotonic = saved


if __name__ == "__main__":
    unittest.main(verbosity=1)
