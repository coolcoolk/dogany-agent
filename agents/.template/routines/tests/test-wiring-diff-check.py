#!/usr/bin/env python3
# test-lane: fast
# test-component: wiring-diff-check
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-new-component: wiring-diff-check.py is a new routine (DGN-1658); no suite owned it before
# promise: a baseline hook missing from settings.json + settings.local.json is reported LOST
# promise: a command registered twice is reported DUPLICATE, every count names its unit (2026-09-23 fixture)
# promise: the instance baseline (wiring-snapshot.custom.json) is united with the header declarations
# promise: a framework routine wired only in settings.local.json and declared nowhere is reported LOCAL-ONLY (DGN-1341)
# promise: the header declarations equal the template settings.json wiring
# promise: the template ships the detector and the update routines channel does not exclude it
# promise: the self-update call site leaves one line when the detector is absent, prints findings, never fails
"""DGN-1658: the hook wiring loss detector and its self-update call site."""
import fnmatch
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROUTINES = Path(__file__).resolve().parents[1]
TEMPLATE = ROUTINES.parent
REPO = TEMPLATE.parents[1]
DETECTOR = ROUTINES / "wiring-diff-check.py"
FIXTURE = ROUTINES / "tests/fixtures/wiring-diff/2026-09-23-health"

spec = importlib.util.spec_from_file_location("wiring_diff_check", DETECTOR)
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)


def clean_env():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_"))}
    return env


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wd-"))
        (self.root / ".claude").mkdir()
        (self.root / "routines").mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else json.dumps(data))

    def check(self):
        out = io.StringIO()
        rc = wd.run(str(self.root), "/nonexistent-home", out)
        return rc, out.getvalue().splitlines()

    def template_settings(self):
        return json.loads((TEMPLATE / ".claude/settings.json").read_text())


class Detector(Base):
    def test_template_wiring_is_clean(self):
        self.put(".claude/settings.json", self.template_settings())
        rc, lines = self.check()
        self.assertEqual(rc, 0, lines)
        self.assertIn("lost_hooks 0", lines[0])

    def test_lost_framework_hook_reported(self):
        data = self.template_settings()
        groups = data["hooks"]["PreToolUse"]
        removed = next(g for g in groups
                       if g.get("matcher") == "Edit|Write|MultiEdit|NotebookEdit|Bash")
        groups.remove(removed)
        gone = removed["hooks"][0]["command"]
        self.put(".claude/settings.json", data)
        rc, lines = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("lost_hooks 1", lines[0])
        self.assertEqual([l for l in lines if "LOST hook" in l],
                         ["wiring-diff: LOST hook -- event PreToolUse, matcher "
                          "Edit|Write|MultiEdit|NotebookEdit|Bash, command " + gone])

    def test_absolute_paths_count_as_the_declared_command(self):
        text = json.dumps(self.template_settings()).replace("__PROJECT_ROOT__", str(self.root))
        self.put(".claude/settings.json", text)
        self.assertEqual(self.check()[0], 0)

    def test_instance_snapshot_united_with_header(self):
        self.put(".claude/settings.json", self.template_settings())
        self.put("routines/wiring-snapshot.custom.json", {"wiring": [
            ["PreToolUse", "routines/register-canon-gate.py"],
            {"event": "Stop", "matcher": None, "command": "routines/kept-gate.py"}]})
        self.put(".claude/settings.local.json", {"hooks": {"Stop": [{"hooks": [
            {"type": "command",
             "command": "/usr/bin/python3 %s/routines/kept-gate.py" % self.root}]}]}})
        rc, lines = self.check()
        self.assertEqual(rc, 1)
        self.assertEqual([l for l in lines if "LOST" in l],
                         ["wiring-diff: LOST hook -- event PreToolUse, matcher (any), "
                          "command routines/register-canon-gate.py"])
        self.assertIn("baseline_hooks %d" % (len(wd.header_declarations()) + 2), lines[0])

    def test_regression_fixture_2026_09_23_counts_and_duplicates(self):
        for name in ("settings.json", "settings.local.json"):
            shutil.copy(FIXTURE / name, self.root / ".claude" / name)
        rc, lines = self.check()
        self.assertEqual(rc, 1)
        self.assertRegex(lines[0], r"^wiring-diff: events 5, matcher_blocks 15, "
                                   r"individual_hook_commands 20, duplicate_commands 2, ")
        dups = [l for l in lines if "DUPLICATE" in l]
        self.assertEqual(len(dups), 2, lines)
        for l in dups:
            self.assertIn("registrations 2", l)
        self.assertTrue(any("event Stop" in l and "status-footer.py" in l for l in dups))
        self.assertTrue(any("event PreToolUse" in l and "bash-safety-gate.py" in l for l in dups))

    def test_unreadable_settings_is_rc2_with_a_line(self):
        rc, lines = self.check()
        self.assertEqual((rc, len(lines)), (2, 1))
        self.assertIn("settings.json missing", lines[0])
        self.put(".claude/settings.json", "{not json")
        rc, lines = self.check()
        self.assertEqual((rc, len(lines)), (2, 1))

    def test_framework_routine_wired_only_locally_is_local_only(self):
        # DGN-1341: decomp-gate lived only in the gitignored local file.
        self.put(".claude/settings.json", self.template_settings())
        gate = "/usr/bin/python3 %s/routines/decomp-gate.py" % self.root
        self.put(".claude/settings.local.json", {"hooks": {
            "PreToolUse": [{"matcher": "Agent|Task", "hooks": [
                {"type": "command", "command": gate}]}],
            "Stop": [{"hooks": [
                {"type": "command", "command": "/usr/bin/python3 %s/routines/mine.custom.py" % self.root},
                {"type": "command", "command": "/usr/bin/python3 /opt/elsewhere/tool.py"}]}]}})
        rc, lines = self.check()
        self.assertEqual(rc, 1, lines)
        self.assertIn("lost_hooks 0, local_only_hooks 1", lines[0])
        self.assertEqual([l for l in lines if "LOCAL-ONLY" in l],
                         ["wiring-diff: LOCAL-ONLY hook (machine-local, never ships) -- "
                          "event PreToolUse, matcher Agent|Task, command "
                          "/usr/bin/python3 __PROJECT_ROOT__/routines/decomp-gate.py"])
        # Declaring it in the instance snapshot makes it audited, not unshipped.
        self.put("routines/wiring-snapshot.custom.json",
                 {"wiring": [["PreToolUse", "routines/decomp-gate.py"]]})
        rc, lines = self.check()
        self.assertEqual(rc, 0, lines)
        self.assertIn("local_only_hooks 0", lines[0])

    def test_bad_snapshot_warns_and_still_checks(self):
        self.put(".claude/settings.json", self.template_settings())
        self.put("routines/wiring-snapshot.custom.json", {"wiring": "nope"})
        rc, lines = self.check()
        self.assertEqual(rc, 0)
        self.assertIn("WARN", lines[0])
        self.assertIn("lost_hooks 0", lines[1])


