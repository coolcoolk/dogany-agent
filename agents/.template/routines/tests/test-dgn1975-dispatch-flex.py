#!/usr/bin/env python3
# test-lane: manual devkit-owned suite; run against extracted-devkit/framework
"""DGN-1975: dispatch runtime assignment with the machine's resource flexibility.

Owner 2026-10-10 12:34 (paraphrased): fix the work-machine assignment so it
takes this kind of resource flexibility into account. Observed the same
morning: a claude-api run was refused at "cap 0/0" (machine load5=19) while
cloud had room; a --local-why that only cited reading a doc outside the repo
pinned a run local; 20/20 runs went local and queued on the shared test slot.

Locks:
  cap-full local -> the walk takes cloud (cloud-eligible run), selection line
      names the fallback and the numbers, decision recorded slot_cap_fallback;
  all runtimes full -> host queue (DGN-2011), including legacy callers;
  machine load term flips the loss pick from a local pool to cloud, and held
      test slots count as pressure; the order field lists the walk order;
  --attach inlines a file (basename only) and refuses past the cloud prompt
      cap and binary files;
  --local-why citing only outside-repo reads is refused with the --attach
      hint; a reason naming a machine need is accepted.

Offline: fixture usage/quota/machine reads, a bare local origin behind a
github.com insteadOf rewrite, a stub re-evaluation scheduler. No workers,
no network, no real cloud call.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / 'routines'
sys.path.insert(0, str(ROUTINES / 'lib'))
import runtime_loss as rl  # noqa: E402

H = 3600
D = 86400
GH_URL = 'https://github.com/acme/widgets.git'


def git(*args, cwd=None):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(tmp):
    """A clone whose origin reads as GitHub but resolves to a local bare repo."""
    origin = os.path.join(tmp, 'origin.git')
    seed = os.path.join(tmp, 'seed')
    repo = os.path.join(tmp, 'repo')
    git('init', '-q', '-b', 'main', seed)
    git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'base',
        cwd=seed)
    git('clone', '-q', '--bare', seed, origin)
    git('clone', '-q', origin, repo)
    git('remote', 'set-url', 'origin', GH_URL, cwd=repo)
    git('config', f'url.{origin}.insteadOf', GH_URL, cwd=repo)
    return repo


PRELUDE = r'''source "$TEST_ROOT/routines/dispatch-detached.sh"
WORKSPACE="$TEST_TMP"
SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"
DISPATCH_DIR="$TEST_TMP/dispatch"
DECISION_DIR="$TEST_TMP/decisions"
ACTIVE_COUNT_FILE="$DISPATCH_DIR/active-count"
ACTIVE_COUNT_FILE_CODEX="$DISPATCH_DIR/active-count.codex"
ACTIVE_COUNT_FILE_CLOUD="$DISPATCH_DIR/active-count.cloud"
ACTIVE_COUNT_FILE_API="$DISPATCH_DIR/active-count.claude-api"
COUNT_LOCK_DIR="$DISPATCH_DIR/.count.lock"
USAGE_GATE=/usr/bin/true
USAGE_DEFER_RETRY="$TEST_TMP/defer-stub.sh"
PUSH_SH=/usr/bin/true
claude_quota_remaining() { echo '80 70'; }
claude_hours_left() { return 1; }
reset_pass_mode() { printf 'none\t-\t-\tfixture\n'; }
api_pool_gate() { API_POOL_NOTE='pool fixture ok'; return 0; }
runtime_account_pace() { echo "${STUB_PACE:-0}"; }   # DGN-2180: main account under pace
runtime_account_elapsed() { echo "${STUB_ELAPSED:-50}"; }
machine_snapshot() { echo "${STUB_MACHINE:-0.5 8 16000}"; }
cloud_account_check() { CLOUD_ACCOUNT_NOTE='fixture: owning login'; return 0; }   # DGN-2104
mkdir -p "$DISPATCH_DIR"
'''


def run_bash(body, env_extra=None, tmp=None, args=()):
    env = dict(os.environ, TEST_ROOT=str(ROOT), TEST_TMP=tmp)
    setup = '''source "$TEST_ROOT/routines/tests/fixtures/dispatch-env.sh"
isolate_dispatch_env "$TEST_TMP"
export DISPATCH_TEST=1 DOGANY_CLOUD_ENV_ID=env_TEST01
'''
    setup += ''.join(f'export {key}={shlex.quote(value)}\n'
                     for key, value in (env_extra or {}).items())
    result = subprocess.run(['bash', '-c', setup + PRELUDE + body, 'fixture', *args],
                          env=env, text=True, capture_output=True,
                          cwd=tmp)
    assert not (ROOT / '.telegram_bot/po-seat').exists()
    assert not (ROOT.parents[1] / '.telegram_bot').exists()
    return result


def run_gates_case(tmp, repo, loss_order, cloud_ok, kind='build'):
    """A repo-contained read-only run the loss score put on claude-api, with the
    machine saturated (load5 19 on 8 cores -> build cap 0). Declare build for
    the load-gated cases; kind=None exercises the no-worktree llm default."""
    Path(tmp, 'task.md').write_text('Draft the widget README.\n')
    Path(tmp, 'defer-stub.sh').write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$TEST_TMP/defer-args"\nexit 0\n')
    os.chmod(Path(tmp, 'defer-stub.sh'), 0o755)
    body = f'''
TASK_FILE="$TEST_TMP/task.md"; LABEL=readme-draft; BOTTLENECK=coverage; BOTTLENECK_WHY="fixture: a README draft is breadth work"
MODEL=opus; REQUESTED_MODEL=opus; MODEL_SOURCE=explicit
READ_ONLY=1; REPO_PATH="{repo}"; REPO_CONTAINED=1
REQUESTED_RUNTIME=auto; RUNTIME=claude-api
RUNTIME_SELECTION_SOURCE=auto; RUNTIME_SELECTION_REASON=loss_score_pick
RUNTIME_SELECTION_DETAIL="loss fixture"
LOSS_DONE=1; LOSS_ORDER="{loss_order}"; LOSS_CLOUD_OK={cloud_ok}
CFC_TOP="{repo if cloud_ok else ''}"; CFC_BASE=main
rc=0; run_gates || rc=$?
echo "RC=$rc RUNTIME=$RUNTIME REPO_PATH=$REPO_PATH WT_BASE_ARG=$WT_BASE_ARG MODEL=$MODEL"
echo "CLOUD_ACTIVE=$(read_active_count cloud) API_ACTIVE=$(read_active_count claude-api)"
echo "KIND=$(host_run_kind)"
'''
    env = {'STUB_MACHINE': '19 8 4000', 'DOGANY_DISPATCH_MACHINE': '19 8 4000 0'}
    if kind is not None:
        env['DOGANY_DISPATCH_RUN_KIND'] = kind
    r = run_bash(body, env, tmp)
    recs = sorted(Path(tmp, 'decisions').glob('*.json'))
    doc = json.loads(recs[-1].read_text()) if recs else {}
    return r, doc


class CapFullWalk(unittest.TestCase):
    def test_no_worktree_llm_stays_local_under_load(self):
        # DGN-2138: load alone no longer forces a no-worktree run to cloud
        # or the queue, whether a cloud fallback is eligible or not.
        for cloud_ok in (0, 1):
            with self.subTest(cloud_ok=cloud_ok), tempfile.TemporaryDirectory() as tmp:
                repo = make_repo(tmp)
                r, _ = run_gates_case(tmp, repo, 'claude-api,claude-code,cloud', cloud_ok,
                                      kind=None)
                out = r.stdout + r.stderr
                self.assertEqual(r.returncode, 0, out)
                self.assertIn('KIND=llm', r.stdout, out)
                self.assertIn('RC=0 RUNTIME=claude-api', r.stdout, out)
                self.assertIn('CLOUD_ACTIVE=0 API_ACTIVE=1', r.stdout, out)
                self.assertNotIn('[dispatch] queued ', out)

    def test_cap_full_local_goes_to_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            r, doc = run_gates_case(tmp, repo, 'claude-api,claude-code,cloud', 1)
            out = r.stdout + r.stderr
            self.assertIn('RC=0 RUNTIME=cloud', r.stdout, out)
            self.assertIn(f'REPO_PATH={repo}', r.stdout, out)
            self.assertIn('WT_BASE_ARG=main', r.stdout, out)
            self.assertIn('CLOUD_ACTIVE=1 API_ACTIVE=0', r.stdout, out)
            line = next(x for x in r.stdout.splitlines() if 'runtime selection: cloud' in x)
            self.assertIn('slot_cap_fallback', line)
            self.assertIn('claude-api at cap 0/0', line)
            self.assertIn('claude-code: cap 0/0', line)      # tried in order, with its numbers
            self.assertIn('load5=19.0', line)                 # the machine reason behind the 0
            self.assertIn('cloud has room (1/', line)
            self.assertIn('order: claude-code > cloud', line)
            self.assertEqual(doc.get('status'), 'selected', doc)
            self.assertEqual(doc.get('reason_code'), 'slot_cap_fallback', doc)
            self.assertEqual(doc.get('selected_runtime'), 'cloud', doc)
            self.assertEqual(doc.get('fallback_direction'), 'claude-api->cloud', doc)

    def test_all_full_queues_on_host_with_walk_note(self):
        # DGN-1979: by default no slot anywhere -> the host queue (rc 3),
        # the walk detail remains in the decision record (DGN-2011).
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            r, doc = run_gates_case(tmp, repo, 'claude-api,claude-code,cloud', 0)
            out = r.stdout + r.stderr
            self.assertIn('RC=3 RUNTIME=claude-api', r.stdout, out)
            line = next(x for x in r.stdout.splitlines() if x.startswith('[dispatch] queued '))
            self.assertIn('queued #1 (ticket T', line)
            rec = [json.loads(p.read_text()) for p in Path(tmp, 'decisions').glob('*.json')]
            self.assertTrue(any(d.get('status') == 'queued' and d.get('reason_code') == 'host_queue'
                                for d in rec), rec)

    def test_explicit_runtime_keeps_the_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_bash('LOSS_ORDER=""; LOSS_CLOUD_OK=1; CFC_TOP=/x\n'
                         'echo "A=$(slot_walk_order claude-code) B=$(slot_walk_order codex) '
                         'C=$(slot_walk_order claude-api)."\n'
                         'LOSS_ORDER="codex,cloud,claude-code"; LOCAL_WHY="needs the device lab"\n'
                         'echo "D=$(slot_walk_order claude-code)."', tmp=tmp)
            self.assertIn('A=codex B=claude-code C=.', r.stdout, r.stderr)
            self.assertIn('D=codex.', r.stdout, r.stderr)          # --local-why: never cloud


class MachineTerm(unittest.TestCase):
    def test_load_flips_the_pick(self):
        scores = {'claude-code': (30.0, '30%', 'cl'), 'cloud': (12.0, '12%', 'cd')}
        self.assertEqual(rl.rank(dict(scores), [])[0], 'claude-code')
        s2 = dict(scores)
        txt = rl.apply_machine(s2, '19,8,4000,,')
        pick, verdict, line = rl.rank(s2, [])
        self.assertEqual((pick, verdict), ('cloud', 'loss_score_pick'), line)
        self.assertIn('claude 30%-40', line)
        self.assertIn('load5=19.0/12', txt)
        self.assertEqual(rl.order(s2), ['cloud', 'claude-code'])
        # An idle machine costs nothing; half-way costs part.
        s3 = dict(scores)
        self.assertEqual(rl.apply_machine(s3, '2,8,16000,0,2').endswith('pressure=0.17'), True)
        self.assertEqual(s3['claude-code'][0], 30.0)
        self.assertAlmostEqual(rl.machine_penalty(0.75), 20.0)

    def test_held_test_slots_are_pressure(self):
        p, txt = rl.machine_pressure('1,8,16000,2,2')
        self.assertEqual(p, 1.0)
        self.assertIn('test_slots=2/2', txt)
        self.assertEqual(rl.machine_pressure(',,,,'), (None, 'machine unread'))

    def test_cli_line_and_order(self):
        now = 1_780_000_000
        until = time.strftime('%Y-%m-%d', time.gmtime(now + 30 * D))
        # Explicit empty passes avoid consulting either real account store.
        base = [sys.executable, '-B', str(ROUTINES / 'lib' / 'runtime_loss.py'), 'rank',
                '--pools', 'codex,cloud', '--claude-pass', '', '--codex-pass', '', '--codex-used', '60',
                '--codex-resets-at', str(int(now + 10 * H)), '--cloud-until', until,
                '--cloud-cap', '4', '--cloud-active', '0', '--now', str(now)]
        out = subprocess.run(base, input='', text=True, capture_output=True).stdout.rstrip('\n')
        pick, _, line, order = out.split('\t')
        self.assertEqual((pick, order), ('codex', 'codex,cloud'), line)
        out = subprocess.run(base + ['--machine', '19,8,4000,1,2'], input='', text=True,
                             capture_output=True).stdout.rstrip('\n')
        pick, _, line, order = out.split('\t')
        self.assertEqual((pick, order), ('cloud', 'cloud,codex'), line)
        self.assertIn('machine load5=19.0/12 mem_avail=4000MB test_slots=1/2', line)
        self.assertIn('-> local pools -40', line)

    def test_shell_reads_slots_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = Path(tmp, 'shared', 'cron-queue')
            live = q / 'worker-test' / 'slot-1'
            dead = q / 'worker-test' / 'slot-2'
            heavy = q / 'heavy-test' / 'slot-1'
            for d in (live, dead, heavy):
                d.mkdir(parents=True)
            sleeper = subprocess.Popen(['sleep', '30'])
            try:
                live.joinpath('pid').write_text(str(sleeper.pid))
                heavy.joinpath('pid').write_text(str(sleeper.pid))
                dead.joinpath('pid').write_text('999999')
                r = run_bash('unset DOGANY_DISPATCH_MACHINE_PRESSURE\nDGN_WORKER_TEST_SLOTS=3\n'
                             'echo "ARG=$(machine_pressure_arg)"', {'STUB_MACHINE': '6.5 8 9000'},
                             tmp=tmp)
            finally:
                sleeper.kill()
                sleeper.wait()
            self.assertIn('ARG=6.5,8,9000,2,3', r.stdout, r.stderr)


class Attach(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        Path(self.tmp, 'task.md').write_text('Write the README from the attached notes.\n')
        self.doc = Path(self.tmp, 'outside', 'ops-notes.md')
        self.doc.parent.mkdir()
        self.doc.write_text('# Ops notes\nThe pipeline has three stages.\n')

    def tearDown(self):
        self._tmp.cleanup()

    def test_inlines_basename_only(self):
        r = run_bash(f'TASK_FILE="$TEST_TMP/task.md"; ATTACH_FILES=("{self.doc}")\n'
                     'rc=0; attach_inline || rc=$?; echo "RC=$rc"; echo "TF=$TASK_FILE"; cat "$TASK_FILE"',
                     tmp=self.tmp)
        self.assertIn('RC=0', r.stdout, r.stderr)
        self.assertIn('--- attached file: ops-notes.md (', r.stdout)
        self.assertIn('The pipeline has three stages.', r.stdout)
        self.assertIn('--- end attached file: ops-notes.md ---', r.stdout)
        self.assertNotIn(str(self.doc.parent), r.stdout.split('TF=', 1)[1].split('\n', 1)[1])
        self.assertIn('[dispatch] attach: inlined 1 file(s)', r.stdout)
        self.assertEqual(Path(self.tmp, 'task.md').read_text(),
                         'Write the README from the attached notes.\n')   # caller's file untouched

    def test_size_cap_and_binary(self):
        big = Path(self.tmp, 'big.txt')
        big.write_text('x' * 5000)
        r = run_bash(f'TASK_FILE="$TEST_TMP/task.md"; ATTACH_FILES=("{big}"); PROMPT_MAX_BYTES=4096\n'
                     'rc=0; attach_inline || rc=$?; echo "RC=$rc TF=$TASK_FILE"', tmp=self.tmp)
        self.assertIn('RC=1 TF=' + os.path.join(self.tmp, 'task.md'), r.stdout, r.stderr)
        self.assertIn('cloud prompt cap 4096B', r.stderr)
        binf = Path(self.tmp, 'blob.bin')
        binf.write_bytes(b'ab\x00cd')
        r = run_bash(f'TASK_FILE="$TEST_TMP/task.md"; ATTACH_FILES=("{binf}")\n'
                     'rc=0; attach_inline || rc=$?; echo "RC=$rc"', tmp=self.tmp)
        self.assertIn('RC=1', r.stdout)
        self.assertIn('binary', r.stderr)
        r = run_bash('TASK_FILE="$TEST_TMP/task.md"; ATTACH_FILES=("$TEST_TMP/nope.md")\n'
                     'rc=0; attach_inline || rc=$?; echo "RC=$rc"', tmp=self.tmp)
        self.assertIn('RC=1', r.stdout)
        self.assertIn('not a readable regular file', r.stderr)

    def test_flag_parses_repeatable(self):
        r = run_bash('echo "N=${#ATTACH_FILES[@]} 1=${ATTACH_FILES[0]} 2=${ATTACH_FILES[1]}"',
                     tmp=self.tmp,
                     args=('--task', 'x', '--attach', '/a.md', '--attach', '/b c.md', '--label', 'l'))
        self.assertIn('N=2 1=/a.md 2=/b c.md', r.stdout, r.stderr)


class CloudBaseRead(unittest.TestCase):
    """A failed origin read while resolving the cloud base is a refusal, never
    "branch absent" (which silently retargeted the default base to main, or
    told the owner to push a branch that was already pushed)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def preflight(self, pre=''):
        return run_bash(f'REPO_PATH="{self.repo}"; LABEL=base-read\n{pre}\n'
                        'rc=0; cloud_preflight || rc=$?\n'
                        'echo "RC=$rc WT_BASE=$WT_BASE SRC=$CLOUD_BASE_SOURCE"', tmp=self.tmp)

    def test_default_base_read_failure_refuses(self):
        r = self.preflight('cloud_remote_sha() { [[ "$2" == claude/* ]] && return 0; return 1; }')
        self.assertRegex(r.stdout, r'RC=1 WT_BASE=\S* SRC=\n', r.stderr)
        self.assertIn('cannot read origin', r.stderr)
        self.assertIn('default base (dev)', r.stderr)

    def test_flag_base_read_failure_refuses(self):
        r = self.preflight('WT_BASE_ARG=dsp/stem\n'
                           'cloud_remote_sha() { [[ "$2" == claude/* ]] && return 0; return 1; }')
        self.assertIn('RC=1', r.stdout, r.stderr)
        self.assertIn('cannot read origin', r.stderr)
        self.assertNotIn('push it first', r.stderr)

    def test_readable_origin_keeps_dev_then_main(self):
        r = self.preflight()
        self.assertIn('RC=0 WT_BASE=main SRC=default_main', r.stdout, r.stderr)
        # Seed the local origin directly; the runner blocks network-shaped
        # push URLs even when insteadOf redirects their read side locally.
        git('branch', 'dev', 'main', cwd=os.path.join(self.tmp, 'origin.git'))
        r = self.preflight()
        self.assertIn('RC=0 WT_BASE=dev SRC=default', r.stdout, r.stderr)

    def test_overhead_measured_before_runid(self):
        r = run_bash('WT_BASE=dev; LABEL=x; unset RUNID\n'
                     'echo "PRE=$(cloud_task_overhead)"\n'
                     'RUNID=dsp-20261010-120000-0042\n'
                     'echo "REAL=$( { cloud_task_preamble; cloud_task_footer; } | wc -c | tr -d \' \')"',
                     tmp=self.tmp)
        pre = int(r.stdout.split('PRE=')[1].split()[0])
        real = int(r.stdout.split('REAL=')[1].split()[0])
        self.assertEqual(pre, real, r.stderr)
        self.assertNotIn('unbound variable', r.stderr)


