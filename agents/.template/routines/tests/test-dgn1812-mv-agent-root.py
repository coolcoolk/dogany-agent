#!/usr/bin/env python3
# test-lane: fast
"""DGN-1812: a hand `mv` of an agent root is a hand retirement.

The Bash safety gate flags `mv <dir holding .instance.conf> ...` (row
DGN-1812, class destructive) and its deny names the retire verb, which also
removes the estate records a hand move leaves behind. Moving files inside an
agent, moving something INTO an agent, a plain directory and scratch
fixtures are not findings.
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROUTINES = Path(__file__).resolve().parent.parent
_TMP_BASE = os.environ.get("V27_D34_TMP") or os.path.expanduser("~")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BS = _load("bash_safety_dgn1812", ROUTINES / "lib" / "bash_safety.py")
GATE = _load("bash_safety_gate_dgn1812", ROUTINES / "bash-safety-gate.py")


class MvAgentRoot(unittest.TestCase):

    def setUp(self):
        # not under /tmp: /tmp is a scratch root
        self.tmp = tempfile.mkdtemp(prefix=".dgn1812-", dir=_TMP_BASE)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        agent = os.path.join(self.tmp, "agents", "x")
        os.makedirs(os.path.join(agent, "files"))
        Path(agent, ".instance.conf").write_text("DOGANY_AGENT_NAME=x\n")
        Path(agent, "files", "notes.md").write_text("n\n")
        os.makedirs(os.path.join(self.tmp, "plain"))
        self.scratch = tempfile.mkdtemp(prefix="dgn1812-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.scratch, True)
        os.makedirs(os.path.join(self.scratch, "y"))
        Path(self.scratch, "y", ".instance.conf").write_text("DOGANY_AGENT_NAME=y\n")

    def rows(self, cmd):
        ctx = BS.Ctx(self.tmp, env={}, pid_command=lambda p: None, armed=set())
        return [(f.cls, f.row) for f in BS.classify(cmd, ctx, trash_cmd="trash").findings]

    def test_hand_retire_is_flagged(self):
        hit = [("destructive", "DGN-1812")]
        self.assertEqual(hit, self.rows("mv agents/x agents/.retired/x"))
        self.assertEqual(hit, self.rows("mv agents/x/ agents/.retired/"))
        self.assertEqual(hit, self.rows("mv -v %s/agents/x /elsewhere" % self.tmp))
        self.assertEqual(hit, self.rows("mv -t agents/.retired agents/x"))
        self.assertEqual(hit, self.rows("mkdir -p agents/.retired && mv agents/x agents/.retired/"))
        self.assertEqual(hit, self.rows("bash -c 'mv agents/x agents/.retired/x'"))

    def test_not_a_retirement(self):
        self.assertEqual([], self.rows("mv agents/x/files/notes.md agents/x/files/n2.md"))
        self.assertEqual([], self.rows("mv plain plain2"))
        self.assertEqual([], self.rows("mv plain agents/x"))           # into an agent
        self.assertEqual([], self.rows("mv %s/y %s/z" % (self.scratch, self.scratch)))
        self.assertEqual([], self.rows("mv $ROOT elsewhere"))

    def test_deny_names_the_retire_verb(self):
        f = BS.Finding("destructive", "DGN-1812", "mv of an agent root (agents/x) is a hand retirement")
        reason = GATE.deny_reason(f, "key", questions=["Q?"])
        self.assertIn("scripts/pack/mint_run.sh retire --root", reason)
        self.assertIn("install registry row", reason)
        self.assertNotIn("trash.sh", reason)


if __name__ == "__main__":
    unittest.main()
