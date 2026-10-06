#!/usr/bin/env python3
# test-lane: fast    # measured 0.7s (2026-09-22, DGN-1610 added TestRetireCLI
# test-covers: *    # tree scanner, measured 0.89s (DGN-1734): runs on every push
#                       subprocess tests; was 0.2s at DGN-1267)
"""DGN-986 axis A acceptance tests: health-observer parsing + state machine.

All launchctl behavior is FIXTURE-FIXED text (captured live 2026-08-21 on this
machine's real launchd output shapes); no test ever calls launchctl (spec
requirement: environment-independent).

Covers (spec R1/R2/R3 + DGN-989 regression):
  1. launchctl list / print parsing -- 4-state exit field (line absent /
     never exited / negative signal / positive), runs, top-level keepalive
     only (sub-block "keepalive = 0" noise ignored), path, pid.
  2. runs delta < 0 -> episode reset (reload).
  3. lost-label detection (known-set minus loaded-set) + reappearance clear;
     DGN-1427 amendment: first miss is a grace window only (no notify), a
     restart gap landing inside one poll fires nothing, a genuine absence
     past LOST_GRACE_SECONDS still fires.
  4. retired marker suppresses the loss notification.
  5. disk-vs-loaded bidirectional diff: reboot_nonsurvivor / plist_divergence
     WARN vs not_loaded INFO (static disk state is not a signal -- only
     transitions warn).
  6. bucket classification: bridge (Label-set membership, file name is NOT
     the label) / resident_service (keepalive) / waiting (runs=0 never
     exited) / scheduled; dynamic counts.
  7. one-notification-per-episode: threshold fire once, no re-fire while the
     episode persists, recovery -> new episode -> new fire.
  8. structural exits (126/127/78) fire at streak 2; general at 3; negative
     signal exits are general, never structural.
  9. DGN-989 regression fixture: 2 loaded-outside-LaunchAgents jobs = the
     only WARNs; 9 on-disk-unloaded jobs = info records (success criterion 6
     as amended by the coordinator correction).
 10. first run (empty known set) produces zero events (no false "new" alarms).
 11. corrupt jobs.json -> quarantined, fresh state, loud warning record.
 12. notification copy carries CTA sentences + an opt: button (no dead ends).
 13. DGN-1610 CLI hardening: --retire rejects an unknown label (rc=3, state
     unchanged) unless --force; --why is mandatory (rc=2); an unrecognized
     flag is a usage error (rc=2) and never falls through to a live poll;
     --list-retired gives a readback surface for a written marker. These
     four run the real CLI as a subprocess against a PATH-stubbed launchctl
     (never the real binary) so rc codes and the argv parser are exercised
     end to end; every other test in this file stays import-level and
     launchctl-free as before.
 15. DGN-1582 r3 (dec-236) dawn queue never-ran nights, from fixture log
     dirs: night 1 silent, night 2 one owner + one operator notice
     (confirmed across polls), night 3 no repeat, a night that ran resets
     the streak; a running queue holds; non-steward / unregistered /
     never-installed machines are never judged.
 14. DGN-1713 machine readings are faked: swap/disk episode dedupe, 5-minute
     load's 15-minute sustain window, shared state, representative gate, and
     session-inbox handoff (never a canned owner push).  DGN-1773: a static
     breach alerts once; only a worsening step (disk +2pp, swap +1024MB) or
     the 24h reminder re-alerts; recovery + re-breach alerts again.

Run: python3 routines/tests/test-health-observer.py
Exit 0 = all pass.
"""

import importlib.util
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location(
    "health_observer", os.path.join(ROUTINES, "health-observer.py"))
ho = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ho)

PFX = "com.telegram-skill-bot."


# ---------------------------------------------------------------------------
# fixture builders (shapes captured from real launchctl output, 2026-08-21)
# ---------------------------------------------------------------------------

def print_fixture(runs=None, exit_line=None, keepalive=False,
                  path=None, pid=None, state="not running"):
    """Compose a launchctl print block. exit_line=None omits the line
    entirely (state (i): running). Includes the real-world noise this parser
    must survive: stdout/stderr path lines, sub-block keepalive = 0 lines,
    jetsamproperties, sub-block state = active lines."""
    lines = ["gui/501/com.example = {", "\tactive count = 0"]
    if path is not None:
        lines.append("\tpath = %s" % path)
    lines += [
        "\ttype = LaunchAgent",
        "\tstate = %s" % state,
        "",
        "\tprogram = /bin/bash",
        "\tstdout path = /tmp/x.stdout.log",
        "\tstderr path = /tmp/x.stderr.log",
    ]
    if pid is not None:
        lines.append("\tpid = %d" % pid)
    if runs is not None:
        lines.append("\truns = %d" % runs)
    if exit_line is not None:
        lines.append("\tlast exit code = %s" % exit_line)
    lines += [
        "\tevent channels = {",
        "\t\t\tkeepalive = 0",
        "\t\t\tkeepalive = 0",
        "\t\tstate = active",
        "\t}",
        "\tjetsamproperties category = daemon",
        "\tproperties = %s" % (
            "keepalive | runatload | inferred program" if keepalive
            else "inferred program"),
        "}",
    ]
    return "\n".join(lines)


LIST_FIXTURE = (
    "PID\tStatus\tLabel\n"
    "-\t0\t%sKojeni.portfolio-snapshot\n"
    "7465\t0\t%sskull\n"
    "-\t-15\t%swarg.rx-gen\n"
    "89077\t0\t%smetal.dashboard\n"
    "123\t0\tcom.apple.unrelated.job\n"
) % (PFX, PFX, PFX, PFX)


def obs_for(loaded, printinfo, disk=None, bridge_labels=None,
            la_dir="/tmp/nonexistent-la", print_errors=None):
    return {
        "loaded": loaded,
        "printinfo": printinfo,
        "print_errors": print_errors or {},
        "disk": disk or {},
        "bridge_labels": bridge_labels or set(),
        "launch_agents_dir": la_dir,
    }


def parsed(runs=None, exit_line=None, **kw):
    return ho.parse_launchctl_print(print_fixture(runs, exit_line, **kw))


class TestParsers(unittest.TestCase):

    def test_list_parse_prefix_filter_and_pid(self):
        jobs = ho.parse_launchctl_list(LIST_FIXTURE)
        self.assertEqual(jobs[PFX + "Kojeni.portfolio-snapshot"], None)
        self.assertEqual(jobs[PFX + "skull"], 7465)
        self.assertEqual(jobs[PFX + "metal.dashboard"], 89077)
        self.assertNotIn("com.apple.unrelated.job", jobs)
        self.assertEqual(len(jobs), 4)

    def test_print_state_i_exit_line_absent(self):
        info = parsed(runs=5, exit_line=None, pid=4242, state="running")
        self.assertTrue(info["parse_ok"])
        self.assertFalse(info["has_exit_line"])
        self.assertIsNone(info["last_exit"])
        self.assertEqual(info["pid"], 4242)

    def test_print_state_ii_never_exited(self):
        info = parsed(runs=0, exit_line="(never exited)")
        self.assertTrue(info["has_exit_line"])
        self.assertEqual(info["last_exit"], ho.NEVER_EXITED)

    def test_print_state_iii_negative_signal(self):
        info = parsed(runs=7, exit_line="-15")
        self.assertEqual(info["last_exit"], -15)

    def test_print_state_iv_positive(self):
        info = parsed(runs=10559, exit_line="127")
        self.assertEqual(info["runs"], 10559)
        self.assertEqual(info["last_exit"], 127)

    def test_print_keepalive_top_level_only(self):
        # sub-block "keepalive = 0" noise must NOT set keepalive; the
        # top-level properties line is the only source (live-verified shape).
        info = parsed(runs=18, exit_line="0", keepalive=False)
        self.assertFalse(info["keepalive"])
        info2 = parsed(runs=10, exit_line=None, keepalive=True, pid=89077)
        self.assertTrue(info2["keepalive"])

    def test_print_path_ignores_stdout_stderr_paths(self):
        info = parsed(runs=1, exit_line="0",
                      path="/Users/u/Library/LaunchAgents/x.plist")
        self.assertEqual(info["plist_path"],
                         "/Users/u/Library/LaunchAgents/x.plist")
        info2 = parsed(runs=1, exit_line="0", path=None)
        self.assertIsNone(info2["plist_path"])

    def test_print_garbage_is_parse_failure_not_crash(self):
        info = ho.parse_launchctl_print("Could not find service")
        self.assertFalse(info["parse_ok"])


