#!/usr/bin/env python3
# test-lane: fast
"""Offline access-contract tests. All execution is in disposable scratch copies.

Gate checkpoint replaces quota gates before slots or workers can be created.
No real worker, scheduler, notification, or live workspace is invoked.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = Path(os.environ.get('DISPATCH_WRAPPER_UNDER_TEST', ROOT / 'routines/dispatch-detached.sh'))

# DGN-1430 carryback guard: this suite's deferred-replay case exercises
# defer_or_block(), which DGN-1470 (runtime fallback) introduces. DGN-1470
# landed in canonical with DGN-1680, so the case runs; the presence-keyed
# skip stays only for wrappers under test (DISPATCH_WRAPPER_UNDER_TEST) that
# predate it.
HAS_DEFER_OR_BLOCK = 'defer_or_block()' in WRAPPER.read_text()


class AccessContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dgn1430-access-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'ws'
        (self.ws / 'routines').mkdir(parents=True)
        self.wrapper = self.ws / 'routines/dispatch-detached.sh'
        shutil.copy2(WRAPPER, self.wrapper)
        self.task = self.root / 'task.md'
        self.task.write_text('Modify the repository and commit the fix.\n')
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = {**os.environ, 'DISPATCH_TEST': '1',
                    'DOGANY_DISPATCH_RUNTIME': 'claude-code',
                    'WRAPPER': str(self.wrapper), 'TEST_ROOT': str(self.root)}
        self.base = ['--task', str(self.task), '--model', 'sonnet',
                     '--label', 'access-test', '--display-name', 'Access test',
                     '--bottleneck', 'accuracy',
                     '--bottleneck-why', 'Verify the declared filesystem scope.']

    def shell(self, code, args=()):
        return subprocess.run(['bash', '-c', 'source "$WRAPPER" "$@"\n' + code,
                               'test', *args], env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def launch(self, args=()):
        result = self.shell('''
print_calibration() { :; }
bottleneck_gate() { :; }
observe_sandbox_available() { echo SANDBOX_CHECK; }
write_sandbox_available() { echo WRITE_SANDBOX_CHECK; }
run_gates() { echo "GATE_CHECKPOINT:${ACCESS_MODE:-unknown}"; return 1; }
main
''', [*self.base, *args])
        # A refused launch mints no run state. DGN-1470/1592: the pre-spawn
        # runtime decision record under dispatch/decisions/ is the one
        # sanctioned survivor -- it lives outside every run dir by design.
        dispatch = self.ws / '.telegram_bot/dispatch'
        leftovers = sorted(p.name for p in dispatch.iterdir()
                           if p.name != 'decisions') if dispatch.exists() else []
        self.assertEqual(leftovers, [], result)
        return result

    def accepted(self, args, mode):
        r = self.launch(args)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('GATE_CHECKPOINT:' + mode, r.stdout)
        self.assertEqual('\nSANDBOX_CHECK' in '\n' + r.stdout, mode == 'read')
        # DGN-1504: a write declaration builds the kernel write boundary.
        self.assertEqual('WRITE_SANDBOX_CHECK' in r.stdout, mode == 'write')

    def refused(self, args):
        r = self.launch(args)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('CHECKPOINT', r.stdout)
        self.assertNotIn('SANDBOX_CHECK', r.stdout)
        return r

    def test_missing_declaration_refused(self):
        r = self.refused([])
        self.assertIn('--read-only', r.stderr)
        self.assertIn('--worktree <slug>', r.stderr)

    def test_repo_is_not_declaration(self):
        self.refused(['--repo', str(self.repo)])

    def test_write_needs_worktree(self):
        r = self.refused(['--write'])
        self.assertIn('--worktree <slug>', r.stderr)

    def test_write_repo_needs_worktree(self):
        self.refused(['--write', '--repo', str(self.repo)])

    def test_read(self):
        self.accepted(['--read-only'], 'read')

    def test_read_other_repo(self):
        self.accepted(['--read-only', '--repo', str(self.repo),
                       '--local-why', 'fixture repo has no cloud origin'], 'read')

    def test_read_text_is_not_classified_by_verbs(self):
        self.task.write_text('Review whether "modify" and "commit" are documented.\n')
        self.accepted(['--read-only'], 'read')

    def test_worktree_is_existing_write_declaration(self):
        self.accepted(['--worktree', 'fix'], 'write')

    def test_explicit_write(self):
        self.accepted(['--write', '--worktree', 'fix'], 'write')

    def test_other_repo_write(self):
        self.accepted(['--worktree', 'fix', '--worktree-repo', str(self.repo),
                       '--local-why', 'fixture repo has no cloud origin'], 'write')

    def test_dgn1742_repo_run_needs_contained_or_local_why(self):
        for flags in (['--read-only', '--repo', str(self.repo)],
                      ['--worktree', 'fix', '--worktree-repo', str(self.repo)]):
            with self.subTest(flags=flags):
                r = self.refused(flags)
                self.assertIn('--repo-contained', r.stderr)
                self.assertIn('--local-why', r.stderr)

    def test_conflicting_declarations_both_orders(self):
        for flags in (['--read-only', '--write'], ['--write', '--read-only'],
                      ['--read-only', '--worktree', 'fix'],
                      ['--worktree', 'fix', '--read-only']):
            with self.subTest(flags=flags):
                self.refused(flags)

    def test_empty_worktree_is_not_declaration(self):
        self.refused(['--worktree', ''])

    def test_existing_scope_errors_stay_refused(self):
        for flags in (['--read-only', '--worktree-repo', str(self.repo)],
                      ['--read-only', '--base', 'main'],
                      ['--worktree', 'fix', '--repo', str(self.repo)]):
            with self.subTest(flags=flags):
                self.refused(flags)

    def test_control_modes_need_no_declaration(self):
        for flags in (['--help'], ['--list']):
            with self.subTest(flags=flags):
                r = self.shell('main', flags)
                self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(HAS_DEFER_OR_BLOCK,
                         'defer_or_block() absent -- DGN-1470 not carried back yet')
    def test_deferred_read_replays_declaration(self):
        # A stub records argv only; it never schedules anything.
        scheduler = self.root / 'scheduler'
        scheduler.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$TEST_ROOT/replay"\n')
        scheduler.chmod(0o755)
        r = self.shell('''
validate_access_mode
USAGE_DEFER_RETRY="$TEST_ROOT/scheduler"
record_runtime_decision() { :; }
defer_or_block 2 unavailable
''', [*self.base, '--read-only', '--repo', str(self.repo)])
        self.assertEqual(r.returncode, 0, r.stderr)
        args = (self.root / 'replay').read_text().splitlines()
        self.assertIn('--read-only', args)
        self.assertIn('--repo', args)
        self.assertNotIn('--write', args)

    def test_run_env_records_access_before_worker_creation(self):
        for flags, expected in ((['--read-only'], 'read'),
                                (['--worktree', 'fix'], 'write')):
            with self.subTest(flags=flags):
                r = self.shell('''
print_calibration() { :; }
bottleneck_gate() { :; }
observe_sandbox_available() { :; }
run_gates() { mkdir -p "$DISPATCH_DIR"; }
provision_worktree() { WT_PATH="$TEST_ROOT/repo"; BRANCH=dsp/fix; }
render_observe_profile() { :; }
observe_profile_selfcheck() { :; }
write_sandbox_available() { :; }
render_write_profile() { :; }
write_profile_selfcheck() { :; }
ensure_ledger() { cp "$RUNDIR/run.env" "$TEST_ROOT/captured.env"; exit 88; }
main
''', [*self.base, *flags])
                self.assertEqual(r.returncode, 88, r.stderr)
                env_text = (self.root / 'captured.env').read_text()
                self.assertIn('ACCESS_MODE=' + expected + '\n', env_text)

    def test_status_records_declared_access_and_legacy_unknown(self):
        for flags, expected in ((['--read-only'], 'read'),
                                (['--worktree', 'fix'], 'write'), ([], '')):
            with self.subTest(flags=flags):
                r = self.shell('''
if [[ $# -gt 0 ]]; then validate_access_mode; fi
RUNID=test
RUNDIR="$TEST_ROOT"
BRANCH=""
RESULT=completed
EXIT_CODE=0
write_status_json completed 0 silent
''', flags)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads((self.root / 'status.json').read_text()).get('access_mode'), expected)


if __name__ == '__main__':
    unittest.main()
