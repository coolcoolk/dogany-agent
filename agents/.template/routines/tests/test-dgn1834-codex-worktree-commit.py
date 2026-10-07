#!/usr/bin/env python3
# test-lane: fast
"""Model-free Codex write policy checks plus a real Seatbelt commit test.

The kernel test runs the same WRAP selected for codex exec. It never calls
an LLM, uses only scratch repositories, and skips when nested Seatbelt is
unavailable. Model verdicts are not reported as kernel enforcement.
"""
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

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
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null',
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
        allowed = [self.own / 'change', self.admin / 'index.lock',
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
                  self.own / '.git', self.own.parent / 'other/change',
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
        sandbox = shutil.which('sandbox-exec')
        if not sandbox:
            self.skipTest('macOS Seatbelt unavailable')
        probe = subprocess.run([sandbox, '-p', '(version 1)(allow default)',
                                '/usr/bin/true'], text=True, capture_output=True)
        if probe.returncode:
            self.skipTest('Seatbelt cannot be applied: ' + probe.stderr.strip())
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