class TestClassification(unittest.TestCase):

    def test_buckets(self):
        bridges = {PFX + "Kojeni"}
        self.assertEqual(
            ho.classify_job(PFX + "Kojeni", parsed(runs=3, exit_line=None),
                            bridges), ho.BUCKET_BRIDGE)
        self.assertEqual(
            ho.classify_job(PFX + "metal.dashboard",
                            parsed(runs=10, exit_line=None, keepalive=True,
                                   pid=1), bridges), ho.BUCKET_RESIDENT)
        self.assertEqual(
            ho.classify_job(PFX + "metal.oneshot",
                            parsed(runs=0, exit_line="(never exited)"),
                            bridges), ho.BUCKET_WAITING)
        self.assertEqual(
            ho.classify_job(PFX + "ag.mirror-poll",
                            parsed(runs=10559, exit_line="0"), bridges),
            ho.BUCKET_SCHEDULED)

    def test_bridge_membership_is_label_not_filename(self):
        # DGN-989 live proof: file <slug>.newbridge.plist carries Label
        # com.telegram-skill-bot.<slug> (no dot-suffix heuristic works).
        tmp = tempfile.mkdtemp(prefix="ho-la-")
        try:
            p = os.path.join(tmp, "Kojeni.newbridge.plist")
            with open(p, "wb") as f:
                plistlib.dump({
                    "Label": PFX + "Kojeni",
                    "ProgramArguments": [
                        "/bin/bash", "/inst/Kojeni/bridge/start.sh",
                        "--path", "/inst/Kojeni", "--_launchd_child"],
                }, f)
            bridges = ho.bridge_map_from_dir(tmp)
            self.assertEqual(bridges, {PFX + "Kojeni": "/inst/Kojeni"})
        finally:
            shutil.rmtree(tmp)


class TestStreakMachine(unittest.TestCase):

    def _poll_seq(self, seq, label=PFX + "metal.job"):
        """Run a sequence of (runs, exit_line) observations; return
        (state, all_events)."""
        state = {}
        events_all = []
        for i, (runs, exit_line) in enumerate(seq):
            obs = obs_for({label: None},
                          {label: parsed(runs=runs, exit_line=exit_line)})
            events, _ = ho.poll(state, obs, "2026-08-21T15:%02d:00+09:00" % i)
            events_all.extend(events)
        return state, events_all

    def test_first_run_is_baseline_no_false_alarm(self):
        state, events = self._poll_seq([(5, "127")])
        self.assertEqual(events, [])
        entry = state["jobs"][PFX + "metal.job"]
        self.assertEqual(entry["fail_streak"], 0)

    def test_general_streak_fires_at_3_once_per_episode(self):
        label = PFX + "metal.job"
        seq = [(1, "0"), (2, "1"), (3, "1"), (4, "1"), (5, "1"), (6, "1")]
        state, events = self._poll_seq(seq)
        fails = [e for e in events if e["kind"] == "fail_streak"]
        self.assertEqual(len(fails), 1)  # episode-once
        self.assertEqual(fails[0]["streak"], 3)
        self.assertFalse(fails[0]["structural"])
        self.assertIsNotNone(state["jobs"][label]["notified_at"])

    def test_structural_streak_fires_at_2(self):
        seq = [(1, "0"), (2, "127"), (3, "127")]
        _, events = self._poll_seq(seq)
        fails = [e for e in events if e["kind"] == "fail_streak"]
        self.assertEqual(len(fails), 1)
        self.assertEqual(fails[0]["streak"], 2)
        self.assertTrue(fails[0]["structural"])
        self.assertEqual(fails[0]["exit"], 127)

    def test_negative_exit_is_general_not_structural(self):
        seq = [(1, "0"), (2, "-15"), (3, "-15")]
        _, events = self._poll_seq(seq)
        self.assertEqual([e for e in events if e["kind"] == "fail_streak"], [])
        seq += [(4, "-15")]
        _, events = self._poll_seq(seq)
        fails = [e for e in events if e["kind"] == "fail_streak"]
        self.assertEqual(len(fails), 1)
        self.assertEqual(fails[0]["streak"], 3)
        self.assertFalse(fails[0]["structural"])

    def test_recovery_resets_episode_and_renotifies(self):
        seq = [(1, "0"), (2, "1"), (3, "1"), (4, "1"),   # fire #1 at streak 3
               (5, "0"),                                  # recovery
               (6, "1"), (7, "1"), (8, "1")]              # fire #2
        state, events = self._poll_seq(seq)
        fails = [e for e in events if e["kind"] == "fail_streak"]
        self.assertEqual(len(fails), 2)

    def test_runs_delta_negative_resets_episode(self):
        # streak 2 accumulated, then the job is reloaded (runs counter drops)
        seq = [(10, "0"), (11, "1"), (12, "1"),
               (1, "1")]  # reload: runs 12 -> 1
        state, _ = self._poll_seq(seq)
        entry = state["jobs"][PFX + "metal.job"]
        self.assertEqual(entry["fail_streak"], 0)
        self.assertIsNone(entry["episode_started_at"])
        self.assertIsNone(entry["notified_at"])

    def test_same_runs_does_not_double_count(self):
        seq = [(1, "0"), (2, "1"), (2, "1"), (2, "1"), (2, "1")]
        state, events = self._poll_seq(seq)
        self.assertEqual(state["jobs"][PFX + "metal.job"]["fail_streak"], 1)
        self.assertEqual([e for e in events if e["kind"] == "fail_streak"], [])

    def test_running_hold_keeps_streak(self):
        seq = [(1, "0"), (2, "1"), (3, None), (4, "1"), (5, "1")]
        state, events = self._poll_seq(seq)
        fails = [e for e in events if e["kind"] == "fail_streak"]
        self.assertEqual(len(fails), 1)
        self.assertEqual(fails[0]["streak"], 3)


class TestResidentService(unittest.TestCase):

    def test_resident_down_notifies_once_and_recovers(self):
        label = PFX + "metal.dashboard"
        state = {}

        def one(pid):
            info = parsed(runs=10, exit_line=None, keepalive=True, pid=pid)
            if pid is None:
                info = parsed(runs=10, exit_line="0", keepalive=True, pid=None)
            obs = obs_for({label: pid}, {label: info})
            ev, _ = ho.poll(state, obs, "2026-08-21T15:00:00+09:00")
            return ev

        self.assertEqual(one(4242), [])           # up: no event
        ev = one(None)                            # down: fires
        self.assertEqual([e["kind"] for e in ev], ["resident_down"])
        self.assertEqual(one(None), [])           # still down: episode-once
        self.assertEqual(one(4242), [])           # back up: reset
        ev = one(None)                            # down again: new episode
        self.assertEqual([e["kind"] for e in ev], ["resident_down"])


class TestLossEvents(unittest.TestCase):

    LABEL = PFX + "warg.update-1300"

    def _seed(self):
        state = {}
        obs = obs_for({self.LABEL: None},
                      {self.LABEL: parsed(runs=3, exit_line="0")})
        ho.poll(state, obs, "2026-08-21T15:00:00+09:00")
        return state

    def test_lost_label_fires_once(self):
        state = self._seed()
        obs = obs_for({}, {})
        # first miss (DGN-1427): grace window only, no event yet -- could be
        # a planned-restart gap landing inside this poll.
        ev0, _ = ho.poll(state, obs, "2026-08-21T15:05:00+09:00")
        self.assertEqual(ev0, [])
        # still missing past LOST_GRACE_SECONDS -> fires
        ev, _ = ho.poll(state, obs, "2026-08-21T15:10:00+09:00")
        self.assertEqual([e["kind"] for e in ev], ["label_lost"])
        self.assertEqual(ev[0]["label"], self.LABEL)
        # next poll: still lost -> no repeat
        ev2, _ = ho.poll(state, obs, "2026-08-21T15:15:00+09:00")
        self.assertEqual(ev2, [])

    def test_restart_gap_within_grace_fires_nothing(self):
        """DGN-1427 acceptance: a poll landing inside a planned-restart gap
        (measured live 2m15s) must produce zero events."""
        state = self._seed()  # last_seen_at 15:00:00
        # one poll catches the label mid-restart (2m15s gap, well under the
        # 240s grace) -- pending only, no notify.
        ev, _ = ho.poll(state, obs_for({}, {}), "2026-08-21T15:02:15+09:00")
        self.assertEqual(ev, [])
        # job is back before grace elapses -> loss marker clears, no event.
        obs_back = obs_for({self.LABEL: 43163},
                           {self.LABEL: parsed(runs=4, exit_line="0")})
        ev2, _ = ho.poll(state, obs_back, "2026-08-21T15:03:00+09:00")
        self.assertEqual(ev2, [])
        self.assertIsNone(state["jobs"][self.LABEL]["lost_at"])

    def test_real_absence_past_grace_still_fires(self):
        """A genuine vanish (not a restart) still notifies, just delayed to
        past LOST_GRACE_SECONDS instead of on the very first miss."""
        state = self._seed()
        ev0, _ = ho.poll(state, obs_for({}, {}), "2026-08-21T15:05:00+09:00")
        self.assertEqual(ev0, [])
        ev, _ = ho.poll(state, obs_for({}, {}), "2026-08-21T15:09:01+09:00")
        self.assertEqual([e["kind"] for e in ev], ["label_lost"])

    def test_retired_marker_suppresses_loss(self):
        state = self._seed()
        state["jobs"][self.LABEL]["retired"] = {
            "when": "2026-08-21T15:04:00+09:00", "why": "job removed on purpose"}
        ev, _ = ho.poll(state, obs_for({}, {}), "2026-08-21T15:05:00+09:00")
        self.assertEqual(ev, [])
        self.assertIsNotNone(state["jobs"][self.LABEL]["lost_at"])
        self.assertIsNone(state["jobs"][self.LABEL].get("lost_notified_at"))

    def test_reappearance_clears_then_new_loss_fires_again(self):
        state = self._seed()  # last_seen_at 15:00:00
        ho.poll(state, obs_for({}, {}), "2026-08-21T15:05:00+09:00")  # miss 1: pending
        ev1, _ = ho.poll(state, obs_for({}, {}), "2026-08-21T15:10:00+09:00")
        self.assertEqual([e["kind"] for e in ev1], ["label_lost"])   # loss #1
        obs_back = obs_for({self.LABEL: None},
                           {self.LABEL: parsed(runs=4, exit_line="0")})
        ho.poll(state, obs_back, "2026-08-21T15:15:00+09:00")        # reappears
        self.assertIsNone(state["jobs"][self.LABEL]["lost_at"])
        ho.poll(state, obs_for({}, {}), "2026-08-21T15:20:00+09:00")  # miss again: pending
        ev2, _ = ho.poll(state, obs_for({}, {}), "2026-08-21T15:25:00+09:00")
        self.assertEqual([e["kind"] for e in ev2], ["label_lost"])   # loss #2

    def test_first_run_empty_known_set_no_loss_storm(self):
        state = {}
        ev, _ = ho.poll(state, obs_for({}, {}), "t0")
        self.assertEqual(ev, [])


