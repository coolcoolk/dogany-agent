#!/usr/bin/env python3
# test-lane: fast
# test-component: host-dispatcher
# test-tier: 1
# test-prereq: fixture-repo, tmp-files
# promise: Host enqueue transfers a legacy reservation under the barrier; late enabled admission and publication refuse.
# promise: alternating writers with different monotonic bases never advance the awake clock beyond wall time (DGN-2142)
# promise: a ticket queued 9 minutes is not expired, whatever a foreign writer left in clock.json (DGN-2142)
# promise: a pinned/test clock can never write the real dispatcher state dir (DGN-2142)
# promise: when room opens (any drain, not only an enqueue) the best-scored waiting ticket is granted first
# promise: a later ticket never takes a lane slot ahead of an older equal-tier ticket still waiting on that lane
# promise: an llm-bound run is not blocked by load5 where a build/test run is
# promise: the swap and memory ceilings close the host to every run kind
# promise: a ticket that expires is never silent: the agent's session-inbox gets a notice and the closed record a reason
# promise: a refusal raised inside a drained run keeps the drained ticket id and frees its lease, so N drain cycles leave at most one ticket (DGN-2218)
# promise: an identical (agent, label, brief digest) request is merged into the queued ticket, never a second one (DGN-2218)
# promise: a request refused more than MAX_REFUSALS times is closed, not requeued forever (DGN-2218)
# promise: a completed empty pid receipt of a dead reservation holder is reclaimed and frees the slot (M1-3 N3)
# promise: owner-tier work is re-queued with a boosted score instead of expiring, up to a bounded number of windows
# Reapers, parallel-acquire and shell queue-fanout cases: test-host-dispatcher-reapers.py.
"""DGN-1979 phase 1: one host dispatcher -- soft priority queue, escalation,
machine pressure.

Owner 2026-10-10: every order goes through ONE ordering machine; the queue is
not FIFO but a soft priority the machine scores (aging, owner waiting,
deadline, agent bump with a reason, starvation guard). Offline: temp HOME,
temp DOGANY_SHARED_HOME, a pinned machine reading, pinned clocks, live
`sleep` processes as lease holders, fixture credit books.
Covers:
  - ordering by tier, aging inside a tier, the fairness term (25 vs 1);
  - two agents, one lane: the host cap holds across agents; a fresh request
    never jumps a queued ticket that outranks it; 20 parallel acquires from
    3 agents never exceed the cap;
  - bump: reason checks, owner_waiting verified / unverified / stale,
    deadline window, blocks_release marker-bound, 4th bump refused and
    logged, the charge lands before the effect, never reaches pin, only the
    owning agent; owner pin newest on top with a reservation;
  - starvation guard: 5 skips -> one tier up; second trip -> reservation;
    90 awake minutes -> trip; sleep (monotonic still, wall +8h) -> no mass
    expiry, no trip;
  - machine term: young leases charged, swap under load -> no room, a young
    heavy-test slot charged, a waiting release cut holds new local leases;
  - heavy-test order: release before push, older first within a class, a
    dead waiter ignored;
  - reap: dead holder, recycled pid without its marker, unbound starting
    lease past 120 s (requeued once), drained worker never inherits the lock;
  - api books (DGN-1978 r3 ledger): two agents on one account are paced off
    the SHARED ledger, a granted-not-yet-flying run is charged provisionally;
  - degraded: an unwritable state dir -> `unavailable` (exit 3);
  - reapers: scratch roots of dead runners, finished clean worktrees only.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
HD_PY = os.path.join(LIB, "host_dispatcher.py")
sys.path.insert(0, LIB)
for _key in list(os.environ):
    if _key.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
        os.environ.pop(_key)
import host_dispatcher as hd  # noqa: E402

NOW = 1791600000.0


def _rm_fixture(path):
    """DGN-2090: remove THIS test's own fixture dir. The terminal-state ledger
    it wrote is chflags uappend (macOS), which makes a bare rmtree leave the
    dir behind; clear the flags on this path only, then remove it."""
    if shutil.which("chflags"):
        subprocess.run(["chflags", "-R", "nouappend,nouchg", path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(path, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        # drained stub owners run detached; their Popen objects are never waited
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = tempfile.mkdtemp(prefix="dgn1979-")
        self.env_saved = dict(os.environ)
        os.environ["HOME"] = os.path.join(self.tmp, "home")
        os.makedirs(os.environ["HOME"])
        os.environ["DOGANY_SHARED_HOME"] = os.path.join(self.tmp, "shared")
        os.environ["DOGANY_DISPATCH_MACHINE"] = "1 8 32000 0"
        os.environ["DOGANY_DISPATCH_BOOT"] = "boot-1"
        self.clock(0)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            with contextlib.suppress(Exception):
                p.kill()
                p.wait()
        os.environ.clear()
        os.environ.update(self.env_saved)
        _rm_fixture(self.tmp)

    def clock(self, mono, wall=None):
        os.environ["DOGANY_DISPATCH_MONO"] = str(1000.0 + mono)
        os.environ["DOGANY_DISPATCH_NOW"] = str(NOW + mono if wall is None else wall)

    def holder(self):
        p = subprocess.Popen(["sleep", "300"])
        self.procs.append(p)
        return p.pid

    def run_hd(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hd.main([str(a) for a in args])
        return rc, out.getvalue().rstrip("\n").split("\t")

    def test_codex_cap_refresh_updates_local_and_cloud_lanes(self):
        stale = {"codex-cloud@acct": {"cap": 2, "note": "stale", "at": NOW - 60}}
        hd.write_json(hd._p("lanes.json"), stale)

        rc, result = self.run_hd("refresh", "--runtime", "codex", "--account", "acct",
                                 "--cap", "16", "--cap-note", "weekly refill")

        self.assertEqual(rc, 0)
        self.assertEqual(result[0], "refreshed")
        lanes = hd.read_json(hd._p("lanes.json"))
        self.assertEqual(lanes["codex@acct"]["cap"], 16)
        self.assertEqual(lanes["codex-cloud@acct"]["cap"], 16)
        self.assertEqual(lanes["codex-cloud@acct"]["note"], "weekly refill")

    def acquire(self, agent, cap=1, runtime="claude-code", cls="background", pid=None, **kw):
        args = ["acquire", "--agent", agent, "--runtime", runtime, "--cap", cap,
                "--class", cls, "--pid", pid or self.holder()]
        for k, v in kw.items():
            args += ["--" + k.replace("_", "-"), v]
        return self.run_hd(*args)

    def enqueue(self, agent, cls="background", runtime="claude-code", cap=1, script="/bin/true", extra=()):
        f = os.path.join(self.tmp, "argv.json")
        with open(f, "w") as fh:
            json.dump({"argv": ["--task", "x"], "env": {}}, fh)
        rc, out = self.run_hd("enqueue", "--agent", agent, "--runtime", runtime, "--cap", cap,
                              "--class", cls, "--argv-file", f, "--script", script, *extra)
        self.assertEqual(out[0], "queued", out)
        return out[1]

    def tickets(self):
        return hd.load_state()["tickets"]

    def leases(self):
        return hd.load_state()["leases"]

    def decisions(self, kind=None):
        rows = hd.read_lines(hd._p("decisions.jsonl"))
        return [r for r in rows if kind is None or r["kind"] == kind]

    def free_slot(self, lease_id):
        return self.run_hd("release", "--lease", lease_id)

    def granted_tickets(self):
        return [r.get("ticket") for r in self.decisions("grant") if r.get("via") == "drain"]


class TicketlessBranch(Base):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", "-b", "fixture", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "core.hooksPath=/dev/null",
                        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-qm", "fixture", "--allow-empty"], check=True)
        self.target = dict(result_repo=self.repo, result_branch="dsp/same")
        self.spawn = mock.patch.object(hd, "spawn", return_value=True)
        self.spawn.start()
        self.addCleanup(self.spawn.stop)

    def test_live_branch_refusal_names_existing_run_across_agents_and_runtimes(self):
        _, first = self.acquire("A", cap=16, runid="existing-run", **self.target)
        _, second = self.acquire("B", cap=16, runtime="codex", **self.target)
        self.assertEqual("granted", first[0])
        self.assertEqual("duplicate", second[0])
        self.assertEqual("existing-run", second[1])
        self.assertIn("dispatch-detached.sh --list", second[2])
        self.assertEqual(1, len(self.leases()))
        self.free_slot(first[1])
        _, resumed = self.acquire("B", cap=16, **self.target)
        self.assertEqual("granted", resumed[0])

    def test_linked_worktrees_share_identity(self):
        linked = os.path.join(self.tmp, "linked")
        subprocess.run(["git", "-C", self.repo, "-c", "core.hooksPath=/dev/null",
                        "worktree", "add", "-q", "--detach", linked], check=True)
        self.acquire("A", cap=16, **self.target)
        _, result = self.acquire("B", cap=16, result_repo=linked, result_branch="dsp/same")
        self.assertEqual("duplicate", result[0])
        self.assertIn("follow existing run/request", result[2])

    def test_other_branch_and_other_repo_still_allowed(self):
        self.acquire("A", cap=16, **self.target)
        _, branch = self.acquire("B", cap=16, result_repo=self.repo, result_branch="dsp/other")
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "init", "-q", "-b", "fixture", other], check=True)
        _, repo = self.acquire("C", cap=16, result_repo=other, result_branch="dsp/same")
        self.assertEqual("granted", branch[0])
        self.assertEqual("granted", repo[0])

    def test_script_command_arguments_do_not_claim_a_result_branch(self):
        self.assertIsNone(hd.request_result(dict(workspace=self.repo,
            argv=['--script', '--label', 'fixture', '--', 'tool', '--worktree', 'command-argument'])))
        self.assertEqual((self.repo, 'dsp/last'), hd.request_result(dict(workspace=self.repo,
            argv=['--worktree', 'first', '--worktree', 'last'])))

    def test_queue_refusal_points_to_existing_request(self):
        tid = self.enqueue("A", cap=0, extra=("--result-repo", self.repo,
                                              "--result-branch", "dsp/same"))
        argv = os.path.join(self.tmp, "argv.json")
        rc, second = self.run_hd("enqueue", "--agent", "B", "--runtime", "codex", "--cap", 0,
                                 "--argv-file", argv, "--result-repo", self.repo,
                                 "--result-branch", "dsp/same")
        self.assertEqual(0, rc)
        self.assertEqual(["duplicate", tid], second[:2])
        self.assertIn("--list", second[2])
        self.assertEqual(1, len(self.tickets()))

    def test_split_attachment_digest_matches_merged_prompt_and_changes_with_evidence(self):
        from pathlib import Path
        brief = Path(self.tmp, 'brief.md')
        evidence = Path(self.tmp, 'evidence.md')
        merged = Path(self.tmp, 'merged.md')
        brief.write_text('Replay the fixture.\n')
        evidence.write_text('old evidence\n')
        merged.write_text('Replay the fixture.\n\n\n--- attached file: evidence.md (13 bytes; inlined at dispatch -- the original path is not readable from the worker) ---\nold evidence\n\n--- end attached file: evidence.md ---\n')
        split = hd.prompt_digest(['--task', str(brief), '--attach', str(evidence)])
        self.assertEqual(hd.prompt_digest(['--task', str(merged)]), split)
        evidence.write_text('different evidence\n')
        self.assertNotEqual(split, hd.prompt_digest(['--task', str(brief), '--attach', str(evidence)]))

    def test_queue_drain_reserves_branch_once_and_replay_adopts_it(self):
        tid = self.enqueue("A", cap=0, extra=("--result-repo", self.repo,
                                              "--result-branch", "dsp/same"))
        # A pre-upgrade duplicate queue record must not get a second lease.
        original = self.tickets()[tid]
        hd.save_ticket(dict(original, id="T-legacy-duplicate", agent="B"))
        self.run_hd("refresh", "--runtime", "claude-code", "--cap", 16)
        self.run_hd("drain")
        self.assertEqual(1, len(self.leases()))
        lease = next(iter(self.leases().values()))
        _, adopted = self.acquire(lease["agent"], cap=16, lease=lease["id"], **self.target)
        self.assertEqual("granted", adopted[0])
        self.assertEqual(lease["id"], adopted[1])


class Ordering(Base):
    def test_tier_then_age_not_fifo(self):
        _, g = self.acquire("A")
        self.assertEqual(g[0], "granted")
        t_bg_old = self.enqueue("B")
        self.clock(60)
        t_bg_new = self.enqueue("C")
        self.clock(120)
        t_live = self.enqueue("D", cls="owner-live")
        self.clock(130)
        # release cut class needs the live release marker
        self.run_hd("release-marker", "--pid", os.getpid())
        t_rel = self.enqueue("E", cls="release")
        # drained runs bind nothing (stub script), so free each slot in turn
        order = []
        cur = g[1]
        for _ in range(4):
            self.free_slot(cur)
            new = [r for r in self.decisions("grant") if r.get("via") == "drain"][-1]
            order.append(new["ticket"])
            cur = new["lease"]
        self.assertEqual(order, [t_rel, t_live, t_bg_old, t_bg_new])

    def test_release_class_without_marker_is_background(self):
        self.acquire("A")
        tid = self.enqueue("B", cls="release")
        t = self.tickets()[tid]
        self.assertEqual(t["tier"], 0)
        self.assertIn("downgraded", t["class_source"])

    def test_fairness_one_ticket_agent_is_not_starved_by_a_flood(self):
        _, g = self.acquire("A", cap=1)
        flood = []
        for i in range(25):
            flood.append(self.enqueue("FLOOD"))
        self.clock(1)
        lone = self.enqueue("LONE")
        cur = g[1]
        got = []
        for _ in range(2):
            self.free_slot(cur)
            new = [r for r in self.decisions("grant") if r.get("via") == "drain"][-1]
            got.append(new["ticket"])
            cur = new["lease"]
        self.assertIn(lone, got, "the agent with 1 ticket runs within the first 2 drains")




class Bump(Base):
    def setUp(self):
        super().setUp()
        self.acquire("A")
        self.tid = self.enqueue("B")

    def bump(self, reason, note="owner asked twice", agent="B", **kw):
        args = ["bump", "--agent", agent, "--ticket", self.tid, "--reason", reason, "--note", note]
        for k, v in kw.items():
            args += ["--" + k.replace("_", "-"), v]
        return self.run_hd(*args)

    def test_reason_and_note_required(self):
        self.assertEqual(self.bump("because")[1][0], "refused")
        self.assertEqual(self.bump("owner_waiting", note="")[1][0], "refused")
        self.assertEqual(self.tickets()[self.tid]["tier"], 0)

    def test_owner_waiting_verified_unverified_stale(self):
        rc, out = self.bump("owner_waiting", owner_turn_at=NOW - 3600)
        self.assertEqual(out[0], "refused")
        rc, out = self.bump("owner_waiting", owner_turn_at=NOW - 60)
        self.assertEqual(out[0], "bumped")
        self.assertIn("verified", out[2])
        self.assertEqual(self.tickets()[self.tid]["tier"], hd.TIERS["owner-live"])
        rc, out = self.bump("owner_waiting")
        self.assertIn("unverified", out[2])
        rows = self.decisions("bump")
        self.assertEqual([r["verified"] for r in rows], ["verified", "unverified"])
        self.assertTrue(all(r["note"] == "owner asked twice" for r in rows))

    def test_deadline_window(self):
        self.assertEqual(self.bump("deadline", deadline=NOW + 2 * 86400)[1][0], "refused")
        self.assertEqual(self.bump("deadline", deadline=NOW - 10)[1][0], "refused")
        self.assertEqual(self.bump("deadline", deadline=NOW + 3600)[1][0], "bumped")

    def test_blocks_release_is_marker_bound(self):
        self.assertEqual(self.bump("blocks_release")[1][0], "refused")
        self.run_hd("release-marker", "--pid", os.getpid())
        self.assertEqual(self.bump("blocks_release")[1][0], "bumped")
        self.assertEqual(self.tickets()[self.tid]["tier"], hd.TIERS["release"])
        # a dead marker holder is no release
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.run_hd("release-marker", "--pid", dead.pid)
        self.assertFalse(hd.release_running())

    def test_rate_limit_charged_before_effect(self):
        for _ in range(3):
            self.assertEqual(self.bump("owner_waiting")[1][0], "bumped")
        rc, out = self.bump("owner_waiting")
        self.assertEqual(out[0], "refused")
        self.assertIn("rate limit", out[2])
        self.assertTrue(any("rate limit" in r["why"] for r in self.decisions("bump_refused")))
        # a crash between charge and effect: the charge alone still counts
        tid2 = self.enqueue("C")
        hd.append_line(hd._p("agents", "C.bumps.jsonl"), {"ts": NOW, "ticket": tid2})
        hd.append_line(hd._p("agents", "C.bumps.jsonl"), {"ts": NOW, "ticket": tid2})
        hd.append_line(hd._p("agents", "C.bumps.jsonl"), {"ts": NOW + 999, "ticket": tid2})  # future stamp counts
        self.tid = tid2
        self.assertEqual(self.bump("owner_waiting", agent="C")[1][0], "refused")
        # the window rolls: a day later the agent may bump again
        self.clock(60, wall=NOW + 86400 + 1000)   # a day of wall time, little awake time
        self.assertEqual(self.bump("owner_waiting", agent="C")[1][0], "bumped")

    def test_only_owner_and_never_pin(self):
        self.assertEqual(self.bump("owner_waiting", agent="X")[1][0], "refused")
        self.run_hd("release-marker", "--pid", os.getpid())
        self.bump("blocks_release")
        self.bump("owner_waiting")   # set, not added: stays release
        self.assertEqual(self.tickets()[self.tid]["tier"], hd.TIERS["release"])

    def test_owner_pin_newest_on_top_and_reserved(self):
        t2 = self.enqueue("C")
        self.assertEqual(self.run_hd("bump", "--agent", "B", "--ticket", self.tid, "--owner-pin")[1][0],
                         "refused")   # needs the owner turn id
        self.run_hd("bump", "--agent", "B", "--ticket", self.tid, "--owner-pin", "--turn", "u1")
        self.clock(5)
        self.run_hd("bump", "--agent", "C", "--ticket", t2, "--owner-pin", "--turn", "u2")
        st = hd.load_state()
        ordered, _ = hd.order(st, hd.awake_tick(st))
        self.assertEqual([t["id"] for t in ordered[:2]], [t2, self.tid])
        self.assertTrue(st["tickets"][t2]["reserved"])
        # a pinned lane takes no fresh request
        rc, out = self.acquire("Z", cap=5)
        self.assertEqual(out[0], "full")


class Starvation(Base):
    def test_skips_trip_then_reservation(self):
        _, g = self.acquire("A", cap=1)
        bg = self.enqueue("BG")
        cur = g[1]
        # a steady stream of owner-live work outranks it; every grant skips it
        for i in range(hd.GUARD_SKIPS):
            self.clock(i + 1)
            self.enqueue("LIVE%d" % i, cls="owner-live")
            self.free_slot(cur)
            cur = [r for r in self.decisions("grant") if r.get("via") == "drain"][-1]["lease"]
        t = self.tickets()[bg]
        self.assertEqual(t["tier"], hd.TIERS["owner-live"], t)
        self.assertEqual(t["trips"], 1)
        self.assertTrue(any(r["ticket"] == bg for r in self.decisions("guard")))
        # it is owner-live now and the oldest of its tier -> granted next
        self.clock(50)
        self.enqueue("LIVE-late", cls="owner-live")
        self.free_slot(cur)
        self.assertEqual(self.granted_tickets()[-1], bg)

    def test_second_trip_reserves_the_lane(self):
        _, g = self.acquire("A", cap=1)
        bg = self.enqueue("BG")
        # two time-based trips (90 awake minutes each)
        self.clock(hd.GUARD_WAIT_S + 1)
        self.run_hd("drain")
        self.clock(2 * hd.GUARD_WAIT_S + 2)
        self.run_hd("drain")
        t = self.tickets()[bg]
        self.assertEqual(t["trips"], 2)
        self.assertTrue(t["reserved"])
        self.assertTrue(self.decisions("reserve"))
        # even a pin-free owner-live newcomer cannot backfill past it
        live = self.enqueue("LIVE", cls="owner-live")
        self.free_slot(g[1])
        self.assertEqual(self.granted_tickets()[-1], bg)
        self.assertIn(live, self.tickets())

    def test_sleep_is_not_waiting(self):
        self.acquire("A", cap=1)
        tid = self.enqueue("B")
        # 8 h of wall clock while the monotonic clock stood still (lid closed)
        self.clock(10, wall=NOW + 8 * 3600)
        self.run_hd("drain")
        t = self.tickets().get(tid)
        self.assertIsNotNone(t, "no mass expiry after sleep")
        self.assertEqual(t.get("trips", 0), 0)
        # wall clock stepped back: ages never go negative
        self.clock(20, wall=NOW - 3600)
        st = hd.load_state()
        self.assertGreaterEqual(hd.awake_tick(st), 0)

    def test_deadline_expiry_by_awake_time(self):
        self.acquire("A", cap=1)
        tid = self.enqueue("B")
        self.clock(hd.TICKET_DEADLINE_S + 1)
        self.run_hd("drain")
        self.assertNotIn(tid, self.tickets())
        self.assertTrue(any(r["ticket"] == tid for r in self.decisions("expired")))

    def test_reboot_gap_is_capped(self):
        self.acquire("A", cap=1)
        tid = self.enqueue("B")
        os.environ["DOGANY_DISPATCH_BOOT"] = "boot-2"
        self.clock(-900, wall=NOW + 20 * 3600)   # new boot: mono restarted
        self.run_hd("drain")
        st = hd.load_state()
        self.assertLessEqual(st["clock"]["awake"], hd.REBOOT_GAP_CAP_S + 1)
        self.assertIn(tid, self.tickets())


class Machine(Base):
    def test_young_leases_are_charged(self):
        # 8 cpu -> limit 12; load5 9 -> room 3 by load; each young lease costs 1.0
        os.environ["DOGANY_DISPATCH_MACHINE"] = "9 8 32000 0"
        res = [self.acquire("A%d" % i, cap=10)[1][0] for i in range(6)]
        self.assertEqual(res.count("granted"), 3, res)
        full = self.decisions("full")[-1]["why"]
        self.assertIn("young", full)

    def test_swap_under_load_holds(self):
        os.environ["DOGANY_DISPATCH_MACHINE"] = "9.5 8 32000 6500"
        rc, out = self.acquire("A", cap=10)
        self.assertEqual(out[0], "full")
        self.assertIn("swap=6500MB", out[2])
        # swap alone on an idle machine is not pressure
        os.environ["DOGANY_DISPATCH_MACHINE"] = "1 8 32000 6500"
        self.assertEqual(self.acquire("A", cap=10)[1][0], "granted")

    def test_cloud_lane_has_no_machine_term(self):
        os.environ["DOGANY_DISPATCH_MACHINE"] = "50 8 100 9000"
        self.assertEqual(self.acquire("A", cap=2, runtime="cloud")[1][0], "granted")
        self.assertEqual(self.acquire("A", cap=2)[1][0], "full")

    def test_young_heavy_test_slot_is_charged(self):
        os.environ["DOGANY_DISPATCH_MACHINE"] = "6 8 32000 0"   # room 6 without it
        slot = os.path.join(os.environ["DOGANY_SHARED_HOME"], "cron-queue", "heavy-test", "slot-1")
        os.makedirs(slot)
        with open(os.path.join(slot, "pid"), "w") as f:
            f.write(str(self.holder()))
        os.environ["DOGANY_DISPATCH_NOW"] = str(time.time())
        st = hd.load_state()
        room, note = hd.machine_room(st, hd.machine_read())
        self.assertEqual(room, 2, note)
        self.assertIn("heavy-test young", note)

    def test_release_cut_waiting_holds_new_local_leases(self):
        os.environ["DOGANY_DISPATCH_NOW"] = str(time.time())
        rel = self.holder()
        hd.write_json(hd._p("test-queue", "%d.json" % rel),
                      {"pid": rel, "class": "release", "since": time.time() - 16 * 60})
        rc, out = self.acquire("A", cap=5)
        self.assertEqual(out[0], "full")
        self.assertIn("HOLD", out[2])
        self.assertEqual(self.acquire("A", cap=5, runtime="cloud")[1][0], "granted")


class HeavyOrder(Base):
    def turn(self, pid, cls):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hd.main(["heavy-turn", "--pid", str(pid), "--class", cls, "--who", "t"])
        return rc

    def test_release_first_then_age_dead_ignored(self):
        # injected clock: the later waiter queues one pinned second after the older
        clock = time.time()
        os.environ["DOGANY_DISPATCH_NOW"] = str(clock)
        push_old, push_new, rel = self.holder(), self.holder(), self.holder()
        self.assertEqual(self.turn(push_old, "push"), 0)
        os.environ["DOGANY_DISPATCH_NOW"] = str(clock + 1)
        self.assertEqual(self.turn(push_new, "push"), 1, "older push waiter first")
        self.assertEqual(self.turn(rel, "release"), 0)
        self.assertEqual(self.turn(push_old, "push"), 1, "a release waiter is ahead")
        self.run_hd("heavy-done", "--pid", rel)
        self.assertEqual(self.turn(push_old, "push"), 0)
        # a dead waiter never blocks
        for p in self.procs:
            if p.pid == push_old:
                p.kill()
                p.wait()
        self.assertEqual(self.turn(push_new, "push"), 0)

    def test_worker_class_waits_behind_push(self):
        os.environ.pop("DOGANY_DISPATCH_NOW")
        w, p = self.holder(), self.holder()
        self.assertEqual(self.turn(w, "worker"), 0)
        self.assertEqual(self.turn(p, "push"), 0)
        self.assertEqual(self.turn(w, "worker"), 1)


class Reap(Base):
    def test_dead_holder_and_recycled_pid(self):
        p = subprocess.Popen(["sleep", "300"])
        self.procs.append(p)
        _, g = self.acquire("A", cap=5, pid=p.pid, marker="/runs/dsp-1")
        self.assertEqual(g[0], "granted")
        # alive, but its argv lacks the run marker -> a recycled pid -> reaped
        self.clock(5)
        # Worker sandboxes may hide ps output. Supply the observed fixture
        # argv; keep the real pid check and real lease reaping under test.
        with mock.patch.object(hd, "proc_snapshot", return_value={p.pid: "sleep 300"}):
            self.run_hd("drain")
        self.assertNotIn(g[1], self.leases())
        self.assertTrue(any(r["lease"] == g[1] for r in self.decisions("reap")))
        q = subprocess.Popen(["sleep", "300"])
        self.procs.append(q)
        _, g2 = self.acquire("A", cap=5, pid=q.pid)
        q.kill()
        q.wait()
        self.clock(6)
        self.run_hd("drain")
        self.assertNotIn(g2[1], self.leases())

    def test_unbound_starting_lease_requeued_once(self):
        _, g = self.acquire("A", cap=1)
        tid = self.enqueue("B")
        self.free_slot(g[1])       # B granted, stub never binds
        self.assertNotIn(tid, self.tickets())
        self.clock(hd.STARTING_TTL_S + 10)
        self.run_hd("drain")       # reaped -> requeued -> granted again (lane free)
        self.assertEqual(self.granted_tickets().count(tid), 2)
        self.clock(2 * hd.STARTING_TTL_S + 30)
        self.run_hd("drain")
        self.assertTrue(any(r.get("ticket") == tid for r in self.decisions("blocked")))
        self.assertEqual(self.granted_tickets().count(tid), 2)

    def test_drained_worker_does_not_inherit_the_lock(self):
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no /proc fd view on this platform")
        out = os.path.join(self.tmp, "fds.txt")
        script = os.path.join(self.tmp, "owner.sh")
        with open(script, "w") as f:
            f.write("#!/bin/bash\nls -l /proc/$$/fd > %s\n" % out)
        os.chmod(script, 0o755)
        _, g = self.acquire("A", cap=1)
        self.enqueue("B", script=script)
        self.free_slot(g[1])
        for _ in range(1000):
            if os.path.exists(out) and os.path.getsize(out):
                break
            time.sleep(0.005)
        with open(out) as f:
            fds = f.read()
        self.assertNotIn(hd._p("lock"), fds)
        self.assertNotIn("dispatcher/lock", fds)


class ApiBooks(Base):
    def test_two_agents_paced_off_the_shared_ledger(self):
        import api_credit_pool as pool
        adir = pool.account_dir("abc123def456")
        paths = pool.account_paths(adir)
        os.makedirs(adir)
        # month spend so far 42.57 + 67.30 (the 2026-10-10 replay)
        month = time.strftime("%Y-%m", time.localtime(NOW))
        with open(paths["ledger"], "w") as f:
            for i, usd in enumerate((42.57, 67.30)):
                f.write(json.dumps({"ts": NOW - 100 + i, "month": month, "runid": "r%d" % i,
                                    "model": "opus", "cost_usd": usd}) + "\n")
        facts = dict(api_reserve="0", api_ledger=paths["ledger"],
                     api_marker=paths["marker"], api_reconcile=paths["reconcile"],
                     api_inflight=os.path.join(adir, "inflight"), account="abc123def456")
        # $200 budget on day 10: allowed ~$58.78 < shared spend $109.87 -> the
        # second agent is paced off even though its OWN rows are fewer.
        rc, out = self.acquire("B", cap=5, runtime="claude-api", model="opus",
                               api_budget="200", **facts)
        self.assertEqual(out[0], "full", out)
        self.assertIn("api books", out[2])
        # $400: allowed ~$117.57; spent $109.87 admits one run; the granted run
        # is not in flight yet, so it is charged provisionally (the model's
        # median from the shared ledger) and the next request is refused.
        rc, out = self.acquire("A", cap=5, runtime="claude-api", model="opus",
                               api_budget="400", **facts)
        self.assertEqual(out[0], "granted", out)
        rc, out = self.acquire("B", cap=5, runtime="claude-api", model="opus",
                               api_budget="400", **facts)
        self.assertEqual(out[0], "full", out)
        self.assertIn("1 in flight", out[2])


class Degraded(Base):
    def test_unwritable_state_is_unavailable(self):
        bad = os.path.join(self.tmp, "ro")
        os.makedirs(bad)
        with open(os.path.join(bad, "dispatcher"), "w") as f:
            f.write("not a dir")
        os.environ["DOGANY_SHARED_HOME"] = bad
        rc, out = self.acquire("A")
        self.assertEqual(rc, 3)
        self.assertEqual(out[0], "unavailable")




class Books(Base):
    def test_lane_account_is_the_books_fingerprint(self):
        import api_credit_pool as pool
        with open(os.path.join(os.environ["HOME"], ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"accountUuid": "uuid-1"}}, f)
        self.assertEqual(hd.account_for("claude-code"), pool.key_fingerprint("uuid-1"))
        self.assertEqual(hd.account_for("claude-api", "/x/.dogany/api-credit/ffee00112233"),
                         "ffee00112233")
        self.assertEqual(hd.account_for("codex"), "unknown")


class TimerDrain(Base):
    """B-2112: queued work starts when ROOM appears, with no enqueue/bump."""

    HIGH = "20 8 32000 0"    # load5 20 on 8 cpu: limit 12 -> room 0
    LOW = "1 8 32000 0"

    def stub_owner(self):
        out = os.path.join(self.tmp, "started")
        sh = os.path.join(self.tmp, "owner.sh")
        with open(sh, "w") as f:
            f.write("#!/bin/sh\necho \"$DOGANY_HOST_LEASE\" >> %s\n" % out)
        os.chmod(sh, 0o755)
        return sh, out

    def started(self, out):
        for _ in range(500):
            if os.path.exists(out):
                return True
            time.sleep(0.01)
        return False

    def fill_and_queue(self, cap=1):
        sh, out = self.stub_owner()
        _, g = self.acquire("A", cap=cap)
        self.assertEqual(g[0], "granted")
        tid = self.enqueue("B", cap=cap, script=sh)
        return g[1], tid, out

    def test_release_under_load_then_timer_drain_starts_without_event(self):
        lease, tid, out = self.fill_and_queue()
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.HIGH
        self.clock(60)
        self.free_slot(lease)                    # release: load high -> room 0
        self.assertIn(tid, self.tickets())
        self.assertFalse(os.path.exists(out))
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.LOW   # load falls, no event
        self.clock(360)
        rc, o = self.run_hd("drain")             # the timer tick
        self.assertEqual((rc, o[0]), (0, "drained"))
        self.assertTrue(self.started(out))
        self.assertNotIn(tid, self.tickets())

    def test_crash_reap_path_is_drained_by_timer(self):
        sh, out = self.stub_owner()
        p = subprocess.Popen(["sleep", "300"])
        self.procs.append(p)
        _, g = self.acquire("A", pid=p.pid)
        tid = self.enqueue("B", script=sh)
        p.kill()
        p.wait()                                 # holder dies, no release call
        self.assertIn(tid, self.tickets())
        self.clock(300)
        self.run_hd("drain")
        self.assertTrue(self.started(out))
        self.assertTrue(self.decisions("reap"))

    def test_cancelled_ticket_does_not_block_next(self):
        lease, tid, out = self.fill_and_queue()
        sh, out2 = self.stub_owner()
        rc, o = self.run_hd("cancel", "--agent", "B", "--ticket", tid)
        self.assertEqual(o[0], "cancelled")
        self.free_slot(lease)
        self.assertFalse(self.tickets())

    def test_stale_zero_cap_does_not_strand_ticket(self):
        sh, out = self.stub_owner()
        self.enqueue("B", cap=0, script=sh)
        self.clock(hd.LANE_CAP_TTL_S + 60)
        self.run_hd("drain")
        self.assertTrue(self.started(out))

    def test_stall_finding_after_15_min_with_room(self):
        sh, out = self.stub_owner()
        self.enqueue("B", cap=0, script=sh)      # fresh cap 0: lane closed
        self.clock(600)
        self.run_hd("drain")
        self.assertFalse(self.decisions("stall"))
        self.clock(hd.STALL_AFTER_S + 30)        # past 15 min, cap stale -> would start
        # keep the cap fresh at 0 so the ticket stays blocked
        st = hd.read_json(hd._p("lanes.json"))
        for v in st.values():
            v["at"] = hd.wall_now()
        hd.write_json(hd._p("lanes.json"), st)
        rc, o = self.run_hd("drain")
        self.assertIn("STALL", o[2])
        self.assertEqual(len(self.decisions("stall")), 1)
        self.run_hd("drain")                     # standing stall: not re-logged
        self.assertEqual(len(self.decisions("stall")), 1)

    def test_no_stall_when_lane_busy_or_no_room(self):
        lease, tid, out = self.fill_and_queue()
        self.clock(hd.STALL_AFTER_S + 60)
        self.run_hd("drain")
        self.assertFalse(self.decisions("stall"))     # lane has a live lease
        self.free_slot(lease)
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.HIGH
        self.clock(2 * hd.STALL_AFTER_S + 60)
        self.run_hd("drain")
        self.assertFalse(self.decisions("stall"))     # no room

    def test_health_observer_tick_calls_drain(self):
        lease, tid, out = self.fill_and_queue()
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.HIGH
        self.free_slot(lease)
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.LOW
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ho_b2112", os.path.join(os.path.dirname(HERE), "health-observer.py"))
        ho = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ho)
        line = ho.host_dispatch_drain()
        self.assertIn("drained", line or "")
        self.assertTrue(self.started(out))


class AwakeClockDgn2142(Base):
    """DGN-2142: every queued ticket 'waited ~8,800 awake min' because a writer
    with a ~0 monotonic base left clock.json behind and the next honest tick
    added its whole monotonic value."""

    def tick(self, mono_base, wall):
        os.environ["DOGANY_DISPATCH_MONO"] = str(mono_base)
        os.environ["DOGANY_DISPATCH_NOW"] = str(wall)
        st = hd.load_state()
        ha = hd.awake_tick(st)
        hd.save_meta(st)
        return ha

    def test_alternating_bases_never_exceed_wall(self):
        a0 = self.tick(531000.0, NOW)
        for i in range(1, 40):                       # real writer vs foreign-base writer
            wall = NOW + i * 30
            self.tick(i * 30.0, wall)                # foreign base ~0
            ha = self.tick(531000.0 + i * 30, wall)  # honest
            self.assertLessEqual(ha - a0, wall - NOW + 1e-6)
        self.assertGreater(ha - a0, 0)

    def test_poisoned_stored_clock_is_clamped(self):
        self.tick(531000.0, NOW)
        hd.write_json(hd._p("clock.json"), dict(hd.read_json(hd._p("clock.json")), mono=0.0))
        ha0 = hd.read_json(hd._p("clock.json"))["awake"]
        ha = self.tick(531060.0, NOW + 60)           # mono delta 531060, wall delta 60
        self.assertLessEqual(ha - ha0, 60 + 1e-6)

    def test_nine_minute_ticket_not_expired(self):
        self.acquire("A", cap=1)
        tid = self.enqueue("B")
        self.tick(5.0, NOW + 60)                     # foreign writer, tiny base
        self.clock(9 * 60)                           # honest, 9 min later
        self.run_hd("drain")
        self.assertIn(tid, self.tickets())
        self.assertFalse(self.decisions("expired"))

    def test_pinned_clock_refuses_real_state_dir(self):
        real = os.path.join(hd._real_state_dir(), "..", "..")
        os.environ["DOGANY_SHARED_HOME"] = os.path.join(real, ".dogany")
        self.assertEqual(os.path.realpath(hd.shared_home()), os.path.realpath(os.path.dirname(hd._real_state_dir())))
        with self.assertRaises(RuntimeError):
            hd.root()
class Dgn2138(Base):
    """Priority allocation, run-kind machine charge, never-silent expiry."""

    HIGH = "20 8 32000 0"    # load5 20 on 8 cpu: limit 12 -> room 0 for a build run
    LOW = "1 8 32000 0"

    def inbox(self, ws):
        d = os.path.join(ws, ".telegram_bot", "session-inbox")
        return sorted(n for n in os.listdir(d) if not n.startswith(".")) if os.path.isdir(d) else []

    def test_room_opening_grants_best_scored_first_on_drain(self):
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.LOW
        _, g = self.acquire("A", cap=1)
        t_old = self.enqueue("B")
        self.clock(60)
        t_live = self.enqueue("C", cls="owner-live")
        self.clock(120)
        t_new = self.enqueue("D")
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.HIGH
        self.free_slot(g[1])
        self.assertEqual(self.granted_tickets(), [])
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.LOW   # room opens, no enqueue
        self.clock(180)
        self.run_hd("drain")
        self.assertEqual(self.granted_tickets(), [t_live])
        self.free_slot([r for r in self.decisions("grant")][-1]["lease"])
        self.assertEqual(self.granted_tickets(), [t_live, t_old])
        self.assertIn(t_new, self.tickets())

    def test_later_ticket_never_jumps_older_equal_tier_on_its_lane(self):
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.HIGH   # the older build run does not fit
        self.acquire("A", cap=3, kind="llm")
        t_build = self.enqueue("B", cap=3, extra=("--kind", "build"))
        self.clock(60)
        t_llm = self.enqueue("C", cap=3, extra=("--kind", "llm"))   # would fit alone
        self.clock(120)
        self.run_hd("drain")
        self.assertEqual(self.granted_tickets(), [])
        self.assertEqual(set(self.tickets()), {t_build, t_llm})

    def test_llm_run_not_blocked_by_load_build_run_is_(self):
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.HIGH
        self.assertEqual(self.acquire("A", cap=5, kind="build")[1][0], "full")
        self.assertEqual(self.acquire("A", cap=5, kind="llm")[1][0], "granted")
        lid = self.acquire("B", cap=5, kind="llm")[1][1]
        self.assertEqual(self.leases()[lid]["kind"], "llm")

    def test_legacy_caller_without_kind_stays_build(self):
        # The shell's no-worktree default is llm, but the host CLI must
        # fail closed for older acquire/enqueue callers omitting --kind.
        os.environ["DOGANY_DISPATCH_RUN_KIND"] = "llm"
        _, granted = self.acquire("A", cap=5)
        self.assertEqual(granted[0], "granted")
        self.assertEqual(self.leases()[granted[1]]["kind"], "build")
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.HIGH
        self.assertEqual(self.acquire("B", cap=5)[1][0], "full")
        tid = self.enqueue("C", cap=5)
        self.assertEqual(self.tickets()[tid]["kind"], "build")
        self.run_hd("drain")
        self.assertIn(tid, self.tickets())
        self.assertEqual(len(self.leases()), 1)

    def test_hard_ceiling_closes_every_kind(self):
        os.environ["DOGANY_DISPATCH_MACHINE"] = "1 8 32000 9000"   # swap past the hard ceiling, idle cpu
        self.assertEqual(self.acquire("A", cap=5, kind="llm")[1][0], "full")
        self.assertEqual(self.acquire("A", cap=5, kind="build")[1][0], "full")
        os.environ["DOGANY_DISPATCH_MACHINE"] = "1 8 1200 0"       # memory under the reserve
        self.assertEqual(self.acquire("A", cap=5, kind="llm")[1][0], "full")

    def test_expiry_notifies_the_agent_and_records_a_reason(self):
        ws = os.path.join(self.tmp, "ws")
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.LOW
        self.acquire("A", cap=1)
        tid = self.enqueue("B", extra=("--workspace", ws, "--why", "machine load5=14.4 room 0"))
        self.clock(hd.TICKET_DEADLINE_S + 1)
        self.run_hd("drain")
        self.assertNotIn(tid, self.tickets())
        self.assertEqual(self.inbox(ws), ["dispatch-expired-%s.md" % tid])
        with open(os.path.join(ws, ".telegram_bot", "session-inbox", self.inbox(ws)[0])) as f:
            self.assertIn("EXPIRED", f.read())
        closed = hd.read_json(hd._p("closed", tid + ".json"))
        self.assertIn("never admitted", closed["reason"])
        self.assertIn("load5=14.4", closed["reason"])

    def test_owner_tier_is_requeued_boosted_then_expires_with_notice(self):
        ws = os.path.join(self.tmp, "ws")
        os.environ["DOGANY_DISPATCH_MACHINE"] = self.LOW
        self.acquire("A", cap=1)
        tid = self.enqueue("B", cls="owner-live", extra=("--workspace", ws))
        base = hd.score(self.tickets()[tid], 0.0, {})
        self.clock(hd.TICKET_DEADLINE_S + 1)
        self.run_hd("drain")
        t = self.tickets()[tid]
        self.assertEqual((t["requeues"], t["boost"]), (1, hd.REQUEUE_BOOST))
        self.assertGreater(hd.score(t, t["expiry_base"], {}), base + hd.REQUEUE_BOOST - 1)
        self.assertEqual(self.inbox(ws), ["dispatch-requeued-%s.md" % tid])
        for i in range(2, hd.MAX_REQUEUES + 2):
            self.clock(i * (hd.TICKET_DEADLINE_S + 1))
            self.run_hd("drain")
        self.assertNotIn(tid, self.tickets())
        self.assertEqual(hd.read_json(hd._p("closed", tid + ".json"))["closed"], "expired")
        self.assertEqual(self.inbox(ws), ["dispatch-expired-%s.md" % tid, "dispatch-requeued-%s.md" % tid])


class Apo9HostRoute(Base):
    def test_activation_barrier_covers_host_reservation_transfer_and_late_writers(self):
        import return_route as rr
        ws = Path(self.tmp) / "activation-ws"
        config = ws / ".telegram_bot/po-seat/config.json"
        rr.atomic_json(config, dict(enabled=False, host_request_dir=hd._p("queue")))
        pinned = rr.resolve_json("", "", str(ws))
        with rr.po_seat_publication_barrier(ws):
            rr.reserve_route(ws, "fixture-host", pinned)
        replay = Path(self.tmp) / "replay.json"
        rr.atomic_json(replay, dict(argv=["--task","brief"], return_route=pinned, return_reservation="fixture-host"))
        args = ["enqueue", "--agent", "A", "--runtime", "codex", "--cap", 0,
                "--argv-file", str(replay), "--workspace", str(ws)]
        rc, queued = self.run_hd(*args)
        self.assertEqual((rc, queued[0]), (0, "queued"))
        self.assertFalse(rr.reservation_path(ws, "fixture-host").exists())
        with self.assertRaisesRegex(ValueError, "host"):
            with rr.po_seat_activation_guard(ws):
                self.fail("transferred host work escaped inventory")
        self.assertEqual(self.tickets()[queued[1]]["return_route"], pinned)
        self.run_hd("cancel", "--agent", "A", "--ticket", queued[1])
        with rr.po_seat_activation_guard(ws):
            rr.atomic_json(config, dict(enabled=True, host_request_dir=hd._p("queue")))
        rc, refused = self.run_hd(*args)
        self.assertNotEqual(rc, 0)
        self.assertEqual(refused[0], "unavailable")
        self.assertEqual(self.tickets(), {})
        with self.assertRaisesRegex(ValueError, "legacy publisher fenced"):
            hd.notify_inbox(dict(id="old", workspace=str(ws), return_route=pinned), "expired", ["old return"])
        self.assertFalse((ws / ".telegram_bot/session-inbox").exists())

    def test_expiry_requeue_and_cancel_publish_to_pinned_queue_after_rollback(self):
        import return_route as rr
        ws = os.path.join(self.tmp, "route-ws")
        queue = os.path.join(ws, "queue")
        config = os.path.join(ws, ".telegram_bot/po-seat/config.json")
        rr.atomic_json(config, dict(enabled=True, instance_id="host-fixture", config_generation=2,
            destination=queue, registered_queue_dirs=[queue]))
        pinned = rr.resolve_json("product", "", ws, parent_event_id="parent:host")
        rr.atomic_json(config, {"enabled": False})
        for kind in ("expired", "requeued", "canceled"):
            ticket = dict(id="fixture-" + kind, agent="A", workspace=ws, lane="codex@acct",
                          return_route=pinned, requeues=1)
            self.assertTrue(hd.notify_inbox(ticket, kind, ["retained work"]))
            self.assertTrue(hd.notify_inbox(ticket, kind, ["retained work"]))
        files = sorted(Path(queue).glob("*.json"))
        self.assertEqual(len(files), 3)
        for path in files:
            event = json.loads(path.read_text())
            self.assertEqual(event["route_id"], pinned["route_id"])
            self.assertEqual(event["parent_event_id"], "parent:host")
            self.assertTrue(event["event_id"].startswith("host:host-fixture:"))
        self.assertFalse(os.path.exists(os.path.join(ws, ".telegram_bot/session-inbox")))

    def test_pinned_legacy_queue_cancel_remains_silent(self):
        import return_route as rr
        ws = os.path.join(self.tmp, "legacy-cancel")
        pinned = rr.resolve_json("", "", ws)
        argvf = os.path.join(self.tmp, "legacy-replay.json")
        rr.atomic_json(argvf, dict(argv=["--task", "brief"], return_route=pinned))
        rc, queued = self.run_hd("enqueue", "--agent", "A", "--runtime", "codex", "--cap", 0,
            "--argv-file", argvf, "--workspace", ws)
        self.assertEqual(queued[0], "queued")
        self.assertEqual(self.run_hd("cancel", "--agent", "A", "--ticket", queued[1])[1][0], "cancelled")
        self.assertFalse(os.path.exists(os.path.join(ws, ".telegram_bot/session-inbox")))
        self.assertEqual(hd.read_json(hd._p("closed", queued[1] + ".json"))["return_route"], pinned)

    def test_queue_transport_and_replacement_keep_contract_and_seat_lane(self):
        import return_route as rr
        ws = os.path.join(self.tmp, "transport-ws")
        review = rr.resolve_json("product", "", ws, job_kind="seat-review", parent_event_id="parent:seat")
        argv = hd.pinned_argv(["--task", "brief", "--runtime", "codex"], review)
        replay = hd.replace_argv(argv, "snapshot", "run", "branch")
        self.assertEqual(json.loads(replay[replay.index("--return-route-json") + 1]), review)
        argvf = os.path.join(self.tmp, "route-replay.json")
        # The host must transport a real reservation, not authorize a seat
        # request from route metadata alone. No real runtime is started.
        import seat_review_budget as budget
        home = Path(ws) / ".telegram_bot/po-seat"
        home.mkdir(parents=True)
        contract = dict(schema=budget.CAPABILITY, job_id="a" * 64, deadline=time.time() + 299)
        with sqlite3.connect(home / "seat.sqlite3") as db:
            db.execute("CREATE TABLE jobs(id TEXT,event TEXT,deadline REAL,state TEXT,reserved INTEGER)")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (contract["job_id"], "parent:seat", contract["deadline"], "reserved", 300))
            db.execute("CREATE TABLE budget_days(job TEXT,day TEXT,seconds REAL)")
            db.executemany("INSERT INTO budget_days VALUES(?,?,?)", [(contract["job_id"], d, n) for d, n in budget.charges(contract["deadline"])])
        rr.atomic_json(argvf, dict(argv=argv, return_route=review))
        self.assertEqual(self.run_hd("enqueue", "--agent", "A", "--runtime", "codex", "--cap", 0,
            "--argv-file", argvf, "--workspace", ws)[1][0], "unavailable")
        rr.atomic_json(argvf, dict(argv=argv, return_route=review, seat_budget=contract))
        os.environ["DOGANY_DISPATCH_MACHINE"] = "20 8 32000 0"
        with mock.patch.object(budget, "host_capability", return_value=budget.CAPABILITY):
            rc, result = self.run_hd("enqueue", "--agent", "A", "--runtime", "codex", "--cap", 0,
                "--argv-file", argvf, "--workspace", ws)
        self.assertEqual(result[0], "queued")
        ticket = self.tickets()[result[1]]
        self.assertEqual(ticket["return_route"], review)
        self.assertEqual(ticket["runtime_policy"], "codex-only")
        self.assertEqual(ticket["job_kind"], "seat-review")
        self.assertEqual(ticket["seat_budget"], contract)
        self.assertEqual(self.run_hd("cancel", "--agent", "A", "--ticket", result[1])[1][0], "cancelled")
        self.assertFalse(os.path.isdir(os.path.join(ws, ".telegram_bot/session-inbox")))


class R3Reservations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="r3-reservations-")
        self.saved = dict(os.environ)
        for key in list(os.environ):
            if key.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
                del os.environ[key]
        os.environ.update(DOGANY_SHARED_HOME=self.tmp.name, DOGANY_DISPATCH_MACHINE="0 8 32000 0")
        self.lane = "codex@r3-test"
        hd.r3_resource_snapshot(dict(worker=3, test=1, review=1), "host observation")
        st = hd.load_state()
        st["lanes"][self.lane] = dict(cap=3, at=hd.wall_now(), note="host observation")
        hd.write_json(hd._p("lanes.json"), st["lanes"])
        self.request = dict(lane=self.lane, run_id="dsp-20261021-120000-3000", judgment_id="r3-1",
                            scope="sample", pid=os.getpid(), atoms=[dict(ticket="APP-1")],
                            run_dir=os.path.join(self.tmp.name, "dispatch", "dsp-20261021-120000-3000"),
                            ledger_revision=hd.r3_snapshot()["revision"],
                            resources={k: dict(low=1, high=1, unknown=False, basis="one request") for k in ("worker", "test", "review")},
                            baseline=dict(version="schedule-1", done="Accepted", remaining=["gate"], dependencies=[],
                                          resource_window=[1, 10], source="lane observation", completion_upper=10, forecast_upper=9))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def reserve(self, **updates):
        return hd.r3_reserve(dict(self.request, **updates), "bound-input")

    def receipt(self, lease, result=None, live=False):
        marker = self.request["run_dir"]
        os.makedirs(marker, exist_ok=True)
        pid = os.getpid() if live else 2147483647
        lease["pid"] = pid
        hd.save_lease(lease)
        with open(os.path.join(marker, "pid"), "w") as f:
            f.write(str(pid))
        with open(os.path.join(marker, "run.env"), "w") as f:
            f.write("RUNID=" + self.request["run_id"] + "\nTICKET=APP-1\nSPAWN_EPOCH=1\n")
        if result:
            hd.write_json(os.path.join(marker, "status.json"), dict(runid=self.request["run_id"], ticket="APP-1",
                          result=result, ended_at="2026-10-21T12:01:00Z"))

    def test_last_slot_concurrent_requests_exactly_one(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier = threading.Barrier(2)
        def reserve(n):
            barrier.wait()
            run = "dsp-20261021-120000-" + str(3000 + n)
            return self.reserve(judgment_id="r3-" + str(n), run_id=run,
                                run_dir=os.path.join(self.tmp.name, "dispatch", run),
                                atoms=[dict(ticket="APP-" + str(n))])
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, (1, 2)))
        self.assertEqual(sum(r["state"] == "reserved" for r in results), 1)
        self.assertEqual(len(hd.load_state()["leases"]), 1)

    def test_dead_pre_start_holder_releases_slot_for_next_request(self):
        script = """import json, os, sys
