#!/usr/bin/env python3
# test-lane: slow    # real lock-contention waits; measured 7.384s before, 6.989s after
# promise: original turn question stems preserve slots, frames and answer binding
# test-component: why-net (AMEND-2 fixture family FR1-FR28)
# test-tier: 2
# test-prereq: none beyond python3 (AMEND-1 harness, scratch store)
# test-new-component: why-net keeps one suite per locked amendment (test-why-net-amend1.py precedent); AMEND-2 fixtures are keyed to its own section 5
"""v2.7 T1 Why net AMEND-2 -- the Why asked inside the working turn
(DGN-2028 slice 3; docs/design/v27-t1-why-net-AMEND-2.md section 5, group FR).

Every fixture id of AMEND-2 section 5 (FR1-FR28 with their lettered legs)
is a test method named after it.  The turn-level legs (TL-*, GF*, MC*,
acceptance A1 / A2) live in tests/turn_frame_why_turn_selftest.py.

Live-shaped turns on the AMEND-1 harness (test-why-net-amend1.Base): every
owner turn publishes a fresh bridge inbound-context and runs `ups`; the
agent's replies are appended to the same transcript and the bridge records
which bubble carried each [[OPTIONS]] keyboard.  A devkit base contract is
installed (target devkit/product-direction).  The in-turn question copy is
MD 14 Q2 A, pinned as PENDING_OWNER_CONFIRM in the instance layer
(config/i18n.local) -- the framework catalog ships a placeholder only.
Synthetic data only.

Run: python3 routines/tests/test-why-net-amend2.py [--write-golden]
"""

import fcntl
import hashlib
import importlib.util
import io
import json
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
GOLDEN = os.path.join(HERE, "fixtures", "why-net", "amend2-fr12-golden.json")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AM = _load("_amend2_amend1", os.path.join(HERE, "test-why-net-amend1.py"))
EL = AM.EL
cli = EL.cli

from why_net import ask, budget, derive, events, retro, store, surfaces, ups  # noqa: E402
from why_net import nodes as N  # noqa: E402
import base_contract  # noqa: E402
import why_owner_turn  # noqa: E402

try:
    from why_net import frame as F  # noqa: E402
    from why_net import metacog as MC  # noqa: E402
except ImportError:          # the base checkout (FR12 golden writer only)
    F = MC = None

T0, SID, CHAT = AM.T0, AM.SID, AM.CHAT
DAY, MIN = 86400, 60
TGT = "devkit/product-direction"
TGT2 = "devkit/home"
C1 = "\uc624\ub298 \uc6b4\ub3d9\uc774 \ub418\uac8c"          # "today's workout works"
C2 = "v2 \ud750\ub984 \uc644\uc131"                          # "finish the v2 flow"
C3 = "\ubc84\uadf8 \uc5c6\uc774 \ucd9c\uc2dc"                # "ship without bugs"
PRIORITY = "\uc774\uac70 \ucd5c\uc6b0\uc120\uc73c\ub85c \ud574\uc918."
# MD 14 Q2 A (PENDING_OWNER_CONFIRM): stem, "another reason"; skip / proceed
# has no Q2 wording yet -- the test layer reuses the confirmed escape copy.
Q2 = {"why.ask.stem.turn": "\uc774\ubc88 \uc77c, \uc5b4\ub290 \ucabd\uc5d0 \ub354 "
                           "\uac00\uae4c\uc6b4\uac00\uc694?",
      "why.ask.turn.other": "\ub2e4\ub978 \uc774\uc720",
      "why.ask.turn.escape": "\uc9c0\uae08\uc740 \ud328\uc2a4"}
BOTH, OTHER, ESC = "\ub458 \ub2e4", Q2["why.ask.turn.other"], Q2["why.ask.turn.escape"]


def install_contract(root):
    d = os.path.join(root, "config", "packs")
    os.makedirs(d, exist_ok=True)
    c = {"version": 1,
         "targets": [{"id": "product-direction", "why_scope": "kit:kit_dev"},
                     {"id": "home", "why_scope": "kit:kit_dev"}],
         "workflows": [{"id": "priority-change", "target": "product-direction",
                        "cues": {"en": ["highest priority"], "ko": ["\ucd5c\uc6b0\uc120"]},
                        "stakes_hint": "high"}]}
    with open(os.path.join(d, "devkit.base_contract.json"), "w", encoding="utf-8") as fh:
        json.dump({"pack_id": "devkit", "base_contract": c}, fh, ensure_ascii=False)


def write_copy(root, extra):
    d = os.path.join(root, "config", "i18n.local")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "ko.json")
    try:
        with open(p, encoding="utf-8") as fh:
            cur = json.load(fh)
    except (OSError, ValueError):
        cur = {}
    cur.update(extra)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, ensure_ascii=False)


