#!/usr/bin/env python3
# test-lane: fast
"""DGN-1983: reset passes are one store per account per host.

Owner 2026-10-10 19:09: a second agent on the same Mac said no pass was
registered -- the passes lived in one instance's agent.conf. Offline (temp
HOME with fixture account files, two fixture instance confs, no network).
Covers:
  - two instances on one account see the same passes (list / specs / the
    /usage block), and an add on one shows on the other;
  - the one-time migration merges each instance's old conf line (the same
    pass seen twice counts once), leaves a pointer comment, is idempotent;
  - add / remove round trip through the CLI; another account has its own
    store; only the account fingerprint is written, never the id;
  - passes kept while no account was readable fold into the account store;
  - concurrent adds are all kept (lock);
  - reads never invent a pass (no user action -> nothing stored);
  - the dispatcher reads the store (reset_pass_spec), env still wins.
"""
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'routines' / 'lib'
sys.path.insert(0, str(LIB))
import reset_passes as rp  # noqa: E402

CLAUDE_ID = 'c0ffee00-1111-2222-3333-444455556666'
CODEX_ID = 'acct-codex-fixture-77'


def _add_one(args):
    home, conf, expiry = args
    return rp.add(conf, 'codex', expiry, 'full', home=home)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, 'home')
        os.makedirs(os.path.join(self.home, '.codex'))
        Path(self.home, '.claude.json').write_text(json.dumps(
            {'oauthAccount': {'accountUuid': CLAUDE_ID, 'emailAddress': 'x@example.com'}}))
        Path(self.home, '.codex', 'auth.json').write_text(json.dumps(
            {'tokens': {'account_id': CODEX_ID}}))
        self.alpha = self.instance('alpha', "PLAN=max\nRESET_PASSES_CLAUDE='1|2099-10-23:full'\n"
                                   "RESET_PASSES_CODEX='3|2099-10-23T05:50:full,"
                                   "2099-10-30T04:18:full,2099-11-07T08:43:full'\nX=1\n")
        self.beta = self.instance('beta', "PLAN=max\nAGENT_LNG=ko\n")

    def instance(self, name, conf_text):
        d = Path(self.tmp.name, name, 'config')
        d.mkdir(parents=True)
        conf = d / 'agent.conf'
        conf.write_text(conf_text)
        return conf

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('RESET_PASSES_') and k not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
        env.update(HOME=self.home, **extra)
        return env

    def cli(self, conf, *args, **env):
        return subprocess.run([sys.executable, str(LIB / 'reset_passes.py'), *args,
                               '--conf', str(conf)], env=self.env(**env),
                              text=True, capture_output=True)

    def specs(self, conf):
        r = self.cli(conf, 'specs')
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.rstrip('\n').split('\t')

    def listing(self, conf):
        r = self.cli(conf, 'list')
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = json.loads(r.stdout)
        return {rt: [(p['expiry'], p['kind']) for p in inv[rt]['passes']] for rt in rp.RUNTIMES}

    def store(self, rt):
        path, _ = rp.resolve_store(rt, env={}, home=self.home)
        return rp._read_json(path) or {}


