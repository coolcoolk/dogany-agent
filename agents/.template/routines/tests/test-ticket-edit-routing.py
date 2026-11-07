#!/usr/bin/env python3
# test-lane: fast
# test-component: why-net
# test-new-component: cross-surface writer guidance spans dispatch returns, playbooks and reader diagnostics beyond the graph suite
# test-tier: 0
# test-prereq: tmp-files
# promise: hand-edit supplements name the typed writer without clearing judgment
# promise: dispatch returns and agent playbooks route lifecycle edits to the writer
import importlib.util
from pathlib import Path
import unittest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / '.git').exists())
spec = importlib.util.spec_from_file_location('citation_regressions', ROOT / 'routines/tests/test-why-net-p3.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

class Routing(unittest.TestCase):
    def test_hand_edit_guidance(self):
        fixture = base.CitationPath('runTest')
        fixture.setUp()
        try:
            fixture.replace(fixture.path, 'status: open', 'status: wip')
            trace = fixture.trace()
            self.assertEqual(trace['state'], 'supplement')
            check = next(c for c in trace['rechecks'] if c['kind'] == 'unjudged-change')
            for text in ('devkit_ticket set', '--status', '--progress'):
                self.assertIn(text, check['supported_path'])
        finally:
            fixture.tearDown()

    def test_entry_points(self):
        for rel in ('routines/dispatch-detached.sh', 'agents/.template/routines/dispatch-detached.sh',
                    'agents/.template/playbooks/layout.md'):
            text = (ROOT / rel).read_text()
            self.assertTrue('devkit_ticket set' in text, rel)
            self.assertIn('--progress', text, rel)
            self.assertIn('--status', text, rel)

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Routing)
    for name in ('test_writer_status_with_refs_preserves_confirmation',
                 'test_writer_progress_with_multiline_preserves_confirmation',
                 'test_gap_in_receipt_chain_stays_unjudged',
                 'test_scope_receipt_cannot_be_cleared_by_status'):
        suite.addTest(base.CitationPath(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
