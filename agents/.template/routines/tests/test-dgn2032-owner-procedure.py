#!/usr/bin/env python3
# test-lane: fast
"""DGN-2032: real incident phrases, near misses, record-only adapter and parity."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROUTINES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTINES / "lib"))
import output_gate as og

FIXTURES = json.loads((ROUTINES / "tests/fixtures/dgn2032-owner-procedure.json").read_text())


def hits(text, **kwargs):
    return [v for v in og.check_message(text, **kwargs)
            if v.rule_id == "OWNER_PROCEDURE"]


class OwnerProcedureTests(unittest.TestCase):
    def test_evidence_and_closed_shapes(self):
        for text in FIXTURES["positive"]:
            with self.subTest(text=text):
                found = hits(text)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].severity, "detect")
                self.assertEqual(found[0].metrics, {"hits": 1, "exhausted_means_cited": 0})
                self.assertLessEqual(len(found[0].excerpt), 60)

    def test_near_misses(self):
        for text in FIXTURES["negative"] + ["", None]:
            with self.subTest(text=text):
                self.assertEqual(hits(text), [])

    def test_citation_records_evidence_but_never_licenses_procedures(self):
        procedure = FIXTURES["positive"][0]
        for prefix, cited in [
            ("\ud655\uc778\ud55c \uc218\ub2e8: \ube0c\ub77c\uc6b0\uc800 \ub85c\uadf8\uc778 \uc2e4\ud328 [\uae30\ub85d](/tmp/check.md)", 1),
            ("Exhausted means: browser login failed [record](https://example.org/result)", 1),
            ("\ud655\uc778\ud55c \uc218\ub2e8: \ub2e4 \ud574\ubd24\uc2b5\ub2c8\ub2e4", 0),
            ("[\uad00\ub828 \ub9c1\ud06c](https://example.org)", 0),
            ("> \ud655\uc778\ud55c \uc218\ub2e8: \ube0c\ub77c\uc6b0\uc800 [\uae30\ub85d](/tmp/check.md)", 0),
            ("```\n\ud655\uc778\ud55c \uc218\ub2e8: \ube0c\ub77c\uc6b0\uc800 [\uae30\ub85d](/tmp/check.md)\n```", 0),
        ]:
            with self.subTest(prefix=prefix):
                self.assertEqual(hits(prefix + "\n" + procedure)[0].metrics["exhausted_means_cited"], cited)
        self.assertEqual(hits("\ud655\uc778\ud55c \uc218\ub2e8: \ube0c\ub77c\uc6b0\uc800 [\uae30\ub85d](/tmp/check.md)\n"
                              "9\uc6d4 \ub0a9\ubd80 \uacb0\uc81c\uc218\ub2e8 \ud558\ub098\ub9cc \uc54c\ub824 \uc8fc\uc138\uc694."), [])

    def test_counts_and_domain_independence(self):
        text = "\n".join(FIXTURES["positive"][:3])
        self.assertEqual(hits(text, content={"role": "health"})[0].metrics["hits"], 3)

    def test_stop_adapter_records_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "session.jsonl"
            log = root / "canary.log"
            entries = [
                {"type": "user", "message": {"content": "check payment"}},
                {"type": "assistant", "message": {"id": "m1", "content": [
                    {"type": "text", "text": FIXTURES["positive"][0]}]}},
            ]
            transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            proc = subprocess.run(
                [sys.executable, str(ROUTINES / "output-gate-stop.py")],
                input=json.dumps({"hook_event_name": "Stop", "session_id": "s1",
                                  "transcript_path": str(transcript), "cwd": tmp,
                                  "stop_hook_active": False}),
                env=dict(os.environ, DOGANY_OUTPUT_GATE_LOG=str(log)),
                text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn('"block"', proc.stdout)
            lines = [line for line in log.read_text().splitlines() if "OWNER_PROCEDURE" in line]
            self.assertEqual(len(lines), 1)
            self.assertIn("hits=1 exhausted_means_cited=0", lines[0])

    def test_canonical_root_template_parity(self):
        repo = ROUTINES.parents[2]
        root_copy = repo / "routines/lib/output_gate.py"
        if root_copy.exists():
            self.assertEqual(root_copy.read_bytes(), (ROUTINES / "lib/output_gate.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