class LocalWhyHint(unittest.TestCase):
    def test_read_only_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = {
                'reads operator-side docs outside the repo': 0,
                'needs to consult the design spec in my notes folder': 0,
                'reads docs and runs the Xcode simulator tests': 1,
                'writes to the live instance launch agents': 1,
                'base has unpushed commits I cannot push yet': 1,
                'needs the GPU on this machine': 1,
            }
            body = ''.join(f'local_why_read_only "{w}" && echo "Y:{w}" || echo "N:{w}"\n'
                           for w in cases)
            r = run_bash(body, tmp=tmp)
            for w, need in cases.items():
                self.assertIn(('N:' if need else 'Y:') + w, r.stdout, r.stderr)

    def test_refused_with_attach_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            Path(tmp, 'task.md').write_text('Draft the README.\n')
            base = (f'TASK_FILE="$TEST_TMP/task.md"; LABEL=readme; BOTTLENECK=coverage; '
                    f'BOTTLENECK_WHY="fixture axis evidence"; MODEL=opus; READ_ONLY=1; REPO_PATH="{repo}"; '
                    'DISPLAY_NAME=readme; RUNTIME=claude-code\n')
            r = run_bash(base + 'LOCAL_WHY="reads operator-side docs outside the repo"\n'
                         'rc=0; validate_spawn_args || rc=$?; echo "RC=$rc"', tmp=tmp)
            self.assertIn('RC=1', r.stdout, r.stderr)
            self.assertIn('--attach <file>', r.stderr)
            r = run_bash(base + 'LOCAL_WHY="runs the device test rig attached to this host"\n'
                         'rc=0; validate_spawn_args || rc=$?; echo "RC=$rc"', tmp=tmp)
            self.assertIn('RC=0', r.stdout, r.stderr)


if __name__ == '__main__':
    unittest.main()
