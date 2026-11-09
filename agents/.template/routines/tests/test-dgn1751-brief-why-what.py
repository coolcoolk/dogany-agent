#!/usr/bin/env python3
# test-lane: fast
"""DGN-1751 -- a dispatch brief pins its Why and What before build.

  L1-L9  routines/lib/brief_why_what.py: label shapes, min length, first
         match wins, fenced code ignored, verdict tokens, unreadable file.
  D1-D4  dispatch-detached.sh brief_why_what_check, run as the script runs
         it: ok / missing:why keeps What in its own field (no tab collapse) /
         advisory goes to stderr and never fails / no brief -> unread.
  S1     the fields reach run.env and status.json.

Synthetic data only.  Run: python3 routines/tests/test-dgn1751-brief-why-what.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

ROUTINES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROUTINES, "lib", "brief_why_what.py")
DISPATCH = os.path.join(ROUTINES, "dispatch-detached.sh")

spec = importlib.util.spec_from_file_location("brief_why_what", LIB)
bww = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bww)


class Lib(unittest.TestCase):
    def test_L1_plain_lines(self):
        f = bww.scan("Why: the owner must not be told a false thing\n"
                     "What: one alarm line the owner can act on, shown as a render\n")
        self.assertEqual(bww.verdict(f), "ok")
        self.assertEqual(f["why"], "the owner must not be told a false thing")

    def test_L2_markdown_shapes(self):
        f = bww.scan("- **Why:** owner wants one build, not three\n"
                     "## What (artifact): the /update offer card as a Telegram render\n"
                     "* Sample: files/outbox/offer-render.png (owner confirmed)\n")
        self.assertEqual(bww.verdict(f), "ok")
        self.assertEqual(f["why"], "owner wants one build, not three")
        self.assertTrue(f["what"].startswith("the /update offer card"))
        self.assertTrue(f["sample"].startswith("files/outbox/offer-render.png"))

    def test_L3_too_short_does_not_pin(self):
        f = bww.scan("Why: x\nWhat:\n")
        self.assertEqual(bww.verdict(f), "missing:why,what")

    def test_L4_missing_one(self):
        self.assertEqual(bww.verdict(bww.scan("Why: owner wants it fast\n")),
                         "missing:what")
        self.assertEqual(bww.verdict(bww.scan("What: a pushed branch with tests\n")),
                         "missing:why")

    def test_L5_first_qualifying_match_wins(self):
        f = bww.scan("Why: tiny\nWhy: the real intent line here\nWhy: a later one too\n")
        self.assertEqual(f["why"], "the real intent line here")

    def test_L6_fenced_code_ignored(self):
        f = bww.scan("```\nWhy: inside a code fence only\n```\n")
        self.assertEqual(f["why"], "")

    def test_L7_how_only_brief(self):
        f = bww.scan("# Task\n1. edit foo.py\n2. run tests\nwhy is this slow? no idea\n")
        self.assertEqual(bww.verdict(f), "missing:why,what")

    def test_L8_mid_line_label_not_matched(self):
        f = bww.scan("The Why: should be at line start to count, not here\n")
        self.assertEqual(f["why"], "")

    def test_L9_cli_unreadable_and_output_shape(self):
        p = subprocess.run([sys.executable, LIB, "/nonexistent/brief.md"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("What: a\tpushed branch with tests\n")
        try:
            p = subprocess.run([sys.executable, LIB, fh.name],
                               capture_output=True, text=True)
        finally:
            os.unlink(fh.name)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "missing:why\t\ta pushed branch with tests\t\n")


def run_check(brief_text):
    fn = subprocess.run(["sed", "-n", "/^brief_why_what_check() {/,/^}/p", DISPATCH],
                        capture_output=True, text=True).stdout
    assert "brief_why_what.py" in fn, "function not found"
    with tempfile.TemporaryDirectory() as d:
        task = os.path.join(d, "brief.md")
        if brief_text is not None:
            with open(task, "w") as fh:
                fh.write(brief_text)
        script = (fn + '\nSCRIPT_DIR="$1" TASK_FILE="$2"\nbrief_why_what_check; rc=$?\n'
                  'printf "%s|%s|%s|%s|%s\\n" "$rc" "$BRIEF_WHY_WHAT" "$BRIEF_WHY" '
                  '"$BRIEF_WHAT" "$BRIEF_SAMPLE"')
        return subprocess.run(["bash", "-c", script, "_", ROUTINES, task],
                              capture_output=True, text=True, timeout=30)


class Dispatch(unittest.TestCase):
    def test_D1_ok(self):
        p = run_check("Why: the owner wants one build\nWhat: a pushed branch with tests\n")
        self.assertEqual(p.stdout.strip(),
                         "0|ok|the owner wants one build|a pushed branch with tests|")
        self.assertEqual(p.stderr, "")

    def test_D2_missing_why_keeps_fields_aligned(self):
        p = run_check("What: a pushed branch with tests\nSample: files/outbox/x.png render\n")
        self.assertEqual(p.stdout.strip(),
                         "0|missing:why||a pushed branch with tests|files/outbox/x.png render")

    def test_D3_advisory_never_fails(self):
        p = run_check("1. edit foo.py\n2. run the tests\n")
        self.assertTrue(p.stdout.startswith("0|missing:why,what|"), p.stdout)
        self.assertIn("advisory (DGN-1751)", p.stderr)
        self.assertIn("not refused", p.stderr)

    def test_D4_no_brief_is_unread(self):
        p = run_check(None)
        self.assertEqual(p.stdout.strip(), "0|unread|||")
        self.assertEqual(p.stderr, "")


class Wiring(unittest.TestCase):
    def test_S1_fields_recorded(self):
        with open(DISPATCH) as fh:
            src = fh.read()
        self.assertIn("  resolve_dispatch_why\n  brief_why_what_check\n", src)
        fn = subprocess.run(["sed", "-n", "/^run_env_write() {/,/^}/p", DISPATCH],
                            capture_output=True, text=True, check=True).stdout
        values = {
            "BRIEF_WHY_WHAT": "missing:what",
            "BRIEF_WHY": "유저 wants 'one' result",
            "BRIEF_WHAT": "a path with spaces and \\slashes",
            "BRIEF_SAMPLE": "files/outbox/결과 image.png",
        }
        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, "run.env")
            script = fn + '\n: > "$1"; file="$1"; shift\n' + \
                'while [ "$#" -gt 0 ]; do run_env_write "$1" "$2" >> "$file"; shift 2; done\n' + \
                'unset BRIEF_WHY_WHAT BRIEF_WHY BRIEF_WHAT BRIEF_SAMPLE; source "$file"\n' + \
                'printf "%s\\0%s\\0%s\\0%s" "$BRIEF_WHY_WHAT" "$BRIEF_WHY" "$BRIEF_WHAT" "$BRIEF_SAMPLE"\n'
            args = [item for pair in values.items() for item in pair]
            saved = subprocess.run(["bash", "-c", script, "_", env_file, *args],
                                   capture_output=True, check=True)
            self.assertEqual(saved.stdout.split(b"\0"),
                             [value.encode() for value in values.values()])
            open(env_file, encoding="utf-8").read()
        for key in ("brief_why_what", "brief_why", "brief_what", "brief_sample"):
            self.assertIn('"%s":' % key, src)


if __name__ == "__main__":
    unittest.main()
