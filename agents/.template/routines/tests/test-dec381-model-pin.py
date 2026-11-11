#!/usr/bin/env python3
# test-lane: manual devkit-owned suite; run against extracted-devkit/framework
"""DEC-381: model pin accountability and one shared local machine budget.

Offline function and spawn-path fixtures. The spawn fixture stops after
writing registration artifacts, before any worker or notification is launched.
"""
import json
import os
import sys
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

ROOT = Path(__file__).resolve().parents[2]
PRELUDE = r'''
source "$TEST_ROOT/routines/dispatch-detached.sh"
WORKSPACE="$TEST_TMP"
SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"
# Exercise the missing-host legacy counter fallback without a production opt-out.
host_dispatch_on() { return 1; }
DISPATCH_DIR="$TEST_TMP/dispatch"
DECISION_DIR="$TEST_TMP/decisions"
ACTIVE_COUNT_FILE="$DISPATCH_DIR/active-count"
ACTIVE_COUNT_FILE_CODEX="$DISPATCH_DIR/active-count.codex"
ACTIVE_COUNT_FILE_CLOUD="$DISPATCH_DIR/active-count.cloud"
ACTIVE_COUNT_FILE_API="$DISPATCH_DIR/active-count.claude-api"
COUNT_LOCK_DIR="$DISPATCH_DIR/.count.lock"
mkdir -p "$DISPATCH_DIR"
TASK_FILE="$TEST_TMP/task.md"
LABEL=pin-fixture; BOTTLENECK=accuracy
BOTTLENECK_WHY='Verify the given cases against their expected results'
READ_ONLY=1
claude_usage_snapshot() { printf '%s' "$TEST_USAGE"; }
codex_quota_gate() {
  CODEX_USED_PERCENT=2; CODEX_RESETS_AT=$(( $(date +%s) + 36000 ))
  CODEX_QUOTA_NOTE='codex fixture: 2% used, 98% remaining'
  return "${TEST_QUOTA_RC:-0}"
}
codex_hook_gate() { return "${TEST_HOOK_RC:-0}"; }
api_pool_budget() { return 1; }
api_pool_verdict() { printf 'keep\tapi_off\tfixture\n'; }
reset_pass_specs() { :; }
reset_pass_spec() { :; }
reset_pass_review() { :; }
cloud_first_check() { return 1; }
claude_quota_remaining() { echo '80 80'; }
codex_quota_remaining() { echo 98; }
claude_hours_left() { return 1; }
reset_pass_mode() { printf 'none\t-\t-\tfixture\n'; }
machine_snapshot() { echo '16.1 8 16000'; }
test_slots_held() { echo '0 2'; }
'''


