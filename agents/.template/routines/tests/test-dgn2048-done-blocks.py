#!/usr/bin/env python3
# test-lane: fast
"""DGN-2048 -- DONE is the user's unit of work: the dispatch forcing points.

  L1-L7  routines/lib/done_blocks.py: brief Unit of work / E2E gate lines,
         return Open items / Re-checked blocks (inline or listed under the
         label), glosses, fences, stderr tail ignored, CLI shape.
  D1-D3  dispatch-detached.sh brief_unit_gate_check, run as the script runs
         it: ok / advisory on stderr, never fails / no brief -> unread.
  R1-R4  write_session_inbox: a completed run whose report lacks the blocks
         returns "completed, report incomplete" with a report line and a
         REPORT INCOMPLETE directive; a report with both is plain completed;
         a failed run is not judged on its report.
  W1     wiring: run.env / status.json fields, backstop states the blocks.

Synthetic data only.  Run: python3 routines/tests/test-dgn2048-done-blocks.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

ROUTINES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROUTINES, "lib", "done_blocks.py")
DISPATCH = os.path.join(ROUTINES, "dispatch-detached.sh")

spec = importlib.util.spec_from_file_location("done_blocks", LIB)
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)

GOOD_REPORT = ("Built the session flow.\n\n"
               "Open items vs the unit of work: none\n"
               "Re-checked (previously working):\n"
               "- set cards keep their done button (replay open -> close)\n")
BAD_REPORT = "Built slice P1. All tests pass. Done.\n"


class Lib(unittest.TestCase):
    def test_L1_brief_ok(self):
        f = db.scan_brief("Why: owner gets a button on every card\n"
                          "- **Unit of work:** a workout session, open -> prep -> sets -> close\n"
                          "E2E gate (user screen): replay the whole session, every card answers\n")
        self.assertEqual(db.verdict(f), "ok")
        self.assertTrue(f["unit"].startswith("a workout session"))
        self.assertTrue(f["gate"].startswith("replay the whole session"))

    def test_L2_brief_missing(self):
        self.assertEqual(db.verdict(db.scan_brief("Why: x\nWhat: a branch\n")),
                         "missing:unit,gate")
        self.assertEqual(db.verdict(db.scan_brief("Unit of work: onboarding flow\n")),
                         "missing:gate")
        self.assertEqual(db.verdict(db.scan_brief("Unit of work: tiny\n")),
                         "missing:unit,gate")

    def test_L3_return_ok_inline_and_listed(self):
        f = db.scan_return(GOOD_REPORT)
        self.assertEqual(db.verdict(f), "ok")
        self.assertEqual(f["open-items"], "none")
        self.assertTrue(f["rechecked"].startswith("- set cards"))

    def test_L4_return_missing(self):
        self.assertEqual(db.verdict(db.scan_return(BAD_REPORT)),
                         "missing:open-items,rechecked")
        self.assertEqual(db.verdict(db.scan_return("Open items: prep cards\n")),
                         "missing:rechecked")

    def test_L5_label_without_content_does_not_count(self):
        f = db.scan_return("Open items:\n\nRe-checked:\nRe-checked: \n")
        self.assertEqual(db.verdict(f), "missing:open-items,rechecked")

    def test_L6_fence_and_stderr_ignored(self):
        text = ("```\nOpen items: none\nRe-checked: all\n```\nreport body\n"
                "\n--- stderr ---\nOpen items: none\nRe-checked: x\n")
        self.assertEqual(db.verdict(db.scan_return(text)),
                         "missing:open-items,rechecked")

    def test_L7_cli_shape(self):
        p = subprocess.run([sys.executable, LIB, "brief", "/nonexistent"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        p = subprocess.run([sys.executable, LIB, "bogus", LIB],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("E2E gate: owner taps through\tthe full flow\n")
        try:
            p = subprocess.run([sys.executable, LIB, "brief", fh.name],
                               capture_output=True, text=True)
        finally:
            os.unlink(fh.name)
        self.assertEqual(p.stdout, "missing:unit\t\towner taps through the full flow\n")


def _fn(name):
    out = subprocess.run(["sed", "-n", "/^%s() {/,/^}/p" % name, DISPATCH],
                         capture_output=True, text=True).stdout
    assert out.strip(), "function %s not found" % name
    return out


def run_check(brief_text):
    fn = _fn("brief_unit_gate_check")
    with tempfile.TemporaryDirectory() as d:
        task = os.path.join(d, "brief.md")
        if brief_text is not None:
            with open(task, "w") as fh:
                fh.write(brief_text)
        script = (fn + '\nSCRIPT_DIR="$1" TASK_FILE="$2"\nbrief_unit_gate_check; rc=$?\n'
                  'printf "%s|%s|%s|%s\\n" "$rc" "$BRIEF_UNIT_GATE" "$BRIEF_UNIT" "$BRIEF_GATE"')
        return subprocess.run(["bash", "-c", script, "_", ROUTINES, task],
                              capture_output=True, text=True, timeout=30)


class Dispatch(unittest.TestCase):
    def test_D1_ok(self):
        p = run_check("Unit of work: the onboarding flow\nE2E gate: a fresh install reaches chat\n")
        self.assertEqual(p.stdout.strip(),
                         "0|ok|the onboarding flow|a fresh install reaches chat")
        self.assertEqual(p.stderr, "")

    def test_D2_advisory_never_fails(self):
        p = run_check("Why: the owner wants it\nWhat: a branch with tests\n")
        self.assertEqual(p.stdout.strip(), "0|missing:unit,gate||")
        self.assertIn("advisory (DGN-2048)", p.stderr)
        self.assertIn("not refused", p.stderr)

    def test_D3_no_brief_is_unread(self):
        p = run_check(None)
        self.assertEqual(p.stdout.strip(), "0|unread||")


def run_inbox(result, report, runtime="claude", brief="ok"):
    fn = _fn("write_session_inbox")
    with tempfile.TemporaryDirectory() as d:
        rundir = os.path.join(d, "run")
        inbox = os.path.join(d, "inbox")
        os.makedirs(rundir)
        with open(os.path.join(rundir, "out.log"), "w") as fh:
            fh.write(report)
        # The extracted writer needs an instance root as well as an inbox;
        # an unset WORKSPACE resolves to the framework checkout in this test.
        script = (fn + '\nSCRIPT_DIR="$1" WORKSPACE="$2" RUNDIR="$2/run" SESSION_INBOX_DIR="$3"\n'
                  'RUNID=r1 LABEL=job TICKET=DGN-0 MODEL=opus RUNTIME="$4" '
                  'MODEL_DISPLAY_BINDINGS="" BRANCH=dsp/x COMMIT_COUNT=1 '
                  'COMMIT_HASH=abc BRIEF_UNIT_GATE="$6"\n'
                  'write_session_inbox "$5" 0\n')
        p = subprocess.run(["bash", "-c", script, "_", ROUTINES, d, inbox,
                            runtime, result, brief],
                           capture_output=True, text=True, timeout=30)
        path = os.path.join(inbox, "dispatch-r1.md")
        assert os.path.exists(path), p.stderr
        with open(path, encoding="utf-8") as fh:
            return fh.read()


class Return(unittest.TestCase):
    def test_R1_missing_blocks_marked_incomplete(self):
        body = run_inbox("completed", BAD_REPORT)
        first = body.splitlines()[0]
        self.assertTrue(first.startswith("[dispatch-return] "), first)
        self.assertTrue(first.endswith("-- completed, report incomplete"), first)
        self.assertIn("- report: missing:open-items,rechecked", body)
        self.assertIn("REPORT INCOMPLETE", body)
        self.assertIn("- result: completed (exit=0)", body)

    def test_R2_both_blocks_plain_completed(self):
        body = run_inbox("completed", GOOD_REPORT)
        self.assertTrue(body.splitlines()[0].endswith("-- completed"))
        self.assertIn("- report: ok", body)
        self.assertNotIn("REPORT INCOMPLETE", body)

    def test_R3_failed_run_not_judged(self):
        body = run_inbox("error", BAD_REPORT)
        self.assertNotIn("report incomplete", body)
        self.assertNotIn("- report:", body)

    def test_R4_brief_verdict_carried(self):
        body = run_inbox("completed", GOOD_REPORT, brief="missing:gate")
        self.assertIn("- brief: unit/gate missing:gate", body)


class Wiring(unittest.TestCase):
    def test_W1_fields_and_backstop(self):
        with open(DISPATCH, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("  brief_why_what_check\n  brief_unit_gate_check\n", src)
        fn = _fn("run_env_write")
        values = {
            "BRIEF_UNIT_GATE": "missing:gate",
            "BRIEF_UNIT": "사용자가 owns the 'whole' flow",
            "BRIEF_GATE": "fresh install \\ path reaches chat",
        }
        with tempfile.TemporaryDirectory() as d:
            env_file = os.path.join(d, "run.env")
            script = fn + '\n: > "$1"; file="$1"; shift\n' + \
                'while [ "$#" -gt 0 ]; do run_env_write "$1" "$2" >> "$file"; shift 2; done\n' + \
                'unset BRIEF_UNIT_GATE BRIEF_UNIT BRIEF_GATE; source "$file"\n' + \
                'printf "%s\\0%s\\0%s" "$BRIEF_UNIT_GATE" "$BRIEF_UNIT" "$BRIEF_GATE"\n'
            args = [item for pair in values.items() for item in pair]
            saved = subprocess.run(["bash", "-c", script, "_", env_file, *args],
                                   capture_output=True, check=True)
            self.assertEqual(saved.stdout.split(b"\0"),
                             [value.encode() for value in values.values()])
            open(env_file, encoding="utf-8").read()
        for key in ("brief_unit_gate", "brief_unit", "brief_gate"):
            self.assertIn('"%s":' % key, src)
        backstop = _fn("worker_backstop")
        self.assertIn("Open items:", backstop)
        self.assertIn("Re-checked:", backstop)
        self.assertIn("Unit of work (set by the dispatcher)", backstop)


if __name__ == "__main__":
    unittest.main()
