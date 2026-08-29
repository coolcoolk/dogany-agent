#!/usr/bin/env python3
"""DGN-986 axis A acceptance tests: health-observer parsing + state machine.

All launchctl behavior is FIXTURE-FIXED text (captured live 2026-08-21 on this
machine's real launchd output shapes); no test ever calls launchctl (spec
requirement: environment-independent).

Covers (spec R1/R2/R3 + DGN-989 regression):
  1. launchctl list / print parsing -- 4-state exit field (line absent /
     never exited / negative signal / positive), runs, top-level keepalive
     only (sub-block "keepalive = 0" noise ignored), path, pid.
  2. runs delta < 0 -> episode reset (reload).
  3. lost-label detection (known-set minus loaded-set) + reappearance clear.
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

Run: python3 routines/tests/test-health-observer.py
Exit 0 = all pass.
"""

import importlib.util
import json
import os
import plistlib
import shutil
import sys
import tempfile
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
        # DGN-989 live proof: file Kojeni.newbridge.plist carries Label
        # com.telegram-skill-bot.Kojeni (no dot-suffix heuristic works).
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
        ev, _ = ho.poll(state, obs, "2026-08-21T15:05:00+09:00")
        self.assertEqual([e["kind"] for e in ev], ["label_lost"])
        self.assertEqual(ev[0]["label"], self.LABEL)
        # next poll: still lost -> no repeat
        ev2, _ = ho.poll(state, obs, "2026-08-21T15:10:00+09:00")
        self.assertEqual(ev2, [])

    def test_retired_marker_suppresses_loss(self):
        state = self._seed()
        state["jobs"][self.LABEL]["retired"] = {
            "when": "2026-08-21T15:04:00+09:00", "why": "job removed on purpose"}
        ev, _ = ho.poll(state, obs_for({}, {}), "2026-08-21T15:05:00+09:00")
        self.assertEqual(ev, [])
        self.assertIsNotNone(state["jobs"][self.LABEL]["lost_at"])
        self.assertIsNone(state["jobs"][self.LABEL].get("lost_notified_at"))

    def test_reappearance_clears_then_new_loss_fires_again(self):
        state = self._seed()
        ho.poll(state, obs_for({}, {}), "t1")           # loss #1
        obs_back = obs_for({self.LABEL: None},
                           {self.LABEL: parsed(runs=4, exit_line="0")})
        ho.poll(state, obs_back, "t2")                  # reappears
        self.assertIsNone(state["jobs"][self.LABEL]["lost_at"])
        ev, _ = ho.poll(state, obs_for({}, {}), "t3")   # loss #2
        self.assertEqual([e["kind"] for e in ev], ["label_lost"])

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
    warn when they differ. Live case rebuilt verbatim: label
    com.telegram-skill-bot.metal, LaunchAgents copy vs loaded repo copy
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
        ho.cmd_retire(label, "renamed to 0830")
        state, _ = ho.load_state()
        self.assertEqual(state["jobs"][label]["retired"]["why"],
                         "renamed to 0830")
        ho.cmd_unretire(label)
        state, _ = ho.load_state()
        self.assertNotIn("retired", state["jobs"][label])


class TestNotificationCopy(unittest.TestCase):
    """Dead-end forbidden: every push must carry sentence CTAs + a button."""

    def test_fail_streak_copy_has_ctas_and_button(self):
        ev = {"kind": "fail_streak", "label": PFX + "warg.update-1300",
              "streak": 2, "exit": 127, "structural": True,
              "episode_started_at": "2026-08-19T13:00:00+09:00"}
        text, button = ho.compose_fail_streak(ev, "metal")
        self.assertIn("update-1300 작업 실패 원인 봐줘", text)
        self.assertIn("update-1300 작업 없애줘", text)
        self.assertIn(">! 기술 상세", text)
        self.assertIn(PFX + "warg.update-1300", text)
        self.assertIn("127", text)
        # domain-live job observed from metal -> Skull-lane wording
        self.assertIn("스컬", text)
        self.assertTrue(button.endswith("::opt:1"))
        self.assertIn("실패 원인 봐줘", button)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
