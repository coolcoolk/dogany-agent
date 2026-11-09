#!/usr/bin/env python3
# test-lane: fast
"""DGN-1984: the reset-pass lib says which runtimes the user onboarded.

Owner 2026-10-10 19:15: a user who did not onboard Codex never sees Codex,
the reset-pass view and the /usage block included. Offline: a temp HOME with
or without a fixture Codex auth.json, a temp conf, no network. Covers:
  - onboarded(): Claude always; Codex only with a signed-in Codex account;
  - list (inventory) reports `onboarded` per runtime;
  - the /usage summary block leaves Codex out when it is not onboarded, and
    keeps its line when it is;
  - passes stored for a runtime that is not onboarded stay in the store
    (hidden, never dropped).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'routines' / 'lib'
sys.path.insert(0, str(LIB))
import reset_passes as rp  # noqa: E402


class Onboarded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'home')
        os.makedirs(self.home)
        self.conf = os.path.join(self.tmp.name, 'agent.conf')
        Path(self.conf).write_text('PLAN=max\n')

    def env(self):
        e = {k: v for k, v in os.environ.items()
             if not k.startswith('RESET_PASSES_') and k not in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR')}
        e['HOME'] = self.home
        # hermetic: no live codex CLI unless a test installs a stub (DGN-1985)
        e['CODEX_BIN'] = os.path.join(self.tmp.name, 'no-codex')
        return e

    def stub_codex(self, out, rc=0):
        stub = os.path.join(self.tmp.name, 'codex-stub')
        Path(stub).write_text('#!/bin/sh\necho "%s"\nexit %d\n' % (out, rc))
        os.chmod(stub, 0o755)
        return stub

    def sign_in_codex(self):
        os.makedirs(os.path.join(self.home, '.codex'), exist_ok=True)
        Path(self.home, '.codex', 'auth.json').write_text(
            json.dumps({'tokens': {'account_id': 'acct-fixture-1984'}}))

    def cli(self, *args):
        r = subprocess.run([sys.executable, '-B', str(LIB / 'reset_passes.py'), *args],
                           text=True, capture_output=True, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_onboarded_flag(self):
        env = self.env()
        self.assertTrue(rp.onboarded('claude', env, self.home))
        self.assertFalse(rp.onboarded('codex', env, self.home))
        self.sign_in_codex()
        self.assertTrue(rp.onboarded('codex', env, self.home))

    def test_codex_login_owns_the_flag(self):
        # DGN-1985: the live `codex login status` answer wins over auth.json
        env = self.env()
        self.sign_in_codex()
        env['CODEX_BIN'] = self.stub_codex('Not logged in', 1)
        self.assertFalse(rp.onboarded('codex', env, self.home))
        os.remove(os.path.join(self.home, '.codex', 'auth.json'))
        env['CODEX_BIN'] = self.stub_codex('Logged in using ChatGPT')
        self.assertTrue(rp.onboarded('codex', env, self.home))
        # no CLI -> the auth.json account decides (fallback)
        env['CODEX_BIN'] = os.path.join(self.tmp.name, 'no-codex')
        self.assertFalse(rp.onboarded('codex', env, self.home))

    def test_list_reports_onboarded(self):
        inv = json.loads(self.cli('list', '--conf', self.conf))
        self.assertEqual((inv['claude']['onboarded'], inv['codex']['onboarded']), (True, False))
        self.sign_in_codex()
        inv = json.loads(self.cli('list', '--conf', self.conf))
        self.assertTrue(inv['codex']['onboarded'])

    def test_summary_hides_codex_until_onboarded(self):
        self.cli('add', '--conf', self.conf, '--runtime', 'codex',
                 '--expiry', '2099-10-23', '--kind', 'full')
        out = self.cli('summary', '--conf', self.conf, '--locale', 'en')
        self.assertIn('Claude: none', out)
        self.assertNotIn('Codex', out)
        # hidden, not dropped: the pass is still in the store
        inv = json.loads(self.cli('list', '--conf', self.conf))
        self.assertEqual([p['expiry'] for p in inv['codex']['passes']], ['2099-10-23'])
        self.sign_in_codex()                  # the 'local' passes fold into the account
        out = self.cli('summary', '--conf', self.conf, '--locale', 'en')
        self.assertIn('Codex: x1', out)


if __name__ == '__main__':
    unittest.main()