class Base(AM.Base):
    def setUp(self):
        super().setUp()
        install_contract(self.root)
        write_copy(self.root, Q2)
        self._saved_tf = os.environ.pop("DOGANY_TURN_FRAME", None)

    def tearDown(self):
        os.environ.pop("DOGANY_TURN_FRAME", None)
        if self._saved_tf is not None:
            os.environ["DOGANY_TURN_FRAME"] = self._saved_tf
        if F is not None:
            F.HOLD_HOOK[0] = None
        super().tearDown()

    # -- verbs -------------------------------------------------------------------
    def run_cli(self, *argv):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cli.main(list(argv), ctx=self.ctx)
        return rc, o.getvalue(), e.getvalue()

    def frame(self, *args):
        return self.run_cli("frame", *args)

    def infer(self, cands=(C1, C2), target=TGT, extra=()):
        args = ["--mode", "infer", "--ambiguous"]
        if target:
            args += ["--target", target]
        for c in cands:
            args += ["--cand", c]
        return self.frame(*(args + list(extra)))

    def retired_frame(self, mode="infer", cands=(C1, C2), target=TGT, work=None, extra=()):
        args = ["frame", "--mode", mode]
        if target:
            args += ["--target", target]
        if work:
            args += ["--work", work]
        for candidate in cands:
            args += ["--cand", candidate]
        self.assert_retired(*(args + list(extra)))

    def historical_infer(self, cands=(C1, C2), target=TGT, extra=()):
        # v4 sections 1/2 retire issuance; 3.3 retains historical evidence.
        from legacy_why_fixtures import historical_frame
        args = ["frame", "--mode", "infer", "--ambiguous"]
        if target:
            args += ["--target", target]
        for candidate in cands:
            args += ["--cand", candidate]
        self.assert_retired(*(args + list(extra)))
        options = dict(zip(extra[::2], extra[1::2]))
        sid, ledger = F.session_ledger()
        otl = ledger[-1]["id"]
        current = F.turn_frame_row(self.ctx, sid, ledger[-1].get("request_id"))
        old = [a for a in self.turn_asks() if a.get("frame_otl") == otl]
        if old:
            return 0, old[-1]["rendered"] + "\n", "historical fixture " + old[-1]["ask"]
        aid, text = historical_frame(self.ctx, otl, cands, target,
            work=options.get("--work"), stakes=current.get("stakes", "normal") if current else options.get("--stakes", "normal"),
            span=options.get("--span"), tf=current["frame_id"] if current else None)
        return 0, text + "\n", "historical fixture " + aid

    def answer(self, aid, pid, *args):
        return self.run_cli("frame", "--answer", "--ask", aid, "--probe", pid, *args)

    def issued(self, cands=(C1, C2), target=TGT, prompt=PRIORITY, extra=(), send=True):
        """An owner turn, the agent's frame and its reply carrying the ask."""
        otl = self.turn(prompt)
        self.tick(5)
        rc, out, err = self.historical_infer(cands, target, extra)
        self.assertEqual(rc, 0, out + err)
        if send:
            self.reply(out.rstrip("\n"))
        return otl, self.turn_asks()[-1], out, err

    def tap(self, label, **kw):
        self.tick()
        self.ups(label, source="callback", **kw)

    # -- reads --------------------------------------------------------------------
    def turn_asks(self):
        return sorted((a for a in self.asks().values() if a.get("trigger") == "turn"),
                      key=lambda a: a["ask"])

    def frames(self):
        return store.read_rows(store.FRAMES)

    def labels(self, a):
        return [s["label"] for s in a["shown"]]

    def born(self):
        return {w: n for w, n in events.nodes().items() if n.get("born")}

    def file_bytes(self):
        out = {}
        for name in store.CHAINED + (store.OWNER_TURNS,):
            try:
                with open(store.path(name), "rb") as fh:
                    out[name] = fh.read()
            except FileNotFoundError:
                out[name] = None
        return out


# ---------------------------------------------------------------------------
# FR1-FR4: the ask, taps, both, three candidates
# ---------------------------------------------------------------------------

