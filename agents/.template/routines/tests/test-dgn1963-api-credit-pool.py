#!/usr/bin/env python3
# test-lane: fast
# test-component: api-credit-pool
# test-tier: 0
# test-prereq: tmp-files
# promise: selection rules a-e (pacing, overflow, month-end burn, pace spend, exhausted/off) are pure functions of usage, ledger and clock
# promise: the ledger resets on the credit reset day; exhaustion persists until a successful probe, with a fresh probe due at reset
# promise: plan budgets come from the plan table and agent.conf; admin cost reports page cents and keep state fresh
# promise: one real select_default_runtime run spills overflow to the api pool, not codex
# Real-bash wiring cases (key file, gate, exhausted, plan, admin key) live in test-api-credit-pool-selection.py (slow).
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
from unittest.mock import patch

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

os.environ['TZ'] = 'UTC'
time.tzset()
# DGN-1981: key readers go through lib/secret_store.py -- pin it to an empty
# temp store so no host keychain or ~/.dogany/secrets entry leaks in.
_STORE = tempfile.TemporaryDirectory(prefix='dgn1963-store-')
os.environ['DOGANY_SECRET_BACKEND'] = 'file'
os.environ['DOGANY_SECRET_DIR'] = os.path.join(_STORE.name, 'secrets')

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


class Ultra(unittest.TestCase):
    def test_no_five_hour_overflow(self):
        self.assertEqual(pool.five_hour_overflow({"five_hour_absent": True,
            "seven_day_util": 51}, [], time.time()), (False, "weekly window only"))

    def test_weekly_snapshot_accepted(self):
        usage = {"five_hour_absent": True, "seven_day_util": 51}
        self.assertEqual(pool._read_snapshot(json.dumps({"usage": usage}))[0], usage)


