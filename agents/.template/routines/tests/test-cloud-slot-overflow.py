#!/usr/bin/env python3
# test-lane: manual devkit-owned suite; run against extracted-devkit/framework
# test-component: runtime-selector
# test-tier: 1
# test-prereq: fixture-repo, subprocess, tmp-files
# test-new-component: real spawn/admission artifact boundary with a foreground worker transport; dispatch-flex covers run_gates only and host-dispatcher covers the Python host ledger
# promise: a full cloud-credit lane overflows to the next eligible cloud lane before queueing
# promise: full cloud lanes queue, and explicit runtime pins never overflow
# promise: cloud overflow persists its direction and capacity reason in run.env, status.json and the running ledger
"""Foreground dispatch fixtures: real selector, host admission and spawn records.

Only provider/account facts and worker transport are stubbed; no worker,
notification, remote write or live provider request is made.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('dispatch_flex', HERE / 'test-dgn1975-dispatch-flex.py')
flex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flex)


class CloudOverflow(unittest.TestCase):
    def fixture(self, tmp, *, full=(), pin='', order='', extra='', spawn=False,
                real_credit_cap=False):
        # Use the shared scrubber before sourcing dispatch. In particular keep
        # the runner's budget tokens and GIT_CONFIG_* push guard, and isolate
        # usage state as well as the host's queue, leases and provider knobs.
        isolated = subprocess.check_output([
            'bash', '-c', 'source "$1"; isolate_dispatch_env "$2"; env -0',
            'fixture', str(HERE / 'fixtures/dispatch-env.sh'), tmp], text=True, timeout=30)
        fixture_env = dict(row.split('=', 1) for row in isolated.split('\0') if row)
        repo = flex.make_repo(tmp)
        bins = Path(tmp, 'bin')
        bins.mkdir()
        for cli in ('codex', 'claude'):
            path = bins / cli
            path.write_text('#!/bin/sh\nexit 99\n')
            path.chmod(0o755)
        Path(tmp, 'task.md').write_text('Why: fix the widget bug.\nWhat: edit the widget implementation.\n')
        ledger = Path(tmp, 'ledger.sh')
        ledger.write_text('#!/bin/bash\nexec bash "$TEST_ROOT/routines/lib/ledger-update.sh" "$@" --ledger "$TEST_TMP/ledger.md"\n')
        ledger.chmod(0o755)
        # compute_dynamic_cap invokes cloud_credit_capacity.py in a child:
        # shell usage stubs cannot supply its balance. Exercise the real
        # balance/burn/expiry calculation with one affordable run ($1.50/$1).
        Path(tmp, 'balance.json').write_text(json.dumps({'iguana_necktie': {
            'limit_dollars': 250, 'used_dollars': 248.5,
            'remaining_dollars': 1.5, 'resets_at': '2099-11-06T00:00:00Z'}}))
        usage = Path(tmp, 'usage.sh')
        usage.write_text('#!/bin/sh\ncat "$TEST_TMP/balance.json"\n')
        receipt = Path(tmp, 'dispatch/history')
        receipt.mkdir(parents=True)
        (receipt / 'cloud-credit.json').write_text(json.dumps({
            'state': 'completed', 'spent_dollars': 1}))
        # Provider facts only. Capacity remains decided by the real host lock
        # and lane books, including leases owned by another fixture agent.
        body = f'''
WORKSPACE="$TEST_TMP/workspace"; mkdir -p "$WORKSPACE"
WORKTREE_BASE="$TEST_TMP/worktrees"
TASK_FILE="$TEST_TMP/task.md"; LABEL=overflow-fixture; DISPLAY_NAME="Widget fix"
BOTTLENECK=coverage; BOTTLENECK_WHY="fixture breadth work"
WT_REPO="{repo}"; WT_REPO_ARG="{repo}"; WT_SLUG=widget
REPO_CONTAINED=1; WRITE_REQUESTED=1; READ_ONLY=0
ACCESS_MODE=write; CREDIT_FIRST_SELECTED=1
MODEL=opus; MODEL_SOURCE=binding; REQUESTED_RUNTIME={pin or 'auto'}
RUNTIME=cloud-credit; NOTIFY=silent; PIN_WHY="fixture runtime pin stays pinned"
ORIG_ARGV=(--task "$TASK_FILE" --label "$LABEL" --worktree widget --worktree-repo "$WT_REPO" --repo-contained)
LEDGER_UPDATE="$TEST_TMP/ledger.sh"; LEDGER_FILE="$TEST_TMP/ledger.md"
claude_usage_snapshot() {{ return 1; }}
api_pool_budget() {{ return 1; }}
codex_quota_gate() {{ CODEX_USED_PERCENT=4; CODEX_RESETS_AT=$(( $(date +%s) + 3600 )); CODEX_QUOTA_NOTE="fixture quota"; return 0; }}
codex_hook_gate() {{ return 0; }}
cloud_credit_until() {{ echo 2099-11-05; }}
credit_first_check() {{ CREDIT_FIRST_NOTE="fixture credit available"; return 0; }}
cloud_preflight() {{ return 0; }}
reset_pass_review() {{ :; }}
host_account() {{ echo fixture-account; }}
print_calibration() {{ :; }}
resolve_dispatch_why() {{ DISPATCH_WHY="fixture widget fix"; }}
brief_why_what_check() {{ :; }}
brief_unit_gate_check() {{ :; }}
render_dispatch_prompt() {{ cat "$TASK_FILE"; }}
# Keep detach foreground: exercise the real status writer as a worker would.
detach_worker() {{ write_status_json running 0 silent; echo "$$"; }}
'''
        if real_credit_cap:
            body += '''compute_dynamic_cap cloud-credit
'''
        else:
            # Capacity arithmetic has its own assertion below.  The remaining
            # matrix covers admission/overflow, so do not repeat its external
            # meter process for every row (that made this fast suite scale with
            # host process-launch pressure and exceed the Mac's 90s budget).
            body += '''DYNAMIC_CAP_VALUE=1
HOST_CAP_VALUE=1
DYNAMIC_CAP_NOTE="cloud-credit: remaining=fixture burn=fixture"
'''
        body += '''
[[ "$DYNAMIC_CAP_VALUE" == 1 && "$HOST_CAP_VALUE" == 1 ]] || exit 95
echo "CREDIT_CAP=$HOST_CAP_VALUE $DYNAMIC_CAP_NOTE"
# The overflow matrix needs stable lane sizes, not repeated provider probes.
# Its one capacity case above already exercises the real credit calculation.
compute_dynamic_cap() {
  if [[ "$1" == cloud-credit ]]; then
    DYNAMIC_CAP_VALUE=1
  else
    DYNAMIC_CAP_VALUE=2
  fi
  HOST_CAP_VALUE="$DYNAMIC_CAP_VALUE"
  DYNAMIC_CAP_NOTE="$1: fixture capacity"
}
codex_cloud_candidate() { CODEX_CLOUD_NOTE="fixture Codex seat available"; return 0; }
'''
        if pin:
            body += 'PIN_RUNTIME_FLAG=1; CREDIT_FIRST_SELECTED=0\n'
        if order:
            body += f'LOSS_DONE=1; LOSS_ORDER="{order}"; LOSS_CLOUD_OK=1; CFC_TOP="{repo}"; CFC_BASE=main\n'
        for rt in full:
            cap = 1 if rt == 'cloud-credit' else 2
            body += f'''
host_call acquire --agent busy-{rt} --workspace "$WORKSPACE" --runtime {rt} --account fixture-account \\
  --cap {cap} --cap-note fixture-full --label busy-{rt} --pid "$$" --kind llm || exit 91
'''
            # Codex and routine cloud fixtures use cap 2, so fill both slots.
            if cap == 2:
                body += f'host_call acquire --agent busy2-{rt} --workspace "$WORKSPACE" --runtime {rt} --account fixture-account --cap 2 --cap-note fixture-full --label busy2-{rt} --pid "$$" --kind llm || exit 92\n'
        body += extra + '\n'
        if spawn:
            # Positive spawn cases enter through the real Why admission door.
            subprocess.run([sys.executable, str(HERE / 'fixtures/stage-why.py'),
                            str(Path(tmp, 'workspace'))], check=True)
            body += 'WHY_ID=why-9999\ndo_spawn\n'
        else:
            body += '''
select_default_runtime || exit 93
rc=0; run_gates || rc=$?
echo "RC=$rc RUNTIME=$RUNTIME ACCESS_MODE=$ACCESS_MODE WT_SLUG=$WT_SLUG MODEL=$MODEL"
echo "FALLBACK=$RUNTIME_FALLBACK_DIRECTION"
echo "CODEX_CLOUD_ACTIVE=$(read_active_count codex-cloud)"
'''
        env = {'PATH': str(bins) + os.pathsep + os.environ['PATH'],
               'CLOUD_CREDIT_USAGE_SH': str(usage),
               'DOGANY_CODEX_CLOUD_ENV_ID': 'fixture-env', 'DOGANY_CLOUD_FIRST_UNTIL': '2099-11-05',
               # Fixture-owned sizes for the other two lanes. The credit cap
               # must still be derived from its meter, ignoring this override.
               'DOGANY_DISPATCH_MAX_ACTIVE': '2', 'DOGANY_DISPATCH_MAX_ACTIVE_CLOUD': '2',
               'DOGANY_TSL_LEDGER': str(Path(tmp, 'terminal-ledger.json')),
               'DOGANY_TSL_INBOX': str(Path(tmp, 'inbox')),
               'DOGANY_BOARD_REFRESH': '0',
               'DOGANY_DISPATCH_MACHINE': '0.5 8 16000 0',
               # Owner 2026-11-02: the credit pool ships off; these overflow
               # cases exercise an enabled credit lane.
               'DOGANY_CLOUD_CREDIT_POOL': 'on'}
        scenario = Path(tmp, 'scenario.sh')
        scenario.write_text(flex.PRELUDE + body)
        fixture_env.update(env, DISPATCH_TEST='1', TEST_ROOT=str(flex.ROOT), TEST_TMP=tmp,
                           DOGANY_CLOUD_ENV_ID='env_TEST01', DOGANY_DISPATCH_MACHINE_PRESSURE='off',
                           DOGANY_SHARED_HOME=str(Path(tmp, 'shared')))
        result = subprocess.run(['bash', str(scenario)], env=fixture_env, cwd=tmp,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        decisions = [p for p in Path(tmp, 'decisions').glob('rtd-*.json')
                     if not p.name.endswith('.replay.json')]
        self.assertEqual(len(decisions), 1, result.stdout + result.stderr)
        return result, json.loads(decisions[0].read_text())

    def test_credit_with_room_admits_on_original_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, decision = self.fixture(tmp, real_credit_cap=True)
            self.assertIn('RC=0 RUNTIME=cloud-credit', result.stdout, result.stderr)
            self.assertIn('CREDIT_CAP=1 cloud-credit: remaining=$1.50/$250.00 burn=$1.000/run',
                          result.stdout)
            self.assertIn('burn basis: recent p75 of 1 billing receipts', result.stdout)
            self.assertEqual(decision['selected_runtime'], 'cloud-credit')
            self.assertEqual(decision['fallback_direction'], '')

    def test_credit_cap_spawns_on_codex_cloud_with_all_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, decision = self.fixture(tmp, full=('cloud-credit',), spawn=True)
            self.assertIn('[dispatch] spawned:', result.stdout, result.stderr)
            self.assertEqual(decision['selected_runtime'], 'codex-cloud', decision)
            self.assertEqual(decision['fallback_direction'], 'cloud-credit->codex-cloud')
            self.assertIn('lane cloud-credit@fixture-account at cap 1/1', decision['fallback_note'])
            runs = list(Path(tmp, 'dispatch').glob('dsp-*/run.env'))
            self.assertEqual(len(runs), 1, result.stdout + result.stderr)
            run = runs[0]
            env = subprocess.run(['bash', '-c', 'source "$1"; printf "%s\\n" "$RUNTIME" "$RUNTIME_FALLBACK_DIRECTION" "$RUNTIME_FALLBACK_NOTE"',
                                  'fixture', str(run)], capture_output=True, text=True, check=True).stdout
            self.assertIn('codex-cloud\ncloud-credit->codex-cloud\n', env)
            status = json.loads(run.with_name('status.json').read_text())
            self.assertEqual(status['runtime'], 'codex-cloud')
            self.assertEqual(status['runtime_fallback_direction'], 'cloud-credit->codex-cloud')
            self.assertIn('at cap 1/1', status['runtime_fallback_note'])
            ledger = Path(tmp, 'ledger.md').read_text()
            self.assertIn('| running |', ledger)
            self.assertIn('overflow=cloud-credit->codex-cloud', ledger)
            self.assertIn('at cap 1/1', ledger)

    def test_routine_worker_refresh_preserves_overflow_on_real_ledger_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            extra = '''
# This fixture exercises the routine transport after credit overflow; the
# preferred Codex seat is unavailable, so fresh re-selection reaches it.
codex_cloud_candidate() { CODEX_CLOUD_NOTE='fixture Codex seat unavailable'; return 1; }
cloud_routine_call() {
  printf 'trigger_id=fixture-trigger\\ndisabled=yes\\nsession_id=fixture-session\\nview=https://example.invalid/session\\n' > "$RUNDIR/cloud-routine.out"
}
# End this foreground worker fixture after its real session-row refresh.
cloud_disable_routine() { exit 0; }
detach_worker() { write_status_json running 0 silent; (do_worker_cloud); echo "$$"; }
'''
            result, decision = self.fixture(tmp, full=('cloud-credit',),
                                            order='cloud-credit,cloud', extra=extra, spawn=True)
            self.assertIn('[dispatch] spawned:', result.stdout, result.stderr)
            self.assertEqual(decision['fallback_direction'], 'cloud-credit->cloud')
            ledger = Path(tmp, 'ledger.md').read_text()
            self.assertIn('| running |', ledger)
            self.assertIn('session=fixture-session', ledger)
            self.assertIn('overflow=cloud-credit->cloud', ledger)
            self.assertIn('at cap 1/1', ledger)

    def test_every_cloud_lane_full_queues_on_original_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, decision = self.fixture(tmp, full=('cloud-credit', 'codex-cloud', 'cloud', 'claude-code'))
            self.assertIn('RC=3 RUNTIME=cloud-credit', result.stdout, result.stderr)
            self.assertEqual(decision['status'], 'queued', decision)
            self.assertEqual(decision['fallback_direction'], '')

    def test_other_cloud_runtime_pins_stay_queued_at_cap(self):
        for runtime in ('cloud', 'codex-cloud'):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as tmp:
                extra = '''
CREDIT_FIRST_SELECTED=0; CLOUD_FIRST_SELECTED=0
REPO_PATH="$WT_REPO"; WT_SLUG=""; WRITE_REQUESTED=0; MODEL=""
if [[ "$REQUESTED_RUNTIME" == codex-cloud ]]; then codex_cloud_check || exit 94; fi
'''
                result, decision = self.fixture(tmp, full=(runtime,), pin=runtime,
                                                order='cloud,codex-cloud', extra=extra)
                self.assertIn(f'RC=3 RUNTIME={runtime}', result.stdout, result.stderr)
                self.assertEqual(decision['status'], 'queued')
                self.assertEqual(decision['fallback_direction'], '')

    def test_pin_at_cap_queues_without_overflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, decision = self.fixture(tmp, full=('cloud-credit',), pin='cloud-credit',
                                            order='cloud-credit,codex-cloud,cloud')
            self.assertIn('RC=3 RUNTIME=cloud-credit', result.stdout, result.stderr)
            self.assertEqual(decision['status'], 'queued', decision)
            self.assertEqual(decision['fallback_direction'], '')

    def test_walk_respects_ranked_cloud_order_and_skips_local_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, decision = self.fixture(tmp, full=('cloud-credit',),
                                            order='cloud-credit,codex,cloud,codex-cloud')
            self.assertIn('RC=0 RUNTIME=cloud', result.stdout, result.stderr)
            self.assertEqual(decision['fallback_direction'], 'cloud-credit->cloud')
            self.assertIn('order: cloud > codex-cloud', decision['fallback_note'])

    def test_host_full_codex_cloud_falls_through_to_routine_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, decision = self.fixture(tmp, full=('cloud-credit', 'codex-cloud'),
                                            order='cloud-credit,codex-cloud,cloud')
            self.assertIn('RC=0 RUNTIME=cloud', result.stdout, result.stderr)
            self.assertEqual(decision['fallback_direction'], 'cloud-credit->cloud')
            self.assertIn('codex-cloud:', decision['fallback_note'])

    def test_failed_candidate_restores_original_shape_before_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, decision = self.fixture(tmp, full=('cloud-credit', 'codex-cloud'),
                                            order='cloud-credit,codex-cloud')
            self.assertIn('RC=3 RUNTIME=cloud-credit ACCESS_MODE=write WT_SLUG=widget MODEL=opus',
                          result.stdout, result.stderr)
            self.assertEqual(decision['fallback_direction'], '')

    def test_routine_cloud_cap_can_overflow_to_codex_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            extra = '''
CREDIT_FIRST_SELECTED=0; CLOUD_FIRST_SELECTED=1
cloud_first_convert
'''
            result, decision = self.fixture(tmp, full=('cloud',), order='cloud,codex-cloud', extra=extra)
            self.assertIn('RC=0 RUNTIME=codex-cloud', result.stdout, result.stderr)
            self.assertEqual(decision['fallback_direction'], 'cloud->codex-cloud')

    def test_failed_decision_record_releases_overflow_and_restores_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            extra = '''
eval "$(declare -f record_runtime_decision | sed '1s/record_runtime_decision/fixture_record_runtime_decision/')"
record_runtime_decision() {
  [[ "$2" != slot_cap_fallback ]] || return 1
  fixture_record_runtime_decision "$@"
}
'''
            result, decision = self.fixture(tmp, full=('cloud-credit',),
                                            order='cloud-credit,codex-cloud', extra=extra)
            self.assertIn('RC=3 RUNTIME=cloud-credit ACCESS_MODE=write WT_SLUG=widget MODEL=opus',
                          result.stdout, result.stderr)
            self.assertEqual(decision['status'], 'queued')
            self.assertEqual(decision['fallback_direction'], '')
            self.assertIn('CODEX_CLOUD_ACTIVE=0', result.stdout)

    def test_non_capacity_host_refusal_never_overflows(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, decision = self.fixture(tmp, order='cloud-credit,codex-cloud', extra='''
host_acquire() { HOST_FULL_NOTE="R3 resource ledger unavailable"; return 1; }
''')
            self.assertIn('RC=3 RUNTIME=cloud-credit', result.stdout, result.stderr)
            self.assertEqual(decision['fallback_direction'], '')
            self.assertIn('CODEX_CLOUD_ACTIVE=0', result.stdout)

    def test_effort_hint_alone_keeps_codex_cloud_eligible(self):
        # Lane autopilot always sends --effort; that hint must not veto
        # codex-cloud (owner 2026-11-02: lane runs all fell to cloud Opus).
        with tempfile.TemporaryDirectory() as tmp:
            extra = """