class Issue(Base):
    def test_FR1_infer_writes_one_create_row_only(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.turn(PRIORITY)
        self.retired_frame()
        self.assertEqual(self.turn_asks(), [])
        self.assertEqual(self.frames(), [])
        self.assertEqual(store.read_rows(store.EVENTS), [])

    def test_FR2_tap_on_a_text_slot_births_one_node(self):
        otl, a, _, _ = self.issued()
        self.tap(C1)
        [p] = self.probes()
        born = self.born()
        self.assertEqual(len(born), 1)              # slot 3's text is never a node
        [n] = born.values()
        self.assertEqual((n["text"], n["status"], n["worded_by"]), (C1, N.PROVISIONAL, "agent"))
        self.assertEqual((n["born"]["slot"], n["born"]["ask"], n["born"]["probe"]),
                         (2, a["ask"], p["id"]))
        self.assertEqual([(e["otl"], e["role"]) for e in n["evidence"]],
                         [(otl, "frame"), (p["otl"], "answer")])
        self.assertEqual((n["target"], n["scope"]), (TGT, "kit:kit_dev"))
        self.assertEqual(self.asks()[a["ask"]]["state"], "answered")

    def test_FR3_both_births_two_provisional_and_one_cowhy(self):
        self.issued()
        self.tap(BOTH)
        born = sorted(self.born().values(), key=lambda n: n["born"]["slot"])
        self.assertEqual([(n["text"], n["status"]) for n in born],
                         [(C1, N.PROVISIONAL), (C2, N.PROVISIONAL)])
        self.assertEqual(len(self.evs("cowhy")), 1)
        self.assertEqual(born[0]["co"], [born[1]["id"]])
        rc, out = self.cli("derive", "--audit", "--no-model")
        self.assertIn("WHY_AUDIT_OK", out)

    def test_FR4_three_candidates_no_both_shape_set(self):
        self.issued(cands=(C1, C2, C3))
        a = self.turn_asks()[-1]
        self.assertEqual(a["shape"], "set")
        self.assertFalse(any(s.get("both") or s.get("both_slots") for s in a["shown"]))
        self.assertEqual(self.labels(a), [C1, C2, C3, OTHER, ESC])
        self.tap(C3)
        [n] = self.born().values()
        self.assertEqual((n["text"], n["born"]["slot"]), (C3, 3))


# ---------------------------------------------------------------------------
# FR5: "another reason" and the free-text paths
# ---------------------------------------------------------------------------

REASON = "\uc0ac\uc2e4 \ub2e4\uc74c \uc8fc \ub370\ubaa8\uac00 \uc81c\uc77c \uae09\ud574."


class Another(Base):
    def other_then(self, typed=REASON):
        otl, a, _, _ = self.issued()
        self.tap(OTHER)
        st = self.asks()[a["ask"]]
        self.assertEqual(st["state"], "awaiting")
        reason, _ = budget.topic_check(self.ctx, "tgt:" + TGT, self.asks(), {"tgt:" + TGT})
        self.assertNotEqual(reason, "topic-declined-7d")
        self.tick()
        self.ups(typed)
        return otl, a

    def test_FR5_other_awaits_then_the_typed_reason_binds(self):
        otl, a = self.other_then()
        p_other, p_free = self.probes()
        self.assertEqual((p_other["verdict"], p_other["outcome"]), ("free", "awaiting"))
        self.assertEqual((p_free["verdict"], p_free["outcome"], p_free["verbatim"]),
                         ("free", "answered", REASON))
        self.assertEqual(p_free["otl"], self.last_otl())
        self.assertNotEqual(p_free["otl"], p_other["otl"])
        st = self.asks()[a["ask"]]
        self.assertEqual(st["state"], "answered")
        self.assertNotIn("declined", st["events"])
        self.assertEqual(store.read_rows(store.EVENTS), [])

    def test_FR5_typed_other_label_awaits_like_the_tap(self):
        _otl, a, _, _ = self.issued()
        self.tick()
        self.ups(OTHER)                                  # typed, not tapped
        self.assertEqual(self.asks()[a["ask"]]["state"], "awaiting")
        self.assertEqual(self.probes()[-1]["verdict"], "free")

    def test_FR5a_path_a_owner_words(self):
        otl, a = self.other_then()
        pid = self.probes()[-1]["id"]
        rc, out, err = self.answer(a["ask"], pid, "--quote", REASON)
        self.assertEqual(rc, 0, out + err)
        [n] = self.born().values()
        self.assertEqual((n["text"], n["worded_by"], n["status"]), (REASON, "owner", N.PROVISIONAL))
        self.assertEqual([e["role"] for e in n["evidence"]], ["frame", "answer"])
        [ans] = [r for r in self.frames() if r["ev"] == "answer"]
        self.assertEqual((ans["path"], ans["node"]), ("a", n["id"]))
        rc2, out2, _ = self.answer(a["ask"], pid, "--quote", REASON)   # idempotent
        self.assertEqual((rc2, out2), (0, out))
        self.assertEqual(len(self.born()), 1)

    def test_FR5b_path_b_agent_rewording_is_a_hypothesis(self):
        otl, a = self.other_then()
        p = self.probes()[-1]
        rc, out, err = self.answer(a["ask"], p["id"], "--reword", "\ub2e4\uc74c \uc8fc \ub370\ubaa8 \uc900\ube44")
        self.assertEqual(rc, 0, out + err)
        [n] = self.born().values()
        self.assertEqual((n["status"], n["worded_by"]), ("hypothesis", "agent"))
        self.assertIn({"otl": p["otl"], "role": "answer", "span": None}, n["evidence"])
        self.assertEqual(n["evidence"][0]["otl"], otl)

    def test_FR5c_path_c_writes_no_node(self):
        _otl, a = self.other_then()
        pid = self.probes()[-1]["id"]
        rc, out, _ = self.answer(a["ask"], pid, "--none")
        self.assertEqual(rc, 0)
        self.assertEqual(self.born(), {})
        [ans] = [r for r in self.frames() if r["ev"] == "answer"]
        self.assertEqual((ans["path"], ans["node"]), ("c", None))

    def test_FR5d_quote_from_another_turn_is_refused(self):
        _otl, a = self.other_then()
        pid = self.probes()[-1]["id"]
        n = len(store.read_rows(store.EVENTS))
        rc, out, _ = self.answer(a["ask"], pid, "--quote", PRIORITY)
        self.assertEqual(rc, 3)
        self.assertIn("reason=answer-turn-mismatch", out)
        self.assertEqual(len(store.read_rows(store.EVENTS)), n)
        self.assertEqual(self.frames(), [])


# ---------------------------------------------------------------------------
# FR6-FR8: escape, pick, provisional held
# ---------------------------------------------------------------------------

class Escape(Base):
    def surfaces_now(self):
        rc, gate_out = self.cli("gate", "--work", "DGN-777", "--before", "irreversible")
        return (surfaces.head_lines(self.root, None, [], None),
                ask.work_whys(self.ctx, "DGN-777"), (rc, gate_out),
                retro.section(self.ctx))

    def test_FR6_skip_proceed_declines_and_changes_no_surface(self):
        before = self.surfaces_now()
        _otl, a, _, _ = self.issued()
        self.tap(ESC)
        st = self.asks()[a["ask"]]
        self.assertEqual(st["state"], "declined")
        self.assertEqual(self.evs("propose")[1:], [])
        self.assertEqual(self.surfaces_now(), before)

    def test_FR6b_new_wording_same_target_is_topic_declined(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.issued()
        self.tap(ESC)
        self.tick(31 * MIN)
        self.turn(PRIORITY)
        self.retired_frame(cands=("Different wording one", "Different wording two"))
        self.assertEqual(len(self.turn_asks()), 1)


class Pick(Base):
    def test_FR7_pick_over_two_hypotheses_keeps_the_other(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        w1 = events.propose(C1, "kit:kit_dev", work="DGN-701", target=TGT)["why"]
        w2 = events.propose(C2, "kit:kit_dev", work="DGN-701", target=TGT)["why"]
        self.turn(PRIORITY)
        self.retired_frame(mode="pick", cands=(w1, w2), work="DGN-701")
        self.assertEqual([self.node(w)["status"] for w in (w1, w2)], ["hypothesis", "hypothesis"])

    def test_FR7b_pick_pulls_a_matching_queued_ask_inline(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        from legacy_why_fixtures import historical_queue
        w1, w2 = self.why(C1), self.why(C2)
        q = historical_queue(self.ctx, "why", "new-work", "DGN-705", [w1, w2], work="DGN-705")[0]
        self.turn(PRIORITY)
        self.retired_frame(mode="pick", cands=(w1, w2), work="DGN-705")
        self.assertEqual(self.asks()[q]["state"], "queued")

    def test_turn_trigger_is_not_an_ask_or_enqueue_trigger(self):
        # (a) v4 section 2: even the retired trigger cannot expose a question.
        self.assertNotIn("turn", derive.PRIORITY)
        self.assert_retired("ask", "--trigger", "turn", "--topic", "DGN-1", "--shape", "contrast")
        self.assert_retired("enqueue", "--trigger", "turn", "--topic", "DGN-1", "--cand", C1)

    def test_FR8_provisional_held_gives_proceed(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        w, other = self.why(C1), self.why(C2)
        self.provisional(w, other)
        self.turn(PRIORITY)
        self.retired_frame(mode="pick", cands=(w, other), work="DGN-702")
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.node(w)["status"], N.PROVISIONAL)


# ---------------------------------------------------------------------------
# FR9-FR10: idempotency, refusals, the transition
# ---------------------------------------------------------------------------

class Replay(Base):
    def test_FR9_same_call_twice_same_bytes_no_budget_recheck(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.turn(PRIORITY)
        self.retired_frame()
        self.tick(45 * MIN)
        self.retired_frame()
        self.assertEqual(self.turn_asks(), [])

    def refused(self):
        self.issued()                                   # the 30-min gap starts
        self.turn("\uadf8\ub9ac\uace0 \uc9d1 \uc77c\ub3c4")
        self.tick(5)
        return self.infer(cands=(C3, "\uc9d1 \uc815\ub9ac"), target=TGT2)

    def test_FR10_budget_refusal_create_refused_no_queue_no_events(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.write_conf("WHY_ASK_INLINE_DAY=0\n")
        self.turn(PRIORITY)
        self.retired_frame()
        self.assertEqual(self.turn_asks(), [])
        self.assertEqual(self.frames(), [])

    def test_FR10b_replay_after_refusal_reprints_and_writes_nothing(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.turn(PRIORITY)
        self.retired_frame()
        self.tick(45 * MIN)
        self.write_conf("WHY_ASK_INLINE_DAY=5\n")
        self.retired_frame()
        self.assertEqual(self.turn_asks(), [])

    def test_FR10c_lost_transition_is_healed_by_the_replay(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        import turn_frame as tfl
        os.environ["DOGANY_TURN_FRAME"] = "on"
        self.turn(PRIORITY)
        self.retired_frame()
        self.retired_frame()
        rows = tfl.read_rows(tfl.store_path(self.root, SID))
        self.assertEqual([r for r in rows if r.get("ev") == "why"], [])


# ---------------------------------------------------------------------------
# FR11: locks
# ---------------------------------------------------------------------------

class Locks(Base):
    def ups_main(self, prompt, session=SID):
        """The real hook entry (bounded 1.5 s wait, skip on StoreBusy)."""
        self.rid += 1
        with open(os.path.join(self.ctx.bot_dir, "inbound-context.json"), "w") as fh:
            json.dump({"schema": 1, "request_id": "req-%d" % self.rid, "source": "message",
                       "message_id": None, "chat_id": CHAT,
                       "text_sha": why_owner_turn.turn_sha(prompt), "ts": self.ctx.now}, fh)
        payload = json.dumps({"prompt": prompt, "session_id": session,
                              "transcript_path": str(self.transcript(session))})
        ups.main(self.ctx, stdin=io.StringIO(payload))
        self._entry(session, {"type": "user", "userType": "external", "uuid": "u-%d" % self.rid,
                              "message": {"role": "user", "content": prompt}})

    def test_FR11a_frame_hold_does_not_starve_ups(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        from unittest.mock import patch
        self.turn(PRIORITY)
        # A retired command must return before lock acquisition or transcript reads.
        with patch.object(F, "_bounded_lock", side_effect=AssertionError("retired command acquired a lock")), patch.object(why_owner_turn, "resolve", side_effect=AssertionError("retired command resolved owner text")):
            self.retired_frame()
        self.assertEqual(self.turn_asks(), [])

    def test_FR11a_u3_hold_does_not_block_frame(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        from unittest.mock import patch
        self.turn(PRIORITY)
        # A retired command must return before lock acquisition or transcript reads.
        with patch.object(F, "_bounded_lock", side_effect=AssertionError("retired command acquired a lock")), patch.object(why_owner_turn, "resolve", side_effect=AssertionError("retired command resolved owner text")):
            self.retired_frame()
        self.assertEqual(self.turn_asks(), [])

    def test_FR11b_no_resolver_or_transcript_read_in_F1(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        from unittest.mock import patch
        self.turn(PRIORITY)
        # A retired command must return before lock acquisition or transcript reads.
        with patch.object(F, "_bounded_lock", side_effect=AssertionError("retired command acquired a lock")), patch.object(why_owner_turn, "resolve", side_effect=AssertionError("retired command resolved owner text")):
            self.retired_frame()
        self.assertEqual(self.turn_asks(), [])

    def test_FR11c_lock_held_elsewhere_store_busy(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        from unittest.mock import patch
        self.turn(PRIORITY)
        # A retired command must return before lock acquisition or transcript reads.
        with patch.object(F, "_bounded_lock", side_effect=AssertionError("retired command acquired a lock")), patch.object(why_owner_turn, "resolve", side_effect=AssertionError("retired command resolved owner text")):
            self.retired_frame()
        self.assertEqual(self.turn_asks(), [])

    def test_FR11d_path_a_resolves_with_the_lock_free(self):
        otl, a = Another.other_then(self)
        pid = self.probes()[-1]["id"]
        seen = []
        real = why_owner_turn.resolve

        def spy(*a, **k):
            fd = os.open(store.path(".lock"), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                seen.append("free")
            except BlockingIOError:
                seen.append("held")
            finally:
                os.close(fd)
            return real(*a, **k)
        why_owner_turn.resolve = spy
        try:
            rc, out, err = self.answer(a["ask"], pid, "--quote", REASON)
        finally:
            why_owner_turn.resolve = real
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(seen, ["free"])

    def test_FR11e_no_nested_lock_on_any_path(self):
        depth, worst = [0], [0]
        enter, leave = store._Lock.__enter__, store._Lock.__exit__

        def e(self_):
            depth[0] += 1
            worst[0] = max(worst[0], depth[0])
            try:
                return enter(self_)
            except BaseException:
                depth[0] -= 1
                raise

        def x(self_, *a):
            depth[0] -= 1
            return leave(self_, *a)
        store._Lock.__enter__, store._Lock.__exit__ = e, x
        try:
            otl, a = Another.other_then(self)                  # F, U1/U3
            pid = self.probes()[-1]["id"]
            self.assertEqual(self.answer(a["ask"], pid, "--quote", REASON)[0], 0)   # A0/A1
            self.tick(31 * MIN)
            self.issued(target=TGT2, cands=(C3, "\uc9d1 \uc815\ub9ac"))
            self.tap(BOTH)                                      # U3 both
        finally:
            store._Lock.__enter__, store._Lock.__exit__ = enter, leave
        self.assertEqual(worst[0], 1)
        self.assertEqual(len(self.born()), 3)


# ---------------------------------------------------------------------------
# FR12: the regression pin (no turn asks: every consumer byte-equal)
# ---------------------------------------------------------------------------

def fr12_scenario(t, raw=False):
    """A pre-AMEND-2 store (nodes, inline asks, a tap, corrections, a
    ticket) and the outputs of every node consumer AMEND-2 must not touch.
    Runs on the base checkout too (the golden writer): old APIs only."""
    w1 = t.why("\ubc30\ud3ec \uc18d\ub3c4", work="DGN-1301")
    w2 = t.why("\uc6b4\uc601 \uc548\uc815\uc131", work="DGN-1301")
    w3 = t.why("\uc0ac\uc6a9\uc790 \uacbd\ud5d8", work="DGN-1302")
    t.ticket("DGN-1302", why=w3)
    t.provisional(w1, w2)
    t.turn("\uc544\ub2c8 \uadf8\uac8c \uc544\ub2c8\ub77c DGN-1301 \ub85c\uc9c1\uc744 \uace0\uccd0\uc57c "
           "\ud55c\ub2e4\ub294 \uac70\uc57c")
    known = events.nodes()
    out = {}
    out["head"] = surfaces.head_lines(t.root, None,
                                      [os.path.join(t.root, "worklog", "DGN-1302-t.md")], None)
    out["work_whys"] = [ask.work_whys(t.ctx, w) for w in ("DGN-1301", "DGN-1302")]
    out["gate"] = [t.cli("gate", "--work", w, "--before", "irreversible")
                   for w in ("DGN-1301", "DGN-1302")]
    out["board"] = surfaces.board_counts(t.root)
    from why_net import corrections
    out["open_readings"] = [corrections.open_readings(w, known, t.root)
                            for w in ("DGN-1301", "DGN-1302")]
    out["tcorr"] = sorted((a.get("trigger"), a.get("topic"), a.get("state"))
                          for a in t.asks().values())
    out["retro_read"] = sorted(retro.current())
    out["statuses"] = sorted((w, n["status"]) for w, n in known.items())
    if raw:
        return out
    return {k: hashlib.sha256(json.dumps(v, ensure_ascii=False, sort_keys=True)
                              .encode("utf-8")).hexdigest() for k, v in out.items()}


class Pin(Base):
    def test_FR12_no_turn_asks_every_consumer_byte_equal(self):
        got = fr12_scenario(self, raw=True)
        with open(GOLDEN, encoding="utf-8") as fh:
            want = json.load(fh)["sha256"]
        self.assertEqual(set(got), set(want))
        # (a) FDGN-594 v4 section 5 retires gate solicitation and queues.
        # The old gate calls created two queued before-work asks, which
        # also changed the board's open-ask count. Pin the new raw values
        # explicitly; do not regenerate the unchanged historical golden.
        self.assertEqual(got["gate"], [(3, "signal::WHY_REFUSED reason=retired-use-four-slot-intake\n")] * 2)
        self.assertEqual(got["board"], dict(none=0, hypothesis=0, provisional=0,
                                            contested=0, asks=0, fold=[]))
        self.assertEqual(got["tcorr"], [("fork", "DGN-1001", "answered")])
        for key in sorted(set(want) - {"gate", "board", "tcorr"}):
            with self.subTest(consumer=key):
                digest = hashlib.sha256(json.dumps(got[key], ensure_ascii=False,
                                                  sort_keys=True).encode("utf-8")).hexdigest()
                self.assertEqual(digest, want[key])
        for work in ("DGN-1301", "DGN-1302"):
            self.assert_retired("gate", "--work", work, "--before", "irreversible")
        before = store.read_rows(store.ASKS)
        self.assertEqual(derive.run(self.ctx), 0)
        self.assertEqual(store.read_derived("queue.json"), [])
        from why_net import morning
        morning.run(self.ctx, sender=lambda *a, **kw: self.fail("retired morning sender called"),
                    out=io.StringIO())
        self.assertEqual(store.read_rows(store.ASKS), before)
        self.assertFalse(any(a.get("state") == "queued" for a in self.asks().values()))


# ---------------------------------------------------------------------------
# FR13-FR14: lookup, reserved slots
# ---------------------------------------------------------------------------

class Lookup(Base):
    def lookup(self, *args):
        rc, out, err = self.run_cli("lookup", *args)
        self.assertEqual(rc, 0, out + err)
        return json.loads(out)

    def tree(self):
        out = []
        for d, _dirs, files in os.walk(self.ddir):
            for f in files:
                p = os.path.join(d, f)
                out.append((p, os.stat(p).st_mtime_ns))
        return sorted(out)

    def test_FR13_lookup_off_on_and_writes_nothing(self):
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write("AGENT_LANG=ko\nWHY_NET=off\n")
        doc = self.lookup("--scope", "kit:kit_dev", "--json")
        self.assertEqual((doc["enabled"], doc["whys"], doc["open_asks"]), (False, [], []))
        self.write_conf("")
        events.propose(C1, "kit:kit_dev", target=TGT)
        before = self.tree()
        doc = self.lookup("--scope", "kit:kit_dev", "--target", TGT, "--json")
        self.assertEqual((doc["preference"], doc["state"], doc["metacog"]), (None, None, "unknown"))
        self.assertEqual([w["text"] for w in doc["whys"]], [C1])
        self.assertEqual(self.tree(), before)

    def test_FR13b_scope_filter_and_dispatch_worker_redaction(self):
        a = events.propose(C1, "kit:a")["why"]
        events.propose(C2, "kit:b")
        doc = self.lookup("--scope", "kit:a", "--json")
        self.assertEqual([w["id"] for w in doc["whys"]], [a])
        os.environ[why_owner_turn.DISPATCH_ENV_MARKER] = "1"
        try:
            doc = self.lookup("--scope", "kit:a", "--json")
        finally:
            os.environ.pop(why_owner_turn.DISPATCH_ENV_MARKER)
        self.assertEqual(doc["redacted"], "dispatch-worker")
        self.assertNotIn("text", doc["whys"][0])
        self.assertNotIn(C1, json.dumps(doc, ensure_ascii=False))

    def test_FR14_reserved_slot_refused(self):
        c = {"version": 1, "targets": [{"id": "t"}],
             "bindings": [{"slot": "preference", "target": "t", "mode": "none"}]}
        with self.assertRaises(base_contract.ContractError) as cm:
            base_contract.validate(c)
        self.assertIn("reserved for DGN-2120", str(cm.exception))
        c["bindings"][0]["slot"] = "state"
        with self.assertRaises(base_contract.ContractError):
            base_contract.validate(c)


# ---------------------------------------------------------------------------
# FR15: races
# ---------------------------------------------------------------------------

class Races(Base):
    def test_FR15i_two_sessions_race_the_day_cap(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.write_conf("WHY_ASK_INLINE_DAY=1\n")
        for sid in (SID, "sess-other"):
            self.turn(PRIORITY, session=sid)
            self.retired_frame()
        self.assertEqual(self.turn_asks(), [])

    def test_FR15ii_tap_after_an_intervening_turn_binds_by_carrier(self):
        self.turn(PRIORITY)
        self.tick(5)
        rc, out, _ = self.historical_infer()
        self.tick()
        self.ups("\uc544 \uadf8\ub9ac\uace0 \uc774\uac83\ub3c4")          # N+1 before the reply
        self.reply(out.rstrip("\n"))
        a = self.turn_asks()[-1]
        self.assertEqual(a["state"], "create")
        self.tap(C2)                                                        # N+2
        st = self.asks()[a["ask"]]
        self.assertIn("shown", st["events"])
        shown = [r for r in store.read_rows(store.ASKS) if r.get("ask") == a["ask"]
                 and r["ev"] == "shown"]
        self.assertEqual(shown[0]["by"], "ups-carrier")
        [n] = self.born().values()
        self.assertEqual(n["text"], C2)

    def test_FR15iii_second_frame_after_n1_binds_to_n1_and_is_refused(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.turn(PRIORITY)
        self.retired_frame()
        self.turn("One more request")
        self.retired_frame(cands=(C2, C3), target=TGT2)
        self.assertEqual(self.turn_asks(), [])


# ---------------------------------------------------------------------------
# FR16-FR18: confirm floors, voice / wrong bubble, double tap, crash repair
# ---------------------------------------------------------------------------

class Floors(Base):
    def test_FR16_confirm_quote_against_unchanged_candidate_refused(self):
        w = self.why(C1)
        self.turn("\uadf8\uac74 v2 \ud750\ub984\uc774 \uba3c\uc800\uc57c.")
        rc, out = self.cli("confirm", "--why", w, "--quote",
                           "\uadf8\uac74 v2 \ud750\ub984\uc774 \uba3c\uc800\uc57c.", "--session", SID)
        self.assertEqual(rc, 3)
        self.assertIn("reason=text-not-quote", out)

    def test_FR17_voice_one_and_wrong_bubble_write_no_events(self):
        _otl, a, _, _ = self.issued()
        self.tick()
        self.ups(C1, source="message", voice=True)       # a voice "1": Whisper text
        self.assertEqual(store.read_rows(store.EVENTS), [])
        self.tick(31 * MIN)
        self.issued(target=TGT2, cands=(C3, "\uc9d1 \uc815\ub9ac"))
        self.tap(C3, message_id=self.last_bubble - 1)   # the other bubble
        self.assertEqual(store.read_rows(store.EVENTS), [])

    def test_FR18_double_tap_one_born_node(self):
        self.issued()
        self.tap(C1)
        self.tap(C1)
        self.assertEqual(len(self.born()), 1)
        self.assertEqual(len(self.evs("confirm")), 1)

    def crash_tap(self):
        self.issued()
        real = events.confirm_tap_unlocked
        events.confirm_tap_unlocked = lambda *a, **k: (_ for _ in ()).throw(
            events.WhyRefusal("simulated-crash"))
        try:
            self.tap(C1)
        finally:
            events.confirm_tap_unlocked = real
        [n] = self.born().values()
        self.assertEqual((n["status"], n["confirm"]), ("hypothesis", None))
        return n

    def test_FR18b_derive_confirms_a_born_node_once(self):
        n = self.crash_tap()
        self.derive()
        self.assertEqual(self.node(n["id"])["status"], N.PROVISIONAL)
        self.derive()
        self.assertEqual(len(self.evs("confirm")), 1)

    def test_FR18b_unbackable_birth_is_retired_and_audited(self):
        n = self.crash_tap()
        real = why_owner_turn.resolve_prompt
        why_owner_turn.resolve_prompt = lambda *a, **k: why_owner_turn.Refusal("turn-off-chain")
        try:
            self.derive()
        finally:
            why_owner_turn.resolve_prompt = real
        node = self.node(n["id"])
        self.assertEqual((node["status"], node.get("end_reason")), ("retired", "born-unbacked"))
        [r] = self.evs("retire")
        self.assertEqual(r["actor"], "derive")
        rc, out = self.cli("derive", "--audit", "--no-model")
        self.assertIn("reason=born-unbacked", out)


# ---------------------------------------------------------------------------
# FR19-FR20: flags, frame identity
# ---------------------------------------------------------------------------

class Flags(Base):
    def test_FR19a_why_on_turn_frame_off(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        os.environ["DOGANY_TURN_FRAME"] = "off"
        self.turn(PRIORITY)
        self.retired_frame(extra=("--stakes", "high"))
        self.assert_retired("gate", "--target", TGT, "--before", "irreversible")

    def test_FR19b_why_off_turn_frame_on_disposition(self):
        import turn_frame as tfl
        os.environ["DOGANY_TURN_FRAME"] = "on"
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write("AGENT_LANG=ko\nWHY_NET=off\n")
        rc, out, _ = self.infer()
        self.assertEqual(rc, 4)
        self.assertIn("reason=off", out)
        stop = _load("_amend2_tfstop", os.path.join(ROUTINES, "turn-frame.py"))
        path = tfl.store_path(self.root, SID)
        tfl.append_row(path, {"v": 1, "ev": "issued", "frame_id": "tf-feed", "request_id": "r",
                              "stakes": "high", "prompt_sha": tfl._sha("p"), "ts": time.time()})
        frame = tfl.current_frame(tfl.read_rows(path), "p", None)
        self.assertEqual([f["check"] for f in tfl.stop_findings(frame, "ok", [], "p", False)],
                         ["S1"])
        buf = io.StringIO()
        self.assertEqual(stop.disposition(["--frame-id", "tf-feed", "--value", "proceed",
                                           "--session", SID], root=self.root, out=buf), 0)
        frame = tfl.current_frame(tfl.read_rows(path), "p", None)
        self.assertEqual(tfl.stop_findings(frame, "ok", [], "p", False), [])
        self.assertEqual(stop.disposition(["--frame-id", "tf-nope", "--value", "proceed",
                                           "--session", SID], root=self.root,
                                          out=io.StringIO()), 3)

    def test_FR20_otl_validation_and_gate_by_target(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        first = self.turn(PRIORITY)
        self.turn("One more owner turn")
        for otl in (first, self.last_otl(), "otl-unknown"):
            self.retired_frame(extra=("--otl", otl))
        self.assert_retired("gate", "--target", TGT, "--before", "trial")


# ---------------------------------------------------------------------------
# FR21-FR23: cause, metacog reset, gate, escalation
# ---------------------------------------------------------------------------

class Cause(Base):
    def test_FR21_cause_required_metacog_counts_owner_revision_reset(self):
        w1 = events.propose(C1, "kit:kit_dev", work="DGN-703", target=TGT)["why"]
        events.propose(C2, "kit:kit_dev", work="DGN-703", target=TGT)
        self.turn(PRIORITY)
        self.tick(5)
        rc, out, _ = self.historical_infer(cands=(w1, ask.work_whys(self.ctx, "DGN-703")[1]), extra=("--work", "DGN-703"))
        self.reply(out.rstrip("\n"))
        self.tap(C1)
        pid = self.probes()[-1]["id"]
        rc, out = self.cli("revise", "--why", w1, "--text", "v2 \ub9cc", "--probe", pid)
        self.assertEqual(rc, 2)
        self.assertIn("reason=cause-required", out)
        rc, out = self.cli("revise", "--why", w1, "--text", "v2 \ub9cc", "--probe", pid,
                           "--cause", "agent-misread")
        self.assertEqual(rc, 0, out)
        eps = MC.episodes()[TGT]
        self.assertFalse(any(e["rev"] for e in eps.values()))     # MC5
        rc, out = self.cli("revise", "--why", w1, "--text", "v2 \uc804\ubd80", "--probe", pid,
                           "--cause", "owner-revision")
        self.assertEqual(rc, 0, out)
        self.assertTrue(any(e["rev"] for e in MC.episodes()[TGT].values()))
        self.tick(DAY)
        self.turn()
        rc, out = self.cli("metacog-reset", "--target", TGT)
        self.assertEqual(rc, 0, out)
        rec = MC.classify(MC.episodes()[TGT], self.ctx.now, MC.resets()[TGT])
        self.assertEqual(rec["episodes"], 0)                      # MC7
        os.environ[why_owner_turn.DISPATCH_ENV_MARKER] = "1"
        try:
            rc, out = self.cli("metacog-reset", "--target", TGT)
        finally:
            os.environ.pop(why_owner_turn.DISPATCH_ENV_MARKER)
        self.assertEqual(rc, 3)
        self.assertIn("reason=dispatch-worker", out)


class Gate(Base):
    def gate(self, *args):
        return self.cli("gate", "--before", "irreversible", *args)

    def test_FR22a_high_stakes_not_shown_is_undecided(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.issued(extra=("--stakes", "high"), send=False)
        self.assert_retired("gate", "--target", TGT, "--before", "irreversible")
        self.assertEqual(len(self.turn_asks()), 1)

    def test_FR22b_high_stakes_no_frame_call_is_undecided(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.turn(PRIORITY)
        self.assert_retired("gate", "--target", TGT, "--before", "irreversible")
        self.assertEqual(self.turn_asks(), [])

    def test_FR22c_skip_proceed_is_owner_deferred(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.issued(extra=("--stakes", "high"))
        self.tap(ESC)
        self.assert_retired("gate", "--target", TGT, "--before", "irreversible")
        self.assertEqual(self.turn_asks()[-1]["state"], "declined")

    def test_FR22d_answered_gives_todays_logic_byte_equal(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.issued(extra=("--stakes", "high", "--work", "DGN-704"))
        self.tap(C1)
        self.assert_retired("gate", "--work", "DGN-704", "--before", "trial")
        self.assertEqual(self.turn_asks()[-1]["state"], "answered")

    def test_FR22e_open_ask_of_this_otl_is_frame_pick(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.issued()
        self.assert_retired("gate", "--target", TGT, "--before", "irreversible")
        self.assertEqual(self.turn_asks()[-1]["state"], "create")


class Escalate(Base):
    def test_FR23_one_escalation_then_replay_with_differs(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        self.turn(PRIORITY)
        self.retired_frame(mode="proceed")
        self.retired_frame(mode="infer")
        self.retired_frame(mode="proceed", target=TGT2)
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.turn_asks(), [])


# ---------------------------------------------------------------------------
# FR24-FR28: retro migration, erase, voice / question, re-offer, expiry
# ---------------------------------------------------------------------------

class Rest(Base):
    def correction(self, cid, text, work):
        store.append_row(store.CORRECTIONS, {"id": cid, "said_at": store.iso_at(self.t),
                                             "text": text, "session_id": SID, "work": work,
                                             "otl": self.last_otl()})

    def test_FR24_retro_view_only_migration_once(self):
        w = self.why(C1)
        self.turn()
        self.correction("cor-0001", "\uc544\ub2c8\uc57c", "DGN-901")
        self.cli("read", "--signal", "cor-0001", "--why", w)
        from legacy_why_fixtures import historical_queue
        self.assertIsNone(retro.readings_bubble(self.ctx))
        rid = historical_queue(self.ctx, "why", "readings", "DGN-901", [], shape="scatter")[0]
        self.assertEqual(retro.migrate_view_only(self.ctx), [rid])
        a = self.asks()[rid]
        self.assertEqual((a["state"], a["reason"]), ("ignored", "retro-view-only"))
        self.assertEqual(retro.migrate_view_only(self.ctx), [])

    def test_FR25_erase_drops_assumption_text_chain_valid(self):
        self.turn(PRIORITY)
        self.assert_retired("frame", "--mode", "proceed", "--target", TGT, "--assume", "\uc9d0\uc791 \ud558\ub098")
        store.append_row(store.FRAMES, dict(ev="proceed", otl=self.last_otl(), target=TGT,
            assumption="\uc9d0\uc791 \ud558\ub098", at=store.iso_at(self.ctx.now)))
        self.tick(31 * MIN)
        Another.other_then(self)
        a = self.turn_asks()[-1]
        pid = self.probes()[-1]["id"]
        self.assertEqual(self.answer(a["ask"], pid, "--none")[0], 0)
        rc, out = self.cli("erase-frames", "--target", TGT)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("\uc9d0\uc791", json.dumps(self.frames(), ensure_ascii=False))
        self.assertEqual([r["ev"] for r in self.frames()], ["proceed"])
        self.assertTrue(all(c["text"] is None for c in self.turn_asks()[-1]["cands"]))
        self.assertEqual(store.audit(), [])
        self.assertEqual(self.evs("act-void")[-1]["data"]["reason"], "owner-erase-target")

    def test_FR26_voice_and_question_turns_issue_span_audited(self):
        otl = self.turn("\ucd5c\uc6b0\uc120\uc73c\ub85c \ud574\uc918", voice=True)
        self.assertEqual(store.read_rows(store.TURN_LEDGER)[-1]["source"], "voice")
        self.tick(5)
        rc, out, err = self.historical_infer(extra=("--span", "\uc5c6\ub294 \ub9d0"))
        self.assertEqual(rc, 0, err)
        self.reply(out.rstrip("\n"))
        self.tap(C1)
        [n] = self.born().values()
        self.assertEqual(n["evidence"][0], {"otl": otl, "role": "frame",
                                            "span": "\uc5c6\ub294 \ub9d0"})
        rc, audit = self.cli("derive", "--audit", "--no-model")
        self.assertIn("reason=span-unbacked", audit)
        self.tick(31 * MIN)
        q = self.turn("\uc774\uac70 \uc65c \ud574\uc57c \ub3fc?")
        self.tick(5)
        rc, out, err = self.historical_infer(target=TGT2, cands=(C3, "\uc9d1 \uc815\ub9ac"),
                                  extra=("--span", "\uc65c \ud574\uc57c"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.turn_asks()[-1]["frame_otl"], q)
        spans = [u for u in events.audit_spans() if u["evt"] == self.turn_asks()[-1]["ask"]]
        self.assertEqual(spans, [])                               # in the turn: backed

    def test_FR27_reoffer(self):
        # (a) v4 sections 1 D2-D4 and 2: retired, never asks or enqueues.
        hypothesis = self.why(C1)
        w, other = self.why(C2), self.why("Other purpose")
        self.provisional(w, other)
        retired = self.why(C3)
        events.end("retire", retired)
        self.turn(PRIORITY)
        self.retired_frame(cands=(C1, C2, C3))
        self.assertEqual(self.node(hypothesis)["status"], "hypothesis")
        self.assertEqual(self.node(retired)["status"], "retired")

    def test_FR28_awaiting_expires_and_nothing_to_expire(self):
        _otl, a, _, _ = self.issued()
        self.tap(OTHER)
        self.tick(ups.IGNORE_AFTER + 60)
        self.derive()
        self.assertEqual(self.asks()[a["ask"]]["state"], "ignored")
        self.tick(30 * DAY)
        self.derive()
        self.assertEqual(events.nodes(), {})
        self.assertEqual(store.read_rows(store.EVENTS), [])


# ---------------------------------------------------------------------------
# copy gate (G5) and the WHY_NET-off contract (G1)
# ---------------------------------------------------------------------------

class CopyAndOff(Base):
    def test_G5_placeholder_copy_never_reaches_the_owner(self):
        for lang in ("ko", "en"):
            with open(os.path.join(ROUTINES, "..", "config", "i18n", lang + ".json"),
                      encoding="utf-8") as fh:
                cat = json.load(fh)
            for k in ask.COPY_KEYS_PENDING_PLACEHOLDER:
                self.assertEqual(cat[k], ask.PENDING_PREFIX + k, (lang, k))
        os.remove(os.path.join(self.root, "config", "i18n.local", "ko.json"))
        otl = self.turn(PRIORITY)
        self.tick(5)
        self.assert_retired("frame", "--mode", "infer", "--ambiguous", "--target", TGT, "--cand", C1, "--cand", C2)
        self.assertEqual(self.turn_asks(), [])
        self.assertEqual(self.frames(), [])

    def test_G1_why_off_frame_refuses_and_writes_nothing(self):
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write("AGENT_LANG=ko\nWHY_NET=off\n")
        before = self.file_bytes()
        for args in (("--mode", "infer", "--ambiguous", "--target", TGT, "--cand", C1,
                      "--cand", C2), ("--mode", "proceed")):
            rc, out, err = self.frame(*args)
            self.assertEqual((rc, out.strip()), (4, "signal::WHY_REFUSED reason=off"))
        self.assertEqual(self.run_cli("metacog-reset", "--target", TGT)[0], 4)
        self.assertEqual(self.file_bytes(), before)

    def test_console_shows_target_evidence_born_and_frames(self):
        from why_net import render
        self.issued()
        self.tap(C1)
        self.assert_retired("frame", "--mode", "proceed")
        store.append_row(store.FRAMES, dict(ev="proceed", otl=self.last_otl(), target=TGT,
            assumption="historical assumption", at=store.iso_at(self.ctx.now)))
        render.write()
        with open(store.path("why-net.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("  - target: %s" % TGT, md)
        self.assertIn("  - evidence: 2 born=%s/2" % self.turn_asks()[-1]["ask"], md)
        rc, out = self.cli("status")
        self.assertEqual(json.loads(out)["frames"], 1)

    def test_propose_target_validated_scope_defaults(self):
        rc, out = self.cli("propose", "--text", C1, "--target", TGT)
        self.assertEqual(rc, 0, out)
        w = out.split("why=")[1].strip()
        self.assertEqual((self.node(w)["target"], self.node(w)["scope"]), (TGT, "kit:kit_dev"))
        for target, scope, reason in (("devkit/nope", None, "unknown-target"),
                                      ("base/cpu", None, "unknown-target"),
                                      ("devkit", None, "unknown-target"),
                                      (TGT, "framework", "scope-target-mismatch")):
            args = ["propose", "--text", C2, "--target", target]
            if scope:
                args += ["--scope", scope]
            rc, out = self.cli(*args)
            self.assertEqual((rc, "reason=" + reason in out), (3, True), (target, out))
        self.assertEqual(budget.canonical_topic("tgt:" + TGT), "tgt:" + TGT)
        self.assertIsNone(budget.canonical_topic("tgt:base:cpu"))

    def test_cli_act_kinds_pin(self):
        from why_net import acts
        self.assertEqual(cli.ACT_KINDS, acts.KINDS)   # the lazy CLI's copy

    def test_unanswered_propose_detect_only(self):
        self.issued()
        self.tick()
        w = events.propose("\uadf8\ub0e5 \uc9d0\uc791", "framework")["why"]
        rc, out = self.cli("derive", "--audit", "--no-model")
        self.assertEqual(rc, 0)
        self.assertIn("signal::WHY_AUDIT_UNANSWERED_PROPOSE why=%s" % w, out)


def write_golden():
    """Pin FR12 from the checkout this file runs in (the pre-AMEND-2 base)."""
    t = Pin("test_FR12_no_turn_asks_every_consumer_byte_equal")
    Base.setUp = AM.Base.setUp       # the base has no contract / copy hooks
    Base.tearDown = AM.Base.tearDown
    t.setUp()
    try:
        got = fr12_scenario(t)
    finally:
        t.tearDown()
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8") as fh:
        json.dump({"what": "AMEND-2 FR12: consumer outputs with no turn asks, "
                           "pinned on the pre-AMEND-2 base", "sha256": got}, fh, indent=1)
        fh.write("\n")
    print("wrote", GOLDEN)


if __name__ == "__main__":
    if "--write-golden" in sys.argv:
        write_golden()
        sys.exit(0)
    unittest.main(verbosity=1)
