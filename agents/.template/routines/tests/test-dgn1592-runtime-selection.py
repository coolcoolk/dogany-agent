#!/usr/bin/env python3
# test-lane: fast
"""Offline function tests: no workers, notifications or live quota RPCs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

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
                 axis='coverage', model='opus', claude=None):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME=env_request,
                       TEST_ROOT=str(ROOT), TEST_TMP=tmp)
            script = '''source "$TEST_ROOT/routines/dispatch-detached.sh"
DECISION_DIR="$TEST_TMP/decisions"
LABEL=test
WT_REPO=/fixture/estate
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
        self.run_case('codex', 'read_quota_verified', used=95)

    def test_b_read_spills_to_codex_when_claude_is_tight(self):
        # DGN-1874: Claude 5h 98% / 7d 96% -> blocked; codex under its ceiling.
        doc = self.run_case('codex', 'read_quota_verified', used=60,
                            claude=claude_snapshot(98, 96))
        self.assertIn('headroom=claude_tight_codex_open', doc['selection_detail'])

    def test_c_write_estate(self):
        self.run_case('codex', 'write_hooks_and_quota_verified', write=True, used=95)

    def test_d_write_outside_estate(self):
        self.run_case('claude-code', 'codex_hooks_unavailable', write=True, hooks=False)

    def test_read_prefers_claude_when_claude_has_headroom(self):
        # DGN-1874 (dec-254) reverses the DGN-1592 "codex whenever quota
        # passes" default: Claude open and on pace keeps the run on Claude.
        for write in (False, True):
            doc = self.run_case('claude-code', 'claude_headroom_on_pace', used=73,
                                write=write, claude=claude_snapshot(20, 30))
            self.assertEqual(doc['model'], 'opus')
            self.assertIn('codex 7d used=73%', doc['selection_detail'])

    def test_unknown_is_not_permission(self):
        self.run_case('codex', 'read_quota_verified', usage_rc=1)
        self.run_case('claude-code', 'codex_quota_unavailable', quota_rc=3)
        self.run_case('claude-code', 'codex_quota_unavailable', write=True, quota_rc=2)

    def test_model_less_codex_candidate_resolves_binding_before_tier_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='',
                       TEST_ROOT=str(ROOT), TEST_TMP=tmp)
            script = '''source "$TEST_ROOT/routines/dispatch-detached.sh"
DECISION_DIR="$TEST_TMP/decisions"
MODEL=""; MODEL_SOURCE=explicit; BOTTLENECK=coverage; LABEL=test
WT_REPO=/fixture/estate; WT_SLUG=""
codex_quota_gate() { CODEX_USED_PERCENT=95; CODEX_QUOTA_NOTE=fixture; return 0; }
claude_usage_snapshot() { return 1; }
select_default_runtime
printf 'RUNTIME=%s MODEL=%s SOURCE=%s\\n' "$RUNTIME" "$MODEL" "$MODEL_SOURCE"
'''
            result = subprocess.run(['bash', '-c', script], env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('RUNTIME=codex MODEL=fable SOURCE=binding', result.stdout)
            record = json.loads(next(Path(tmp, 'decisions').glob('*.json')).read_text())
            self.assertEqual(record['model_source'], 'binding')

    def test_accuracy_caller_model_uses_codex_axis_binding(self):
        for model in ('opus', 'sonnet'):
            for write in (False, True):
                with self.subTest(model=model, write=write):
                    doc = self.run_case(
                        'codex', 'write_hooks_and_quota_verified' if write else
                        'read_quota_verified', axis='accuracy', model=model,
                        args=('--model', model), write=write,
                        extra='bottleneck_gate\n')
                    self.assertEqual(doc['requested_model'], model)
                    self.assertEqual(doc['model'], 'fable')
                    self.assertEqual(doc['codex_model'], 'fable')
                    self.assertEqual(doc['model_source'], 'binding')

    def test_accuracy_gate_failure_preserves_caller_model(self):
        for gates, reason in (({'quota_rc': 3}, 'codex_quota_unavailable'),
                              ({'write': True, 'hooks': False},
                               'codex_hooks_unavailable')):
            doc = self.run_case('claude-code', reason, axis='accuracy',
                                model='opus', extra='bottleneck_gate\n', **gates)
            self.assertEqual(doc['model'], 'opus')
            self.assertEqual(doc['requested_model'], 'opus')
            self.assertEqual(doc['codex_model'], '')

    def test_judgment_without_codex_row_stays_claude(self):
        doc = self.run_case('claude-code', 'codex_tier_unbound',
                            axis='judgment', extra='bottleneck_gate\n')
        self.assertEqual(doc['model'], 'opus')

    def test_explicit_runtime_preserves_model(self):
        doc = self.run_case('claude-code', 'explicit_runtime', axis='accuracy',
                            request='claude-code', extra='bottleneck_gate\n')
        self.assertEqual(doc['model'], 'opus')

    def test_late_claude_fallback_restores_requested_model(self):
        self.run_case('codex', 'read_quota_verified', axis='accuracy', extra=r'''
USAGE_GATE=/usr/bin/true
record_runtime_decision() { return 0; }
codex_to_claude_switch fixture || exit 10
[[ "$RUNTIME" == claude-code && "$MODEL" == opus && "$MODEL_SOURCE" == explicit ]] || exit 11
bottleneck_gate
''')

    def test_late_fallback_record_failure_restores_codex_model(self):
        self.run_case('codex', 'read_quota_verified', axis='accuracy', extra=r'''
USAGE_GATE=/usr/bin/true
record_runtime_decision() { return 1; }
rc=0; codex_to_claude_switch fixture || rc=$?
[[ "$rc" == 2 && "$RUNTIME" == codex && "$MODEL" == fable && "$MODEL_SOURCE" == binding ]]
''')

    def test_codex_candidate_follows_changed_binding_data(self):
        self.run_case('codex', 'read_quota_verified', axis='accuracy', extra=r'''
MODEL=opus
TIER_BINDINGS="$TEST_TMP/bindings.tsv"
printf 'codex\taccuracy\tsonnet\tfixture\n' > "$TIER_BINDINGS"
record_runtime_decision() { return 0; }
select_default_runtime
[[ "$RUNTIME" == codex && "$MODEL" == sonnet && "$CODEX_MODEL" == sonnet ]]
''')

    def test_status_keeps_requested_and_codex_models(self):
        self.run_case('codex', 'read_quota_verified', axis='accuracy', extra=r'''
RUNDIR="$TEST_TMP"; RUNID=fixture; BRANCH=fixture
write_status_json completed 0 silent
python3 - "$RUNDIR/status.json" <<'PYTEST'
import json, sys
with open(sys.argv[1]) as f:
    doc = json.load(f)
assert doc['requested_model'] == 'opus', doc
assert doc['codex_model'] == doc['model'] == 'fable', doc
assert doc['model_source'] == 'binding', doc
PYTEST
''')

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

    def test_spawn_resolves_default_before_tier_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='',
                       TEST_TMP=tmp)
            script = """source routines/dispatch-detached.sh