class SharedStore(Base):
    def test_two_instances_see_the_same_passes(self):
        want = ['1|2099-10-23:full',
                '3|2099-10-23T05:50:full,2099-10-30T04:18:full,2099-11-07T08:43:full']
        self.assertEqual(self.specs(self.alpha), want)
        self.assertEqual(self.specs(self.beta), want)       # the bug: Beta saw none
        self.assertEqual(self.listing(self.alpha), self.listing(self.beta))
        r = self.cli(self.beta, 'add', '--runtime', 'claude', '--expiry', '2099-12-01',
                     '--kind', 'weekly')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.listing(self.alpha)['claude'],
                         [('2099-10-23', 'full'), ('2099-12-01', 'weekly')])
        a = self.cli(self.alpha, 'summary', '--locale', 'en').stdout
        b = self.cli(self.beta, 'summary', '--locale', 'en').stdout
        self.assertEqual(a, b)
        self.assertIn('Claude: x2 (next 10-23 full)', a)
        self.assertIn('Codex: x3 (next 10-23 05:50 full)', a)
        self.assertIn('shared by all agents on this Mac', a)

    def test_add_remove_round_trip(self):
        r = self.cli(self.beta, 'add', '--runtime', 'codex', '--expiry', 'unknown', '--kind', '5h')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.store('codex')['spec'], '1|unknown:5h')
        r = self.cli(self.beta, 'remove', '--runtime', 'codex', '--expiry', 'unknown',
                     '--kind', '5h')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.store('codex')['spec'], '')
        self.assertEqual(self.cli(self.beta, 'remove', '--runtime', 'codex',
                                  '--expiry', 'unknown').returncode, 2)
        self.assertEqual(self.specs(self.beta), ['-', '-'])

    def test_store_is_per_account_and_keeps_only_the_fingerprint(self):
        self.cli(self.beta, 'add', '--runtime', 'codex', '--expiry', '2099-01-05')
        path, fp = rp.resolve_store('codex', env={}, home=self.home)
        self.assertEqual(fp, rp._ap().key_fingerprint(CODEX_ID))
        self.assertEqual(Path(path).parent.parent.name, 'codex')
        for f in Path(self.home, '.dogany').rglob('*'):
            if f.is_file():
                self.assertNotIn(CODEX_ID, f.read_text())
                self.assertNotIn(CLAUDE_ID, f.read_text())
        # Another Codex account on the same host has its own (empty) store.
        Path(self.home, '.codex', 'auth.json').write_text(json.dumps(
            {'tokens': {'account_id': 'acct-other'}}))
        self.assertEqual(self.listing(self.beta)['codex'], [])
        r = self.cli(self.beta, 'specs', CODEX_HOME=os.path.join(self.tmp.name, 'none'))
        self.assertEqual(r.stdout.split('\t')[1].strip(), '-')   # 'local' store: empty

    def test_reads_never_invent_a_pass(self):
        for verb in ('specs', 'list', 'summary'):
            self.assertEqual(self.cli(self.beta, verb).returncode, 0)
        for rt in rp.RUNTIMES:
            self.assertEqual(self.store(rt).get('spec', ''), '')
        self.assertEqual(self.beta.read_text(), "PLAN=max\nAGENT_LNG=ko\n")

    def test_concurrent_adds_are_all_kept(self):
        days = [f'2099-02-{d:02d}' for d in range(1, 13)]
        with multiprocessing.Pool(6) as pool:
            pool.map(_add_one, [(self.home, str(self.beta), d) for d in days])
        passes = rp.parse_passes(self.store('codex')['spec'])
        self.assertEqual(sorted(e for e, _ in passes), days)


