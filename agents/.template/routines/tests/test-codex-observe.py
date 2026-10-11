#!/usr/bin/env python3
# test-lane: fast    # measured 0.5s (2026-09-15, DGN-1444 landing)
# test-host: macos sandbox-exec kernel boundary (seatbelt)
"""Codex observe acceptance, hook gating, and real seatbelt boundary tests."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'routines/dispatch-detached.sh'


class CodexObserve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='codex-observe-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.lab = Path(self.tmp.name).resolve()
        for name in ('repo', 'workspace', 'worktrees'):
            (self.lab / name).mkdir()
        (self.lab / 'task').write_text('Read VERSION.\n')
        (self.lab / 'repo/VERSION').write_text('probe-version\n')
        (self.lab / 'reject.py').write_text('raise SystemExit(2)\n')

    def run_shell(self, body):
        setup = f'''
DISPATCH_TEST=1 source {shlex.quote(str(SCRIPT))}
set +e
LAB={shlex.quote(str(self.lab))}
RUNTIME=codex
WORKSPACE="$LAB/workspace"
WORKTREE_BASE="$LAB/worktrees"
TASK_FILE="$LAB/task"
MODEL=fable
LABEL=observe-test
DISPLAY_NAME=observe-test
BOTTLENECK=accuracy
BOTTLENECK_WHY="given checks need exact pass fail verification"
REPO_PATH="$LAB/repo"
# DGN-1742: a --repo run must declare repo-contained or a local reason.
LOCAL_WHY="codex observe lane test fixture"
WT_PATH=""
SESSION_ID=00000000-0000-0000-0000-000000000001
SANDBOX_PROFILE="$LAB/observe.sb"
'''
        return subprocess.run(['bash', '-c', setup + body], text=True,
                              capture_output=True, timeout=20)

    def assert_ok(self, body):
        result = self.run_shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_repo_and_default_observe_accepted(self):
        self.assert_ok('validate_spawn_args && REPO_PATH="" && LOCAL_WHY="" && validate_spawn_args')

    def test_other_argument_refusals_remain(self):
        for change in ('WT_SLUG=write', 'WT_REPO_ARG="$LAB/repo"',
                       'WT_BASE_ARG=main', 'BOTTLENECK=""', 'MODEL=invalid',
                       'TIMEOUT_MIN=0', 'CODEX_UNGUARDED_WHY=short'):
            with self.subTest(change=change):
                result = self.run_shell(change + '\nvalidate_spawn_args')
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_hook_gate_required_only_for_write_lane(self):
        self.assert_ok('''
codex_quota_gate() { return 0; }
active_count_incr() { echo slot >> "$LAB/slots"; }
CODEX_HOOK_VERIFY_SCRIPT="$LAB/reject.py"
run_gates || exit 1
[[ $(cat "$LAB/slots") == slot ]] || exit 1
WT_SLUG=write
run_gates
rc=$?
[[ $rc == 2 && $(wc -l < "$LAB/slots" | tr -d ' ') == 1 ]]
''')

    def test_profile_runtime_scope_and_target_order(self):
        self.assert_ok('''
render_observe_profile "$SANDBOX_PROFILE" "$REPO_PATH" "$SESSION_ID" || exit 1
RUNTIME=claude-code
render_observe_profile "$LAB/claude.sb" "$REPO_PATH" "$SESSION_ID"
''')
        profile = (self.lab / 'observe.sb').read_text()
        self.assertIn(f'(subpath "{os.environ["HOME"]}/.codex")', profile)
        self.assertNotIn('/.codex', (self.lab / 'claude.sb').read_text())
        self.assertIn('(deny file-write*)', profile)
        self.assertIn(f'(deny file-read* (subpath "{os.environ["HOME"]}/.ssh")', profile)
        for target in ('repo', 'workspace', 'worktrees'):
            self.assertIn(f'(subpath "{self.lab / target}")', '\n'.join(profile.splitlines()[-3:]))

    def test_outer_wrapper_and_no_internal_nesting(self):
        self.assert_ok('''
render_observe_profile "$SANDBOX_PROFILE" "$REPO_PATH" "$SESSION_ID" || exit 1
select_codex_tool_policy || exit 1
[[ "${WRAP[*]}" == "sandbox-exec -f $SANDBOX_PROFILE" &&
   "${CODEX_ARGS[*]}" == "--sandbox danger-full-access --skip-git-repo-check" ]]
''')

    def test_missing_profile_or_sandbox_refuses(self):
        self.assertEqual(self.run_shell('select_codex_tool_policy').returncode, 1)
        self.assertEqual(self.run_shell('''
render_observe_profile "$SANDBOX_PROFILE" "$REPO_PATH" "$SESSION_ID"
PATH=/nonexistent
select_codex_tool_policy
''').returncode, 1)

    def test_worktree_requires_write_profile(self):
        result = self.run_shell('WT_PATH="$REPO_PATH"\nselect_codex_tool_policy')
        self.assertEqual(result.returncode, 1)
        self.assertIn('without a rendered write profile', result.stderr)

    def test_kernel_boundary_without_hook_gate(self):
        probe = subprocess.run(['sandbox-exec', '-p', '(version 1)(allow default)',
                                '/usr/bin/true'], capture_output=True, text=True)
        if probe.returncode:
            self.skipTest(f'seatbelt unavailable: rc={probe.returncode} {probe.stderr.strip()}')
        self.assert_ok('''
render_observe_profile "$SANDBOX_PROFILE" "$REPO_PATH" "$SESSION_ID" || exit 1
observe_profile_selfcheck "$SANDBOX_PROFILE" "$REPO_PATH" "$WORKSPACE" "$WORKTREE_BASE" || exit 1
select_codex_tool_policy || exit 1
"${WRAP[@]}" cat "$REPO_PATH/VERSION" || exit 1
for target in "$REPO_PATH" "$WORKSPACE" "$WORKTREE_BASE"; do
  result=$("${WRAP[@]}" touch "$target/probe" 2>&1)
  [[ $? != 0 && "$result" == *"Operation not permitted"* && ! -e "$target/probe" ]] || exit 1
done
result=$("${WRAP[@]}" ls "$HOME/.ssh" 2>&1)
[[ $? != 0 && "$result" == *"Operation not permitted"* ]]
''')


if __name__ == '__main__':
    unittest.main(verbosity=2)
