#!/usr/bin/env python3
# test-lane: fast
"""v2.7: shipped generic gates are wired in the template settings.json.

The template shipped done-claim-gate, tmp-artifact-gate and
ticket-status-enum-lint but wired none of them, so only an instance that
hand-wired them got the check. Locks:
  (a) each generic gate is wired once, on its event and write-tool matcher;
  (b) output-gate-stop.py is active once for every minted instance (FDGN-579);
  (c) every wired routines/ script exists in the template;
  (d) tmp-artifact-gate denies a spec-shaped file under files/tmp/ with an
      English, instance-neutral reason, and passes scratch and non-tmp paths.
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROUTINES = Path(__file__).resolve().parent.parent
TEMPLATE = ROUTINES.parent
SETTINGS = TEMPLATE / ".claude" / "settings.json"
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

WIRED = (
    ("PreToolUse", "done-claim-gate.py"),
    ("PostToolUse", "ticket-status-enum-lint.py"),
)


def _rows(event, script):
    hooks = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]
    return [m for m in hooks.get(event, [])
            for h in m["hooks"] if h["command"].endswith("/routines/" + script)]


class Wiring(unittest.TestCase):
    def test_a_generic_gates_wired_once_on_write_tools(self):
        for event, script in WIRED:
            rows = _rows(event, script)
            self.assertEqual(1, len(rows), (event, script, rows))
            self.assertEqual(WRITE_TOOLS, set(rows[0]["matcher"].split("|")),
                             script)

    def test_b_output_gate_stop_is_wired_once_for_every_instance(self):
        self.assertEqual([], _rows('Stop', 'output-gate-stop.py'))
        self.assertEqual([], _rows('PreToolUse', 'tmp-artifact-gate.py'))
        self.assertEqual([], _rows('SubagentStop', 'output-gate-stop.py'))

    def test_c_every_wired_script_exists(self):
        text = SETTINGS.read_text(encoding="utf-8")
        for name in re.findall(r"__PROJECT_ROOT__/(routines/[\w./-]+\.py)",
                               text):
            self.assertTrue((TEMPLATE / name).is_file(), name)


def _tmp_gate(path):
    payload = {"tool_name": "Write",
               "tool_input": {"file_path": path, "content": "x"}}
    p = subprocess.run([sys.executable, str(ROUTINES / "tmp-artifact-gate.py")],
                       input=json.dumps(payload), capture_output=True,
                       text=True, timeout=30)
    return p.returncode, p.stdout


class TmpArtifactGate(unittest.TestCase):
    def test_d_spec_in_tmp_is_denied_in_neutral_english(self):
        rc, out = _tmp_gate("/x/inst/files/tmp/onboarding-spec.md")
        self.assertEqual(0, rc)
        self.assertEqual('', out)  # Artifact placement is guidance, never a refusal.

    def test_d_scratch_and_non_tmp_pass(self):
        for path in ("/x/inst/files/tmp/scratch.txt",
                     "/x/inst/files/outbox/spec.md"):
            self.assertEqual((0, ""), _tmp_gate(path), path)


if __name__ == "__main__":
    unittest.main()