class PurePolicy(unittest.TestCase):
    def setUp(self):
        pool.configure_reset_day()
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, 'ledger.jsonl')

    def tearDown(self):
        pool.configure_reset_day()
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

    def test_plan_credit_reset_day_cycle(self):
        pool.configure_reset_day('6')
        nov2 = calendar.timegm((2026, 11, 2, 12, 0, 0))
        nov6 = calendar.timegm((2026, 11, 6, 0, 0, 0))
        marker = os.path.join(self.tmp.name, 'exhausted.json')
        write_ledger(self.ledger, [(pool.month_key(nov2), 9.0)])
        Path(marker).write_text(json.dumps({'month': pool.month_key(nov2),
                                           'runid': 'nov2'}))
        self.assertEqual(pool.month_key(nov2), '2026-10-06')
        self.assertEqual(pool.month_spent(self.ledger, nov2), 9.0)
        self.assertIsNotNone(pool.exhausted_marker(marker, nov2))
        self.assertEqual(pool.month_key(nov6), '2026-11-06')
        self.assertEqual(pool.month_spent(self.ledger, nov6), 0.0)
        # The refill resets spend, but only a successful probe can reopen.
        for now in (nov6, nov6 + 86400):
            self.assertIsNotNone(pool.exhausted_marker(marker, now))
            rt, verdict, _ = pool.decide(100.0, self.ledger, 'codex', 'x', True,
                                         usage(95, now), [], None, now, marker)
            self.assertEqual((rt, verdict), ('codex', 'api_credit_exhausted'))
        self.assertEqual(time.strftime('%m-%d', time.localtime(pool.cycle_bounds(nov2)[1])),
                         '11-06')

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
                 codex_used=60, extra='', env_extra=None, marker=None, probe=None,
                 probe_state=None):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, 'ledger.jsonl')
            write_ledger(ledger, [(time.strftime('%Y-%m', time.gmtime(now)), spent)])
            if marker is not None:
                Path(tmp, 'exhausted.json').write_text(json.dumps(marker))
            if probe_state is not None:
                Path(tmp, 'probe.json').write_text(json.dumps(probe_state))
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='',
                       TEST_ROOT=str(ROOT), TEST_TMP=tmp, HOME=tmp, TZ='UTC', PLAN='',
                       API_CREDIT_MONTHLY_USD=budget, DOGANY_API_POOL_NOW=str(now))
            env.pop('ANTHROPIC_ADMIN_KEY_FILE', None)
            env.update(env_extra or {})
            probe_setup = 'api_pool_probe() { return 0; }'
            if probe is not None:
                cli = Path(tmp, 'claude')
                cli.write_text('#!/bin/sh\nprintf \'%s\\n\' "$TEST_PROBE_EVENT"\n'
                               'exit "$TEST_PROBE_RC"\n')
                cli.chmod(0o755)
                env.update(PATH=tmp + os.pathsep + env['PATH'],
                           TEST_PROBE_EVENT=json.dumps(probe),
                           TEST_PROBE_RC='1' if probe.get('is_error') else '0')
                probe_setup = ("api_key_read() { printf '%s' fixture-key; }\n"
                               "api_pool_account() { return 0; }")
            snap = "printf '%s' '" + claude + "'" if claude else 'return 1'
            script = f'''source "$TEST_ROOT/routines/dispatch-detached.sh"
DISPATCH_DIR="$TEST_TMP/dispatch"
DECISION_DIR="$TEST_TMP/decisions"
API_CREDIT_LEDGER="{ledger}"
API_CREDIT_EXHAUSTED_MARKER="$TEST_TMP/exhausted.json"
API_CREDIT_RECONCILE_STATE="$TEST_TMP/reconcile.json"
API_CREDIT_INFLIGHT_DIR="$TEST_TMP/inflight"
API_CREDIT_PROBE_STATE="$TEST_TMP/probe.json"
LABEL=test
WT_REPO=/fixture/estate
MODEL=opus; BOTTLENECK=coverage; MODEL_SOURCE=explicit
REQUESTED_RUNTIME=auto
WT_SLUG=
api_key_present() {{ return {0 if key else 1}; }}
{probe_setup}
claude_quota_remaining() {{ echo '80 70'; }}
claude_usage_snapshot() {{ {snap}; }}
codex_quota_gate() {{ CODEX_USED_PERCENT={codex_used}; CODEX_RESETS_AT=$(( $(date +%s) + 302400 )); CODEX_QUOTA_NOTE='used={codex_used}'; return 0; }}
codex_hook_gate() {{ CODEX_HOOK_NOTE='fixture'; return 0; }}
select_default_runtime
echo "MODEL=$MODEL CODEX_MODEL=$CODEX_MODEL"
[[ -e "$API_CREDIT_EXHAUSTED_MARKER" ]] && echo marker-present || echo marker-absent
{extra}'''
            r = subprocess.run(['bash', '-c', script], env=env, text=True,
                               capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            recs = list(Path(tmp, 'decisions').glob('*.json'))
            self.assertEqual(len(recs), 1)
            return json.loads(recs[0].read_text()), r.stdout

    def test_404_skips_selection_gate_and_loss_as_unconfirmed(self):
        doc, out = self.run_case(now=MID, claude=snapshot(92, time.time()),
            probe={'type': 'result', 'is_error': True, 'result': 'API Error: 404 unknown model'},
            extra='api_pool_gate 2>&1 || true\nloss_select 0\necho "LOSS=$LOSS_LINE"')
        self.assertNotEqual(doc['selected_runtime'], 'claude-api')
        self.assertIn('api_balance_unconfirmed', doc['selection_detail'])
        self.assertIn('marker-absent', out)
        self.assertIn('REFUSED: --runtime claude-api but api: balance unconfirmed', out)
        self.assertIn('vetoed: api (api: balance unconfirmed)', out)
        self.assertNotIn('no balance', out)

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

    def test_reset_day_selection_probes_cached_exhaustion(self):
        reset = calendar.timegm((2026, 11, 6, 0, 0, 0))
        for event, expected in ((dict(OK_RESULT, subtype='success'), 'claude-api'),
                                (CREDIT_RESULT, 'codex')):
            with self.subTest(expected=expected):
                doc, out = self.run_case(now=reset, claude=snapshot(92, time.time()),
                    env_extra={'API_CREDIT_RESET_DAY': '6'},
                    marker={'ts': reset - 60, 'runid': 'before-reset'},
                    probe_state={'ts': reset - 60, 'result': 'no_balance'}, probe=event)
                self.assertEqual(doc['selected_runtime'], expected)
                self.assertIn('marker-absent' if expected == 'claude-api' else 'marker-present', out)










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
                         (200.0, 'plan ultra (API_CREDIT_MONTHLY_USD agrees)'))
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
        pool.configure_reset_day()
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

    def test_marker_persists_until_successful_probe(self):
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
        # Month rollover alone must not reopen an account that still cannot pay.
        rt, v, _ = pool.decide(100.0, self.ledger, 'codex', 'x', True,
                               usage(95, NOV), [], None, NOV, self.marker)
        self.assertEqual((rt, v), ('codex', 'api_credit_exhausted'))

    def probe(self, event=None, rc=0, stderr='', now=MID, side_effect=None,
              model_tsv=None):
        with patch('subprocess.run', side_effect=side_effect) as run:
            run.return_value = subprocess.CompletedProcess(
                [], rc, json.dumps(event), stderr)
            verdict = pool.probe_credit(KEY, os.path.join(self.d, 'probe.json'),
                                       self.marker, now, model_tsv=model_tsv)
        return verdict, run

    def test_probe_no_balance_good_and_marker_clear(self):
        verdict, run = self.probe(CREDIT_RESULT, rc=1)
        self.assertEqual(verdict, ('no_balance', False))
        self.assertTrue(os.path.exists(self.marker))
        self.assertEqual(json.loads(Path(self.marker).read_text())['reason'], 'no_balance')
        # Credit verdicts are cached to bound the probe volume and cost.
        verdict, run = self.probe(now=MID + 1)
        self.assertEqual(verdict, ('no_balance', True))
        run.assert_not_called()
        verdict, _ = self.probe(dict(OK_RESULT, subtype='success'), now=MID + 301)
        self.assertEqual(verdict, ('payable', False))
        self.assertFalse(os.path.exists(self.marker))
        self.assertNotIn(KEY, Path(self.d, 'probe.json').read_text())

    def test_dgn2444_stale_marker_reprobed_by_summary(self):
        """/usage view: a 11-04 no_balance marker older than the re-enable
        is re-probed (stubbed) -> not exhausted, the REAL marker cleared."""
        ws = Path(self.d, 'ws')
        (ws / 'config').mkdir(parents=True)
        (ws / 'config' / 'agent.conf').write_text('API_CREDIT_MONTHLY_USD=200\n')
        disp = ws / '.telegram_bot' / 'dispatch'
        disp.mkdir(parents=True)
        mk = disp / 'api-credit-exhausted.json'
        mk.write_text(json.dumps({'ts': MID - 6 * 86400, 'runid': 'probe',
                                  'reason': 'no_balance'}))
        os.utime(ws / 'config' / 'agent.conf', (MID - 3600, MID - 3600))
        with patch.dict(os.environ, {'API_CREDIT_MONTHLY_USD': '200'}):
            with patch('subprocess.run') as run:
                run.return_value = subprocess.CompletedProcess(
                    [], 0, json.dumps(dict(OK_RESULT, subtype='success')), '')
                st = pool.summary(str(ws), MID, probe_key=KEY)
        self.assertFalse(st['exhausted'])
        self.assertFalse(mk.exists())
        self.assertTrue((disp / 'api-credit-probe.json').exists())
        # a fresh marker newer than the re-enable is trusted (no probe)
        mk.write_text(json.dumps({'ts': MID - 60, 'runid': 'r', 'reason': 'x'}))
        with patch.dict(os.environ, {'API_CREDIT_MONTHLY_USD': '200'}):
            with patch('subprocess.run') as run:
                st = pool.summary(str(ws), MID, probe_key=KEY)
        run.assert_not_called()
        self.assertTrue(st['exhausted'])

    def test_dgn2444_cost_report_starting_at_utc_aligned(self):
        """KST month start (15:00Z) must be sent UTC-midnight aligned."""
        self.assertTrue(pool.month_start_utc_iso(MID).endswith('Z'))
        seen = {}

        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return b'{"data": [], "has_more": false}'

        def op(req, timeout=0):
            seen['u'] = req.full_url
            return R()
        with patch('urllib.request.urlopen', op), \
                patch.object(pool, 'month_start_utc_iso',
                             return_value='2026-10-31T15:00:00Z'):
            pool.fetch_org_days('k', 'https://x', MID)
        self.assertIn('starting_at=2026-10-31T00%3A00%3A00Z', seen['u'])

    def test_reset_day_reprobes_before_reopening(self):
        self.check_reset_probe(dict(OK_RESULT, subtype='success'), 0, 'payable')

    def test_reset_day_insufficient_credit_keeps_closed(self):
        self.check_reset_probe(CREDIT_RESULT, 1, 'no_balance')

    def test_reset_day_unconfirmed_keeps_closed_and_retries(self):
        self.check_reset_probe({'type': 'result', 'is_error': True,
                               'result': 'network connection failed'}, 1, 'unknown')

    def check_reset_probe(self, event, rc, expected):
        for reset_day, date in (('', (2026, 11, 1)), ('6', (2026, 11, 6)),
                                ('31', (2026, 11, 1)), ('28', (2027, 1, 28))):
            for delay in (0, 86400):
                with self.subTest(reset_day=reset_day, delay=delay):
                    pool.configure_reset_day(reset_day)
                    reset = calendar.timegm((*date, 0, 0, 0))
                    before, now = reset - 60, reset + delay
                    Path(self.d, 'probe.json').unlink(missing_ok=True)
                    verdict, _ = self.probe(CREDIT_RESULT, rc=1, now=before)
                    self.assertEqual(verdict, ('no_balance', False))
                    verdict, run = self.probe(now=before + 1)
                    self.assertEqual(verdict, ('no_balance', True))
                    run.assert_not_called()
                    # Reading policy at reset cannot remove the marker.
                    self.assertIsNotNone(pool.exhausted_marker(self.marker, now))
                    # Long TTL also covers a selection a day after reset.
                    with patch('subprocess.run') as run:
                        run.return_value = subprocess.CompletedProcess([], rc, json.dumps(event), '')
                        verdict = pool.probe_credit(KEY, os.path.join(self.d, 'probe.json'),
                                                   self.marker, now, max_age=172800)
                    self.assertEqual(verdict, (expected, False))
                    run.assert_called_once()
                    self.assertEqual(os.path.exists(self.marker), expected != 'payable')
                    rt, _, _ = pool.decide(100.0, self.ledger, 'codex', 'x', True,
                                          usage(95, now), [], None, now, self.marker)
                    self.assertEqual(rt, 'claude-api' if expected == 'payable' else 'codex')
                    verdict, run = self.probe(event, rc=rc, now=now + 1)
                    self.assertEqual(verdict, (expected, expected != 'unknown'))
                    self.assertEqual(run.call_count, int(expected == 'unknown'))

    def test_probe_model_route_and_network_errors_are_unconfirmed(self):
        for message in ('API Error: 404 model not found', 'unknown model',
                        'network connection failed'):
            with self.subTest(message=message):
                verdict, _ = self.probe(
                    {'type': 'result', 'is_error': True, 'result': message}, rc=1)
                self.assertEqual(verdict, ('unknown', False))
                self.assertFalse(os.path.exists(self.marker))
                # Unknown verdicts are retried, and cannot clear old exhaustion.
                Path(self.marker).write_text('{"runid":"old-credit-error"}')
                verdict, run = self.probe(rc=1, stderr=message)
                self.assertEqual(verdict, ('unknown', False))
                run.assert_called_once()
                self.assertEqual(json.loads(Path(self.marker).read_text()),
                                 {'runid': 'old-credit-error'})
                Path(self.marker).unlink()

    def test_probe_timeout_and_missing_cli_are_unconfirmed(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('claude', 15)):
            verdict, _ = self.probe(side_effect=error)
            self.assertEqual(verdict, ('unknown', False))
            self.assertFalse(os.path.exists(self.marker))

    def test_probe_uses_cli_key_and_cheapest_table_alias(self):
        for rows, expected in ((None, 'haiku'),
                ('claude-api\tsonnet\tsonnet\tClaude Sonnet\n'
                 'claude-api\topus\topus\tClaude Opus\n', 'sonnet')):
            tsv = None
            if rows:
                tsv = os.path.join(self.d, 'models.tsv')
                Path(tsv).write_text(rows)
            verdict, run = self.probe(dict(OK_RESULT, subtype='success'),
                                      now=MID, model_tsv=tsv)
            self.assertEqual(verdict, ('payable', False))
            args, kwargs = run.call_args
            cmd = args[0]
            self.assertEqual(cmd[0], 'claude')
            self.assertEqual(cmd[cmd.index('--model') + 1], expected)
            self.assertEqual(kwargs['env']['ANTHROPIC_API_KEY'], KEY)
            self.assertNotIn(KEY, cmd)
            self.assertNotIn('CLAUDECODE', kwargs['env'])
            self.assertEqual(cmd[cmd.index('--tools') + 1], '')
            self.assertEqual(cmd[cmd.index('--setting-sources') + 1], '')
            self.assertFalse(os.path.exists(kwargs['cwd']))
            Path(self.d, 'probe.json').unlink()

    def test_probe_requires_success_result_and_ignores_model_billing_prose(self):
        for event in ({}, {'type': 'system', 'subtype': 'init'},
                      {'type': 'result', 'subtype': 'error_max_turns', 'is_error': True},
                      PROSE):
            verdict, _ = self.probe(event)
            self.assertEqual(verdict, ('unknown', False))
            self.assertFalse(os.path.exists(self.marker))
        verdict, _ = self.probe(dict(OK_RESULT, subtype='success'))
        self.assertEqual(verdict, ('payable', False))

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
