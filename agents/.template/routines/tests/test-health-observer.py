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
 16. DGN-1900: one machine steward (marker, else first crew's
     representative, a PoC never); a crewless owner is its own leader; a
     mass-loss burst is re-confirmed on a later poll and held through the
     observer's own restart grace (replay of the 2026-10-07 false notice).
 18. DGN-1990: a loss whose first miss falls inside an update apply run's
     ledger window (start .. result + settle) on an instance the run
     touches is held, not pushed (replay of run 4ea6b51d); a unit still
     missing after the settle (or after the left-down instance's restart
     + settle) fires as before; other instances are not held.
 19. DGN-1990: a plist whose only fault is a double hyphen inside an XML
     comment is read with comments removed (unit observed, info note); a
     zero-byte plist is an info note, not a coverage hole; one record per
     file even when two scanners read it.
 17. DGN-1670: every per-unit alert (failure streak, resident down, loss)
     follows the one-announcer route, and an observer never announces a
     unit of an instance with a different owner chat (replay of the
     2026-10-09 23:46Z push into a third party's chat).

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

# DGN-1900: the machine steward reads a machine-global marker and the crews
# home. Pin both to an empty scratch dir so a developer's real
# ~/.dogany/machine-steward or ~/.dogany/crews never leaks into a test.
_ISOLATED_HOME = tempfile.mkdtemp(prefix="ho-isolated-home-")
os.environ["DOGANY_MACHINE_STEWARD_FILE"] = os.path.join(
    _ISOLATED_HOME, "machine-steward")
os.environ["DOGANY_CREW_DIR"] = os.path.join(_ISOLATED_HOME, "crews")


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



def _write_owner(root, chat="111"):
    """DGN-1670 fail-closed: fixtures that share one owner must say so."""
    os.makedirs(os.path.join(root, ".telegram_bot"), exist_ok=True)
    with open(os.path.join(root, ".telegram_bot", ".env"), "w") as fh:
        fh.write("ALLOWED_USER_IDS=%s\n" % chat)

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


class TestMachineAlertSwitch(unittest.TestCase):
    """DGN-1911: a real on/off switch; thresholds stay thresholds."""

    BREACH = {"swap_used_mb": "9800", "load5": "1", "ncpu": "4",
              "disk_used_pct": "86"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-switch-")
        self.root = os.path.join(self.tmp, "steward")
        os.makedirs(os.path.join(self.root, "config"))
        with open(os.path.join(self.root, ".instance.conf"), "w") as f:
            f.write("DOGANY_AGENT_CLASS=main\n")
        self.conf = os.path.join(self.root, "config", "agent.conf")
        self._write("AGENT_LANG=ko\n")
        self.old_state = os.environ.get("DOGANY_HEALTH_OBSERVER_STATE_DIR")
        os.environ["DOGANY_HEALTH_OBSERVER_STATE_DIR"] = os.path.join(
            self.tmp, "shared-home", "health-observer")

    def tearDown(self):
        if self.old_state is None:
            os.environ.pop("DOGANY_HEALTH_OBSERVER_STATE_DIR", None)
        else:
            os.environ["DOGANY_HEALTH_OBSERVER_STATE_DIR"] = self.old_state
        shutil.rmtree(self.tmp)

    def _write(self, text):
        with open(self.conf, "w") as f:
            f.write(text)

    def _read(self):
        with open(self.conf) as f:
            return f.read()

    def _inbox(self):
        box = os.path.join(self.root, ".telegram_bot", "session-inbox")
        return sorted(n for n in os.listdir(box)) if os.path.isdir(box) else []

    def test_default_is_on(self):
        self.assertTrue(ho.machine_alerts_mode(self.root, 100)["on"])
        events = ho.observe_machine_metrics(self.root, self.BREACH, 100)
        self.assertEqual({e["kind"] for e in events}, {"swap", "disk"})

    def test_unknown_value_fails_open_to_on(self):
        self._write("HEALTH_OBSERVER_MACHINE_ALERTS=maybe\n")
        self.assertTrue(ho.machine_alerts_mode(self.root, 100)["on"])

    def test_off_silences_every_kind_and_keeps_thresholds(self):
        ho.set_machine_alerts(False, self.root)
        self.assertIn("HEALTH_OBSERVER_MACHINE_ALERTS=off", self._read())
        self.assertEqual(ho.machine_thresholds(self.root)["disk_pct"],
                         ho.DEFAULT_DISK_WARN_PCT)
        readings = dict(self.BREACH, load5="40")
        for t in (100, 2000, 100000):
            self.assertEqual(ho.observe_machine_metrics(
                self.root, readings, t), [])
        self.assertEqual(self._inbox(), [])

    def test_back_on_alerts_a_breach_that_held_through_the_mute(self):
        ho.set_machine_alerts(False, self.root)
        self.assertEqual(ho.observe_machine_metrics(
            self.root, self.BREACH, 100), [])
        ho.set_machine_alerts(True, self.root)
        self.assertIn("HEALTH_OBSERVER_MACHINE_ALERTS=on", self._read())
        events = ho.observe_machine_metrics(self.root, self.BREACH, 200)
        self.assertEqual({(e["kind"], e["notice"]) for e in events},
                         {("swap", "breach"), ("disk", "breach")})

    def test_off_until_is_time_boxed(self):
        until = "2030-01-02T03:04"
        ho.set_machine_alerts(False, self.root, until=until)
        edge = ho.parse_until(until)
        self.assertEqual(ho.observe_machine_metrics(
            self.root, self.BREACH, edge - 1), [])
        self.assertTrue(ho.observe_machine_metrics(
            self.root, self.BREACH, edge + 1))
        # Turning on (or off without a bound) drops the stale bound.
        ho.set_machine_alerts(True, self.root)
        self.assertNotIn("OFF_UNTIL", self._read())

    def test_until_alone_and_bad_until(self):
        self._write("HEALTH_OBSERVER_MACHINE_ALERTS_OFF_UNTIL=2030-01-02\n")
        self.assertFalse(ho.machine_alerts_mode(self.root, 0)["on"])
        self._write("HEALTH_OBSERVER_MACHINE_ALERTS=off\n"
                    "HEALTH_OBSERVER_MACHINE_ALERTS_OFF_UNTIL=soon\n")
        self.assertFalse(ho.machine_alerts_mode(self.root, 0)["on"])
        self.assertIsNone(ho.parse_until("soon"))
        self.assertIsNotNone(ho.parse_until("2030-01-02T03:04:05+09:00"))

    def test_setter_is_idempotent_and_in_place(self):
        self._write("AGENT_LANG=ko\nHEALTH_OBSERVER_MACHINE_ALERTS=on\n"
                    "AGENT_ACCENT=\n")
        first = ho.set_machine_alerts(False, self.root, stamp="s1")
        self.assertTrue(first["changed"])
        self.assertEqual(self._read(), "AGENT_LANG=ko\n"
                         "HEALTH_OBSERVER_MACHINE_ALERTS=off\nAGENT_ACCENT=\n")
        self.assertTrue(os.path.isfile(self.conf + ".bak-dgn1911-s1"))
        again = ho.set_machine_alerts(False, self.root, stamp="s2")
        self.assertFalse(again["changed"])
        self.assertFalse(os.path.exists(self.conf + ".bak-dgn1911-s2"))

    def test_legacy_threshold_hack_is_detected_noted_once_and_migrated(self):
        # The 2026-10-07 interim hack: three appended unreachable values.
        hack = ("AGENT_LANG=ko\n"
                "# alert off (interim) 2026-10-07\n"
                "HEALTH_OBSERVER_SWAP_WARN_MB=999999999\n"
                "HEALTH_OBSERVER_DISK_WARN_PCT=101\n"
                "HEALTH_OBSERVER_LOAD_WARN_MULTIPLIER=9999\n")
        self._write(hack)
        self.assertEqual([k for _, k, _ in ho.legacy_mute_lines(self.root)],
                         ["HEALTH_OBSERVER_SWAP_WARN_MB",
                          "HEALTH_OBSERVER_DISK_WARN_PCT",
                          "HEALTH_OBSERVER_LOAD_WARN_MULTIPLIER"])
        self.assertEqual(ho.observe_machine_metrics(
            self.root, self.BREACH, 100), [])
        ho.observe_machine_metrics(self.root, self.BREACH, 400)
        notes = [n for n in self._inbox() if "legacy" in n]
        self.assertEqual(len(notes), 1)
        result = ho.set_machine_alerts(False, self.root, stamp="m")
        self.assertEqual(len(result["migrated"]), 3)
        text = self._read()
        self.assertIn("HEALTH_OBSERVER_MACHINE_ALERTS=off", text)
        self.assertEqual(ho.legacy_mute_lines(self.root), [])
        self.assertEqual(text.count(ho.LEGACY_MUTE_TAG), 3)
        with open(self.conf + ".bak-dgn1911-m") as f:
            self.assertEqual(f.read(), hack)
        # Thresholds are back to defaults; the switch alone keeps it quiet.
        self.assertEqual(ho.machine_thresholds(self.root)["swap_mb"],
                         ho.DEFAULT_SWAP_WARN_MB)
        self.assertEqual(ho.observe_machine_metrics(
            self.root, self.BREACH, 500), [])

    def test_real_tuning_is_not_legacy(self):
        self._write("HEALTH_OBSERVER_DISK_WARN_PCT=95\n"
                    "HEALTH_OBSERVER_SWAP_WARN_MB=16384\n"
                    "HEALTH_OBSERVER_LOAD_WARN_MULTIPLIER=3\n")
        self.assertEqual(ho.legacy_mute_lines(self.root), [])

    def test_inbox_instruction_names_the_switch(self):
        ho.observe_machine_metrics(self.root, self.BREACH, 100)
        box = os.path.join(self.root, ".telegram_bot", "session-inbox")
        with open(os.path.join(box, self._inbox()[0])) as f:
            body = f.read()
        self.assertIn("--machine-alerts off", body)
        self.assertIn("Never raise a threshold", body)

    def test_cli_argv(self):
        self.assertEqual(ho.parse_argv(["--machine-alerts", "off"])["alerts"],
                         "off")
        p = ho.parse_argv(["--machine-alerts", "off", "--until", "2030-01-02"])
        self.assertEqual(p["until"], "2030-01-02")
        for bad in (["--machine-alerts"], ["--machine-alerts", "mute"],
                    ["--machine-alerts", "on", "--until", "2030-01-02"],
                    ["--machine-alerts", "off", "--until", "later"]):
            self.assertIn("error", ho.parse_argv(bad), bad)

    def test_cli_status_and_off_output(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(ho.cmd_machine_alerts("off", root=self.root), 0)
        out = buf.getvalue()
        self.assertIn("changed=yes", out)
        self.assertIn("machine_alerts=off", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ho.cmd_machine_alerts("status", root=self.root)
        self.assertIn("machine_alerts=off", buf.getvalue())
        self.assertNotIn("changed=", buf.getvalue())


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
        self.assertIn("담당 에이전트를 통해", text)
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
        self.assertNotIn("담당 에이전트를 통해", text)

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

    # Truly malformed (unclosed <dict>).  DGN-1990: a double hyphen inside a
    # comment alone is read around now -- see TestPlistToleranceDgn1990.
    BAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">'
           '<dict>\n<key>Label</key><string>%s</string></plist>\n')

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
        self.assertNotIn("level", errors[0])

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

    T0, T1, T2, T3 = ("2026-09-27T10:25:00+09:00", "2026-09-27T10:31:00+09:00",
                      "2026-09-27T10:36:00+09:00", "2026-09-27T10:41:00+09:00")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-dgn1745-")
        self.roots = {}
        for slug, cls in (("Lumi", "main"), ("Finny", "domain"),
                          ("Metal", "main"), ("Warg", "domain")):
            root = os.path.join(self.tmp, "agents", slug)
            os.makedirs(root)
            with open(os.path.join(root, ".instance.conf"), "w") as fh:
                fh.write("DOGANY_AGENT_CLASS=%s\n" % cls)
            _write_owner(root)
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
        try:
            os.remove(os.environ["DOGANY_MACHINE_STEWARD_FILE"])
        except OSError:
            pass

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
        pushed = {slug: [] for slug in order}
        # DGN-1900: a burst (>= LOSS_BURST_MIN) is held at T2 and fires on
        # the re-confirming T3 round; a small loss fires at T2 as before.
        for at in (self.T2, self.T3):
            for slug in order:
                ev, _ = ho.poll(state, self._obs(slug, gone, False), at)
                pushes = ho.compose_notifications(ev, slug)
                got = sorted(e["label"] for e in ev
                             if e["kind"] == "label_lost")
                self.assertEqual(len(pushes), 1 if got else 0)
                pushed[slug] = sorted(pushed[slug] + got)
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
        # DGN-1900: ONE steward whatever the poll order -- no marker, so the
        # first crew's (kit order: kit_dev) representative.  Domain members
        # and the other crew leader never announce it.
        rep = os.path.basename(ho.crew_leader(self.crews["kit_dev"]))
        slugs = list(self.roots)
        for order in (slugs, slugs[::-1]):
            pushed = self._storm(orphan, order=order)
            self.assertEqual(pushed, {s: sorted(orphan) if s == rep else []
                                      for s in order})

    def test_dgn1900_explicit_marker_names_the_one_steward(self):
        rep = os.path.basename(ho.crew_leader(self.crews["kit_dev"]))
        with open(os.environ["DOGANY_MACHINE_STEWARD_FILE"], "w") as fh:
            fh.write("# designated crew representative\n%s\n"
                     % self.roots["Lumi"])
        self.assertTrue(ho.is_machine_steward(self.roots["Lumi"], self.crews))
        self.assertFalse(ho.is_machine_steward(self.roots[rep], self.crews))
        orphan = [PFX + "Ghost.job1"]
        pushed = self._storm(orphan, order=(rep, "Lumi"))
        self.assertEqual(pushed, {rep: [], "Lumi": sorted(orphan)})

    def test_dgn1900_poc_main_never_stewards(self):
        """Live case 2026-10-07: a class=main PoC won the steward lock and
        announced another crew's labels on its own bot."""
        poc = os.path.join(self.tmp, "dogany", "lab", "poc", "Probe")
        os.makedirs(poc)
        with open(os.path.join(poc, ".instance.conf"), "w") as fh:
            fh.write("DOGANY_AGENT_CLASS=main\n")
        self.assertTrue(ho.is_poc_root(poc))
        self.assertFalse(ho.is_machine_steward(poc, self.crews))
        self.assertFalse(ho.is_machine_steward(poc, {}))
        # even when the marker names it, the PoC is refused (crew rep wins)
        with open(os.environ["DOGANY_MACHINE_STEWARD_FILE"], "w") as fh:
            fh.write(poc + "\n")
        self.assertFalse(ho.is_machine_steward(poc, self.crews))
        self.assertTrue(ho.is_machine_steward(
            ho.crew_leader(self.crews["kit_dev"]), self.crews))
        # a PoC still announces its OWN crewless labels (it is their owner)
        roots = {"probe": os.path.realpath(poc)}
        self.assertEqual(ho.loss_recipient(PFX + "Probe.job", roots,
                                           self.crews), os.path.realpath(poc))

    def test_dgn1900_crewless_owner_is_its_own_leader(self):
        solo = os.path.join(self.tmp, "agents", "Solo")
        os.makedirs(solo)
        with open(os.path.join(solo, ".instance.conf"), "w") as fh:
            fh.write("DOGANY_AGENT_CLASS=main\n")
        roots = {"solo": os.path.realpath(solo)}
        self.assertEqual(ho.loss_recipient(PFX + "Solo.job", roots,
                                           self.crews), os.path.realpath(solo))
        with open(os.path.join(solo, ".instance.conf"), "w") as fh:
            fh.write("DOGANY_AGENT_CLASS=domain\n")
        self.assertIsNone(ho.loss_recipient(PFX + "Solo.job", roots,
                                            self.crews))

    def test_no_crew_no_marker_keeps_class_rule(self):
        self.assertTrue(ho.is_machine_steward(self.roots["Lumi"], {}))
        self.assertFalse(ho.is_machine_steward(self.roots["Finny"], {}))

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


class TestMassLossBurstDgn1900(unittest.TestCase):
    """DGN-1900 replay: 2026-10-07 06:35 a self-updated PoC (Probe,
    class=main) restarted; its observer read a partial launchctl list and
    pushed "23 jobs vanished" for Rook/Bishop labels at 06:41 although nothing
    was lost.  Fixture estate: crew kit_dev = Rook (domain) + Bishop (main,
    leader); Probe is a crewless PoC.  One shared state dict = jobs.json."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-dgn1900-")
        self.roots = {}
        for slug, rel, cls in (("Rook", "agents/Rook", "domain"),
                               ("Bishop", "agents/Bishop", "main"),
                               ("Probe", "dogany/lab/poc/Probe", "main")):
            root = os.path.join(self.tmp, rel)
            os.makedirs(root)
            with open(os.path.join(root, ".instance.conf"), "w") as fh:
                fh.write("DOGANY_AGENT_CLASS=%s\n" % cls)
            _write_owner(root)
            self.roots[slug] = os.path.realpath(root)
        crews = os.path.join(self.tmp, "crews", "kit_dev")
        os.makedirs(crews)
        with open(os.path.join(crews, "crew.conf"), "w") as fh:
            fh.write("member=%s\nmember=%s\n"
                     % (self.roots["Rook"], self.roots["Bishop"]))
        self.crews = ho.read_crews(os.path.dirname(crews))
        self.bridges = {"%s%s.newbridge" % (PFX, s): s for s in self.roots}
        self.observers = ["%s%s.health-observer" % (PFX, s)
                          for s in self.roots]
        self.jobs = ["%sRook.job%02d" % (PFX, i) for i in range(12)] + \
                    ["%sBishop.job%02d" % (PFX, i) for i in range(11)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _obs(self, self_slug, labels, pids=None):
        pids = pids or {}
        loaded = {lbl: pids.get(lbl) for lbl in labels}
        obs = obs_for(loaded, {lbl: parsed(runs=3, exit_line="0")
                               for lbl in loaded},
                      bridge_labels=set(self.bridges))
        obs["self_root"] = self.roots[self_slug]
        obs["instance_roots"] = {s.lower(): r for s, r in self.roots.items()}
        obs["crews"] = self.crews
        obs["self_bridges"] = ["%s%s.newbridge" % (PFX, self_slug)]
        return obs

    def _full(self, probe_pid=100):
        labels = list(self.bridges) + self.observers + self.jobs
        pids = {lbl: 1000 + i for i, lbl in enumerate(self.bridges)}
        pids[PFX + "Probe.newbridge"] = probe_pid
        return labels, pids

    def _partial(self, probe_pid):
        # The partial read around the restart: every Rook/Bishop label --
        # their observers included -- missing, Probe's own labels present.
        labels, pids = self._full(probe_pid)
        keep = [l for l in labels if ".Rook." not in l and ".Bishop." not in l]
        return keep, pids

    @staticmethod
    def at(hhmm):
        return "2026-10-07T%s:00+09:00" % hhmm

    def test_replay_restart_partial_read_pushes_nothing(self):
        state, pushes = {}, []
        timeline = [("06:30", self._full(100)), ("06:36", self._partial(200)),
                    ("06:41", self._partial(200)), ("06:46", self._partial(200)),
                    ("06:51", self._full(200)), ("06:56", self._full(200))]
        for hhmm, (labels, pids) in timeline:
            ev, _ = ho.poll(state, self._obs("Probe", labels, pids),
                            self.at(hhmm))
            pushes += ho.compose_notifications(ev, "Probe")
        self.assertEqual(pushes, [])
        self.assertEqual(state["jobs"][PFX + "Probe.newbridge"]
                         ["restarted_at"], self.at("06:36"))
        self.assertTrue(all(state["jobs"][j]["lost_at"] is None
                            for j in self.jobs))

    def test_burst_held_once_then_fires_on_reconfirm_poll(self):
        """A REAL mass loss still fires -- one poll later, once, to the
        owning crew's leader only (the PoC never)."""
        state = {}
        full, pids = self._full()
        gone = [l for l in full if l not in self.jobs]
        ho.poll(state, self._obs("Bishop", full, pids), self.at("07:00"))
        ho.poll(state, self._obs("Bishop", gone, pids), self.at("07:05"))
        ev, _ = ho.poll(state, self._obs("Bishop", gone, pids), self.at("07:10"))
        self.assertEqual(ev, [], "burst held on its first confirmed poll")
        # a second observer polling within the same minute is no re-confirm
        ev, _ = ho.poll(state, self._obs("Probe", gone, pids),
                        "2026-10-07T07:10:20+09:00")
        self.assertEqual(ev, [])
        ev_poc, _ = ho.poll(state, self._obs("Probe", gone, pids),
                            self.at("07:15"))
        self.assertEqual(ev_poc, [], "PoC never announces Rook/Bishop labels")
        ev, _ = ho.poll(state, self._obs("Bishop", gone, pids), self.at("07:15"))
        self.assertEqual(sorted(e["label"] for e in ev), sorted(self.jobs))
        self.assertEqual(len(ho.compose_notifications(ev, "Bishop")), 1)
        ev, _ = ho.poll(state, self._obs("Bishop", gone, pids), self.at("07:20"))
        self.assertEqual(ev, [])

    def test_own_restart_grace_holds_a_burst(self):
        state = {}
        full, pids = self._full()
        gone = [l for l in full if l not in self.jobs]
        ho.poll(state, self._obs("Bishop", full, pids), self.at("07:00"))
        pids2 = dict(pids)
        pids2[PFX + "Bishop.newbridge"] = 4242          # Bishop restarted
        ho.poll(state, self._obs("Bishop", gone, pids2), self.at("07:05"))
        for hhmm in ("07:10", "07:14"):                 # inside the 600s grace
            ev, _ = ho.poll(state, self._obs("Bishop", gone, pids2),
                            self.at(hhmm))
            self.assertEqual(ev, [], hhmm)
        ev, _ = ho.poll(state, self._obs("Bishop", gone, pids2), self.at("07:16"))
        self.assertEqual(len(ev), len(self.jobs))

    def test_single_label_loss_goes_to_owning_leader_only(self):
        state = {}
        full, pids = self._full()
        job = PFX + "Rook.job00"
        gone = [l for l in full if l != job]
        ho.poll(state, self._obs("Bishop", full, pids), self.at("07:00"))
        ho.poll(state, self._obs("Probe", gone, pids), self.at("07:05"))
        got = {}
        for slug in ("Probe", "Rook", "Bishop"):
            ev, _ = ho.poll(state, self._obs(slug, gone, pids), self.at("07:10"))
            got[slug] = [e["label"] for e in ev]
        self.assertEqual(got, {"Probe": [], "Rook": [], "Bishop": [job]})


class TestOwnerScopeDgn1670(unittest.TestCase):
    """DGN-1670 replay: 2026-10-09 23:46Z a PoC handed to another user
    (Probe, owner chat 222) pushed "a job keeps failing" for the operator's
    crew unit (Rook.sweep) into that user's chat.  Fixture estate: crew
    kit_dev = Rook (domain) + Bishop (main, leader), operator chat 111;
    Probe = crewless PoC of another owner; Twin = crewless PoC of the
    operator (same chat 111).  One shared state dict = jobs.json."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-dgn1670-")
        self.roots = {}
        for slug, rel, cls, chat in (
                ("Rook", "agents/Rook", "domain", "111"),
                ("Bishop", "agents/Bishop", "main", "111"),
                ("Probe", "dogany/lab/poc/Probe", "main", "222"),
                ("Twin", "dogany/lab/poc/Twin", "main", "111")):
            root = os.path.join(self.tmp, rel)
            os.makedirs(os.path.join(root, ".telegram_bot"))
            with open(os.path.join(root, ".instance.conf"), "w") as fh:
                fh.write("DOGANY_AGENT_CLASS=%s\n" % cls)
            with open(os.path.join(root, ".telegram_bot", ".env"), "w") as fh:
                fh.write("ALLOWED_USER_IDS=%s\n" % chat)
            self.roots[slug] = os.path.realpath(root)
        crews = os.path.join(self.tmp, "crews", "kit_dev")
        os.makedirs(crews)
        with open(os.path.join(crews, "crew.conf"), "w") as fh:
            fh.write("member=%s\nmember=%s\n"
                     % (self.roots["Rook"], self.roots["Bishop"]))
        self.crews = ho.read_crews(os.path.dirname(crews))
        self.observers = ["%s%s.health-observer" % (PFX, s)
                          for s in ("Bishop", "Probe", "Twin")]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _obs(self, self_slug, jobs, observers=None):
        loaded = dict(jobs)
        for lbl in self.observers if observers is None else observers:
            loaded.setdefault(lbl, None)
        info = {lbl: parsed(runs=3, exit_line="0") for lbl in loaded}
        for lbl, value in jobs.items():
            if value is not None:
                info[lbl] = value
        obs = obs_for({l: (None if not isinstance(v, int) else v)
                       for l, v in loaded.items()}, info)
        obs["self_root"] = self.roots[self_slug]
        obs["instance_roots"] = {s.lower(): r for s, r in self.roots.items()}
        obs["crews"] = self.crews
        return obs

    @staticmethod
    def at(minute):
        return "2026-10-10T08:%02d:00+09:00" % minute

    def _fail_storm(self, job, order):
        """The job fails on every run; each observer polls after each run.
        Returns {slug: [fail_streak labels emitted]}."""
        state, got = {}, {s: [] for s in order}
        for run in range(1, 7):
            info = parsed(runs=run, exit_line="1")
            for i, slug in enumerate(order):
                ev, _ = ho.poll(state, self._obs(slug, {job: info}),
                                self.at(run * 5 + i))
                got[slug] += [e["label"] for e in ev
                              if e["kind"] == "fail_streak"]
        return got

    def test_replay_other_owner_poc_never_gets_operator_failure(self):
        job = PFX + "Rook.sweep"
        for order in (("Probe", "Twin", "Bishop"), ("Bishop", "Twin", "Probe")):
            got = self._fail_storm(job, order)
            self.assertEqual(got["Probe"], [], order)
            self.assertEqual(got["Twin"], [], order)
            self.assertEqual(got["Bishop"], [job], order)

    def test_streak_counted_by_another_observer_still_fires_once(self):
        # Probe polls first after every run, so it is the one that counts
        # the streak; Bishop must still announce it (once) on its own poll.
        job = PFX + "Bishop.brief"
        got = self._fail_storm(job, ("Probe", "Bishop"))
        self.assertEqual(got, {"Probe": [], "Bishop": [job]})

    def test_own_unit_of_other_owner_poc_goes_to_itself_only(self):
        job = PFX + "Probe.roller"
        got = self._fail_storm(job, ("Bishop", "Twin", "Probe"))
        self.assertEqual(got, {"Bishop": [], "Twin": [], "Probe": [job]})

    def test_resident_down_follows_the_same_route(self):
        job = PFX + "Rook.daemon"
        down = parsed(runs=2, exit_line="1", keepalive=True)
        state, got = {}, {}
        for slug in ("Probe", "Twin", "Bishop"):
            ev, _ = ho.poll(state, self._obs(slug, {job: down}), self.at(1))
            got[slug] = [e["label"] for e in ev if e["kind"] == "resident_down"]
        self.assertEqual(got, {"Probe": [], "Twin": [], "Bishop": [job]})

    def _lost(self, jobs, order, observers):
        state, got = {}, {s: [] for s in order}
        ho.poll(state, self._obs("Bishop", {j: None for j in jobs}), self.at(0))
        ho.poll(state, self._obs("Bishop", {}, observers), self.at(5))
        for minute in (10, 15):
            for slug in order:
                ev, _ = ho.poll(state, self._obs(slug, {}, observers),
                                self.at(minute))
                got[slug] += [e["label"] for e in ev
                              if e["kind"] == "label_lost"]
        return got

    def test_other_owner_loss_never_falls_back_to_the_steward(self):
        # Probe's own observer is gone with its units: the steward (Bishop)
        # is another owner, so the loss is recorded and announced by nobody.
        job = PFX + "Probe.roller"
        observers = [PFX + "Bishop.health-observer", PFX + "Twin.health-observer"]
        got = self._lost([job], ("Bishop", "Twin"), observers)
        self.assertEqual(got, {"Bishop": [], "Twin": []})

    def test_same_owner_poc_loss_still_falls_back_to_the_steward(self):
        job = PFX + "Twin.roller"
        observers = [PFX + "Bishop.health-observer", PFX + "Probe.health-observer"]
        got = self._lost([job], ("Probe", "Bishop"), observers)
        # Twin's observer went with its unit: both are the operator's.
        self.assertEqual(got, {"Probe": [], "Bishop": sorted(
            [job, PFX + "Twin.health-observer"])})

    def test_owner_chat_reads_env_then_owner_lock(self):
        self.assertEqual(ho.owner_chat(self.roots["Probe"]), "222")
        claimed = os.path.join(self.tmp, "claimed")
        os.makedirs(os.path.join(claimed, ".telegram_bot"))
        with open(os.path.join(claimed, ".telegram_bot", "owner.lock"), "w") as fh:
            fh.write("333\n")
        self.assertEqual(ho.owner_chat(claimed), "333")
        self.assertIsNone(ho.owner_chat(os.path.join(self.tmp, "none")))


class TestUpdateWindowDgn1990(unittest.TestCase):
    """DGN-1990 replay: 2026-10-11 00:09:17Z update run 4ea6b51d stopped
    every unit of Bishop (23 here), finished 9 s later leaving Bishop down,
    and "23 jobs vanished without a retire record" went out at 00:16:55Z.
    Fixture: crewless Bishop (main) and Knight (main), ledger files shaped
    like the live ones (.json with started_at/result, .start, .result)."""

    START = 1791677357          # 2026-10-11T00:09:17Z
    END = 1791677366

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-dgn1990-")
        self.roots = {}
        for slug in ("Bishop", "Knight"):
            root = os.path.join(self.tmp, "agents", slug)
            os.makedirs(root)
            with open(os.path.join(root, ".instance.conf"), "w") as fh:
                fh.write("DOGANY_AGENT_CLASS=main\n")
            _write_owner(root)
            self.roots[slug] = os.path.realpath(root)
        self.ledger = os.path.join(self.tmp, "update-ledger")
        os.makedirs(self.ledger)
        self.jobs = ["%sBishop.job%02d" % (PFX, i) for i in range(23)]
        self.observers = ["%s%s.health-observer" % (PFX, s)
                          for s in ("Knight",)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger(self, run_id="4ea6b51d-9e0f-4ac6-86f4-a1f9ea8b5f21",
                finished=True, left_down=True, restart_at=None,
                start_in_json=True):
        bishop = self.roots["Bishop"]
        ledger = {"id": run_id, "items": [{
            "member_roots": [bishop],
            "lines": ["FW\tbishop\t%s\t2.6.4-dev.2\t2.6.4-dev.5" % bishop]}]}
        if start_in_json:
            ledger["started_at"] = self.START
        path = os.path.join(self.ledger, run_id + ".json")
        with open(path + ".start", "w") as fh:
            fh.write("74506 %d\n" % self.START)
        if finished:
            members = [{"name": "bishop", "root": bishop,
                        "status": "left_down" if left_down else "restarted"}]
            ledger["result"] = {
                "finished_at": self.END, "items": [{"members": members}],
                "record": {"bridges_left_down_members":
                           members if left_down else []}}
            with open(path + ".result", "w") as fh:
                fh.write("{}\n")
        if restart_at:
            ledger["restarts"] = [{"root": bishop, "at": restart_at,
                                   "alive": True}]
        with open(path, "w") as fh:
            json.dump(ledger, fh)
        return ho.read_update_runs(self.ledger, now_epoch=self.START + 600)

    def _obs(self, labels, runs):
        loaded = {lbl: None for lbl in list(labels) + self.observers}
        obs = obs_for(loaded, {l: parsed(runs=3, exit_line="0")
                               for l in loaded})
        obs["self_root"] = self.roots["Knight"]
        obs["instance_roots"] = {s.lower(): r for s, r in self.roots.items()}
        obs["crews"] = {}
        obs["update_runs"] = runs
        return obs

    @staticmethod
    def at(epoch):
        import datetime
        return datetime.datetime.fromtimestamp(
            epoch, datetime.timezone.utc).isoformat(timespec="seconds")

    def _replay(self, runs, labels_at):
        """labels_at: [(epoch, loaded job labels)] -> lost labels pushed."""
        state, pushed = {}, []
        for epoch, labels in labels_at:
            ev, _ = ho.poll(state, self._obs(labels, runs), self.at(epoch))
            pushed += [e["label"] for e in ev if e["kind"] == "label_lost"]
        return state, pushed

    def test_ledger_reader_shapes(self):
        runs = self._ledger()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run["start"], run["end"]), (self.START, self.END))
        self.assertEqual(run["roots"], {self.roots["Bishop"]})
        self.assertEqual(run["left_down"], {self.roots["Bishop"]})
        # no started_at in the json: the .start claim ("pid epoch") is read;
        # no result yet: the run is open
        shutil.rmtree(self.ledger)
        os.makedirs(self.ledger)
        runs = self._ledger(finished=False, start_in_json=False)
        self.assertEqual((runs[0]["start"], runs[0]["end"]), (self.START, None))

    def test_replay_update_stopped_units_raise_no_vanish(self):
        runs = self._ledger(restart_at=self.START + 666)
        t = self.START
        timeline = [(t - 300, self.jobs), (t + 300, []), (t + 600, []),
                    (t + 900, []), (t + 1200, []), (t + 1500, self.jobs),
                    (t + 1800, self.jobs)]
        state, pushed = self._replay(runs, timeline)
        self.assertEqual(pushed, [])
        self.assertTrue(all(state["jobs"][j]["lost_at"] is None
                            and "update_hold" not in state["jobs"][j]
                            for j in self.jobs))

    def test_held_while_left_down_and_not_restarted(self):
        runs = self._ledger()
        t = self.START
        state, pushed = self._replay(
            runs, [(t - 300, self.jobs)] +
            [(t + 300 * k, []) for k in range(1, 12)])
        self.assertEqual(pushed, [])
        self.assertEqual(state["jobs"][self.jobs[0]]["update_hold"],
                         "4ea6b51d-9e0f-4ac6-86f4-a1f9ea8b5f21")

    def test_still_missing_after_restart_and_settle_fires(self):
        runs = self._ledger(restart_at=self.START + 666)
        t = self.START
        state, pushed = self._replay(
            runs, [(t - 300, self.jobs)] +
            [(t + 300 * k, []) for k in range(1, 9)])
        self.assertEqual(sorted(pushed), sorted(self.jobs))

    def test_completed_run_settle_then_real_loss_fires(self):
        runs = self._ledger(left_down=False)
        t = self.START
        job = self.jobs[0]
        rest = self.jobs[1:]
        timeline = [(t - 300, self.jobs), (t + 300, rest), (t + 600, rest),
                    (t + 900, rest)]
        _, pushed = self._replay(runs, timeline)
        self.assertEqual(pushed, [], "inside window + settle")
        _, pushed = self._replay(runs, timeline + [(t + 1200, rest)])
        self.assertEqual(pushed, [job], "settle elapsed, still gone")

    def test_other_instance_loss_inside_window_still_fires(self):
        runs = self._ledger()
        job = PFX + "Knight.brief"
        t = self.START
        _, pushed = self._replay(runs, [(t - 300, [job]), (t + 300, []),
                                        (t + 600, [])])
        self.assertEqual(pushed, [job])

    def test_loss_before_the_run_started_is_not_held(self):
        runs = self._ledger()
        job = self.jobs[0]
        t = self.START
        _, pushed = self._replay(runs, [(t - 900, [job]), (t - 600, []),
                                        (t - 300, [])])
        self.assertEqual(pushed, [job])

    def test_open_run_holds_until_open_max(self):
        runs = self._ledger(finished=False)
        lost = self.at(self.START + 300)
        bishop = self.roots["Bishop"]
        self.assertIsNotNone(ho.update_run_hold(
            lost, bishop, runs, self.at(self.START + 3600)))
        self.assertIsNone(ho.update_run_hold(
            lost, bishop, runs, self.at(
                self.START + ho.UPDATE_OPEN_MAX_SECONDS
                + ho.UPDATE_SETTLE_SECONDS + 1)))


class TestPlistToleranceDgn1990(unittest.TestCase):
    """DGN-1990: the live host showed 5 parse errors -- 3 health hk-ingest
    units rendered from a stale template (double hyphen inside the header
    comment; plutil and launchd accept it) and one zero-byte stale bridge
    plist counted twice.  None of them may read as a coverage hole."""

    DASHES = ('<?xml version="1.0" encoding="UTF-8"?>\n<!-- rendered (NOT by '
              'mint.sh -- stale header) -->\n<plist version="1.0"><dict>\n'
              '<key>Label</key><string>%s</string></dict></plist>\n')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ho-dgn1990p-")
        self.la = os.path.join(self.tmp, "LaunchAgents")
        os.makedirs(self.la)
        self.dash_path = os.path.join(self.la, PFX + "health.hk-ingest-early.plist")
        with open(self.dash_path, "w") as f:
            f.write(self.DASHES % (PFX + "health.hk-ingest-early"))
        self.empty_path = os.path.join(self.la, PFX + "ta.newbridge.plist")
        open(self.empty_path, "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _scan(self):
        errors = []
        bridges = ho.bridge_map_from_dir(self.la, errors)
        disk = ho.scan_disk(self.la, [], errors)
        return bridges, disk, errors

    def test_fixture_really_breaks_plistlib(self):
        with open(self.dash_path, "rb") as f:
            with self.assertRaises(Exception):
                plistlib.load(f)

    def test_double_hyphen_comment_unit_is_observed(self):
        _, disk, errors = self._scan()
        self.assertIn(PFX + "health.hk-ingest-early", disk)
        notes = [e for e in errors if e["path"] == self.dash_path]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["level"], "info")

    def test_empty_file_is_one_info_note_not_a_hole(self):
        _, _, errors = self._scan()
        empty = [e for e in errors if e["path"] == self.empty_path]
        self.assertEqual(len(empty), 1, "read twice, recorded once")
        self.assertEqual(empty[0]["level"], "info")

    def test_poll_has_zero_parse_errors_and_info_records(self):
        _, disk, errors = self._scan()
        obs = obs_for({}, {}, disk=disk, la_dir=self.la)
        obs["plist_errors"] = errors
        state = {}
        ho.poll(state, obs, "t0")
        self.assertEqual(state["parse_errors"], [])
        notes = sorted(w["label"] for w in state["warnings"]
                       if w["kind"] == "plist_note")
        self.assertEqual(notes, sorted([self.dash_path, self.empty_path]))
        self.assertTrue(all(w["level"] == "info" for w in state["warnings"]
                            if w["kind"] == "plist_note"))

    def test_report_keeps_notes_out_of_the_coverage_hole_line(self):
        import contextlib
        import io
        _, _, errors = self._scan()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ho._report_plist_errors(errors)
        self.assertNotIn("COVERAGE HOLE", buf.getvalue())
        self.assertIn("plist note", buf.getvalue())



class TestSameOwnerFailClosedDgn1670(unittest.TestCase):
    def test_unknown_owner_is_not_same_owner(self):
        tmp = tempfile.mkdtemp()
        try:
            a = os.path.join(tmp, "a"); b = os.path.join(tmp, "b")
            os.makedirs(a); os.makedirs(b)
            _write_owner(a, "111")
            self.assertFalse(ho._same_owner(a, b, {}))
            _write_owner(b, "111")
            self.assertTrue(ho._same_owner(a, b, {}))
            _write_owner(b, "222")
            self.assertFalse(ho._same_owner(a, b, {}))
            self.assertTrue(ho._same_owner(b, b, {}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    unittest.main(verbosity=2)