EFFORT=medium; EFFORT_SOURCE=explicit
CREDIT_FIRST_SELECTED=0; CLOUD_FIRST_SELECTED=1
cloud_first_convert
"""
            result, decision = self.fixture(tmp, full=('cloud',), order='cloud,codex-cloud', extra=extra)
            self.assertIn('RC=0 RUNTIME=codex-cloud', result.stdout, result.stderr)

    def test_model_pin_keeps_codex_cloud_ineligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, decision = self.fixture(tmp, full=('cloud-credit',),
                                            order='cloud-credit,codex-cloud',
                                            extra='MODEL_SOURCE=explicit')
            self.assertIn('RC=3 RUNTIME=cloud-credit', result.stdout, result.stderr)
            self.assertEqual(decision['fallback_direction'], '')
            self.assertIn('omit caller --model pins', decision['detail'])

    def test_removing_overflow_reds_the_spawn_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_root = flex.ROOT
            routines = Path(tmp, 'routines')
            routines.mkdir()
            (routines / 'lib').symlink_to(original_root / 'routines/lib', target_is_directory=True)
            source = (original_root / 'routines/dispatch-detached.sh').read_text()
            marker = 'slot_fallback_walk() {\n'
            self.assertEqual(source.count(marker), 1)
            mutant = source.replace(marker, marker + '  case "$RUNTIME" in cloud|cloud-credit|codex-cloud) return 1 ;; esac\n', 1)
            (routines / 'dispatch-detached.sh').write_text(mutant)
            (routines / 'stage-judge.py').write_bytes(
                (original_root / 'routines/stage-judge.py').read_bytes())
            try:
                flex.ROOT = Path(tmp)
                with self.assertRaisesRegex(AssertionError, 'spawned'):
                    self.test_credit_cap_spawns_on_codex_cloud_with_all_records()
            finally:
                flex.ROOT = original_root


if __name__ == '__main__':
    unittest.main()
