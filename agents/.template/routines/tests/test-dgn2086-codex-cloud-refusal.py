#!/usr/bin/env python3
# test-lane: fast
# test-component: codex-cloud
# test-tier: 0
# test-prereq: fixture-repo, tmp-files, subprocess
"""DGN-2086: the Codex Cloud read-only check looks at the user brief only.

The dispatcher preamble says "Do not edit <persona files> ..." and matched the
brief-forbids-changes regex, so every codex-cloud run was refused. Offline:
real codex_cloud_preamble builds prompt.md, real codex_cloud.py run checks it,
a stub `codex` on PATH records whether a submission was attempted.
"""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT.parents[1] / 'extracted-devkit' / 'payload' / 'routines' / 'bundle'
SCRIPT = BUNDLE / 'dispatch-detached.sh'
CLI = BUNDLE / 'lib' / 'codex_cloud.py'


def fixture_env(**values):
    env = {k: v for k, v in os.environ.items()
           if k == 'DOGANY_RESOURCE_SETTINGS_DIR' or (not k.startswith(('GIT_CONFIG_', 'DOGANY_', 'DGN_')))}
    env.update(values)
    return env


def sh(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env or fixture_env(), check=True, capture_output=True, text=True).stdout


class CodexCloudRefusal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        t = self.tmp
        sh('git', 'init', '-q', '-b', 'main', f'{t}/seed')
        sh('git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty',
           '-m', 'base', cwd=f'{t}/seed')
        sh('git', 'clone', '-q', '--bare', f'{t}/seed', f'{t}/origin.git')
        sh('git', 'clone', '-q', f'{t}/origin.git', f'{t}/repo')
        self.sha = sh('git', 'rev-parse', 'HEAD', cwd=f'{t}/repo').strip()
        os.makedirs(f'{t}/bin')
        stub = Path(t, 'bin', 'codex')
        stub.write_text('#!/bin/sh\n[ "$2" = exec ] && { echo submitted >> "$STUB_LOG"; exit 1; }\nexit 0\n')
        stub.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def preamble(self):
        body = ('set --; source "$TEST_SCRIPT"\n'
                f'REPO_PATH={self.tmp}/repo; WT_BASE=main; CODEX_CLOUD_BASE_SHA={self.sha}\n'
                'cloud_github_url() { echo "$1"; }\ncodex_cloud_preamble\n')
        env = fixture_env(DISPATCH_TEST='1', TEST_SCRIPT=str(SCRIPT))
        return sh('bash', '-c', body, env=env, cwd=self.tmp)

    def dispatch(self, brief, with_brief_file=True):
        rd = Path(self.tmp, 'run')
        rd.mkdir(exist_ok=True)
        # Same assembly as dispatch-detached.sh: preamble + brief, raw brief beside it.
        (rd / 'prompt.md').write_text(self.preamble() + brief)
        if with_brief_file:
            (rd / 'brief.md').write_text(brief)
        log = Path(self.tmp, 'stub.log')
        log.write_text('')
        env = fixture_env(PATH=f'{self.tmp}/bin:' + os.environ['PATH'], STUB_LOG=str(log))
        p = subprocess.run([sys.executable, '-B', str(CLI), 'run', '--repo', f'{self.tmp}/repo',
                            '--env', 'e', '--base', 'main', '--sha', self.sha, '--branch', 'dsp/x',
                            '--worktree', f'{self.tmp}/wt', '--rundir', str(rd),
                            '--model-id', 'gpt-6.1-sol'],
                           env=env, capture_output=True, text=True, cwd=self.tmp)
        return p.stdout, log.read_text()

    def test_scoped_preamble_is_not_read_only(self):
        sys.path.insert(0, str(CLI.parent))
        import codex_cloud
        self.assertEqual(codex_cloud.read_only_reason(self.preamble()), '')

    def test_write_brief_is_submitted(self):
        out, log = self.dispatch('Fix the typo in README.md.\n')
        self.assertNotIn('forbids repository changes', out)
        self.assertIn('submitted', log)

    def test_read_only_brief_refused(self):
        out, log = self.dispatch('Read-only: report how dispatch works.\n')
        self.assertIn('read-only', out)
        self.assertEqual(log, '')

    def test_scoped_do_not_edit_brief_submitted(self):
        out, log = self.dispatch('Fix README but do not edit src/app.py.\n')
        self.assertNotIn('forbids repository changes', out)
        self.assertIn('submitted', log)

    def test_dispatcher_writes_brief_file(self):
        src = SCRIPT.read_text()
        self.assertRegex(src, re.compile(r'render_dispatch_prompt\(\) \{\n\s*if \[\[ "\$RUNTIME" == codex-cloud \]\]; then codex_cloud_preamble\n.*\n\s*cat "\$TASK_FILE"'))
        self.assertRegex(src, re.compile(r'if \[\[ "\$RUNTIME" == codex-cloud \]\]; then\n\s*cp "\$TASK_FILE" "\$RUNDIR/brief.md"'))


if __name__ == '__main__':
    unittest.main()
