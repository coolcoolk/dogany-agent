#!/usr/bin/env python3
# test-lane: fast
# test-covers: agents/.template/routines/lib/output_gate.py agents/.template/routines/output-gate-stop.py agents/.template/vendors/telegram.md agents/.template/bridge/tests/fixtures/dgn2033-exit-hatch.json
"""DGN-2033: evidence replay and record-before-block contract."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from output_gate import check_message

# Shared with the bridge test; the fixture lives with the standalone bridge.
CASES = json.loads((ROOT.parent / 'bridge/tests/fixtures/dgn2033-exit-hatch.json').read_text())['cases']


class ExitHatch(unittest.TestCase):
    def test_evidence_and_near_misses(self):
        for case in CASES:
            with self.subTest(case=case['name']):
                hits = [v for v in check_message(case['text'])
                        if v.rule_id == 'EXIT_HATCH_OPTION']
                self.assertEqual(sum(v.metrics['hits'] for v in hits), case['hits'])
                for v in hits:
                    self.assertEqual(v.severity, 'detect')
                    self.assertEqual(v.metrics['stage'], 'record')

    def test_stop_logs_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = root / 'transcript.jsonl'
            text = CASES[0]['text']
            entries = [dict(type='user', message=dict(content='Trademark answer?')),
                       dict(type='assistant', message=dict(content=[dict(type='text', text=text)]))]
            tx.write_text(''.join(json.dumps(e) + '\n' for e in entries))
            # The hook reads BOT_DATA_DIR from config/agent.conf, not the env;
            # DOGANY_OUTPUT_GATE_LOG is its test override.
            env = dict(os.environ, DOGANY_OUTPUT_GATE_LOG=str(root / 'bot/logs/output-gate-canary.log'))
            result = subprocess.run(
                [sys.executable, str(ROOT / 'output-gate-stop.py')],
                input=json.dumps(dict(cwd=tmp, transcript_path=str(tx), hook_event_name='Stop')),
                capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '')
            log = (root / 'bot/logs/output-gate-canary.log').read_text()
            self.assertIn('EXIT_HATCH_OPTION', log)
            self.assertIn('hits=1', log)
            self.assertIn('stage=record', log)

    def test_canonical_root_template_parity(self):
        canonical = ROOT.parents[2]
        if (canonical / 'agents/.template').is_dir():
            self.assertEqual((canonical / 'routines/lib/output_gate.py').read_bytes(),
                             (ROOT / 'lib/output_gate.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