class TestDgn989Regression(unittest.TestCase):
    """The live DGN-989 drift, rebuilt as a disk fixture (spec success
    criterion 6). 2 jobs loaded from outside LaunchAgents = the only WARNs
    (reboot non-survivors). The 9 on-disk-unloaded jobs are INFO-only
    records: static disk state is not a signal (coordinator correction
    2026-08-21 -- all 59 live not_loaded entries were benign; e.g.
    consolidate runs via the dawn-memory-queue successor rail while the
    per-instance plist is an inherited orphan)."""

    UNLOADED_9 = [
        "backup-data", "classify-inbox-0500", "consolidate-0430",
        "generic-brief-morning", "generic-brief-retro", "generic-brief-weekly",
        "regime-briefing-0800", "regime-weekly", "routine-roller",
    ]
    OUTSIDE_2 = ["jpy-vix-watch", "thesis-earnings-watch"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-989-")
        self.la = os.path.join(self.tmp, "LaunchAgents")
        self.root = os.path.join(self.tmp, "poc", "Kojeni")
        self.routines = os.path.join(self.root, "routines")
        os.makedirs(self.la)
        os.makedirs(self.routines)

        def write(directory, filename, label):
            with open(os.path.join(directory, filename), "wb") as f:
                plistlib.dump({"Label": label, "ProgramArguments": ["/bin/true"]}, f)

        # bridge: file name deliberately != label (live-verified trap)
        with open(os.path.join(self.la, "Kojeni.newbridge.plist"), "wb") as f:
            plistlib.dump({
                "Label": PFX + "Kojeni",
                "ProgramArguments": ["/bin/bash", "start.sh",
                                     "--path", self.root, "--_launchd_child"],
            }, f)
        # a healthy job: plist in LaunchAgents AND loaded
        write(self.la, PFX + "Kojeni.ism-reminder.plist",
              PFX + "Kojeni.ism-reminder")
        # stale backup copies must be ignored (suffix-exact .plist match)
        with open(os.path.join(
                self.la, PFX + "Kojeni.ism-reminder.plist.bak-20260821-cli"),
                "w") as f:
            f.write("junk")
        # 2 loaded-outside: bootstrap plists live in instance routines/ only
        for j in self.OUTSIDE_2:
            write(self.routines, PFX + "Kojeni.%s.plist" % j,
                  PFX + "Kojeni." + j)
        # 9 on-disk-unloaded
        for j in self.UNLOADED_9:
            write(self.routines, PFX + "Kojeni.%s.plist" % j,
                  PFX + "Kojeni." + j)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_both_warn_directions(self):
        bridges = ho.bridge_map_from_dir(self.la)
        self.assertEqual(bridges, {PFX + "Kojeni": self.root})
        disk = ho.scan_disk(self.la, [os.path.join(r, "routines")
                                      for r in bridges.values()])

        loaded = {PFX + "Kojeni": 7465, PFX + "Kojeni.ism-reminder": None}
        printinfo = {
            PFX + "Kojeni": parsed(
                runs=1, exit_line=None, keepalive=True, pid=7465,
                path=os.path.join(self.la, "Kojeni.newbridge.plist")),
            PFX + "Kojeni.ism-reminder": parsed(
                runs=4, exit_line="0",
                path=os.path.join(self.la, PFX + "Kojeni.ism-reminder.plist")),
        }
        for j in self.OUTSIDE_2:
            lbl = PFX + "Kojeni." + j
            loaded[lbl] = None
            printinfo[lbl] = parsed(
                runs=18, exit_line="0",
                path=os.path.join(self.routines, "%s.plist" % lbl))

        obs = obs_for(loaded, printinfo, disk=disk,
                      bridge_labels=set(bridges), la_dir=self.la)
        state = {}
        events, warnings = ho.poll(state, obs, "2026-08-21T15:00:00+09:00")

        nonsurvivors = sorted(w["label"] for w in warnings
                              if w["kind"] == "reboot_nonsurvivor")
        self.assertEqual(
            nonsurvivors, sorted(PFX + "Kojeni." + j for j in self.OUTSIDE_2))

        not_loaded = sorted(w["label"] for w in warnings
                            if w["kind"] == "not_loaded")
        self.assertEqual(
            not_loaded, sorted(PFX + "Kojeni." + j for j in self.UNLOADED_9))

        # EXPLICITLY INVERTED (coordinator correction 2026-08-21): the 9
        # on-disk-unloaded jobs are INFO, never WARN. Warn-level = only the
        # 2 reboot non-survivors. Static disk state is not a signal.
        warn_level = sorted(w["label"] for w in warnings
                            if w["level"] == "warn")
        self.assertEqual(
            warn_level, sorted(PFX + "Kojeni." + j for j in self.OUTSIDE_2))
        for w in warnings:
            if w["kind"] == "not_loaded":
                self.assertEqual(w["level"], "info")

        self.assertEqual(len(warnings), 11)  # 2 warn + 9 info records
        self.assertEqual(events, [])  # records are state surface, not pushes
        # records persist in the state file for /health
        self.assertEqual(len(state["warnings"]), 11)
        # dynamic counts, no constants: bridge 1 + scheduled 3
        self.assertEqual(state["counts"][ho.BUCKET_BRIDGE], 1)
        self.assertEqual(state["counts"][ho.BUCKET_SCHEDULED], 3)

    def test_retired_marker_suppresses_not_loaded_warn(self):
        bridges = ho.bridge_map_from_dir(self.la)
        disk = ho.scan_disk(self.la, [os.path.join(r, "routines")
                                      for r in bridges.values()])
        state = {"jobs": {PFX + "Kojeni.regime-briefing-0800": {
            "retired": {"when": "t", "why": "renamed to 0830"}}}}
        obs = obs_for({}, {}, disk=disk, bridge_labels=set(bridges),
                      la_dir=self.la)
        _, warnings = ho.poll(state, obs, "t0")
        labels = [w["label"] for w in warnings if w["kind"] == "not_loaded"]
        self.assertNotIn(PFX + "Kojeni.regime-briefing-0800", labels)


class TestPlistDivergence(unittest.TestCase):
    """Coordinator correction 2026-08-21: reboot_nonsurvivor is judged by
    LaunchAgents Label-set membership, NEVER by the loaded copy's path. A
    label whose LaunchAgents plist exists but whose loaded copy is a
    different file is plist_divergence -- info when contents are equivalent,
    warn when they differ. Live case rebuilt verbatim: an
    instance bridge label, LaunchAgents copy vs loaded repo copy
    differing in one EnvironmentVariables.PATH line."""

    LABEL = PFX + "metal"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-div-")
        self.la = os.path.join(self.tmp, "LaunchAgents")
        self.repo = os.path.join(self.tmp, "dev-crew", "agents", "metal",
                                 "bridge")
        os.makedirs(self.la)
        os.makedirs(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write(self, directory, path_env):
        p = os.path.join(directory,
                         "com.telegram-skill-bot.metal.newbridge.plist")
        with open(p, "wb") as f:
            plistlib.dump({
                "Label": self.LABEL,
                "ProgramArguments": [
                    "/bin/bash", "/inst/metal/bridge/start.sh",
                    "--path", "/inst/metal", "--_launchd_child"],
                "EnvironmentVariables": {"PATH": path_env,
                                         "HOME": "/Users/u"},
                "KeepAlive": True,
            }, f)
        return p

    def _poll(self, loaded_path):
        disk = ho.scan_disk(self.la, [])
        printinfo = {self.LABEL: parsed(runs=1, exit_line=None, keepalive=True,
                                        pid=7465, path=loaded_path)}
        obs = obs_for({self.LABEL: 7465}, printinfo, disk=disk,
                      bridge_labels={self.LABEL}, la_dir=self.la)
        state = {}
        _, warnings = ho.poll(state, obs, "t0")
        return warnings

    def test_metal_case_diverging_copies_warn_not_nonsurvivor(self):
        # live-measured difference: LaunchAgents copy leads with ~/.local/bin,
        # loaded repo copy leads with ~/.npm-global/bin
        self._write(self.la,
                    "/Users/u/.local/bin:/opt/homebrew/bin:/usr/bin:/bin")
        repo_p = self._write(
            self.repo,
            "/Users/u/.npm-global/bin:/opt/homebrew/bin:/usr/bin:/bin")
        warnings = self._poll(repo_p)
        kinds = [w["kind"] for w in warnings]
        self.assertNotIn("reboot_nonsurvivor", kinds)  # the false positive
        div = [w for w in warnings if w["kind"] == "plist_divergence"]
        self.assertEqual(len(div), 1)
        self.assertEqual(div[0]["label"], self.LABEL)
        self.assertEqual(div[0]["level"], "warn")
        self.assertIn("differ", div[0]["detail"])

    def test_identical_copies_info_only_no_warn(self):
        path_env = "/Users/u/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"
        self._write(self.la, path_env)
        repo_p = self._write(self.repo, path_env)
        warnings = self._poll(repo_p)
        self.assertNotIn("reboot_nonsurvivor", [w["kind"] for w in warnings])
        div = [w for w in warnings if w["kind"] == "plist_divergence"]
        self.assertEqual(len(div), 1)
        self.assertEqual(div[0]["level"], "info")  # record only, no warning
        self.assertEqual([w for w in warnings if w["level"] == "warn"], [])

    def test_loaded_from_la_copy_is_clean(self):
        la_p = self._write(self.la,
                           "/Users/u/.local/bin:/opt/homebrew/bin:/usr/bin")
        warnings = self._poll(la_p)
        self.assertEqual(warnings, [])

    def test_no_la_copy_is_still_nonsurvivor(self):
        # DGN-989 (A) semantics survive the correction: no LaunchAgents plist
        # for the label at all -> reboot_nonsurvivor.
        repo_p = self._write(self.repo, "/usr/bin:/bin")
        warnings = self._poll(repo_p)
        self.assertEqual([w["kind"] for w in warnings],
                         ["reboot_nonsurvivor"])

    def test_plists_equivalent_normalized_not_bytewise(self):
        # xml vs binary of the SAME content must compare equal (plutil -p
        # style normalization, not a byte compare).
        data = {"Label": self.LABEL, "StartInterval": 300}
        p_xml = os.path.join(self.tmp, "a.plist")
        p_bin = os.path.join(self.tmp, "b.plist")
        with open(p_xml, "wb") as f:
            plistlib.dump(data, f, fmt=plistlib.FMT_XML)
        with open(p_bin, "wb") as f:
            plistlib.dump(data, f, fmt=plistlib.FMT_BINARY)
        self.assertTrue(ho.plists_equivalent(p_xml, p_bin))
        with open(p_bin, "wb") as f:
            plistlib.dump(dict(data, StartInterval=600), f,
                          fmt=plistlib.FMT_BINARY)
        self.assertFalse(ho.plists_equivalent(p_xml, p_bin))


class TestStatePersistence(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-state-")
        os.environ["DOGANY_HEALTH_OBSERVER_STATE_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("DOGANY_HEALTH_OBSERVER_STATE_DIR", None)
        shutil.rmtree(self.tmp)

    def test_roundtrip_atomic(self):
        state = {"jobs": {"a": {"runs": 1}}, "heartbeat_at": "t"}
        ho.save_state(state)
        loaded, warn = ho.load_state()
        self.assertIsNone(warn)
        self.assertEqual(loaded["jobs"]["a"]["runs"], 1)

    def test_corrupt_state_quarantined_and_loud(self):
        with open(ho.state_path(), "w") as f:
            f.write("{ this is not json")
        loaded, warn = ho.load_state()
        self.assertEqual(loaded, {})
        self.assertEqual(warn["kind"], "state_corrupt_recovered")
        self.assertFalse(os.path.exists(ho.state_path()))
        quarantined = [n for n in os.listdir(self.tmp)
                       if n.startswith("jobs.json.corrupt-")]
        self.assertEqual(len(quarantined), 1)

    def test_retire_cli_roundtrip(self):
        label = PFX + "Kojeni.regime-briefing-0800"
        # DGN-1610: an unseen label is rejected unless --force -- this test
        # exercises the CLI roundtrip, not the unknown-label gate (T1/T3
        # below cover that), so pass force=True.
        rc = ho.cmd_retire(label, "renamed to 0830", force=True)
        self.assertEqual(rc, 0)
        state, _ = ho.load_state()
        self.assertEqual(state["jobs"][label]["retired"]["why"],
                         "renamed to 0830")
        ho.cmd_unretire(label)
        state, _ = ho.load_state()
        self.assertNotIn("retired", state["jobs"][label])


class TestMachineMetrics(unittest.TestCase):
    """DGN-1713: no host reading or real inbox is used in these tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-machine-")
        self.root = os.path.join(self.tmp, "steward")
        os.makedirs(os.path.join(self.root, "config"))
        with open(os.path.join(self.root, ".instance.conf"), "w") as f:
            f.write("DOGANY_AGENT_CLASS=main\n")
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as f:
            f.write("HEALTH_OBSERVER_SWAP_WARN_MB=4096\n"
                    "HEALTH_OBSERVER_LOAD_WARN_MULTIPLIER=1.5\n"
                    "HEALTH_OBSERVER_LOAD_WARN_SECONDS=900\n"
                    "HEALTH_OBSERVER_DISK_WARN_PCT=85\n"
                    "HEALTH_OBSERVER_MACHINE_ALERT_WINDOW_SECONDS=900\n")
        self.old_state = os.environ.get("DOGANY_HEALTH_OBSERVER_STATE_DIR")
        os.environ["DOGANY_HEALTH_OBSERVER_STATE_DIR"] = os.path.join(
            self.tmp, "shared-home", "health-observer")

    def tearDown(self):
        if self.old_state is None:
            os.environ.pop("DOGANY_HEALTH_OBSERVER_STATE_DIR", None)
        else:
            os.environ["DOGANY_HEALTH_OBSERVER_STATE_DIR"] = self.old_state
        shutil.rmtree(self.tmp)

    def test_swap_and_disk_alert_once_per_breach(self):
        readings = {"swap_used_mb": "5000", "load5": "1", "ncpu": "4",
                    "disk_used_pct": "90"}
        first = ho.observe_machine_metrics(self.root, readings, now_epoch=100)
        self.assertEqual({e["kind"] for e in first}, {"swap", "disk"})
        self.assertEqual({e["notice"] for e in first}, {"breach"})
        self.assertEqual(ho.observe_machine_metrics(self.root, readings,
                                                     now_epoch=200), [])
        # DGN-1773: an unchanged breach is not news, even past the old
        # 15-minute window the agent.conf above still names.
        self.assertEqual(ho.observe_machine_metrics(self.root, readings,
                                                     now_epoch=1000), [])
        box = os.path.join(self.root, ".telegram_bot", "session-inbox")
        drops = [n for n in os.listdir(box) if n.endswith(".md")]
        self.assertEqual(len(drops), 1)
        with open(os.path.join(box, drops[0])) as f:
            body = f.read()
        self.assertIn("swap_used_mb", body)
        self.assertIn("speak to the owner in your own words", body)
        self.assertNotIn("push.sh", body)

    def test_dgn1773_static_disk_breach_alerts_once_over_hours(self):
        # The owner's 2026-09-28 morning: 86-87% for hours, polled every
        # 5 minutes -> one alert, not one per 15 minutes.
        events = []
        for i in range(36):
            pct = "86" if i % 3 else "87"
            events += ho.observe_machine_metrics(
                self.root, {"disk_used_pct": pct}, now_epoch=100 + i * 300)
        self.assertEqual([(e["kind"], e["notice"]) for e in events],
                         [("disk", "breach")])

    def test_dgn1773_worsening_step_realerts_once(self):
        obs = lambda pct, t: ho.observe_machine_metrics(
            self.root, {"disk_used_pct": str(pct)}, now_epoch=t)
        self.assertEqual([e["notice"] for e in obs(86, 100)], ["breach"])
        self.assertEqual(obs(87, 400), [])            # +1pp: below the step
        worse = obs(88, 700)                          # +2pp since last notice
        self.assertEqual([e["notice"] for e in worse], ["worsened"])
        self.assertEqual(worse[0]["disk_used_pct"], 88)
        self.assertEqual(worse[0]["last_notice_value"], 86)
        self.assertEqual(obs(88, 1000), [])
        self.assertEqual(obs(89, 1300), [])           # step counts from 88 now
        self.assertEqual(obs(87, 1600), [])           # improving is quiet
        self.assertEqual([e["notice"] for e in obs(90, 1900)], ["worsened"])

    def test_dgn1773_swap_worsening_step_is_1024mb(self):
        obs = lambda mb, t: ho.observe_machine_metrics(
            self.root, {"swap_used_mb": str(mb)}, now_epoch=t)
        self.assertEqual(len(obs(5000, 100)), 1)
        self.assertEqual(obs(6000, 400), [])
        self.assertEqual([e["notice"] for e in obs(6024, 700)], ["worsened"])

    def test_dgn1773_long_reminder_for_a_static_breach(self):
        obs = lambda t: ho.observe_machine_metrics(
            self.root, {"disk_used_pct": "86"}, now_epoch=t)
        self.assertEqual(len(obs(100)), 1)
        self.assertEqual(obs(100 + 86400 - 1), [])
        self.assertEqual([e["notice"] for e in obs(100 + 86400)], ["reminder"])
        self.assertEqual(obs(100 + 86400 + 300), [])

    def test_dgn1773_recovery_then_rebreach_alerts_again(self):
        # DGN-1879: a re-breach counts as new only past the re-arm window.
        obs = lambda pct, t: ho.observe_machine_metrics(
            self.root, {"disk_used_pct": str(pct)}, now_epoch=t)
        self.assertEqual(len(obs(86, 100)), 1)
        self.assertEqual(obs(80, 400), [])            # recovery: no alert
        rearm = ho.DEFAULT_MACHINE_REARM_SECONDS
        self.assertEqual(obs(86, 400 + rearm - 1), [])  # same episode
        self.assertEqual(obs(80, 700 + rearm), [])
        again = obs(86, 700 + 2 * rearm)
        self.assertEqual([e["notice"] for e in again], ["breach"])
        self.assertEqual(again[0]["breach_started_at"], 700 + 2 * rearm)

    def test_dgn1879_oscillating_disk_is_one_breach(self):
        # The ticket's series: 86% delivered four times overnight with a
        # dip to 85% (and lower) between polls, each dip opening a new
        # breach_started_at.  Clear level is 85 - 2 = 83: 84..85 holds; a
        # dip to 83 or below clears, and re-crossing within the re-arm
        # window resumes the same episode.
        series = [86, 85, 86, 84, 86, 83, 87, 85, 86, 82, 86, 85, 86, 84, 86]
        events = []
        for i, pct in enumerate(series):
            events += ho.observe_machine_metrics(
                self.root, {"disk_used_pct": str(pct)},
                now_epoch=1000 + i * 600)
        self.assertEqual([(e["kind"], e["notice"]) for e in events],
                         [("disk", "breach")])
        state = ho.load_machine_state()
        self.assertEqual(state["conditions"]["disk"]["breach_started_at"],
                         1000)

    def test_dgn1879_band_holds_then_clear_below_margin(self):
        obs = lambda pct, t: ho.observe_machine_metrics(
            self.root, {"disk_used_pct": str(pct)}, now_epoch=t)
        self.assertEqual(len(obs(86, 100)), 1)
        for t, pct in ((400, 85), (700, 84), (1000, 85)):
            self.assertEqual(obs(pct, t), [])
            self.assertIn("disk", ho.load_machine_state()["conditions"])
        self.assertEqual(obs(83, 1300), [])          # at 85 - 2: cleared
        state = ho.load_machine_state()
        self.assertNotIn("disk", state["conditions"])
        self.assertEqual(state["cleared"]["disk"]["breach_started_at"], 100)
        # Re-cross inside the window resumes the episode, worsening still
        # counts from the last notice (86): 87 quiet, 88 worsened.
        self.assertEqual(obs(87, 1600), [])
        worse = obs(88, 1900)
        self.assertEqual([e["notice"] for e in worse], ["worsened"])
        self.assertEqual(worse[0]["breach_started_at"], 100)

    def test_dgn1879_band_does_not_trigger_reminder(self):
        obs = lambda pct, t: ho.observe_machine_metrics(
            self.root, {"disk_used_pct": str(pct)}, now_epoch=t)
        self.assertEqual(len(obs(86, 100)), 1)
        self.assertEqual(obs(84, 100 + 86400), [])   # held, not over: quiet
        self.assertEqual([e["notice"] for e in obs(86, 100 + 86700)],
                         ["reminder"])

    def test_dgn1879_oscillating_swap_is_one_breach(self):
        # 4096 MB threshold, 512 MB margin: 3585..4096 holds.
        series = [5000, 4000, 4200, 3600, 4500, 3585, 4100]
        events = []
        for i, mb in enumerate(series):
            events += ho.observe_machine_metrics(
                self.root, {"swap_used_mb": str(mb)}, now_epoch=100 + i * 300)
        self.assertEqual([e["notice"] for e in events], ["breach"])
        self.assertEqual(ho.observe_machine_metrics(
            self.root, {"swap_used_mb": "3584"}, now_epoch=5000), [])
        self.assertIn("swap", ho.load_machine_state()["cleared"])

    def test_dgn1879_oscillating_load_after_notice_is_one_breach(self):
        # ncpu 4: alert over 6.0, clear at 5.0 or lower once notified.
        reading = lambda load: {"load5": str(load), "ncpu": "4"}
        obs = lambda load, t: ho.observe_machine_metrics(
            self.root, reading(load), now_epoch=t)
        self.assertEqual(obs(7, 100), [])
        self.assertEqual([e["kind"] for e in obs(7, 1000)], ["load"])
        events = []
        for i, load in enumerate([5.5, 7, 5.2, 6.5, 4.5, 7, 7, 7, 7]):
            events += obs(load, 1300 + i * 300)
        self.assertEqual(events, [])

    def test_dgn1879_load_sustain_still_needs_staying_over(self):
        # Before the first notice, the plain threshold applies: a dip into
        # the band restarts the 15-minute sustain window as before.
        obs = lambda load, t: ho.observe_machine_metrics(
            self.root, {"load5": str(load), "ncpu": "4"}, now_epoch=t)
        self.assertEqual(obs(7, 100), [])
        self.assertEqual(obs(5.5, 600), [])
        self.assertEqual(obs(7, 900), [])
        self.assertEqual(obs(7, 1000), [])           # 100 + 900: reset
        self.assertEqual([e["kind"] for e in obs(7, 1800)], ["load"])

    def test_dgn1879_margins_from_agent_conf(self):
        with open(os.path.join(self.root, "config", "agent.conf"), "a") as f:
            f.write("HEALTH_OBSERVER_DISK_CLEAR_MARGIN_PCT=0\n"
                    "HEALTH_OBSERVER_MACHINE_REARM_SECONDS=0\n")
        t = ho.machine_thresholds(self.root)
        self.assertEqual((t["disk_clear_margin_pct"], t["rearm_seconds"]),
                         (0, 0))
        self.assertEqual(t["swap_clear_margin_mb"],
                         ho.DEFAULT_SWAP_CLEAR_MARGIN_MB)
        self.assertEqual(t["load_clear_margin"], ho.DEFAULT_LOAD_CLEAR_MARGIN)
        # Both off: the pre-DGN-1879 behavior, every re-cross is new.
        obs = lambda pct, ts: ho.observe_machine_metrics(
            self.root, {"disk_used_pct": str(pct)}, now_epoch=ts)
        self.assertEqual(len(obs(86, 100)), 1)
        self.assertEqual(obs(85, 400), [])
        self.assertEqual([e["notice"] for e in obs(86, 700)], ["breach"])

    def test_dgn1773_steps_and_reminder_from_agent_conf(self):
        with open(os.path.join(self.root, "config", "agent.conf"), "a") as f:
            f.write("HEALTH_OBSERVER_DISK_STEP_PCT=5\n"
                    "HEALTH_OBSERVER_SWAP_STEP_MB=512\n"
                    "HEALTH_OBSERVER_MACHINE_REMIND_SECONDS=3600\n")
        t = ho.machine_thresholds(self.root)
        self.assertEqual((t["disk_step_pct"], t["swap_step_mb"],
                          t["remind_seconds"]), (5, 512, 3600))
        obs = lambda pct, ts: ho.observe_machine_metrics(
            self.root, {"disk_used_pct": str(pct)}, now_epoch=ts)
        self.assertEqual(len(obs(86, 100)), 1)
        self.assertEqual(obs(90, 400), [])
        self.assertEqual([e["notice"] for e in obs(91, 700)], ["worsened"])
        self.assertEqual([e["notice"] for e in obs(91, 700 + 3600)],
                         ["reminder"])

    def test_dgn1773_nonpositive_step_falls_back_to_default(self):
        # A zero step would re-alert every poll -- the defect this fixes.
        with open(os.path.join(self.root, "config", "agent.conf"), "a") as f:
            f.write("HEALTH_OBSERVER_DISK_STEP_PCT=0\n"
                    "HEALTH_OBSERVER_MACHINE_REMIND_SECONDS=0\n")
        t = ho.machine_thresholds(self.root)
        self.assertEqual(t["disk_step_pct"], ho.DEFAULT_DISK_STEP_PCT)
        self.assertEqual(t["remind_seconds"],
                         ho.DEFAULT_MACHINE_REMIND_SECONDS)

    def test_load_requires_fifteen_minutes_then_dedupes(self):
        readings = {"swap_used_mb": "0", "load5": "7", "ncpu": "4",
                    "disk_used_pct": "10"}  # 7 > 4 * 1.5
        self.assertEqual(ho.observe_machine_metrics(self.root, readings, 100), [])
        self.assertEqual(ho.observe_machine_metrics(self.root, readings, 999), [])
        events = ho.observe_machine_metrics(self.root, readings, 1000)
        self.assertEqual([e["kind"] for e in events], ["load"])
        self.assertEqual(ho.observe_machine_metrics(self.root, readings, 1800), [])
        # DGN-1773: a sustained load breach does not re-alert on the old
        # 15-minute cadence; load has no worsening step, only the reminder.
        self.assertEqual(ho.observe_machine_metrics(
            self.root, dict(readings, load5="12"), 2000), [])
        # A healthy interval closes the episode; a breach after the
        # DGN-1879 re-arm window is new and needs the sustain again.
        healthy = dict(readings, load5="1")
        self.assertEqual(ho.observe_machine_metrics(self.root, healthy, 2100), [])
        later = 2100 + ho.DEFAULT_MACHINE_REARM_SECONDS
        self.assertEqual(ho.observe_machine_metrics(self.root, readings, later), [])
        self.assertEqual([e["kind"] for e in ho.observe_machine_metrics(
            self.root, readings, later + 900)], ["load"])

    def test_domain_instance_never_emits_or_writes_shared_state(self):
        with open(os.path.join(self.root, ".instance.conf"), "w") as f:
            f.write("DOGANY_AGENT_CLASS=domain\n")
        events = ho.observe_machine_metrics(
            self.root, {"swap_used_mb": "9999"}, now_epoch=1)
        self.assertEqual(events, [])
        self.assertFalse(os.path.exists(ho.machine_state_path()))

    def test_faked_loadcheck_output_and_threshold_config(self):
        values = ho.parse_key_values("load5=3.5\nncpu=2\nswap_used_mb=42\n")
        self.assertEqual(values["load5"], "3.5")
        thresholds = ho.machine_thresholds(self.root)
        self.assertEqual(thresholds["swap_mb"], 4096)
        self.assertEqual(thresholds["load_seconds"], 900)


class TestDawnNeverRan(unittest.TestCase):
    """DGN-1582 r3 (dec-236): no launchd, no real ~/.dogany -- fixture dirs."""

    def setUp(self):
        import datetime
        self.tmp = tempfile.mkdtemp(prefix="ho-dawn-")
        self.root = os.path.join(self.tmp, "steward")
        os.makedirs(os.path.join(self.root, "config", "i18n"))
        with open(os.path.join(self.root, ".instance.conf"), "w") as f:
            f.write("DOGANY_AGENT_CLASS=main\n")
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as f:
            f.write("AGENT_LANG=ko\n")
        for lang in ("ko", "en"):
            shutil.copy(os.path.join(ROUTINES, "..", "config", "i18n",
                                     "%s.json" % lang),
                        os.path.join(self.root, "config", "i18n"))
        self.paths = {k: os.path.join(self.tmp, k)
                      for k in ("registry", "logs", "lock")}
        os.makedirs(self.paths["registry"])
        os.makedirs(self.paths["logs"])
        self.d0 = datetime.date(2026, 9, 1)
        conf = os.path.join(self.paths["registry"], "a.conf")
        with open(conf, "w") as f:
            f.write("ROOT=/x\nENABLED=1\n")
        reg = self.at(-1, 12)                  # registered the day before d0
        os.utime(conf, (reg, reg))
        self.ran(0)                            # d0's night ran
        self.old_state = os.environ.get("DOGANY_HEALTH_OBSERVER_STATE_DIR")
        os.environ["DOGANY_HEALTH_OBSERVER_STATE_DIR"] = os.path.join(
            self.tmp, "health-observer")

    def tearDown(self):
        if self.old_state is None:
            os.environ.pop("DOGANY_HEALTH_OBSERVER_STATE_DIR", None)
        else:
            os.environ["DOGANY_HEALTH_OBSERVER_STATE_DIR"] = self.old_state
        shutil.rmtree(self.tmp)

    def at(self, day, hour, minute=0):
        import datetime
        d = self.d0 + datetime.timedelta(days=day)
        return time.mktime((d.year, d.month, d.day, hour, minute, 0, 0, 0, -1))

    def ran(self, day):
        import datetime
        d = self.d0 + datetime.timedelta(days=day)
        open(os.path.join(self.paths["logs"],
                          "queue-%s.log" % d.strftime("%Y%m%d")), "w").close()

    def poll(self, t):
        return ho.observe_dawn_runs(self.root, now_epoch=t, paths=self.paths)

    def notices(self, events):
        sent = []
        ho.notify_dawn_never_ran(
            events, self.root,
            sender=lambda text, btn, audience="owner": sent.append((audience, text)))
        return sent

    def test_night1_silent_night2_owner_and_operator_once_night3_quiet(self):
        self.assertEqual(self.poll(self.at(1, 9)), [], "night 1 is silent")
        self.assertEqual(self.poll(self.at(2, 9)), [],
                         "night 2 first sighting waits for confirmation")
        events = self.poll(self.at(2, 9, 11))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["nights"], 2)
        sent = self.notices(events)
        self.assertEqual([a for a, _ in sent], ["owner", "operator"])
        self.assertEqual(sent[0][1],
                         "⏳ 새벽에 Mac이 꺼져 있어서 이틀째 기억 정리를 못 했어요. "
                         "새벽 4시쯤 켜 두시면 다음 날부터 정리돼요.")
        self.assertEqual(sent[1][1], sent[0][1])
        sys.path.insert(0, os.path.join(ROUTINES, ".."))
        from bridge import machine_gate
        self.assertFalse(machine_gate.find_execution_identifiers(sent[0][1]),
                         "push.sh's identifier guard must pass it")
        self.assertEqual(self.poll(self.at(2, 12)), [], "same day: no repeat")
        self.assertEqual(self.poll(self.at(3, 9)), [], "night 3: no repeat")
        self.assertEqual(self.poll(self.at(3, 9, 20)), [])

    def test_a_night_that_ran_resets_the_streak(self):
        self.poll(self.at(2, 9))
        self.assertEqual(len(self.poll(self.at(2, 9, 11))), 1)
        self.ran(3)
        self.assertEqual(self.poll(self.at(3, 9)), [], "ran -> streak over")
        self.assertEqual(self.poll(self.at(4, 9)), [], "new streak, night 1")
        self.poll(self.at(5, 9))
        self.assertEqual(len(self.poll(self.at(5, 9, 11))), 1,
                         "new streak, night 2 -> notified again")

    def test_late_run_after_wake_counts_as_ran(self):
        # Asleep at 04:00, woke at 09:00: launchd starts the missed queue
        # run; the observer's poll may land first. It holds while the queue
        # runs and the night is not missed once the queue has logged.
        self.poll(self.at(1, 9))
        os.makedirs(self.paths["lock"])
        self.assertEqual(self.poll(self.at(2, 9)), [])
        self.assertEqual(self.poll(self.at(2, 9, 30)), [], "queue running: hold")
        self.ran(2)
        os.rmdir(self.paths["lock"])
        self.assertEqual(self.poll(self.at(2, 10)), [])
        self.assertEqual(self.poll(self.at(3, 9)), [], "night 2 ran: streak reset")

    def test_not_due_before_dawn(self):
        self.poll(self.at(1, 9))
        self.assertEqual(self.poll(self.at(2, 3)), [])
        self.assertEqual(self.poll(self.at(2, 4, 20)), [],
                         "04:20 -- tonight is not judged yet")

    def test_never_judged_without_queue_or_steward(self):
        with open(os.path.join(self.root, ".instance.conf"), "w") as f:
            f.write("DOGANY_AGENT_CLASS=domain\n")
        self.poll(self.at(2, 9))
        self.assertEqual(self.poll(self.at(2, 9, 11)), [], "non-steward")
        with open(os.path.join(self.root, ".instance.conf"), "w") as f:
            f.write("DOGANY_AGENT_CLASS=main\n")
        for n in os.listdir(self.paths["logs"]):
            os.remove(os.path.join(self.paths["logs"], n))
        self.poll(self.at(2, 9))
        self.assertEqual(self.poll(self.at(2, 9, 11)), [],
                         "queue never logged a run (not installed)")
        self.ran(0)
        with open(os.path.join(self.paths["registry"], "a.conf"), "w") as f:
            f.write("ROOT=/x\nENABLED=0\n")
        self.poll(self.at(2, 9))
        self.assertEqual(self.poll(self.at(2, 9, 11)), [], "no enabled instance")

    def test_nights_before_registration_are_not_missed(self):
        conf = os.path.join(self.paths["registry"], "a.conf")
        reg = self.at(1, 12)                   # registered after night 1
        os.utime(conf, (reg, reg))
        self.poll(self.at(2, 9))
        self.assertEqual(self.poll(self.at(2, 9, 11)), [])


class TestNotificationCopy(unittest.TestCase):
    """Dead-end forbidden: every push must carry sentence CTAs + a button."""

    def test_fail_streak_copy_has_ctas_and_button(self):
        ev = {"kind": "fail_streak", "label": PFX + "warg.update-1300",
              "streak": 2, "exit": 127, "structural": True,
              "episode_started_at": "2026-08-19T13:00:00+09:00"}
        text, button = ho.compose_fail_streak(ev, "metal")
        self.assertIn("예약 작업 실패 원인 봐줘", text)
        self.assertIn("예약 작업 없애줘", text)
        self.assertIn(">! 기술 상세", text)
        self.assertIn(PFX + "warg.update-1300", text)
        self.assertIn("127", text)
        # domain-live job observed from another instance -> domain-lane wording
        self.assertIn("스컬", text)
        self.assertTrue(button.endswith("::opt:1"))
        self.assertIn("실패 원인 봐줘", button)

    def test_scheduled_job_names_are_korean_and_unknown_is_folded(self):
        approved = {
            "generic-brief-morning": "모닝 브리핑",
            "generic-brief-retro": "하루 회고",
            "generic-brief-weekly": "주간 회고",
            "mirror-poll": "캘린더 동기화",
            "mirror-reconcile": "캘린더 대조",
            "routine-roller": "루틴 일정 갱신",
            "classify-inbox": "기억 분류",
            "memory-consolidate": "기억 정리",
            "lifekit-backup": "라이프킷 백업",
            "cleanup-files": "임시 파일 정리",
            "health-observer": "상태 점검",
            "pack-lag": "팩 버전 점검",
        }
        for key, korean in approved.items():
            self.assertEqual(ho.display_name(key), korean)

        known = {"kind": "fail_streak", "label": PFX + "metal.mirror-poll",
                 "streak": 3, "exit": 1, "episode_started_at": "t"}
        text, _ = ho.compose_fail_streak(known, "metal")
        self.assertIn("캘린더 동기화", text)

        unknown_label = PFX + "metal.unlisted-job"
        unknown = dict(known, label=unknown_label)
        text, _ = ho.compose_fail_streak(unknown, "metal")
        headline, detail = text.split(">! 기술 상세", 1)
        self.assertIn("예약 작업", headline)
        self.assertNotIn("unlisted-job", headline)
        self.assertIn(unknown_label, detail)

    def test_self_job_uses_direct_wording(self):
        ev = {"kind": "fail_streak", "label": PFX + "metal.metal-shift",
              "streak": 3, "exit": 1, "structural": False,
              "episode_started_at": "t"}
        text, _ = ho.compose_fail_streak(ev, "metal")
        self.assertNotIn("스컬", text)

    def test_loss_copy_single_and_aggregated(self):
        ev = {"kind": "label_lost", "label": PFX + "Kojeni.jpy-vix-watch",
              "last_seen_at": "t", "bucket": "scheduled"}
        text, button = ho.compose_label_lost([ev], "metal")
        self.assertIn("jpy-vix-watch", text)
        self.assertIn("일부러 내린 거야", text)
        self.assertTrue(button.endswith("::opt:1"))

        evs = [dict(ev, label=PFX + "Kojeni.j%d" % i) for i in range(4)]
        text, button = ho.compose_label_lost(evs, "metal")
        self.assertIn("4개", text)
        for i in range(4):
            self.assertIn(PFX + "Kojeni.j%d" % i, text)
        self.assertTrue(button.endswith("::opt:1"))

    def test_resident_down_copy(self):
        ev = {"kind": "resident_down", "label": PFX + "metal.dashboard",
              "down_since": "t"}
        text, button = ho.compose_resident_down(ev, "metal")
        self.assertIn("dashboard", text)
        self.assertIn("서비스 상태 봐줘", text)
        self.assertTrue(button.endswith("::opt:1"))


class TestHeartbeat(unittest.TestCase):

    def test_heartbeat_written_every_poll(self):
        state = {}
        ho.poll(state, obs_for({}, {}), "2026-08-21T15:00:00+09:00")
        self.assertEqual(state["heartbeat_at"], "2026-08-21T15:00:00+09:00")
        self.assertEqual(state["schema"], ho.STATE_SCHEMA)

    def test_parse_errors_surface_in_state(self):
        label = PFX + "metal.broken"
        obs = obs_for({label: None}, {},
                      print_errors={label: "launchctl print rc=113: not found"})
        state = {}
        ho.poll(state, obs, "t0")
        self.assertEqual(len(state["parse_errors"]), 1)
        self.assertEqual(state["parse_errors"][0]["label"], label)


class TestRetireCLI(unittest.TestCase):
    """DGN-1610: run the real CLI as a subprocess so argv parsing and rc
    codes are exercised end to end. The launchctl on PATH is always a stub
    (never the real binary) -- one variant returns an empty job list, the
    other also logs every invocation so a test can assert it was never
    called."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-cli-")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir)
        self.stub_dir = os.path.join(self.tmp, "bin")
        os.makedirs(self.stub_dir)
        self.lc_log = os.path.join(self.tmp, "launchctl.log")
        stub = os.path.join(self.stub_dir, "launchctl")
        with open(stub, "w") as f:
            f.write(
                "#!/bin/bash\n"
                'echo "$@" >> "%s"\n'
                'if [ "$1" = "list" ]; then printf "PID\\tStatus\\tLabel\\n"; fi\n'
                "exit 0\n" % self.lc_log)
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        self.env = dict(os.environ)
        self.env["DOGANY_HEALTH_OBSERVER_STATE_DIR"] = self.state_dir
        self.env["PATH"] = self.stub_dir + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _run(self, *args):
        proc = subprocess.run(
            [sys.executable, os.path.join(ROUTINES, "health-observer.py"), *args],
            env=self.env, capture_output=True, text=True, timeout=20)
        return proc

    def _jobs(self):
        path = os.path.join(self.state_dir, "jobs.json")
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f).get("jobs", {})

    def _seed(self, label):
        with open(os.path.join(self.state_dir, "jobs.json"), "w") as f:
            json.dump({"jobs": {label: {"first_seen_at": "t0"}}}, f)

    def test_t1_unknown_label_rejected_rc3(self):
        proc = self._run("--retire", "ghost.label", "--why", "probe")
        self.assertEqual(proc.returncode, 3)
        self.assertNotIn("ghost.label", self._jobs())

    def test_t2_list_retired_never_polls(self):
        self._seed(PFX + "warg.seeded")
        with open(os.path.join(self.state_dir, "jobs.json")) as f:
            data = json.load(f)
        data["jobs"][PFX + "warg.seeded"]["retired"] = {
            "when": "t1", "why": "seeded"}
        with open(os.path.join(self.state_dir, "jobs.json"), "w") as f:
            json.dump(data, f)

        proc = self._run("--list-retired")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(PFX + "warg.seeded", proc.stdout)
        self.assertFalse(os.path.exists(self.lc_log))

        proc2 = self._run("--no-such-flag")
        self.assertEqual(proc2.returncode, 2)
        self.assertFalse(os.path.exists(self.lc_log))

    def test_t3_retire_missing_why_rc2_state_unchanged(self):
        self._seed(PFX + "warg.known")
        proc = self._run("--retire", PFX + "warg.known")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("retired", self._jobs()[PFX + "warg.known"])

    def test_t4_retire_known_label_readback(self):
        self._seed(PFX + "warg.known")
        proc = self._run("--retire", PFX + "warg.known", "--why", "probe")
        self.assertEqual(proc.returncode, 0)
        entry = self._jobs()[PFX + "warg.known"]
        self.assertEqual(entry["retired"]["why"], "probe")
        self.assertTrue(entry["retired"]["when"])


class TestPlistParseFailLoud(unittest.TestCase):
    """DGN-1442: an unparseable plist is a coverage hole -- reported by
    file, never swallowed; the remaining units are still scanned."""

    BAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">'
           '<dict>\n<!-- bad -- comment -->\n<key>Label</key><string>%s'
           '</string></dict></plist>\n')

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.la = os.path.join(self.tmp, "LaunchAgents")
        os.makedirs(self.la)
        with open(os.path.join(self.la, "good.plist"), "wb") as f:
            plistlib.dump({"Label": PFX + "metal.good"}, f)
        self.bad_path = os.path.join(self.la, "bad.plist")
        with open(self.bad_path, "w") as f:
            f.write(self.BAD % (PFX + "metal.bad"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_scan_disk_reports_bad_file_and_keeps_going(self):
        errors = []
        disk = ho.scan_disk(self.la, [], errors)
        self.assertEqual(list(disk), [PFX + "metal.good"])
        self.assertEqual([e["path"] for e in errors], [self.bad_path])
        self.assertIn("ExpatError", errors[0]["error"])

    def test_errors_param_optional(self):
        self.assertIsNone(ho.read_plist(self.bad_path))

    def test_poll_records_plist_error_in_parse_errors(self):
        errors = []
        disk = ho.scan_disk(self.la, [], errors)
        obs = obs_for({}, {}, disk=disk, la_dir=self.la)
        obs["plist_errors"] = errors
        state = {}
        ho.poll(state, obs, "t0")
        self.assertEqual(len(state["parse_errors"]), 1)
        self.assertEqual(state["parse_errors"][0]["label"], self.bad_path)

    def test_report_names_count_and_file(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ho._report_plist_errors([{"path": self.bad_path, "error": "x"}])
        self.assertIn("1 plist(s) unparseable", buf.getvalue())
        self.assertIn(self.bad_path, buf.getvalue())


class TestLossRoutingDgn1745(unittest.TestCase):
    """DGN-1745: a loss storm is announced once per owning crew, by that
    crew's leader only. Fixture estate: life crew (leader Lumi + domain
    Finny), dev crew (leader Metal + domain Warg), one crewless-unknown slug.
    Every observer shares ONE state dict, exactly as the real jobs.json."""

    T0, T1, T2 = ("2026-09-27T10:25:00+09:00", "2026-09-27T10:31:00+09:00",
                  "2026-09-27T10:36:00+09:00")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-dgn1745-")
        self.roots = {}
        for slug, cls in (("Lumi", "main"), ("Finny", "domain"),
                          ("Metal", "main"), ("Warg", "domain")):
            root = os.path.join(self.tmp, "agents", slug)
            os.makedirs(root)
            with open(os.path.join(root, ".instance.conf"), "w") as fh:
                fh.write("DOGANY_AGENT_CLASS=%s\n" % cls)
            self.roots[slug] = os.path.realpath(root)
        crews = os.path.join(self.tmp, "crews")
        # crew.conf order puts the domain member FIRST: the leader is the
        # first NON-domain member, not simply the first line.
        for kit, members in (("kit_life", ("Finny", "Lumi")),
                             ("kit_dev", ("Warg", "Metal"))):
            os.makedirs(os.path.join(crews, kit))
            with open(os.path.join(crews, kit, "crew.conf"), "w") as fh:
                fh.write("# crew registry\n")
                for m in members:
                    fh.write("member=%s\n" % self.roots[m])
        self.crews = ho.read_crews(crews)
        self.observers = ["%s%s.health-observer" % (PFX, s) for s in self.roots]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _obs(self, self_slug, loaded, add_observers=True):
        loaded = dict(loaded)
        for lbl in self.observers if add_observers else ():
            loaded.setdefault(lbl, None)
        info = {lbl: parsed(runs=3, exit_line="0") for lbl in loaded}
        obs = obs_for(loaded, info)
        obs["self_root"] = self.roots[self_slug]
        obs["instance_roots"] = {s.lower(): r for s, r in self.roots.items()}
        obs["crews"] = self.crews
        return obs

    def _storm(self, jobs, order=("Finny", "Warg", "Lumi", "Metal"),
               drop_observers=()):
        """Seed jobs, lose them, then let every observer poll in turn past
        grace (sharing one state). Returns {slug: [lost labels pushed]}."""
        state = {}
        ho.poll(state, self._obs("Lumi", {j: None for j in jobs}), self.T0)
        gone = {lbl: None for lbl in self.observers if lbl not in drop_observers}
        ho.poll(state, self._obs("Lumi", gone, False), self.T1)  # grace
        pushed = {}
        for slug in order:
            ev, _ = ho.poll(state, self._obs(slug, gone, False), self.T2)
            pushes = ho.compose_notifications(ev, slug)
            pushed[slug] = sorted(e["label"] for e in ev
                                  if e["kind"] == "label_lost")
            self.assertEqual(len(pushes), 1 if pushed[slug] else 0)
        return pushed

    def test_leader_is_first_non_domain_member(self):
        self.assertEqual(ho.crew_leader(self.crews["kit_life"]),
                         self.roots["Lumi"])
        self.assertEqual(ho.loss_recipient(PFX + "Finny.brief", {
            "finny": self.roots["Finny"]}, self.crews), self.roots["Lumi"])

    def test_single_crew_storm_only_that_leader_pushes(self):
        jobs = [PFX + "Finny.job%d" % i for i in range(9)] + \
               [PFX + "Lumi.job%d" % i for i in range(3)]
        pushed = self._storm(jobs)
        self.assertEqual(pushed["Lumi"], sorted(jobs))
        self.assertEqual(pushed["Metal"], [])      # other crew's leader
        self.assertEqual(pushed["Finny"], [])      # non-leader member
        self.assertEqual(pushed["Warg"], [])

    def test_two_crew_storm_each_leader_gets_only_its_own(self):
        life = [PFX + "Finny.a", PFX + "Lumi.b"]
        dev = [PFX + "Warg.c", PFX + "Metal.d", PFX + "Warg.e"]
        pushed = self._storm(life + dev)
        self.assertEqual(pushed["Lumi"], sorted(life))
        self.assertEqual(pushed["Metal"], sorted(dev))
        self.assertEqual(pushed["Finny"], [])
        self.assertEqual(pushed["Warg"], [])
        # the multi-loss copy counts only the leader's own jobs
        ev = [{"kind": "label_lost", "label": l, "last_seen_at": "t"}
              for l in dev]
        text, _ = ho.compose_label_lost(ev, "Metal")
        self.assertIn("3개", text)

    def test_unresolvable_owner_goes_to_machine_steward_once(self):
        orphan = [PFX + "Ghost.job1", PFX + "Ghost.job2"]
        pushed = self._storm(orphan)
        # domain members never; the first steward-class observer to poll
        # confirms it; the shared state stops a second steward repeating it.
        self.assertEqual(pushed["Finny"], [])
        self.assertEqual(pushed["Warg"], [])
        self.assertEqual(pushed["Lumi"], sorted(orphan))
        self.assertEqual(pushed["Metal"], [])
        pushed = self._storm(orphan, order=("Warg", "Metal", "Lumi", "Finny"))
        self.assertEqual(pushed["Metal"], sorted(orphan))
        self.assertEqual(pushed["Lumi"], [])

    def test_leader_observer_not_loaded_falls_back_to_steward(self):
        jobs = [PFX + "Finny.a", PFX + "Lumi.health-observer"]
        pushed = self._storm(
            jobs, order=("Finny", "Metal", "Lumi"),
            drop_observers=(PFX + "Lumi.health-observer",))
        self.assertEqual(pushed["Finny"], [])
        self.assertEqual(pushed["Metal"], sorted(jobs))

    def test_non_leader_records_loss_silently(self):
        state = {}
        job = PFX + "Finny.brief"
        ho.poll(state, self._obs("Finny", {job: None}), self.T0)
        ho.poll(state, self._obs("Finny", {}), self.T1)
        ev, _ = ho.poll(state, self._obs("Finny", {}), self.T2)
        self.assertEqual(ev, [])
        self.assertIsNotNone(state["jobs"][job]["lost_at"])
        self.assertIsNone(state["jobs"][job].get("lost_notified_at"))
        self.assertEqual(state["counts"].get("lost"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
