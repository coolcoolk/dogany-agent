#!/usr/bin/env python3
# test-lane: fast
"""DGN-1963 (dec-339): claude-api runtime on the monthly API credit pool.

Offline: fixture usage, a temp ledger and a temp key file -- no workers,
keychain reads, live quota RPCs or network. Covers the selection rules a-d as
pure functions (lib/api_credit_pool.py) and through select_default_runtime(),
the ledger (per-run cost, month reset), the explicit-request gate, the key
never reaching output, and the workbench label.
"""
import calendar
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

os.environ['TZ'] = 'UTC'
time.tzset()

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / 'routines'
sys.path.insert(0, str(ROUTINES / 'lib'))
import api_credit_pool as pool  # noqa: E402
import model_display as md  # noqa: E402

MID = calendar.timegm((2026, 10, 9, 12, 0, 0))     # 27% of October elapsed
END = calendar.timegm((2026, 10, 30, 12, 0, 0))    # last 3 days (29-31)
EVE = calendar.timegm((2026, 10, 28, 12, 0, 0))    # day before the window
NOV = calendar.timegm((2026, 11, 1, 0, 30, 0))     # the 1st: pool reset
H = 3600
KEY = 'sk-ant-api03-FIXTURE-never-print-me'


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(epoch))


def usage(u5, now, r5=2 * H, u7=40):
    return {'five_hour_util': u5, 'five_hour_resets_at': iso(now + r5),
            'seven_day_util': u7, 'seven_day_resets_at': iso(now + 3 * 86400),
            'fetched_at': now}


def sample(age, u5, now, r5=2 * H):
    return {'fetched_at': now - age, 'five_hour_util': u5,
            'five_hour_resets_at': iso(now + r5)}


def write_ledger(path, rows):
    with open(path, 'w') as fh:
        for month, cost in rows:
            fh.write(json.dumps({'ts': 0, 'month': month, 'runid': 'r',
                                 'model': 'opus', 'cost_usd': cost}) + '\n')


class PurePolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, 'ledger.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, now, spent=0.0, base='codex', verdict='claude_5h_velocity_spill',
               key=True, u=None, hist=(), budget=100.0):
        write_ledger(self.ledger, [(time.strftime('%Y-%m', time.gmtime(now)), spent)])
        return pool.decide(budget, self.ledger, base, verdict, key, u, list(hist),
                           None, now)

    def test_a_pacing_allowance(self):
        st = pool.pool_state(100.0, self.ledger, None, MID)
        self.assertAlmostEqual(st['frac'], 8.5 / 31, places=4)
        self.assertAlmostEqual(st['allowed'], 100 * 8.5 / 31 + 3.0, places=4)
        # 5h overflow, but spend is past the paced allowance -> keep the pick.
        rt, v, d = self.decide(MID, spent=40.0, u=usage(70, MID),
                               hist=[sample(1800, 40, MID)])
        self.assertEqual((rt, v), ('codex', 'api_pacing_hold'))
        self.assertIn('allowed=$30.42', d)

    def test_b_overflow_takes_api_before_codex(self):
        # 40% -> 70% in 30 min = 1%/min; 120 min to reset -> proj 190%.
        rt, v, d = self.decide(MID, spent=10.0, u=usage(70, MID),
                               hist=[sample(1800, 40, MID)])
        self.assertEqual((rt, v), ('claude-api', 'api_5h_overflow_paced'))
        self.assertIn('proj5h=190%', d)
        self.assertIn('claude-api before codex', d)
        # ... and ahead of a claude-code pick too (both_tight kept claude).
        rt, v, _ = self.decide(MID, spent=10.0, base='claude-code',
                               verdict='both_tight_less_tight', u=usage(70, MID),
                               hist=[sample(1800, 40, MID)])
        self.assertEqual(rt, 'claude-api')

    def test_b_no_burn_data_uses_gate_line(self):
        rt, v, d = self.decide(MID, spent=0.0, u=usage(92, MID))
        self.assertEqual((rt, v), ('claude-api', 'api_5h_overflow_paced'))
        self.assertIn('burn=n/a', d)
        # No overflow and at the pace line -> the pick stands.
        rt, v, _ = self.decide(MID, spent=31.0, base='claude-code',
                               verdict='claude_headroom_on_pace', u=usage(30, MID))
        self.assertEqual((rt, v), ('claude-code', 'api_not_needed'))

    def test_b_unknown_usage_is_no_overflow(self):
        rt, v, d = self.decide(MID, spent=31.0, u=None)
        self.assertEqual((rt, v), ('codex', 'api_not_needed'))
        self.assertIn('unreadable', d)
        # The pace rule reads the pool's own books, not the usage read.
        rt, v, _ = self.decide(MID, spent=5.0, u=None)
        self.assertEqual((rt, v), ('claude-api', 'api_pace_spend'))

    def test_c_month_end_burns_remainder(self):
        rt, v, d = self.decide(END, spent=60.0, base='claude-code',
                               verdict='claude_headroom_on_pace', u=usage(10, END))
        self.assertEqual((rt, v), ('claude-api', 'api_month_end_burn'))
        self.assertIn('$40.00 would be lost', d)
        # The day before the window: allowed = 100 x 27.5/31 + 3 = $91.71.
        rt, v, _ = self.decide(EVE, spent=95.0, base='claude-code',
                               verdict='claude_headroom_on_pace', u=usage(10, EVE))
        self.assertEqual(v, 'api_not_needed')
        rt, v, _ = self.decide(EVE, spent=60.0, base='claude-code',
                               verdict='claude_headroom_on_pace', u=usage(10, EVE))
        self.assertEqual(v, 'api_pace_spend')

    def test_c_codex_use_it_or_lose_it_still_wins(self):
        rt, v, _ = self.decide(END, base='codex', verdict='codex_use_it_or_lose_it',
                               u=usage(10, END))
        self.assertEqual((rt, v), ('codex', 'api_codex_use_it_or_lose_it'))

    def test_e_pace_spend_under_the_line(self):
        # Day 9, allowed $30.42, nothing spent, no overflow -> spend the pool.
        for base, verdict in (('claude-code', 'claude_headroom_on_pace'),
                              ('codex', 'claude_5h_velocity_spill')):
            rt, v, d = self.decide(MID, spent=0.0, base=base, verdict=verdict,
                                   u=usage(20, MID))
            self.assertEqual((rt, v), ('claude-api', 'api_pace_spend'))
            self.assertIn('spent=$0.00 < allowed=$30.42', d)
            self.assertIn('order: month-end burn > 5h overflow > '
                          'reset-pass subscription first > pace spend', d)
        rt, v, _ = self.decide(MID, spent=30.0, base='claude-code',
                               verdict='claude_headroom_on_pace', u=usage(20, MID))
        self.assertEqual((rt, v), ('claude-api', 'api_pace_spend'))

    def test_e_at_or_over_pace_keeps_base(self):
        for spent in (30.42, 31.0, 80.0):
            rt, v, d = self.decide(MID, spent=spent, base='claude-code',
                                   verdict='claude_headroom_on_pace', u=usage(20, MID))
            self.assertEqual((rt, v), ('claude-code', 'api_not_needed'), spent)
            self.assertIn('(at pace)', d)

    def test_e_held_claude_weekly_pass_subscription_first(self):
        held = '1|2026-10-23:weekly'
        def go(spec, base='claude-code', verdict='claude_headroom_on_pace',
               u=usage(20, MID), hist=(), spent=0.0, now=MID):
            write_ledger(self.ledger, [(time.strftime('%Y-%m', time.gmtime(now)), spent)])
            return pool.decide(100.0, self.ledger, base, verdict, True, u, list(hist),
                               None, now, claude_pass=spec)
        rt, v, d = go(held)
        self.assertEqual((rt, v), ('claude-code', 'api_pace_pass_first'))
        self.assertIn('subscription first', d)
        self.assertIn('claude pass x1', d)
        # full covers the weekly window too; a 5h-only or expired pass does not.
        self.assertEqual(go('1|2026-10-23:full')[1], 'api_pace_pass_first')
        self.assertEqual(go('1|2026-10-23:5h')[1], 'api_pace_spend')
        self.assertEqual(go('1|2026-10-01:weekly')[1], 'api_pace_spend')
        self.assertEqual(go('garbage')[1], 'api_pace_spend')
        # A codex pick is not taking from the subscription -> pace spend.
        self.assertEqual(go(held, base='codex', verdict='claude_5h_velocity_spill')[:2],
                         ('claude-api', 'api_pace_spend'))
        # Overflow and month end are unchanged by a held pass.
        self.assertEqual(go(held, u=usage(70, MID), hist=[sample(1800, 40, MID)])[1],
                         'api_5h_overflow_paced')
        self.assertEqual(go(held, now=END, u=usage(10, END))[1], 'api_month_end_burn')

    def test_pool_loss(self):
        # Loss score (runtime_loss.py): the dollars the month end takes at
        # the current spend rate, as a share of the budget x urgency weight.
        def loss(now, spent):
            write_ledger(self.ledger, [(time.strftime('%Y-%m', time.gmtime(now)), spent)])
            return pool.pool_loss(pool.pool_state(100.0, self.ledger, None, now), now)
        lost, share, txt = loss(MID, 0.0)          # nothing spent, 22 days left
        self.assertAlmostEqual(lost, 100.0)
        self.assertAlmostEqual(share, 100.0 * 7 / 22.5, places=1)
        self.assertIn('-> $100.00 lost at month end', txt)
        self.assertEqual(loss(MID, 40.0)[0], 0.0)  # ahead of pace: nothing lost
        lost, share, _ = loss(END, 60.0)           # month end: full weight
        self.assertAlmostEqual(share, lost, places=6)
        self.assertGreater(lost, 30.0)
        early = calendar.timegm((2026, 10, 2, 12, 0, 0))
        self.assertLess(loss(early, 0.0)[1], 5.0)   # young month: pace assumed
        self.assertIsNone(loss(MID, 100.0))         # spent: not a candidate

    def test_d_exhausted_missing_key_off(self):
        rt, v, _ = self.decide(END, spent=100.0, u=usage(95, END))
        self.assertEqual((rt, v), ('codex', 'api_budget_exhausted'))
        rt, v, _ = self.decide(MID, key=False, u=usage(95, MID))
        self.assertEqual((rt, v), ('codex', 'api_key_missing'))
        rt, v, _ = self.decide(MID, budget=None, u=usage(95, MID))
        self.assertEqual((rt, v), ('codex', 'api_pool_off'))
        self.assertIsNone(pool.parse_budget(''))
        self.assertIsNone(pool.parse_budget('0'))
        self.assertIsNone(pool.parse_budget('lots'))
        self.assertEqual(pool.parse_budget(' 50 '), 50.0)

    def test_ledger_record_and_monthly_reset(self):
        stream = os.path.join(self.tmp.name, 'stream.jsonl')
        with open(stream, 'w') as fh:
            fh.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            fh.write('not json\n')
            fh.write(json.dumps({'type': 'result', 'total_cost_usd': 1.25}) + '\n')
        self.assertEqual(pool.record(self.ledger, stream, 'run1', 'opus', MID), 1.25)
        self.assertEqual(pool.record(self.ledger, stream, 'run2', 'opus', MID + 60), 1.25)
        self.assertAlmostEqual(pool.month_spent(self.ledger, MID), 2.5)
        self.assertEqual(pool.month_spent(self.ledger, NOV), 0.0)
        # DGN-1978: events but neither a cost nor usage -> the model's median
        # run cost, estimated; only a stream with no event at all is no row.
        empty = os.path.join(self.tmp.name, 'empty.jsonl')
        Path(empty).write_text(json.dumps({'type': 'result'}) + '\n')
        self.assertEqual(pool.record(self.ledger, empty, 'run3', 'opus', MID), 1.25)
        Path(empty).write_text('')
        self.assertIsNone(pool.record(self.ledger, empty, 'run4', 'opus', MID))

    def test_status_cli_codes(self):
        write_ledger(self.ledger, [('2026-10', 100.0)])
        run = lambda b: subprocess.run(
            [sys.executable, str(ROUTINES / 'lib' / 'api_credit_pool.py'), 'status',
             '--budget', b, '--ledger', self.ledger, '--now', str(MID)],
            capture_output=True, text=True, env=dict(os.environ, TZ='UTC'))
        self.assertEqual(run('').returncode, 1)
        self.assertEqual(run('100').returncode, 2)
        self.assertEqual(run('200').returncode, 0)