class Migration(Base):
    def test_merge_dedupes_and_leaves_a_pointer(self):
        # Beta held the same Claude pass without its kind, and one of the
        # three Codex passes -- the same passes, not new ones.
        self.beta.write_text("PLAN=max\nRESET_PASSES_CLAUDE='1|2099-10-23'\n"
                              "RESET_PASSES_CODEX='1|2099-10-23T05:50:full'\nAGENT_LNG=ko\n")
        self.specs(self.beta)
        self.specs(self.alpha)
        self.assertEqual(self.store('claude')['spec'], '1|2099-10-23:full')
        self.assertEqual(self.store('codex')['spec'],
                         '3|2099-10-23T05:50:full,2099-10-30T04:18:full,2099-11-07T08:43:full')
        for conf in (self.alpha, self.beta):
            text = conf.read_text()
            self.assertNotRegex(text, r'(?m)^RESET_PASSES_')
            self.assertRegex(text, r"(?m)^# RESET_PASSES_CLAUDE moved to the shared per-account "
                                   r"store \S+/reset-passes/claude/[0-9a-f]{12}/passes\.json "
                                   r"\(DGN-1983, \d{4}-\d\d-\d\d\); edit with /passes\. Was: '1\|")
        self.assertIn('\nX=1\n', self.alpha.read_text())              # rest kept
        self.assertTrue(self.beta.read_text().endswith('AGENT_LNG=ko\n'))
        self.assertEqual(len(self.store('claude')['migrated']), 2)
        # Idempotent: rereads change nothing.
        before = {rt: self.store(rt) for rt in rp.RUNTIMES}
        texts = (self.alpha.read_text(), self.beta.read_text())
        self.specs(self.alpha)
        self.listing(self.beta)
        self.assertEqual({rt: self.store(rt) for rt in rp.RUNTIMES}, before)
        self.assertEqual((self.alpha.read_text(), self.beta.read_text()), texts)

    def test_merge_rule(self):
        m = rp.merge_passes
        a = [('2099-01-01', 'full')]
        self.assertEqual(m(a, a), a)                                       # same pass once
        self.assertEqual(m(a, [('2099-01-01', 'unknown')]), a)             # absorbed by kind
        self.assertEqual(m(a, [('2099-01-01', 'unknown')] * 2),
                         a + [('2099-01-01', 'unknown')])                  # one more held
        self.assertEqual(m(a, [('2099-01-01', '5h')]),
                         [('2099-01-01', 'full'), ('2099-01-01', '5h')])   # different kinds
        self.assertEqual(m([('unknown', 'unknown')] * 3, [('unknown', 'unknown')]),
                         [('unknown', 'unknown')] * 3)

    def test_malformed_line_is_not_guessed(self):
        self.beta.write_text("RESET_PASSES_CODEX='lots'\n")
        inv = json.loads(self.cli(self.beta, 'list').stdout)
        self.assertFalse(inv['codex']['valid'])
        self.assertIn('NOT merged', self.beta.read_text())
        self.assertEqual(self.store('codex').get('spec', ''), '')

    def test_local_store_folds_into_the_account(self):
        nohome = os.path.join(self.tmp.name, 'nocodex')
        r = self.cli(self.beta, 'add', '--runtime', 'codex', '--expiry', '2099-04-01',
                     '--kind', 'full', CODEX_HOME=nohome)
        self.assertEqual(r.returncode, 0, r.stderr)
        local = rp._read_json(rp.store_path('codex', rp.LOCAL_ACCOUNT, self.home))
        self.assertEqual(local['spec'], '1|2099-04-01:full')
        # The account becomes readable: the next read folds 'local' in.
        self.assertEqual(self.specs(self.beta)[1], '1|2099-04-01:full')
        local = rp._read_json(rp.store_path('codex', rp.LOCAL_ACCOUNT, self.home))
        self.assertTrue(local['moved_to'].endswith('passes.json'))
        self.assertEqual(rp.store_spec(rp.store_path('codex', rp.LOCAL_ACCOUNT, self.home)), '')


class Dispatcher(Base):
    def sh(self, body, **env):
        script = f'''source "{ROOT}/routines/dispatch-detached.sh"
WORKSPACE="{self.beta.parent.parent}"
{body}'''
        r = subprocess.run(['bash', '-c', script], text=True, capture_output=True,
                           env=self.env(DISPATCH_TEST='1', **env))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_dispatch_reads_the_shared_store(self):
        self.specs(self.alpha)                          # Alpha's old line migrates
        out = self.sh('reset_pass_spec codex; echo; reset_pass_spec claude; echo\n'
                      'reset_pass_mode codex 40 "" 7d', DISPATCH_TEST_PASS_STORE='1')
        lines = out.splitlines()
        self.assertEqual(lines[0], '3|2099-10-23T05:50:full,2099-10-30T04:18:full,'
                                   '2099-11-07T08:43:full')
        self.assertEqual(lines[1], '1|2099-10-23:full')
        self.assertTrue(lines[2].startswith('held\t'), lines[2])
        # env still wins; a hermetic test (no opt-in) never reads the store.
        out = self.sh('reset_pass_spec claude', DISPATCH_TEST_PASS_STORE='1',
                      RESET_PASSES_CLAUDE='1|2099-01-09:5h')
        self.assertEqual(out, '1|2099-01-09:5h')
        self.assertEqual(self.sh('reset_pass_spec codex'), '')


if __name__ == '__main__':
    unittest.main()
