#!/usr/bin/env python3
# test-lane: fast
# test-component: runtime-selector
# test-tier: 0
# promise: an explicit --runtime or --model without a reason is refused with a one-line message naming --pin-why
# promise: --pin-why is recorded verbatim and counted; local runtime pins additionally need a structured runtime reason
# promise: --local-why covers claude-api, --model-why covers models, and the dispatcher's own re-runs are exempt
# promise: a dispatch with no pin is untouched by the gate and placed by the selector
# Selection decisions (quota, headroom, model binding, fallback mechanics) run in
# test-runtime-selector-selection.py (slow: one real bash selection per case).
"""Offline function tests: no workers, notifications or live quota RPCs."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

ROOT = Path(__file__).resolve().parents[2]



def claude_snapshot(five, seven, five_reset_s=3 * 3600, seven_reset_s=3 * 86400):
    """DGN-1874 fixture: a claude_usage_snapshot() payload (no history)."""
    now = time.time()
    iso = lambda s: time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(now + s))
    return json.dumps({'usage': {
        'five_hour_util': five, 'five_hour_resets_at': iso(five_reset_s),
        'seven_day_util': seven, 'seven_day_resets_at': iso(seven_reset_s),
        'fetched_at': now}, 'history': []})


class Selection(unittest.TestCase):
    def run_case(self, expected, reason, *, remaining='80 70', used=40,
                 write=False, hooks=True, quota_rc=0, usage_rc=0, bound=True,
                 request='auto', extra='', env_request='', args=(),
                 axis='coverage', model='opus', claude=None, refused=False):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME=env_request,
                       TEST_ROOT=str(ROOT), TEST_TMP=tmp)
            script = '''source "$TEST_ROOT/routines/dispatch-detached.sh"
DECISION_DIR="$TEST_TMP/decisions"
LABEL=test
WT_REPO=/fixture/estate
# Local quota/binding cases have no API pool; its policy has a separate suite.
# Seed the budget cache so host plans, credentials and month pace stay out.
API_POOL_BUDGET_READ=1
API_POOL_BUDGET=""
API_POOL_BUDGET_SOURCE="off (local selection fixture)"
'''
            script += f'MODEL={model}; BOTTLENECK={axis}\n'
            if not args:
                script += f'REQUESTED_RUNTIME={request}\n'
            script += f'''
WT_SLUG={'fixture' if write else ''}
claude_quota_remaining() {{ echo '{remaining}'; return {usage_rc}; }}
claude_usage_snapshot() {{ {"printf '%s' '" + claude + "'" if claude else 'return 1'}; }}
codex_quota_gate() {{ CODEX_USED_PERCENT={used}; CODEX_RESETS_AT=$(( $(date +%s) + 302400 )); CODEX_QUOTA_NOTE='used={used}'; return {quota_rc}; }}
codex_hook_gate() {{ CODEX_HOOK_NOTE='fixture hooks={hooks} target='$1; return {0 if hooks else 2}; }}
'''
            if not bound:
                script += 'fallback_tier_eligible() { return 1; }\n'
            script += 'select_default_runtime\n' + extra
            result = subprocess.run(['bash', '-c', script, 'test', *args], env=env,
                                    text=True, capture_output=True)
            if refused:
                # DGN-2180: hooks untrusted never moves to claude-code; the
                # selection refuses (the caller queues) and records no pick.
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('safety_never_moves', result.stdout + result.stderr)
                self.assertEqual(list(Path(tmp, 'decisions').glob('*.json')), [])
                return None
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            records = list(Path(tmp, 'decisions').glob('*.json'))
            self.assertEqual(len(records), 1)
            doc = json.loads(records[0].read_text())
            self.assertEqual(doc['selected_runtime'], expected)
            self.assertEqual(doc['selection_reason'], reason)
            self.assertEqual(doc['fallback_direction'], '')
            self.assertTrue(doc['selection_detail'])
            return doc

    def test_a_read_prefers_codex_when_quota_passes(self):
        doc = self.run_case('codex', 'read_quota_verified', used=95)
        self.assertIn('api_pool=api_pool_off: api_pool off (local selection fixture)',
                      doc['selection_detail'])

    def test_binding_required(self):
        self.run_case('claude-code', 'codex_tier_unbound', bound=False)

    def test_explicit_flag_beats_env_without_probes(self):
        doc = self.run_case('claude-code', 'explicit_runtime', env_request='codex',
                            args=('--runtime', 'claude-code'), usage_rc=99, quota_rc=99)
        self.assertEqual(doc['selection_source'], 'flag')

    def test_env_request(self):
        doc = self.run_case('codex', 'explicit_runtime', env_request='codex',
                            args=('--label', 'fixture'), quota_rc=99)
        self.assertEqual(doc['selection_source'], 'env')

    def test_record_failure_stops_selection(self):
        env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='claude-code')
        result = subprocess.run(['bash', '-c', 'source routines/dispatch-detached.sh; '
                                 'record_runtime_decision() { return 1; }; select_default_runtime'],
                                cwd=ROOT, env=env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)


class PinWhy(unittest.TestCase):
    """DGN-2144: pin_why_gate, driven through the real script (fixtures only)."""

    def gate(self, args, env_extra=None, tmp=None, calls=1):
        own = tmp is None
        tmp = tmp or tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='',
                   TEST_ROOT=str(ROOT), TEST_TMP=tmp, **(env_extra or {}))
        script = (f'source "$TEST_ROOT/routines/dispatch-detached.sh"\n'
                  'DECISION_DIR="$TEST_TMP/decisions"; LABEL=t\n'
                  f'{args}\n' + 'pin_why_gate && rc=0 || rc=$?\n' * 0)
        script += ''.join('pin_why_gate && rc=0 || rc=$?\n' for _ in range(calls))
        script += 'record_runtime_decision selected test "$RUNTIME_SELECTION_DETAIL"\n'
        script += 'echo "rc=$rc"\n'
        r = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
        rc = int(r.stdout.strip().splitlines()[-1].split('=')[1])
        recs = sorted(Path(tmp, 'decisions').glob('rtd-*.json'))
        doc = json.loads(recs[-1].read_text()) if recs else {}
        return rc, r.stdout, r.stderr, doc

    RT = 'REQUESTED_RUNTIME=cloud; RUNTIME=cloud; PIN_RUNTIME_FLAG=1; '

    def test_runtime_pin_without_reason_refused_one_line(self):
        rc, out, err, doc = self.gate(self.RT)
        self.assertEqual(rc, 2)
        lines = [l for l in err.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertIn('--pin-why', lines[0])
        self.assertEqual(doc['status'], 'selected')  # the later explicit record; the blocked one was overwritten

    def test_model_pin_without_reason_refused(self):
        rc, _, err, _ = self.gate('MODEL=opus; PIN_MODEL_FLAG=1')
        self.assertEqual(rc, 2)
        self.assertIn('--pin-why', err)

    def test_pin_why_proceeds_and_is_recorded_and_counted(self):
        tmp = tempfile.mkdtemp()
        rc, out, err, doc = self.gate(self.RT + 'PIN_WHY="repo needs the cloud box"', tmp=tmp, calls=2)
        self.assertEqual(rc, 0, err)
        self.assertEqual(doc['pin_why'], 'repo needs the cloud box')
        self.assertIn('pin_why=repo needs the cloud box', doc['selection_detail'])
        self.assertEqual(doc['pin_count_today'], 2)
        counts = list(Path(tmp, 'decisions').glob('pin-why-count.*'))
        self.assertEqual(len(counts), 1)
        counter = json.loads(counts[0].read_text())
        self.assertEqual(counter['total'], 2)
        self.assertEqual(counter['reasons']['repo needs the cloud box'], 2)

    def test_fourth_normalized_pin_reason_is_refused(self):
        rc, out, err, doc = self.gate(
            self.RT + 'PIN_WHY="  Repo   NEEDS the cloud box  "', calls=4)
        self.assertEqual(rc, 2)
        self.assertEqual(out.count('pin accepted:'), 3)
        self.assertIn('drop --runtime and let the selector choose, or give a run-specific reason', err)
        self.assertEqual(doc['pin_count_today'], 3)

    def test_different_local_why_never_justifies_runtime_pin(self):
        tmp = tempfile.mkdtemp()
        for reason in ('Mac GUI state differs for this run',
                       'Live data exists only on this Mac'):
            rc, out, err, _ = self.gate(
                'REQUESTED_RUNTIME=codex; PIN_RUNTIME_FLAG=1; '
                f'LOCAL_WHY="{reason}"', tmp=tmp, calls=4)
            self.assertEqual(rc, 2)
            self.assertIn('local is satisfied by either local runtime', err)
            self.assertNotIn('pin accepted:', out)

    def test_same_reason_counter_resets_next_day(self):
        tmp = tempfile.mkdtemp()
        clock = 'date() { printf "%s\\n" "$TEST_DAY"; }; '
        first = self.RT + clock + 'PIN_WHY="repo needs the cloud box"'
        self.assertEqual(self.gate(first, {'TEST_DAY': '20261022'}, tmp=tmp, calls=3)[0], 0)
        rc, out, err, doc = self.gate(
            first, {'TEST_DAY': '20261023'}, tmp=tmp)
        self.assertEqual(rc, 0, err)
        self.assertIn('pin #1 today', out)
        self.assertEqual(doc['pin_count_today'], 1)

    def test_pin_why_too_short_or_with_pipe_refused(self):
        self.assertEqual(self.gate(self.RT + 'PIN_WHY=short')[0], 2)
        self.assertEqual(self.gate(self.RT + 'PIN_WHY="has a | pipe in it"')[0], 2)

    def test_existing_reason_flags_still_satisfy(self):
        self.assertEqual(self.gate('REQUESTED_RUNTIME=claude-api; PIN_RUNTIME_FLAG=1; LOCAL_WHY="needs local MCP credentials"')[0], 0)
        self.assertEqual(self.gate('MODEL=opus; PIN_MODEL_FLAG=1; MODEL_WHY="opus only holds the long context"')[0], 0)
        # --local-why does not cover cloud, and --model-why does not cover a runtime pin
        self.assertEqual(self.gate(self.RT + 'LOCAL_WHY="needs local MCP credentials"')[0], 2)
        self.assertEqual(self.gate(self.RT + 'MODEL_WHY="opus only holds the long context"')[0], 2)

    def test_dispatcher_reruns_are_exempt(self):
        for runtime in ('cloud', 'codex', 'claude-code'):
            request = f'REQUESTED_RUNTIME={runtime}; PIN_RUNTIME_FLAG=1'
            for env in ({'DOGANY_DISPATCH_DEFER_ATTEMPT': '1'},
                        {'DOGANY_HOST_LEASE': 'lease-1'},
                        {'DOGANY_DISPATCH_REPLACED_FROM': 'run-1'}):
                self.assertEqual(self.gate(request, env, calls=4)[0], 0)

    def test_no_pin_is_untouched(self):
        rc, out, err, _ = self.gate('')
        self.assertEqual((rc, err), (0, ''))
        self.assertNotIn('pin accepted', out)

    def test_cli_refuses_then_accepts_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp, 'task.md')
            task.write_text('Read-only: report.\n')
            # Own workspace: the dispatcher derives WORKSPACE from its script dir,
            # so a symlinked routines/ keeps the per-day pin counter in tmp, not
            # in the checkout (a shared counter refused this case after 3 runs/day).
            ws = Path(tmp, 'ws'); ws.mkdir()
            subprocess.run([sys.executable, str(ROOT / 'routines/tests/fixtures/stage-why.py'), str(ws)], check=True)
            (ws / 'routines').symlink_to(ROOT / 'routines')
            base = [str(ws / 'routines' / 'dispatch-detached.sh'), '--why', 'why-9999', '--read-only', '--task', str(task),
                    '--label', 'pin-e2e', '--display-name', '핀 게이트 확인', '--bottleneck', 'accuracy', '--bottleneck-why',
                    'given checks need exact pass fail verification', '--runtime', 'claude-api']
            env = dict(os.environ, DOGANY_DISPATCH_RUNTIME='', DISPATCH_TEST='1')
            # Real parser + do_spawn admission, ending before selection/slots.
            # A passing pin must never launch a worker or enqueue a real job.
            plan = 'source "$1" "${@:2}"; print_calibration() { :; }; select_default_runtime() { exit 0; }; do_spawn'
            r = subprocess.run(['bash', '-c', plan, 'plan', *base], env=env, text=True, capture_output=True, cwd=tmp)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn('--pin-why', r.stderr)
            self.assertNotIn('pin accepted', r.stdout)
            r = subprocess.run(['bash', '-c', plan, 'plan', *base, '--pin-why', 'repo needs the cloud box'], env=env,
                               text=True, capture_output=True, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn('pin accepted: repo needs the cloud box', r.stdout, r.stdout + r.stderr)
            self.assertNotIn('--pin-why', r.stderr)  # any later refusal is the selector's own

    def test_pin_why_reaches_run_env_status_and_ledger(self):
        src = (ROOT / 'routines' / 'dispatch-detached.sh').read_text()
        fn = subprocess.run(
            ['sed', '-n', '/^run_env_write() {/,/^}/p',
             str(ROOT / 'routines' / 'dispatch-detached.sh')],
            capture_output=True, text=True, check=True).stdout
        value = "핀 reason with spaces, quotes ' and \\slashes"
        with tempfile.TemporaryDirectory() as tmp:
            env_file = str(Path(tmp, 'run.env'))
            script = (fn + '\nrun_env_write PIN_WHY "$1" > "$2"\n'
                      'unset PIN_WHY; source "$2"; printf "%s" "$PIN_WHY"')
            saved = subprocess.run(['bash', '-c', script, '_', value, env_file],
                                   capture_output=True, check=True)
            self.assertEqual(saved.stdout, value.encode())
            Path(env_file).read_text(encoding='utf-8')
        self.assertIn('"pin_why": os.environ.get("ST_PIN_WHY", "")', src)
        self.assertIn('spawn_note="${spawn_note%)} pin_why=$PIN_WHY)"', src)
        self.assertIn('run_env_write RUNTIME_REASON "$RUNTIME_REASON"', src)
        self.assertIn('"runtime_reason": os.environ.get("ST_RUNTIME_REASON", "")', src)
        self.assertIn('replay+=( --runtime-reason "$RUNTIME_REASON" )', src)




# DGN-2271: real argv/admission/selection plan cases; no workers or slots.
SKULL = ['--runtime', 'claude-code', '--model', 'opus', '--effort', 'high',
         '--local-why', 'rehearsal needs a copy of the live kit DB on this Mac',
         '--pin-why', 'local live-DB-copy rehearsal with opus']
MESSAGE = ('local is satisfied by either local runtime; drop --runtime to let '
           'the selector pick by headroom, or name why THIS runtime')

PLAN = r'''
source "$TEST_WS/routines/dispatch-detached.sh"
WORKSPACE="$TEST_WS"; SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"
WHY_ID=why-9999
claude_usage_snapshot() { printf '%s' "$TEST_USAGE"; }
codex_quota_gate() {
  CODEX_USED_PERCENT="$TEST_CODEX_USED"
  CODEX_RESETS_AT=$(( $(date +%s) + 259200 ))
  CODEX_QUOTA_NOTE="fixture used=$TEST_CODEX_USED"
  return 0
}
api_pool_budget() { return 1; }
api_pool_verdict() { printf 'keep\tapi_off\tfixture\n'; }
codex_cloud_candidate() { return 1; }
reset_pass_specs() { :; }
reset_pass_spec() { :; }
reset_pass_review() { :; }
machine_snapshot() { echo '0 8 16000'; }
test_slots_held() { echo '0 2'; }
print_calibration() { :; }
# Stop after do_spawn's real admission + selection, before sandbox/slot checks.
bottleneck_gate() { printf 'PLAN runtime=%s model=%s\n' "$RUNTIME" "$MODEL"; exit 0; }
# Fail closed if a fixture ever stops reaching the planned boundary.
run_gates() { echo 'UNEXPECTED slot acquisition' >&2; exit 99; }
queue_refusal() { echo "UNEXPECTED queue: $*" >&2; exit 98; }
PUSH_SH=/usr/bin/false
USAGE_DEFER_RETRY=/usr/bin/false
do_spawn
'''


class LocalRuntimePin(unittest.TestCase):
    def plan(self, args, *, codex_used=1, claude_used=95, task_text=None):
        with tempfile.TemporaryDirectory(prefix='dgn2271-') as tmp:
            ws = Path(tmp, 'ws'); ws.mkdir()
            subprocess.run([sys.executable, str(ROOT / 'routines/tests/fixtures/stage-why.py'), str(ws)], check=True)
            (ws / 'routines').symlink_to(ROOT / 'routines')
            task = Path(tmp, 'task.md')
            task.write_text(task_text or 'Why: Verify local runtime placement.\n'
                            'What: Report the selected runtime.\n')
            now = time.time()
            usage = {'usage': {'five_hour_util': claude_used,
                              'seven_day_util': claude_used,
                              'five_hour_resets_at': now + 7200,
                              'seven_day_resets_at': now + 259200,
                              'fetched_at': now}, 'history': []}
            env = {k: v for k, v in os.environ.items()
                   if k == 'DOGANY_RESOURCE_SETTINGS_DIR' or (not k.startswith(('GIT_CONFIG_', 'DOGANY_', 'DGN_', 'PYTHON')))}
            env.update(DISPATCH_TEST='1', TEST_WS=str(ws), TEST_USAGE=json.dumps(usage),
                       TEST_CODEX_USED=str(codex_used), DOGANY_DISPATCH_RUNTIME='',
                       DOGANY_SHARED_HOME=str(Path(tmp, 'shared')),
                       DOGANY_USAGE_STATE_DIR=str(Path(tmp, 'usage')),
                       DOGANY_DISPATCH_MACHINE_PRESSURE='off')
            common = ['--task', str(task), '--read-only', '--repo', str(ROOT),
                      '--label', 'dgn2271-plan', '--display-name', '로컬 런타임 선택 확인',
                      '--bottleneck', 'accuracy', '--bottleneck-why',
                      'Verify given dispatcher cases against expected results']
            r = subprocess.run(['bash', '-c', PLAN, 'plan', *common, *args],
                               cwd=tmp, env=env, text=True, capture_output=True)
            docs = [json.loads(p.read_text()) for p in ws.glob('.telegram_bot/dispatch/decisions/rtd-*.json')]
            self.assertNotIn('UNEXPECTED', r.stdout + r.stderr)
            self.assertFalse(list(ws.glob('.telegram_bot/dispatch/dsp-*')), 'plan created a run directory')
            return r, docs

    def assert_passes(self, args, runtime, **kwargs):
        r, docs = self.plan(args, **kwargs)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'PLAN runtime={runtime}', r.stdout)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['selected_runtime'], runtime)
        return docs[0]

    def test_a_skull_exact_ticket_argv_refused(self):
        r, docs = self.plan(SKULL)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(MESSAGE, r.stderr)
        self.assertNotIn('pin accepted:', r.stdout)
        self.assertEqual(docs[0]['reason_code'], 'runtime_pin_not_specific')

    def test_b_drop_runtime_and_pin_why_selects_codex(self):
        doc = self.assert_passes(SKULL[2:-2], 'codex', codex_used=1, claude_used=95)
        self.assertEqual(doc['requested_runtime'], 'auto')
        self.assertNotEqual(doc['selection_reason'], 'explicit_runtime')
        self.assertIn('local_why=', doc['selection_detail'])

    def test_b_reverse_headroom_ranks_claude_but_host_bound_policy_keeps_codex(self):
        # DGN-2293 owner routing: a host-bound local run seats Codex first even
        # when the loss rank puts Claude ahead; the rank stays visible in the line.
        doc = self.assert_passes(SKULL[2:-2], 'codex', codex_used=90, claude_used=1)
        self.assertEqual(doc['selection_reason'], 'owner_routing_policy')
        self.assertIn('claude 98% > codex 0% -> claude', doc['selection_detail'])
        self.assertIn('owner policy: codex,claude-code', doc['selection_detail'])

    def test_c_codex_sandbox_distinction_passes(self):
        doc = self.assert_passes(
            ['--runtime', 'codex', '--runtime-reason', 'sandbox', '--pin-why',
             'codex sandbox is required to confine writes for this audit',
             '--local-why', SKULL[7]], 'codex')
        self.assertEqual(doc['runtime_reason'], 'sandbox')
        self.assertEqual(doc['pin_count_today'], 1)

    def test_c_alternative_sandbox_cannot_bind_loopback(self):
        self.assert_passes(
            ['--runtime', 'claude-code', '--runtime-reason', 'sandbox', '--pin-why',
             'codex sandbox cannot bind loopback for this live callback replay',
             '--local-why', SKULL[7]], 'claude-code')

    def test_d_claude_connectors_pass_without_pin_text(self):
        self.assert_passes(['--runtime', 'claude-code', '--needs-connectors',
                            '--local-why', SKULL[7]], 'claude-code')

    def test_d_inferred_claude_connectors_pass(self):
        self.assert_passes(['--runtime', 'claude-code', '--local-why', SKULL[7]],
                           'claude-code', task_text='What: inspect mcp__claude_ai_calendar state.\n')

    def test_e_owner_directive_passes_for_both_local_runtimes(self):
        for runtime in ('codex', 'claude-code'):
            with self.subTest(runtime=runtime):
                self.assert_passes(
                    ['--runtime', runtime, '--runtime-reason', 'owner-directive',
                     '--pin-why', f'owner 2026-10-24 explicitly requested {runtime} for this run',
                     '--local-why', SKULL[7]], runtime)

    def test_runtime_feature_passes(self):
        self.assert_passes(['--runtime', 'codex', '--runtime-reason', 'runtime-feature',
                           '--pin-why', 'codex native review mode is required for this reproduction',
                           '--local-why', SKULL[7]], 'codex')

    def test_codex_locality_only_refused(self):
        r, _ = self.plan(['--runtime', 'codex', '--local-why', SKULL[7]])
        self.assertEqual(r.returncode, 2)
        self.assertIn(MESSAGE, r.stderr)

    def test_runtime_keywords_alone_are_not_evidence(self):
        r, _ = self.plan(['--runtime', 'claude-code', '--local-why', SKULL[7],
                          '--pin-why', 'codex sandbox owner directive opus claude-code'])
        self.assertEqual(r.returncode, 2)
        self.assertIn(MESSAGE, r.stderr)

    def test_connectors_do_not_justify_codex(self):
        r, _ = self.plan(['--runtime', 'codex', '--needs-connectors', '--local-why', SKULL[7]])
        self.assertEqual(r.returncode, 2)
        self.assertIn(MESSAGE, r.stderr)

    def test_invalid_or_orphan_structured_reason_refused(self):
        for args in (['--runtime-reason', 'local'],
                     ['--runtime-reason', 'sandbox'],
                     ['--runtime', 'codex', '--runtime-reason', 'sandbox'],
                     ['--runtime-reason']):
            with self.subTest(args=args):
                r, _ = self.plan([*args, '--local-why', SKULL[7]])
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn('--runtime-reason', r.stderr)

    def test_model_that_excludes_claude_still_needs_reason(self):
        r, _ = self.plan(['--model', 'fable', '--local-why', SKULL[7]])
        self.assertEqual(r.returncode, 2)
        self.assertIn('--pin-why', r.stderr)



# DGN-2168: sourcing dispatch-detached.sh runs usage-gate.py from the template;
# a live-succeeding read would write its cache/history into agents/.template/.claude.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib_usage_state_isolation as _iso  # noqa: E402

_ISO = []
_ENV = {}


def setUpModule():
    # Caller dispatch state and Git config are not fixture inputs.
    _ENV.update(os.environ)
    for key in list(os.environ):
        if key != 'DOGANY_RESOURCE_SETTINGS_DIR' and (key.startswith(('GIT_CONFIG_', 'DOGANY_', 'DGN_', 'PYTHON'))):
            del os.environ[key]
    _ISO.append(_iso.Isolation())
    os.environ['DOGANY_SHARED_HOME'] = str(Path(_ISO[-1].tmp, 'shared'))
    os.environ['DOGANY_DISPATCH_MACHINE_PRESSURE'] = 'off'


def tearDownModule():
    _ISO.pop().close()
    os.environ.clear()
    os.environ.update(_ENV)


if __name__ == '__main__':
    unittest.main(verbosity=2)