def snapshot(u5, now, hist=()):
    return json.dumps({'usage': usage(u5, now), 'history': list(hist)})


class Selection(unittest.TestCase):
    """Through select_default_runtime(), same harness as the DGN-1874 suite."""

    def run_case(self, *, now, spent=0.0, budget='100', key=True, claude=None,
                 codex_used=60, extra='', env_extra=None, marker=None):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, 'ledger.jsonl')
            write_ledger(ledger, [(time.strftime('%Y-%m', time.gmtime(now)), spent)])
            if marker is not None:
                Path(tmp, 'exhausted.json').write_text(json.dumps(marker))
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='',
                       TEST_ROOT=str(ROOT), TEST_TMP=tmp, HOME=tmp, TZ='UTC', PLAN='',
                       API_CREDIT_MONTHLY_USD=budget, DOGANY_API_POOL_NOW=str(now))
            env.pop('ANTHROPIC_ADMIN_KEY_FILE', None)
            env.update(env_extra or {})
            snap = "printf '%s' '" + claude + "'" if claude else 'return 1'
            script = f'''source "$TEST_ROOT/routines/dispatch-detached.sh"
DISPATCH_DIR="$TEST_TMP/dispatch"
DECISION_DIR="$TEST_TMP/decisions"
API_CREDIT_LEDGER="{ledger}"
API_CREDIT_EXHAUSTED_MARKER="$TEST_TMP/exhausted.json"
API_CREDIT_RECONCILE_STATE="$TEST_TMP/reconcile.json"
API_CREDIT_INFLIGHT_DIR="$TEST_TMP/inflight"
LABEL=test
WT_REPO=/fixture/estate
MODEL=opus; BOTTLENECK=coverage; MODEL_SOURCE=explicit
REQUESTED_RUNTIME=auto
WT_SLUG=
api_key_present() {{ return {0 if key else 1}; }}
claude_quota_remaining() {{ echo '80 70'; }}
claude_usage_snapshot() {{ {snap}; }}
codex_quota_gate() {{ CODEX_USED_PERCENT={codex_used}; CODEX_RESETS_AT=$(( $(date +%s) + 302400 )); CODEX_QUOTA_NOTE='used={codex_used}'; return 0; }}
codex_hook_gate() {{ CODEX_HOOK_NOTE='fixture'; return 0; }}
select_default_runtime
echo "MODEL=$MODEL CODEX_MODEL=$CODEX_MODEL"
{extra}'''
            r = subprocess.run(['bash', '-c', script], env=env, text=True,
                               capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            recs = list(Path(tmp, 'decisions').glob('*.json'))
            self.assertEqual(len(recs), 1)
            return json.loads(recs[0].read_text()), r.stdout

    def test_b_overflow_spills_to_api_not_codex(self):
        now = time.time()
        doc, out = self.run_case(now=MID, claude=snapshot(92, now))
        self.assertEqual(doc['selected_runtime'], 'claude-api')
        self.assertEqual(doc['selection_reason'], 'api_5h_overflow_paced')
        self.assertIn('headroom=', doc['selection_detail'])
        self.assertIn('api_pool=api_5h_overflow_paced: api_pool month=2026-10 budget=$100.00',
                      doc['selection_detail'])
        # The codex tier model must not leak into the claude-api run.
        self.assertIn('MODEL=opus CODEX_MODEL=', out)
        self.assertIn('[dispatch] runtime selection: claude-api (api_5h_overflow_paced;', out)

    def test_a_pacing_hold_keeps_codex(self):
        now = time.time()
        doc, _ = self.run_case(now=MID, spent=50.0, claude=snapshot(92, now))
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('api_pool=api_pacing_hold', doc['selection_detail'])

    def test_c_month_end_prefers_api(self):
        now = time.time()
        doc, _ = self.run_case(now=END, spent=60.0, claude=snapshot(10, now))
        self.assertEqual(doc['selected_runtime'], 'claude-api')
        self.assertEqual(doc['selection_reason'], 'api_month_end_burn')

    def test_e_pace_spend_through_selection(self):
        now = time.time()
        doc, _ = self.run_case(now=MID, spent=0.0, claude=snapshot(10, now))
        self.assertEqual(doc['selected_runtime'], 'claude-api')
        self.assertEqual(doc['selection_reason'], 'api_pace_spend')
        doc, _ = self.run_case(now=MID, spent=40.0, claude=snapshot(10, now))
        self.assertNotEqual(doc['selected_runtime'], 'claude-api')
        self.assertIn('api_pool=api_not_needed', doc['selection_detail'])

    def test_e_held_pass_reaches_the_pool(self):
        now = time.time()
        doc, _ = self.run_case(now=MID, spent=0.0, claude=snapshot(10, now),
                               env_extra={'RESET_PASSES_CLAUDE': '1|2099-12-31:weekly'})
        self.assertEqual(doc['selected_runtime'], 'claude-code')
        self.assertIn('api_pool=api_pace_pass_first', doc['selection_detail'])
        self.assertIn('subscription first', doc['selection_detail'])

    def test_d_key_missing_silent_fallback(self):
        now = time.time()
        doc, _ = self.run_case(now=MID, key=False, claude=snapshot(92, now))
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('api_pool=api_key_missing', doc['selection_detail'])

    def test_d_exhausted_and_off(self):
        now = time.time()
        doc, _ = self.run_case(now=END, spent=100.0, claude=snapshot(92, now))
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('api_pool=api_budget_exhausted', doc['selection_detail'])
        doc, _ = self.run_case(now=MID, budget='', claude=snapshot(92, now))
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('api_pool=api_pool_off', doc['selection_detail'])


class KeyAndGate(unittest.TestCase):
    def bash(self, body, **env_extra):
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = os.path.join(tmp, 'key')
            Path(keyfile).write_text(KEY + '\n')
            ledger = os.path.join(tmp, 'ledger.jsonl')
            env = dict(os.environ, DISPATCH_TEST='1', TEST_ROOT=str(ROOT), TZ='UTC',
                       ANTHROPIC_API_KEY_FILE=keyfile, PLAN='', TEST_TMP=tmp, HOME=tmp)
            env.pop('ANTHROPIC_ADMIN_KEY_FILE', None)
            env.update(env_extra)
            script = ('source "$TEST_ROOT/routines/dispatch-detached.sh"\n'
                      'DISPATCH_DIR="$TEST_TMP/dispatch"\n'
                      f'API_CREDIT_LEDGER="{ledger}"\n'
                      'API_CREDIT_EXHAUSTED_MARKER="$TEST_TMP/exhausted.json"\n'
                      'API_CREDIT_RECONCILE_STATE="$TEST_TMP/reconcile.json"\n'
                      'uname() { echo Linux; }\n'
                      # no live usage read into the template workspace
                      'claude_usage_snapshot() { return 1; }\n' + body)
            return subprocess.run(['bash', '-c', script], env=env, text=True,
                                  capture_output=True)

    def test_key_read_from_file_and_never_echoed(self):
        r = self.bash('api_key_present && echo present\n'
                      'k="$(api_key_read)"; [[ "$k" == "' + KEY + '" ]] && echo match\n'
                      'api_pool_verdict codex claude_tight_codex_open\n',
                      API_CREDIT_MONTHLY_USD='100')
        self.assertIn('present', r.stdout)
        self.assertIn('match', r.stdout)
        self.assertNotIn(KEY, r.stdout + r.stderr)

    def test_policy_crash_reason_in_detail(self):
        # Under `IFS=$'\t' read <<< "$(...)"` (bash 3.2 leaks that IFS into
        # the callee) the verdict must still parse; a policy crash keeps the
        # base pick, stays off stderr and names its reason in the detail.
        r = self.bash('IFS=$\'\\t\' read -r rt v d <<< "$(api_pool_verdict codex x)"\n'
                      'echo "rt=$rt v=$v"\n'
                      'API_CREDIT_POOL_PY=/nonexistent/pool.py\n'
                      'api_pool_verdict codex x\n',
                      API_CREDIT_MONTHLY_USD='100', DOGANY_API_POOL_NOW=str(MID))
        self.assertIn('rt=claude-api v=api_pace_spend', r.stdout)
        self.assertIn('codex\tapi_pool_unavailable\tapi pool policy produced no verdict (', r.stdout)
        self.assertIn('nonexistent', r.stdout)
        self.assertEqual(r.stderr, '')
        # Nothing configured + unreadable policy = plain off (an install
        # without the pool module), not a crash report.
        r = self.bash('API_CREDIT_POOL_PY=/nonexistent/pool.py\n'
                      'api_pool_verdict codex x\n', API_CREDIT_MONTHLY_USD='')
        self.assertIn('codex\tapi_pool_off\t', r.stdout)

    def test_key_missing_file(self):
        r = self.bash('api_key_present || echo absent\n',
                      ANTHROPIC_API_KEY_FILE='/nonexistent/key')
        self.assertIn('absent', r.stdout)

    def test_explicit_gate(self):
        r = self.bash('api_pool_gate && echo ok\n', API_CREDIT_MONTHLY_USD='')
        self.assertNotIn('ok', r.stdout)
        self.assertIn('pool is off', r.stderr)
        r = self.bash('api_pool_gate && echo ok\n', API_CREDIT_MONTHLY_USD='100')
        self.assertIn('ok', r.stdout)
        r = self.bash('api_pool_gate && echo ok\n', API_CREDIT_MONTHLY_USD='100',
                      ANTHROPIC_API_KEY_FILE='/nonexistent/key')
        self.assertIn('key is missing', r.stderr)

    def test_lane_wiring(self):
        r = self.bash('active_count_file claude-api; effort_passed_to_runtime claude-api max && echo effort\n'
                      'tier_binding_lookup claude-api judgment\n')
        self.assertIn('active-count.claude-api', r.stdout)
        self.assertIn('effort', r.stdout)
        self.assertIn('opus,fable', r.stdout)


# ---- follow-up: budget from the plan, truth from the account ----

def stream_file(dirname, events, name='stream.jsonl'):
    path = os.path.join(dirname, name)
    with open(path, 'w') as fh:
        for ev in events:
            fh.write((ev if isinstance(ev, str) else json.dumps(ev)) + '\n')
    return path


CREDIT_RESULT = {'type': 'result', 'subtype': 'success', 'is_error': True,
                 'result': 'Credit balance is too low', 'total_cost_usd': 0}
CREDIT_ASSISTANT = {'type': 'assistant', 'error': 'billing_error',
                    'message': {'content': [{'type': 'text', 'text':
                                'API Error: 400 {"type":"error","error":{"type":'
                                '"invalid_request_error","message":"Your credit balance '
                                'is too low to access the Anthropic API."}}'}]}}
PROSE = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text':
         'Implemented the "credit balance is too low" detector (billing_error).'}]}}
