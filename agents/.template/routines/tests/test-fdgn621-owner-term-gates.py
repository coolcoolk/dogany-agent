#!/usr/bin/env python3
# test-lane: fast
# test-component: configured owner vocabulary shared by attribution/output gates
# test-tier: 0
# test-prereq: python3
# test-new-component: cross-gate contract has no existing shared owner-term suite
"""FDGN-621: owner gates follow the configured term."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))


class ConfiguredOwnerTerm(unittest.TestCase):
    def setUp(self):
        self.old_cwd = os.getcwd()
        self.old_term = os.environ.get("DOGANY_OWNER_TERM")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chdir(self.tmp.name)
        os.environ["DOGANY_OWNER_TERM"] = "\ub300\ud45c\ub2d8"

    def tearDown(self):
        os.chdir(self.old_cwd)
        if self.old_term is None:
            os.environ.pop("DOGANY_OWNER_TERM", None)
        else:
            os.environ["DOGANY_OWNER_TERM"] = self.old_term
        for name in ("attrib_gate", "output_gate"):
            sys.modules.pop(name, None)

    def test_configured_term_triggers_attribution_claim_gate(self):
        gate = importlib.import_module("attrib_gate")
        body = "\ub300\ud45c\ub2d8 \uc2b9\uc778 \ubc1b\uc558\uc73c\ub2c8 \uc774\ub300\ub85c \uac04\ub2e4."
        hits = [m for m in gate.CLAIM.finditer(body)
                if not gate._claim_voided(body, m)]
        self.assertEqual(1, len(hits))

    def test_configured_term_gets_output_gate_owner_exemptions(self):
        gate = importlib.import_module("output_gate")
        self.assertIsNotNone(gate.IDLE_PROMISE_EXEMPT_RE.search(
            "\ub300\ud45c\ub2d8 \uc2b9\uc778 \ud6c4 \ubc14\ub85c \uc2dc\uc791\ud558\uaca0\uc2b5\ub2c8\ub2e4"))
        self.assertIsNotNone(gate.IDLE_PROMISE_OWNER_TRIGGER_RE.search(
            "\ub300\ud45c\ub2d8\uc774 \uc6d0\ud558\uc2dc\uba74 \uc9c4\ud589\ud558\uaca0\uc2b5\ub2c8\ub2e4"))


if __name__ == "__main__":
    unittest.main()