sys.path.insert(0, sys.argv[1])
import host_dispatcher as hd
request = json.loads(sys.argv[2])
request['pid'] = os.getpid()
print(json.dumps(hd.r3_reserve(request, 'bound-input')))
"""
        child = subprocess.run([sys.executable, "-c", script, LIB, json.dumps(self.request)],
                               capture_output=True, text=True, check=True)
        first = json.loads(child.stdout)
        self.assertEqual(first["state"], "reserved", first)
        self.assertFalse(hd.pid_alive(first["lease"]["pid"]))
        self.assertFalse(os.path.exists(self.request["run_dir"]))
        reclaimed = hd.r3_reclaim()
        self.assertEqual(len(reclaimed), 1, reclaimed)
        self.assertEqual(reclaimed[0]["evidence"]["reason"], "dead-reservation")
        self.request["ledger_revision"] = hd.r3_snapshot()["revision"]
        run = "dsp-20261021-120000-3010"
        self.assertEqual(self.reserve(run_id=run, run_dir=os.path.join(self.tmp.name, "dispatch", run),
                                     atoms=[dict(ticket="APP-2")])["state"], "reserved")
        self.assertEqual(hd.r3_reclaim(), [])

    def test_live_unbound_worker_protects_dead_reservation_holder(self):
        lease = self.reserve()["lease"]
        lease["pid"] = 2147483647
        lease["r3"]["reservation_holder"]["pid"] = lease["pid"]
        hd.save_lease(lease)
        os.makedirs(self.request["run_dir"])
        with open(os.path.join(self.request["run_dir"], "pid"), "w") as stream:
            stream.write(str(os.getpid()))
        self.assertEqual(hd.r3_reclaim(), [])
        self.assertIn(lease["id"], hd.load_state()["leases"])

    def test_partial_worker_pid_receipt_is_protected(self):
        lease = self.reserve()["lease"]
        lease["pid"] = 2147483647
        lease["r3"]["reservation_holder"]["pid"] = lease["pid"]
        hd.save_lease(lease)
        os.makedirs(self.request["run_dir"])
        with open(os.path.join(self.request["run_dir"], "pid"), "w"):
            pass
        self.assertEqual(hd.r3_reclaim(), [])
        self.assertIn(lease["id"], hd.load_state()["leases"])

    def test_empty_pid_receipt_of_dead_holder_is_reclaimed(self):
        lease = self.reserve()["lease"]
        lease["pid"] = 2147483647
        lease["r3"]["reservation_holder"]["pid"] = lease["pid"]
        hd.save_lease(lease)
        os.makedirs(self.request["run_dir"])
        with open(os.path.join(self.request["run_dir"], "pid"), "w") as stream:
            stream.write("\n")
        reclaimed = hd.r3_reclaim()
        self.assertEqual([r["evidence"]["reason"] for r in reclaimed], ["dead-reservation"])
        self.assertFalse(hd.load_state()["leases"])

    def test_host_off_cannot_skip_registered_admission(self):
        path = os.path.join(os.path.dirname(LIB), "dispatch-detached.sh")
        with open(path) as f:
            source = f.read()
        start = source.index("active_count_incr() {")
        end = source.index("\n}\n", start) + 3
        marker = os.path.join(self.tmp.name, "gate-called")
        counter = os.path.join(self.tmp.name, "counter")
        env = dict(os.environ, R3_MARKER=marker, R3_COUNTER=counter)
        helpers = '''
TICKET=APP-1 HOST_ADOPT_LEASE= HOST_FORCE=0 HOST_CAP_VALUE=2
count_lock() { return 0; }
count_unlock() { return 0; }
read_active_count() { echo 0; }
compute_dynamic_cap() { DYNAMIC_CAP_VALUE=2; }
host_dispatch_on() { return 1; }
host_acquire() { touch "$R3_MARKER"; return 1; }
active_count_file() { echo "$R3_COUNTER"; }
'''
        result = subprocess.run(["bash", "-c", helpers + source[start:end] + "\nactive_count_incr codex\n"],
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(os.path.exists(marker))
        self.assertFalse(os.path.exists(counter))

    def test_multi_resource_failure_leaves_no_partial_lease(self):
        self.request["resources"]["review"]["high"] = 2
        self.assertEqual(self.reserve()["state"], "queue")
        self.assertFalse(hd.load_state()["leases"])

    def test_retry_has_one_lease(self):
        first, second = self.reserve(), self.reserve()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["state"], "reserved")
        self.assertEqual(len(hd.load_state()["leases"]), 1)

    def test_ended_run_reclaimed(self):
        lease = self.reserve()["lease"]
        self.receipt(lease, "success")
        self.assertEqual(hd.r3_reclaim()[0]["evidence"]["reason"], "ended")
        self.assertFalse(hd.load_state()["leases"])

    def test_canceled_run_reclaimed(self):
        lease = self.reserve()["lease"]
        self.receipt(lease, "cancelled")
        self.assertEqual(hd.r3_reclaim()[0]["evidence"]["reason"], "cancelled")

    def test_dead_run_reclaimed(self):
        lease = self.reserve()["lease"]
        self.receipt(lease)
        self.assertEqual(hd.r3_reclaim()[0]["evidence"]["reason"], "dead-run")

    def test_live_canceled_run_is_protected(self):
        lease = self.reserve()["lease"]
        self.receipt(lease, "cancelled", live=True)
        self.assertEqual(hd.r3_reclaim(), [])
        self.assertIn(lease["id"], hd.load_state()["leases"])

    def test_new_judgment_cannot_duplicate_same_run(self):
        first = self.reserve()
        self.request["ledger_revision"] = hd.r3_snapshot()["revision"]
        second = self.reserve(judgment_id="rejudged-same-run")
        self.assertEqual(second["state"], "queue")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(hd.load_state()["leases"]), 1)

    def test_registered_reservation_keeps_live_ticket_deduplication(self):
        self.reserve()
        self.request["ledger_revision"] = hd.r3_snapshot()["revision"]
        run = "dsp-20261021-120000-3002"
        result = self.reserve(run_id=run, run_dir=os.path.join(self.tmp.name, "dispatch", run))
        self.assertEqual(result["reason"], "duplicate-live-ticket")
        self.assertEqual(len(hd.load_state()["leases"]), 1)

    def test_reclaimed_run_cannot_reserve_again(self):
        lease = self.reserve()["lease"]
        self.receipt(lease, "success")
        hd.r3_reclaim()
        self.assertEqual(self.reserve()["reason"], "run-already-released")

    def test_reclaim_is_idempotent(self):
        lease = self.reserve()["lease"]
        self.receipt(lease)
        hd.r3_reclaim()
        self.assertEqual(hd.r3_reclaim(), [])

    def test_wrong_receipt_cannot_release(self):
        lease = self.reserve()["lease"]
        self.receipt(lease)
        with open(os.path.join(self.request["run_dir"], "run.env"), "a") as f:
            f.write("TICKET=APP-999\n")
        self.assertEqual(hd.r3_reclaim(), [])

    def test_new_request_cannot_overwrite_pre_spend_baseline(self):
        hd.r3_resource_snapshot(dict(worker=3, test=2, review=2), "observed resource window")
        self.request["ledger_revision"] = hd.r3_snapshot()["revision"]
        self.assertEqual(self.reserve()["state"], "reserved")
        self.request["ledger_revision"] = hd.r3_snapshot()["revision"]
        self.request["baseline"]["completion_upper"] = 100
        run = "dsp-20261021-120000-3001"
        result = self.reserve(judgment_id="another-small-request", run_id=run,
                              run_dir=os.path.join(self.tmp.name, "dispatch", run), atoms=[dict(ticket="APP-2")])
        self.assertEqual(result["reason"], "schedule-snapshot-changed")
        st = hd.load_state()
        hd.set_lane(st, self.lane, 3, "fresh cap observation")
        self.assertEqual(st["lanes"][self.lane]["r3_baseline"]["completion_upper"], 10)

    def test_changed_resource_snapshot_requires_recheck(self):
        self.reserve()
        hd.r3_resource_snapshot(dict(worker=3, test=0, review=1), "new host observation")
        self.assertEqual(self.reserve()["reason"], "resource-snapshot-changed")

    def test_corrupt_lease_is_not_free_space(self):
        os.makedirs(hd._p("leases"), exist_ok=True)
        with open(hd._p("leases", "broken.json"), "w") as f:
            f.write("{broken")
        with self.assertRaises(hd.Unavailable):
            self.reserve()

    def test_legacy_reaper_keeps_r3_without_end_evidence(self):
        lease = self.reserve()["lease"]
        lease["pid"] = 2147483647
        hd.save_lease(lease)
        st = hd.load_state()
        hd.reap(st, {}, hd.wall_now(), 9999999)
        self.assertIn(lease["id"], hd.load_state()["leases"])

    def test_existing_lane_admission_counts_r3_lease(self):
        self.reserve()
        st = hd.load_state()
        st["lanes"][self.lane]["cap"] = 1
        self.assertFalse(hd.fits(st, self.lane, (0, 8, 32000, 0), "")[0])



if __name__ == "__main__":
    unittest.main(verbosity=1)