OK_RESULT = {'type': 'result', 'is_error': False, 'result': 'done; credit balance '
             'is too low handling added', 'total_cost_usd': 0.5}


class PlanBudget(unittest.TestCase):
    def test_plan_table_and_override(self):
        self.assertEqual(pool.resolve_budget('', 'max_5x'), (100.0, 'plan max_5x'))
        self.assertEqual(pool.resolve_budget('', 'max_20x')[0], 200.0)
        # DGN-1978: an override that differs from the plan's credit says so;
        # one that agrees reads as the plan, confirmed.
        self.assertEqual(pool.resolve_budget('150', 'max_5x'),
                         (150.0, 'API_CREDIT_MONTHLY_USD override (plan max_5x carries $100)'))
        self.assertEqual(pool.resolve_budget('200', 'max_20x'),
                         (200.0, 'plan max_20x (API_CREDIT_MONTHLY_USD agrees)'))
        self.assertEqual(pool.resolve_budget('150', ''),
                         (150.0, 'API_CREDIT_MONTHLY_USD override'))
        self.assertEqual(pool.resolve_budget('0', 'max_20x')[0], None)   # explicit off
        self.assertEqual(pool.resolve_budget('', 'team_premium', '4', '25')[0], 100.0)
        self.assertEqual(pool.resolve_budget('', 'team_standard', '', '30')[0], 30.0)
        b, src = pool.resolve_budget('', 'team_standard', '3', '')
        self.assertIsNone(b)
        self.assertIn('API_CREDIT_SEAT_USD', src)
        self.assertIsNone(pool.resolve_budget('', 'team_standard', '0', '30')[0])
        for plan in ('pro', 'free', '', 'enterprise', 'bogus'):
            self.assertIsNone(pool.resolve_budget('', plan)[0], plan)

    def test_selection_reads_plan(self):
        now = time.time()
        doc, _ = Selection().run_case(now=MID, budget='', claude=snapshot(92, now),
                                      env_extra={'PLAN': 'max_20x'})
        self.assertEqual(doc['selected_runtime'], 'claude-api')
        self.assertIn('budget=$200.00 (plan max_20x)', doc['selection_detail'])
        doc, _ = Selection().run_case(now=MID, budget='', claude=snapshot(92, now),
                                      env_extra={'PLAN': 'pro'})
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('api_pool=api_pool_off: api_pool off (plan pro carries no API credit)',
                      doc['selection_detail'])
        doc, _ = Selection().run_case(now=MID, budget='60', claude=snapshot(92, now),
                                      env_extra={'PLAN': 'max_20x'})
        self.assertIn('budget=$60.00 (API_CREDIT_MONTHLY_USD override (plan max_20x',
                      doc['selection_detail'])

    def test_plan_from_agent_conf(self):
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, 'config'))
            Path(ws, 'config', 'agent.conf').write_text('PLAN=max_5x\n')
            env = dict(os.environ, DISPATCH_TEST='1', TEST_ROOT=str(ROOT), PLAN='',
                       API_CREDIT_MONTHLY_USD='')
            r = subprocess.run(['bash', '-c', 'source "$TEST_ROOT/routines/dispatch-detached.sh"\n'
                                f'WORKSPACE="{ws}"\napi_pool_budget && '
                                'echo "b=$API_POOL_BUDGET s=$API_POOL_BUDGET_SOURCE"'],
                               env=env, text=True, capture_output=True)
            self.assertIn('b=100 s=plan max_5x', r.stdout, r.stderr)