class Delivery(unittest.TestCase):
    def test_header_declarations_equal_template_settings(self):
        items, _ = wd.hook_items(json.loads((TEMPLATE / ".claude/settings.json").read_text()))
        self.assertEqual(wd.header_declarations(), items,
                         "regenerate the '# declare:' lines of wiring-diff-check.py "
                         "from agents/.template/.claude/settings.json")

    def test_template_ships_detector_and_channel_keeps_it(self):
        self.assertTrue(os.access(DETECTOR, os.X_OK))
        update = (REPO / "update.sh").read_text()
        self.assertIn('"$TEMPLATE/routines/" "$INSTANCE/routines/"', update)
        block = re.search(r"^COMMON_EXCLUDES=\((.*?)^\)", update, re.S | re.M).group(1)
        pats = re.findall(r"--exclude '([^']+)'", block)
        self.assertTrue(pats)
        for part in ("wiring-diff-check.py",):
            self.assertFalse([p for p in pats if fnmatch.fnmatch(part, p)])
        self.assertFalse(fnmatch.fnmatch("wiring-diff-check.py", "*.custom.*"))


class CallSite(Base):
    def run_call(self):
        src = (ROUTINES / "self-update.sh").read_text()
        fn = re.search(r"# >>> DGN-1658 WIRING-CHECK BEGIN\n(.*?)# <<< DGN-1658 WIRING-CHECK END",
                       src, re.S).group(1)
        env = clean_env()
        env["SELF_ROOT"] = str(self.root)
        env["HOME"] = "/nonexistent-home"
        return subprocess.run(["bash", "-c", "set -eu\n" + fn + "wiring_check; echo rc=$?"],
                              env=env, capture_output=True, text=True, timeout=10)

    def test_old_silent_guard_is_gone(self):
        src = (ROUTINES / "self-update.sh").read_text()
        self.assertNotIn("wiring-diff-check.py\" --root \"$SELF_ROOT\" \\\n      >/dev/null", src)
        self.assertRegex(src, r"\n  wiring_check\n  \[ \"\$RESTART\" -eq 1 \]")

    def test_absent_detector_logs_one_line(self):
        r = self.run_call()
        self.assertEqual(r.stdout, "rc=0\n")
        self.assertEqual(len(r.stderr.splitlines()), 1, r.stderr)
        self.assertIn("wiring detector missing", r.stderr)

    def test_findings_are_printed_and_update_continues(self):
        shutil.copy(DETECTOR, self.root / "routines/wiring-diff-check.py")
        for name in ("settings.json", "settings.local.json"):
            shutil.copy(FIXTURE / name, self.root / ".claude" / name)
        r = self.run_call()
        self.assertEqual(r.stdout, "rc=0\n")
        self.assertIn("hook wiring changed", r.stderr)
        self.assertIn("duplicate_commands 2", r.stderr)
        self.assertIn("LOST hook", r.stderr)

    def test_clean_wiring_is_quiet(self):
        shutil.copy(DETECTOR, self.root / "routines/wiring-diff-check.py")
        shutil.copy(TEMPLATE / ".claude/settings.json", self.root / ".claude/settings.json")
        r = self.run_call()
        self.assertEqual((r.stdout, r.stderr), ("rc=0\n", ""))


if __name__ == "__main__":
    unittest.main()