DECISION_DIR="$TEST_TMP/decisions"
MODEL=opus; BOTTLENECK=coverage; WT_SLUG=fixture
validate_access_mode() { return 0; }
validate_spawn_args() { return 0; }
check_prompt_size() { return 0; }
print_calibration() { return 0; }
codex_quota_gate() { CODEX_USED_PERCENT=95; CODEX_QUOTA_NOTE=fixture; }
codex_hook_gate() { CODEX_HOOK_NOTE=verified; }
claude_usage_snapshot() { return 1; }
bottleneck_gate() { echo "SELECTED=$RUNTIME"; return 1; }
do_spawn
"""
            result = subprocess.run(['bash', '-c', script], cwd=ROOT, env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('SELECTED=codex', result.stdout)

    def test_quota_payloads_fail_closed(self):
        # CODEX_QUOTA_MAX_PERCENT overridden to 90 here on purpose: the live
        # owner ceiling is 100 (DGN-1576, 2026-09-19 second raise -- "no
        # ceiling" by design), which makes a valid (<=100) used_percent
        # payload unable to exercise the over-ceiling branch at all. The
        # override exercises that branch's mechanism independent of
        # whatever value the owner has the live ceiling set to.
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp, 'probe.py')
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='',
                       TEST_PROBE=str(probe))
            for payload, expected in (({'ok': True, 'used_percent': 80}, 0),
                                      ({'ok': True, 'used_percent': 95}, 2),
                                      ({'ok': False, 'used_percent': 1}, 3),
                                      ({'ok': True, 'used_percent': -1}, 3),
                                      ({'ok': True}, 3)):
                probe.write_text('print(' + repr(json.dumps(payload)) + ')')
                result = subprocess.run(['bash', '-c',
                    'source routines/dispatch-detached.sh; '
                    'CODEX_RATELIMIT_SCRIPT="$TEST_PROBE"; CODEX_QUOTA_MAX_PERCENT=90; '
                    'codex_quota_gate'],
                    cwd=ROOT, env=env, capture_output=True)
                self.assertEqual(result.returncode, expected, payload)

    def test_missing_claude_windows_are_unknown(self):
        # DGN-1512: claude_quota_remaining() reads via usage-gate.py's
        # get_usage() (300s TTL cache), not a direct script probe -- so the
        # read-failure fixture is a fake get_usage() returning None, the
        # documented "read failed" contract that function raises on.
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp, 'probe.py')
            probe.write_text("def get_usage():\n    return None\n")
            env = dict(os.environ, DISPATCH_TEST='1', TEST_PROBE=str(probe))
            result = subprocess.run(['bash', '-c',
                'source routines/dispatch-detached.sh; '
                'USAGE_GATE_PY="$TEST_PROBE"; claude_quota_remaining'],
                cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(result.returncode, 1)

    def test_record_failure_stops_selection(self):
        env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='claude-code')
        result = subprocess.run(['bash', '-c', 'source routines/dispatch-detached.sh; '
                                 'record_runtime_decision() { return 1; }; select_default_runtime'],
                                cwd=ROOT, env=env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