class CreditExhausted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.marker = os.path.join(self.d, 'exhausted.json')
        self.ledger = os.path.join(self.d, 'ledger.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def test_detection_sources(self):
        s = stream_file(self.d, [{'type': 'system', 'subtype': 'init'}, CREDIT_RESULT])
        self.assertIn('Credit balance is too low', pool.credit_exhausted_evidence(s))
        s = stream_file(self.d, [CREDIT_ASSISTANT])
        self.assertIn('billing_error', pool.credit_exhausted_evidence(s))
        s = stream_file(self.d, ['not json'])
        err = stream_file(self.d, ['Error: 400 Your credit balance is too low to access '
                                   'the Anthropic API.'], 'claude.err')
        self.assertTrue(pool.credit_exhausted_evidence(s, err).startswith('stderr:'))

    def test_model_prose_is_not_a_signal(self):
        # A worker writing ABOUT the error (like this very task) must not
        # close the lane: only error surfaces are read.
        s = stream_file(self.d, [PROSE, OK_RESULT])
        err = stream_file(self.d, ['some warning'], 'claude.err')
        self.assertIsNone(pool.credit_exhausted_evidence(s, err))
        self.assertIsNone(pool.mark_exhausted(self.marker, s, err, 'r', MID))
        self.assertFalse(os.path.exists(self.marker))

    def test_marker_overrides_ledger_until_the_1st(self):
        s = stream_file(self.d, [CREDIT_RESULT])
        doc = pool.mark_exhausted(self.marker, s, None, 'dsp-1', MID)
        self.assertEqual(doc['month'], '2026-10')
        write_ledger(self.ledger, [('2026-10', 1.0)])      # ledger says $99 left
        rt, v, d = pool.decide(100.0, self.ledger, 'codex', 'x', True,
                               usage(95, MID), [], None, MID, self.marker)
        self.assertEqual((rt, v), ('codex', 'api_credit_exhausted'))
        self.assertIn('ACCOUNT_CREDIT_EXHAUSTED (run dsp-1', d)
        # Month-end burn does not override it either.
        rt, v, _ = pool.decide(100.0, self.ledger, 'claude-code', 'x', True,
                               usage(10, END), [], None, END, self.marker)
        self.assertEqual(v, 'api_credit_exhausted')
        # The 1st: the marker is last month's -> pool open again.
        rt, v, _ = pool.decide(100.0, self.ledger, 'codex', 'x', True,
                               usage(95, NOV), [], None, NOV, self.marker)
        self.assertEqual((rt, v), ('claude-api', 'api_5h_overflow_paced'))

    def test_exhausted_cli_and_status(self):
        cli = [sys.executable, str(ROUTINES / 'lib' / 'api_credit_pool.py')]
        env = dict(os.environ, TZ='UTC')
        s = stream_file(self.d, [PROSE, OK_RESULT])
        r = subprocess.run(cli + ['exhausted', '--marker', self.marker, '--stream', s,
                                  '--now', str(MID)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        s = stream_file(self.d, [CREDIT_ASSISTANT, CREDIT_RESULT])
        r = subprocess.run(cli + ['exhausted', '--marker', self.marker, '--stream', s,
                                  '--runid', 'dsp-2', '--now', str(MID)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn('credit_exhausted month=2026-10', r.stdout)
        r = subprocess.run(cli + ['status', '--budget', '100', '--ledger', self.ledger,
                                  '--marker', self.marker, '--now', str(MID)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn('ACCOUNT_CREDIT_EXHAUSTED', r.stdout)

    def test_selection_and_gate(self):
        now = time.time()
        mark = {'month': '2026-10', 'ts': int(MID), 'runid': 'dsp-9',
                'evidence': 'stream result: Credit balance is too low'}
        doc, _ = Selection().run_case(now=MID, claude=snapshot(92, now), marker=mark)
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('api_pool=api_credit_exhausted', doc['selection_detail'])
        r = KeyAndGate().bash(
            "printf '%s' '" + json.dumps(mark) + "' > \"$API_CREDIT_EXHAUSTED_MARKER\"\n"
            'api_pool_gate && echo ok\n',
            API_CREDIT_MONTHLY_USD='100', DOGANY_API_POOL_NOW=str(MID))
        self.assertNotIn('ok', r.stdout)
        self.assertIn('credit balance too low', r.stderr)

    def test_worker_record_writes_marker(self):
        r = KeyAndGate().bash(
            'RUNDIR="$TEST_TMP/run"; mkdir -p "$RUNDIR"; RUNID=dsp-7; MODEL=opus\n'
            "printf '%s\\n' '" + json.dumps(CREDIT_RESULT) + "' > \"$RUNDIR/stream.jsonl\"\n"
            ': > "$RUNDIR/claude.err"\n'
            'api_pool_record\n'
            'echo "note=$API_RUN_COST_NOTE"\n'
            'cat "$API_CREDIT_EXHAUSTED_MARKER"\n')
        self.assertIn('lane closed until the 1st', r.stdout)
        self.assertIn('"runid": "dsp-7"', r.stdout)


class CostReport(unittest.TestCase):
    """Optional Admin API reconciliation against a local fake endpoint."""
    ADMIN = 'sk-ant-admin01-FIXTURE-never-print-me'

    def setUp(self):
        import http.server
        import threading
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.state = os.path.join(self.d, 'reconcile.json')
        self.ledger = os.path.join(self.d, 'ledger.jsonl')
        seen = self.seen = []
        pages = {None: {'data': [{'starting_at': '2026-10-01T00:00:00Z', 'results': [
                            {'amount': '1234.5', 'currency': 'USD'}]},
                                 {'starting_at': '2026-10-02T00:00:00Z', 'results': []}],
                        'has_more': True, 'next_page': 'p2'},
                 'p2': {'data': [{'starting_at': '2026-10-08T00:00:00Z', 'results': [
                            {'amount': '765.5', 'currency': 'USD'}]}],
                        'has_more': False, 'next_page': None}}
        self.fail_next = False
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import urlparse, parse_qs
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                seen.append((u.path, q, self.headers.get('x-api-key'),
                             self.headers.get('anthropic-version')))
                if outer.fail_next:
                    self.send_response(500)
                    self.end_headers()
                    return
                body = json.dumps(pages[q.get('page')]).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.srv.server_port}'
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def cli(self, key, now, extra=()):
        env = dict(os.environ, TZ='UTC', NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
        return subprocess.run([sys.executable, str(ROUTINES / 'lib' / 'api_credit_pool.py'),
                               'reconcile', '--state', self.state, '--base-url', self.url,
                               '--now', str(now), *extra],
                              input=key, capture_output=True, text=True, env=env)

    def test_fetch_pages_cents_and_freshness(self):
        r = self.cli(self.ADMIN, MID)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('org_spent=$20.00 month=2026-10', r.stdout)   # 2000 cents
        self.assertNotIn(self.ADMIN, r.stdout + r.stderr)
        path, q, key, ver = self.seen[0]
        self.assertEqual(path, '/v1/organizations/cost_report')
        self.assertEqual(q['starting_at'], '2026-10-01T00:00:00Z')
        self.assertEqual((key, ver), (self.ADMIN, '2023-06-01'))
        self.assertEqual(self.seen[1][1].get('page'), 'p2')
        # Within the 15-min console cache: no second fetch.
        r = self.cli(self.ADMIN, MID + 600)
        self.assertIn('fresh', r.stdout)
        self.assertEqual(len(self.seen), 2)
        # DGN-1978 r2: report days before its latest bucket (10-01, 10-02:
        # $12.345) + the ledger from that bucket on (10-08..: $7.50).
        with open(self.ledger, 'w') as fh:
            fh.write(json.dumps({'ts': MID - 60, 'month': '2026-10', 'cost_usd': 5.0}) + '\n')
            fh.write(json.dumps({'ts': MID + 60, 'month': '2026-10', 'cost_usd': 2.5}) + '\n')
        st = pool.pool_state(100.0, self.ledger, None, MID + 120, reconcile=self.state)
        self.assertAlmostEqual(st['spent'], 19.845)
        self.assertIn('org cost report', pool._pool_txt(st))
        # Next month the state is stale -> ledger only.
        st = pool.pool_state(100.0, self.ledger, None, NOV, reconcile=self.state)
        self.assertEqual(st['spent_src'], 'ledger')

    def test_failed_fetch_keeps_state(self):
        self.cli(self.ADMIN, MID)
        before = Path(self.state).read_text()
        self.fail_next = True
        r = self.cli(self.ADMIN, MID + 2 * 86400)
        self.assertEqual(r.returncode, 1)
        self.assertIn('cost report fetch failed', r.stdout)
        # DGN-1978: the last good figure stays (now stale), the error is kept.
        doc = json.loads(Path(self.state).read_text())
        self.assertEqual(doc['org_spent_usd'], json.loads(before)['org_spent_usd'])
        self.assertEqual(doc['fetched_at'], json.loads(before)['fetched_at'])
        self.assertIn('HTTPError', doc['last_error'])
        r = self.cli('', MID)
        self.assertEqual(r.returncode, 1)

    def test_bash_optional_admin_key(self):
        # No admin key: no request at all; with ANTHROPIC_ADMIN_KEY_FILE the
        # verdict reads the org figure and the key never reaches output.
        r = KeyAndGate().bash('api_pool_verdict codex x\n', API_CREDIT_MONTHLY_USD='100',
                              DOGANY_API_POOL_NOW=str(MID), API_ADMIN_BASE_URL=self.url)
        self.assertEqual(self.seen, [])
        self.assertIn('[ledger (no admin key)] truth=estimated', r.stdout)
        with tempfile.TemporaryDirectory() as kd:
            kf = os.path.join(kd, 'admin')
            Path(kf).write_text(self.ADMIN + '\n')
            r = KeyAndGate().bash('api_pool_verdict codex x\n', API_CREDIT_MONTHLY_USD='100',
                                  DOGANY_API_POOL_NOW=str(MID), API_ADMIN_BASE_URL=self.url,
                                  ANTHROPIC_ADMIN_KEY_FILE=kf, NO_PROXY='127.0.0.1',
                                  no_proxy='127.0.0.1')
        self.assertEqual(len(self.seen), 2, r.stderr)
        self.assertIn('spent=$12.35 [org cost report', r.stdout)
        self.assertNotIn(self.ADMIN, r.stdout + r.stderr)


class Workbench(unittest.TestCase):
    def test_runtime_name_on_the_row(self):
        tsv = str(ROUTINES / 'lib' / 'dispatch-model-display.tsv')
        self.assertEqual(md.model_display('claude-api', 'opus', None, tsv),
                         'Claude Opus 5.5 (API)')
        self.assertEqual(md.model_display('claude-api', 'opus', 'claude-opus-5-5', tsv),
                         'Claude Opus 5.5 (API)')
        self.assertEqual(md.model_display('claude-code', 'opus', None, tsv),
                         'Claude Opus 5.5')


if __name__ == '__main__':
    unittest.main()