class PinGate(unittest.TestCase):
    def run_case(self, body, args=(), extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            usage = {'usage': {'five_hour_util': 50, 'seven_day_util': 70,
                              'five_hour_resets_at': now + 7200,
                              'seven_day_resets_at': now + 3 * 86400,
                              'fetched_at': now}, 'history': []}
            Path(tmp, 'task.md').write_text('Why: Prevent silent model vetoes.\nWhat: Verified dispatcher.\n')
            Path(tmp, 'ledger.sh').write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$TEST_TMP/ledger-row"\n')
            Path(tmp, 'ledger.sh').chmod(0o755)
            env = {k: v for k, v in os.environ.items()
                   if k == 'DOGANY_RESOURCE_SETTINGS_DIR' or (not k.startswith(('GIT_CONFIG_', 'DOGANY_', 'DGN_', 'PYTHON')))}
            env.update(DISPATCH_TEST='1', TEST_ROOT=str(ROOT), TEST_TMP=tmp,
                       TEST_USAGE=json.dumps(usage), DOGANY_DISPATCH_RUNTIME='',
                       DOGANY_DISPATCH_MACHINE_PRESSURE='off',
                       DOGANY_SHARED_HOME=str(Path(tmp, 'shared')),
                       DOGANY_USAGE_STATE_DIR=str(Path(tmp, 'usage')))
            env.update(extra or {})
            if 'do_spawn\n' in body:
                subprocess.run([sys.executable, str(ROOT / 'routines/tests/fixtures/stage-why.py'), tmp], check=True)
                body = 'WHY_ID=why-9999\n' + body
            r = subprocess.run(['bash', '-c', PRELUDE + body, 'fixture', *args],
                               env=env, cwd=tmp, text=True, capture_output=True)
            # DGN-2342: redirecting DISPATCH_DIR alone left WORKSPACE at the
            # framework checkout, so resolve_return_route opened its lock there.
            self.assertFalse((ROOT / '.telegram_bot/po-seat').exists())
            self.assertFalse((ROOT.parents[1] / '.telegram_bot').exists())
            if 'do_spawn\n' in body:
                self.assertTrue(Path(tmp, '.telegram_bot/po-seat/publication.lock').is_file())
            artifacts = {str(p.relative_to(tmp)): p.read_text() for p in Path(tmp).rglob('*')
                         if p.is_file() and (p.name in ('run.env', 'status.json', 'ledger-row', 'model-why.roundtrip', 'model-display')
                                             or p.parent.name == 'decisions')}
            return r, artifacts

    # The gate fires only on true removal: the pin leaves a runtime with no
    # candidate model (claude-code accuracy row is sonnet,opus: fable removes
    # it). Codex is never a removal -- its axis binding supplies the model.
    # RUNTIME=codex is preset to model a pinned run that landed away from the
    # runtime the unpinned policy prefers (claude-code, codex 80% used).
    HEADROOM = 'codex_quota_gate() { CODEX_USED_PERCENT=80; CODEX_RESETS_AT=$(( $(date +%s) + 518400 )); }\n'

    def gate(self, args, pre='', rt='codex'):
        return self.run_case(self.HEADROOM + pre + 'RUNTIME=%s; REQUESTED_MODEL="$MODEL"; rc=0; model_pin_gate || rc=$?; echo "RC=$rc"' % rt, args)

    def test_true_removal_with_headroom_refused(self):
        r, files = self.gate(['--model', 'fable'])
        self.assertIn('RC=1', r.stdout, r.stdout + r.stderr)
        for text in ('REFUSED', 'vetoes claude-code', 'headroom', 'Drop --model', '--model-why'):
            self.assertIn(text, r.stderr)
        self.assertEqual(json.loads(next(iter(files.values())))['reason_code'], 'model_pin_headroom')

    def test_true_removal_with_reason_proceeds(self):
        r, _ = self.gate(['--model', 'fable', '--model-why', 'paired evidence needs fable'])
        self.assertIn('RC=0', r.stdout, r.stdout + r.stderr)
        self.assertNotIn('REFUSED', r.stderr)

    def test_codex_mapping_is_not_a_veto(self):
        r, _ = self.run_case('select_default_runtime; echo "PICK=$RUNTIME"', ['--model', 'opus'])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('PICK=codex', r.stdout)
        self.assertNotIn('REFUSED', r.stderr)

    def test_unpinned_selects_codex(self):
        r, _ = self.run_case('select_default_runtime; echo "PICK=$RUNTIME"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('PICK=codex', r.stdout)
        self.assertNotIn('model_pin_veto', r.stdout)

    def test_explicit_pin_survives_loss_pick_to_codex(self):
        r, _ = self.run_case(r'''
loss_select 0
[[ "$LOSS_PICK" == codex && "$LOSS_CODEX_MODEL" == fable && "$LOSS_CODEX_MODEL_SOURCE" == explicit ]]
# Force the headroom path to disagree so the scored pick must replace it.
runtime_headroom_verdict() { printf 'claude-code\tfixture_headroom\tfixture\n'; }
select_default_runtime
printf 'PICK=%s MODEL=%s CODEX=%s SOURCE=%s DETAIL=%s\n' \
  "$RUNTIME" "$MODEL" "$CODEX_MODEL" "$MODEL_SOURCE" "$RUNTIME_SELECTION_DETAIL"
''', ['--model', 'fable'])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('PICK=codex MODEL=fable CODEX=fable SOURCE=explicit', r.stdout)

    def test_explicit_pin_survives_slot_fallback_to_codex(self):
        r, _ = self.run_case(r'''
REQUESTED_MODEL="$MODEL"; RUNTIME=claude-code; MODEL=opus; MODEL_SOURCE=binding
slot_candidate_gate codex
printf 'MODEL=%s CODEX=%s SOURCE=%s NOTE=%s\n' \
  "$MODEL" "$CODEX_MODEL" "$MODEL_SOURCE" "$SLOT_CAND_NOTE"
''', ['--model', 'fable'])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('MODEL=fable CODEX=fable SOURCE=explicit', r.stdout)
        self.assertIn('source=explicit', r.stdout)

    def test_pin_removes_nothing(self):
        r, _ = self.gate(['--model', 'opus'])
        self.assertIn('RC=0', r.stdout, r.stdout + r.stderr)
        self.assertNotIn('--model-why', r.stdout + r.stderr)

    def test_less_headroom_does_not_require_reason(self):
        r, _ = self.gate(['--model', 'fable'], rt='claude-code')
        self.assertIn('RC=0', r.stdout, r.stdout + r.stderr)
        self.assertNotIn('REFUSED', r.stderr)

    def test_reason_validation(self):
        for reason in ('too short', 'ten chars | pipe', 'ten chars\nnewline', 'ten chars\rcarriage'):
            with self.subTest(reason=reason):
                r, _ = self.run_case('validate_model_why', ['--model', 'opus', '--model-why', reason])
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def spawn_case(self, args):
        return self.run_case(r'''
# Keep real argument validation, selection, binding, serialization and ledger
# registration; replace only platform/spawn prerequisites and external readers.
resolve_owner_lang() { echo en; }
print_calibration() { :; }
explicit_runtime_cloud_gate() { :; }
cloud_first_select() { return 1; }
observe_sandbox_available() { :; }
render_observe_profile() { :; }
observe_profile_selfcheck() { :; }
plan_dir_selfcheck() { :; }
run_gates() { :; }
ensure_ledger() { :; }
LEDGER_UPDATE="$TEST_TMP/ledger.sh"
python3() {
  if [[ "$1" == "$SCRIPT_DIR/terminal-state-ledger.py" ]]; then
    write_status_json running 0 silent
    # Round-trip the actual run.env in a new shell, including quoting.
    bash -c 'source "$1"; printf "%s" "$MODEL_WHY"' fixture "$RUNDIR/run.env" > "$TEST_TMP/model-why.roundtrip"
    owner_model_display > "$TEST_TMP/model-display"
    exit 0
  fi
  command python3 "$@"
}
do_spawn
''', args)

    def test_reason_recorded_verbatim_before_spawn(self):
        reason = 'Need this model for "paired" evidence; keep $literal and `quoted` text.'
        r, files = self.spawn_case(['--model', 'fable', '--model-why', reason])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(files['model-why.roundtrip'], reason)
        run_env = next(v for k, v in files.items() if k.endswith('/run.env'))
        self.assertIn('REQUESTED_MODEL=fable\n', run_env)
        self.assertIn('MODEL=fable\n', run_env)
        self.assertIn('CODEX_MODEL=fable\n', run_env)
        self.assertIn('MODEL_SOURCE=explicit\n', run_env)
        self.assertEqual(files['model-display'].strip(), 'GPT 6 Astra')
        status = next(json.loads(v) for k, v in files.items()
                      if k.endswith('/status.json'))
        self.assertEqual(status['model_why'], reason)
        self.assertIn('model_why=' + reason, files['ledger-row'])
        self.assertIn('runtime selection:', r.stdout)

    def test_unpinned_spawn_keeps_axis_binding(self):
        r, files = self.spawn_case([])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        status = next(json.loads(v) for k, v in files.items()
                      if k.endswith('/status.json'))
        self.assertEqual(status['requested_model'], '')
        self.assertEqual(status['model'], 'opus')
        self.assertEqual(status['codex_model'], 'opus')
        self.assertEqual(status['model_source'], 'binding')
        self.assertEqual(files['model-display'].strip(), 'GPT 6.1 Sol')

    def test_shared_capacity_freed_slot_follows_score(self):
        r, _ = self.run_case(r'''
printf '3\n' > "$ACTIVE_COUNT_FILE"
compute_dynamic_cap claude-code
printf 'CLAUDE_CAP=%s\n' "$DYNAMIC_CAP_VALUE"
compute_dynamic_cap codex
printf 'CODEX_CAP=%s\n' "$DYNAMIC_CAP_VALUE"
active_count_incr codex && exit 40
# One shared reading survives the fallback walk and the freeing of a slot.
# No invented per-runtime zero or another machine read at a different load.
machine_snapshot() { echo 'unexpected second machine read' >&2; return 1; }
active_count_decr claude-code
select_default_runtime || exit $?
active_count_incr "$RUNTIME" || exit 41
printf 'PICK=%s TOTAL=%s CODEX=%s\n' "$RUNTIME" "$(local_active_count)" "$(read_active_count codex)"
active_count_incr claude-api && exit 42
exit 0
''')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for text in ('CLAUDE_CAP=3', 'CODEX_CAP=3', 'PICK=codex TOTAL=3 CODEX=1'):
            self.assertIn(text, r.stdout)
        self.assertNotIn('second machine read', r.stderr)

    def test_shared_total_includes_api_but_not_cloud(self):
        r, _ = self.run_case(r'''
printf '2\n' > "$ACTIVE_COUNT_FILE"
printf '1\n' > "$ACTIVE_COUNT_FILE_API"
printf '7\n' > "$ACTIVE_COUNT_FILE_CLOUD"
machine_workers
printf 'CAP=%s TOTAL=%s\n' "$MACHINE_CAP" "$(local_active_count)"
''')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('CAP=3 TOTAL=3', r.stdout)


if __name__ == '__main__':
    unittest.main()
