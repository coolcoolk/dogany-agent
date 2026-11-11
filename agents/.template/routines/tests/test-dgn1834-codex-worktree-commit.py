#!/usr/bin/env python3
# test-lane: manual devkit-owned suite; run against extracted-devkit/framework
# test-covers: git-hooks/run-tests.sh agents/.template/git-hooks/run-tests.sh
"""Model-free Codex write policy checks plus a real Seatbelt commit test.

The kernel test runs the same WRAP selected for codex exec. It never calls
an LLM, uses only scratch repositories, and skips when nested Seatbelt is
unavailable. Model verdicts are not reported as kernel enforcement.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'routines/dispatch-detached.sh'
spec = importlib.util.spec_from_file_location(
    'seatbelt_model', Path(__file__).parent / 'fixtures/sandbox_exec_model.py')
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)


class CodexWorktreeCommit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dgn1834-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.lab = Path(self.tmp.name).resolve()
        self.repo = self.lab / 'repo with spaces'
        self.own = self.lab / 'worktrees/own'
        self.sibling = self.lab / 'sibling'
        self.run = self.lab / 'run'
        self.run.mkdir()
        self.profile = self.run / 'codex-write.sb'
        # Put the queue under an observe-denied parent: /tmp's general grant
        # must not hide a missing test-budget exception (DGN-2008).
        self.shared = self.own.parent / 'shared'
        clean_env = {k: v for k, v in os.environ.items()
                     if k == 'DOGANY_RESOURCE_SETTINGS_DIR' or (not k.startswith(('GIT_CONFIG_', 'DOGANY_', 'DGN_')))}
        self.env = dict(clean_env, DOGANY_SHARED_HOME=str(self.shared),
                        DGN_TEST_CPU_LOG='0', DGN_WORKER_TEST_SLOTS='1',
                        GIT_CONFIG_GLOBAL='/dev/null',
                        GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Test',
                        GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test',
                        GIT_COMMITTER_EMAIL='test@example.invalid')
        self.git(self.lab, 'init', '-q', '-b', 'main', str(self.repo))
        (self.repo / 'README').write_text('base\n')
        self.git(self.repo, 'add', 'README')
        self.git(self.repo, 'commit', '-qm', 'base')
        self.git(self.repo, 'branch', 'dev')
        self.git(self.repo, 'worktree', 'add', '-qb', 'dsp/test', str(self.own))
        self.git(self.repo, 'worktree', 'add', '-qb', 'dsp/sibling', str(self.sibling))
        self.admin = Path(self.git(self.own, 'rev-parse', '--absolute-git-dir').stdout.strip())
        self.common = self.repo / '.git'
        self.shell('render_codex_write_profile "$CODEX_WRITE_PROFILE"')
        self.rules = model.load(self.profile)

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], env=self.env,
                              text=True, capture_output=True, check=True)

    def shell(self, body, check=True):
        setup = f'''
DISPATCH_TEST=1 source {shlex.quote(str(SCRIPT))}
RUNTIME=codex
WORKSPACE={shlex.quote(str(self.repo))}
WORKTREE_BASE={shlex.quote(str(self.own.parent))}
WT_PATH={shlex.quote(str(self.own))}
WT_BASE=main
BRANCH=dsp/test
RUNDIR={shlex.quote(str(self.run))}
SESSION_ID=00000000-0000-0000-0000-000000000001
CODEX_WRITE_PROFILE={shlex.quote(str(self.profile))}
'''
        result = subprocess.run(['bash', '-c', setup + body], env=self.env,
                                text=True, capture_output=True, timeout=30)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_commit_paths_only(self):
        allowed = [self.run / 'tmp/probe', self.own / 'change', self.admin / 'index.lock',
                   self.admin / 'HEAD', self.admin / 'logs/HEAD',
                   self.common / 'objects/ab/new-object']
        for prefix in ('refs/heads', 'logs/refs/heads'):
            allowed += [self.common / prefix / ('dsp/test' + suffix)
                        for suffix in ('', '.lock')]
        for path in allowed:
            with self.subTest(path=path):
                self.assertEqual(model.verdict(self.rules, 'file-write*', str(path)), 'allow')

    def test_shared_metadata_and_checkouts_denied(self):
        denied = [self.repo / 'README', self.sibling / 'README', self.profile,
                  self.run / 'status.json', self.own / '.git', self.own.parent / 'other/change',
                  self.common / 'worktrees/sibling/index.lock']
        denied += [self.common / (name + suffix)
                   for name in ('HEAD', 'index', 'config', 'packed-refs')
                   for suffix in ('', '.lock')]
        denied += [self.common / prefix / (branch + suffix)
                   for prefix in ('refs/heads', 'logs/refs/heads')
                   for branch in ('main', 'dev', 'master', 'dsp/sibling', 'dsp/test-other')
                   for suffix in ('', '.lock')]
        denied += [self.common / 'hooks/pre-commit', self.common / 'refs/tags/new']
        for path in denied:
            with self.subTest(path=path):
                self.assertEqual(model.verdict(self.rules, 'file-write*', str(path)), 'deny')

    def test_only_test_budget_classes_reopened(self):
        for kind in ('worker-test', 'heavy-test'):
            path = self.shared / 'cron-queue' / kind / 'slot-1/pid'
            self.assertEqual(model.verdict(self.rules, 'file-write*', str(path)), 'allow')
        for suffix in ('cron-queue/other/slot-1/pid', 'config', 'cron-queue/new'):
            self.assertEqual(model.verdict(self.rules, 'file-write*',
                                           str(self.shared / suffix)), 'deny')

    def test_extra_write_grants_are_budget_scratch_and_commit_paths(self):
        # DGN-2059: compare the generated configuration, not source strings.
        # Beyond the observe base, only the two budget classes and the
        # owned scratch and existing worktree/commit paths may be writable.
        observe = self.run / 'observe.sb'
        self.shell(f'render_observe_profile {shlex.quote(str(observe))} '
                   '"$WT_PATH" "$SESSION_ID"')
        base = model.load(observe)
        self.assertEqual(self.rules[:len(base)], base)
        grants = [rule for rule in self.rules[len(base):]
                  if rule[:2] == ('allow', 'file-write*')]
        refs = [str(self.common / prefix / ('dsp/test' + suffix))
                for prefix in ('refs/heads', 'logs/refs/heads')
                for suffix in ('', '.lock')]
        self.assertEqual(grants, [
            ('allow', 'file-write*', [
                ('subpath', str(self.shared / 'cron-queue' / kind))
                for kind in ('worker-test', 'heavy-test')]),
            ('allow', 'file-write*', [('subpath', str(self.own))]),
            ('allow', 'file-write*', [('subpath', str(self.run / 'tmp'))]),
            ('allow', 'file-write*', [('subpath', str(self.admin)),
                                      ('subpath', str(self.common / 'objects'))]),
            ('allow', 'file-write*', [('literal', ref) for ref in refs]),
        ])

    def test_default_home_test_budget_paths(self):
        # Production normally leaves DOGANY_SHARED_HOME unset; an empty
        # override must resolve identically to run-tests.sh as well.
        home = self.own.parent / 'home with spaces'
        home.mkdir()
        self.env['HOME'] = str(home)
        for override in (None, ''):
            with self.subTest(shared_home=override):
                if override is None:
                    self.env.pop('DOGANY_SHARED_HOME', None)
                else:
                    self.env['DOGANY_SHARED_HOME'] = override
                self.shell('render_codex_write_profile "$CODEX_WRITE_PROFILE"')
                rules = model.load(self.profile)
                queue = home / '.dogany/cron-queue'
                for kind in ('worker-test', 'heavy-test'):
                    directory = queue / kind
                    self.assertTrue(directory.is_dir())
                    for suffix in ('', 'slot-1', 'slot-1/pid', 'slot-1/who'):
                        self.assertEqual(model.verdict(
                            rules, 'file-write*', str(directory / suffix)), 'allow')
                for path in (home / '.dogany', queue, queue / 'other/slot-1',
                             home / '.dogany/config', self.common / 'refs/heads/dev'):
                    self.assertEqual(model.verdict(rules, 'file-write*', str(path)),
                                     'deny')

    def require_kernel(self):
        sandbox = shutil.which('sandbox-exec')
        if not sandbox:
            self.skipTest('macOS Seatbelt unavailable')
        probe = subprocess.run([sandbox, '-p', '(version 1)(allow default)',
                                '/usr/bin/true'], text=True, capture_output=True)
        if probe.returncode:
            self.skipTest('Seatbelt cannot be applied: ' + probe.stderr.strip())
        return sandbox

    def kernel_gate(self, profile):
        sandbox = self.require_kernel()
        # DGN-2270 assigns worker scratch before applying the profile. The
        # harness's inherited TMPDIR may be outside this worker's grants.
        self.shell('worker_scratch_prepare')
        env = dict(self.env, DGN_WORKER_TEST_TIMEOUT='30',
                   TMPDIR=str(self.run / 'tmp'))
        for key in ('DGN_WORKER_TEST_HELD', 'DGN_HEAVY_LOCK_HELD'):
            env.pop(key, None)
        runner = ROOT / 'git-hooks/run-tests.sh'
        command = ['bash', '-c', '''
[ -n "$DGN_WORKER_TEST_HELD" ] || exit 9
[ "$(cat "$DOGANY_SHARED_HOME/cron-queue/worker-test/slot-1/pid")" = "$DGN_WORKER_TEST_HELD" ] || exit 10
[ -f "$TMPDIR/.dogany-test-scratch" ] || exit 11
[ "$(cat "$TMPDIR/owner.pid")" = "$DGN_WORKER_TEST_HELD" ] || exit 12
touch "$1"
''', '_', str(self.own / 'suite-ran')]
        return subprocess.run([sandbox, '-f', str(profile), 'bash', str(runner),
                               '--gate'] + command, env=env, cwd=self.own,
                              text=True, capture_output=True, timeout=10)

    def test_kernel_profile_takes_and_releases_test_slot(self):
        result = self.kernel_gate(self.profile)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('gate=held rc=0', result.stderr)
        self.assertTrue((self.own / 'suite-ran').exists())
        self.assertFalse((self.shared / 'cron-queue/worker-test/slot-1').exists())
        self.assertEqual(list((self.run / 'tmp').glob('run-tests-disc.gate.*')), [])

    def test_kernel_slot_denial_refuses_without_queueing(self):
        self.require_kernel()
        denied = self.run / 'denied.sb'
        queue = self.shared / 'cron-queue/worker-test'
        # Existing parent reproduces the original mkdir -p success + slot
        # mkdir EPERM; denying pid alone also checks failed ownership writes.
        for path in (queue, queue / 'slot-1/pid'):
            with self.subTest(path=path):
                denied.write_text(self.profile.read_text() +
                                  f'\n(deny file-write* (subpath "{path}"))\n')
                result = self.kernel_gate(denied)
                self.assertEqual(result.returncode, 77, result.stderr)
                self.assertIn('sandbox cannot take a test slot', result.stderr)
                self.assertIn('Operation not permitted', result.stderr)
                self.assertNotIn('QUEUED', result.stderr)
                self.assertFalse((self.own / 'suite-ran').exists())
                self.assertFalse((queue / 'slot-1').exists())

    def test_dispatch_return_flags_budget_refusal_from_command_output(self):
        warning = 'tests not run by worker'
        diagnostic = 'test budget: REFUSED -- sandbox cannot take a test slot'
        for output, command, expected in ((diagnostic, 'bash run-tests.sh', True),
                                          ('gate=held rc=0', diagnostic, False)):
            with self.subTest(output=output):
                (self.run / 'stream.jsonl').write_text(json.dumps({
                    'type': 'item.completed', 'item': {
                        'type': 'command_execution', 'aggregated_output': output,
                        'command': command}}) + '\n')
                (self.run / 'out.log').write_text('Committed the fix.\n')
                self.shell('''
RUNID=dgn2008
LABEL=fixture
TICKET=DGN-2008
MODEL=opus
COMMIT_COUNT=1
COMMIT_HASH=fixture
SESSION_INBOX_DIR="$RUNDIR/inbox"
write_session_inbox completed 0
''')
                report = (self.run / 'inbox/dispatch-dgn2008.md').read_text()
                self.assertEqual(warning in report, expected, report)

    def test_worker_requires_outer_profile_and_avoids_nested_sandbox(self):
        self.shell('''
# Availability alone, no actual kernel invocation in this policy unit test.
observe_sandbox_available() { return 0; }
select_codex_tool_policy
[[ "${WRAP[*]}" == "sandbox-exec -f $CODEX_WRITE_PROFILE" ]]
[[ "${CODEX_ARGS[*]}" == "--sandbox danger-full-access --skip-git-repo-check" ]]
''')
        for body in ('CODEX_WRITE_PROFILE=""', 'CODEX_WRITE_PROFILE=/missing',
                     'PATH=/nonexistent'):
            result = self.shell(body + '\nselect_codex_tool_policy', check=False)
            self.assertNotEqual(result.returncode, 0)

    def test_invalid_metadata_refuses(self):
        for setup in ('WT_PATH="$WORKSPACE"', 'BRANCH=dsp/other',
                      'WT_BASE=dsp/test', 'WT_PATH=/missing', 'RUNDIR=relative'):
            result = self.shell(setup + '\nrender_codex_write_profile "$CODEX_WRITE_PROFILE"',
                                check=False)
            self.assertNotEqual(result.returncode, 0, setup)
        self.git(self.own, 'checkout', '--detach', '-q')
        result = self.shell('render_codex_write_profile "$CODEX_WRITE_PROFILE"', check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_kernel_commit_and_protected_ref_denial(self):
        self.require_kernel()
        before = {ref: self.git(self.repo, 'rev-parse', ref).stdout
                  for ref in ('main', 'dev', 'dsp/test', 'dsp/sibling')}
        self.shell('''
select_codex_tool_policy
"${WRAP[@]}" /bin/bash -c '
  set -e
  cd "$1"
  echo change > change
  git add change
  git commit -qm "sandbox commit"
' _ "$WT_PATH"
''')
        after = self.git(self.own, 'rev-parse', 'HEAD').stdout
        self.assertNotEqual(after, before['dsp/test'])
        self.assertEqual(self.git(self.own, 'show', 'HEAD:change').stdout, 'change\n')
        for ref in ('main', 'dev', 'dsp/sibling'):
            result = self.shell(f'''
select_codex_tool_policy
"${{WRAP[@]}}" git -C "$WT_PATH" update-ref refs/heads/{ref} HEAD
''', check=False)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Operation not permitted', result.stderr)
            self.assertEqual(self.git(self.repo, 'rev-parse', ref).stdout, before[ref])
        # Direct dev-ref writes must also fail, independently of Git.
        dev = self.common / 'refs/heads/dev'
        result = self.shell(f'''
select_codex_tool_policy
"${{WRAP[@]}}" /bin/sh -c 'echo forbidden >> "$1"' _ {shlex.quote(str(dev))}
''', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Operation not permitted', result.stderr)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'dev').stdout, before['dev'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
