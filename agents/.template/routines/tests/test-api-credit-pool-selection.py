#!/usr/bin/env python3
# test-lane: slow
# test-component: api-credit-pool
# test-tier: 2
# test-prereq: subprocess, tmp-files
# test-new-component: the slow half of the api-credit-pool suite, split from test-dgn1963-api-credit-pool.py to keep that file under the 5 s fast budget
# test-slow-reason: every case runs the real dispatcher in bash (selection, gate or cost script), 0.5-1.6 s each; 16 cases total about 12 s
# promise: selection through select_default_runtime honours pacing hold, month-end API preference, pace spend, held pass and exhausted/off
# promise: the API key is read from file, never echoed, and a missing key falls back silently
# promise: the explicit gate, lane wiring and policy-crash reason are reported by the dispatcher
# promise: the plan table is read by selection; exhausted detection reaches selection and gate and worker records write the marker
# promise: the optional admin key gates the cost report and a failed fetch keeps state
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




def snapshot(u5, now, hist=()):
    return json.dumps({'usage': usage(u5, now), 'history': list(hist)})


class Selection(unittest.TestCase):
    """Through select_default_runtime(), same harness as the DGN-1874 suite."""

    def run_case(self, *, now, spent=0.0, budget='100', key=True, claude=None,
                 codex_used=60, extra='', env_extra=None, marker=None, probe=None):
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


    def test_credit_probe_closes_selection_and_writes_marker(self):
        doc, out = self.run_case(now=MID, claude=snapshot(92, time.time()),
                                probe=CREDIT_RESULT)
        self.assertNotEqual(doc['selected_runtime'], 'claude-api')
        self.assertIn('api_no_balance', doc['selection_detail'])
        self.assertIn('marker-present', out)

    def test_model_route_errors_skip_selection_without_marker(self):
        for message in ('unknown model', 'API Error: 404 model not found', 'network error'):
            with self.subTest(message=message):
                doc, out = self.run_case(now=MID, claude=snapshot(92, time.time()),
                    probe={'type': 'result', 'is_error': True, 'result': message})
                self.assertNotEqual(doc['selected_runtime'], 'claude-api')
                self.assertIn('api_balance_unconfirmed', doc['selection_detail'])
                self.assertNotIn('no balance', out)
                self.assertIn('marker-absent', out)

    def test_good_probe_selects_api_and_clears_old_marker(self):
        doc, out = self.run_case(now=MID, claude=snapshot(92, time.time()),
            marker={'runid': 'previous-exhaustion'},
            probe={'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'OK'})
        self.assertEqual(doc['selected_runtime'], 'claude-api')
        self.assertIn('marker-absent', out)

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
                      'claude_usage_snapshot() { return 1; }\n'
                      'api_pool_probe() { return 0; }\n' + body)
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

    def test_selection_reads_plan(self):
        now = time.time()
        doc, _ = Selection().run_case(now=MID, budget='', claude=snapshot(92, now),
                                      env_extra={'PLAN': 'max_20x'})
        self.assertEqual(doc['selected_runtime'], 'claude-api')
        self.assertIn('budget=$200.00 (plan ultra)', doc['selection_detail'])
        doc, _ = Selection().run_case(now=MID, budget='', claude=snapshot(92, now),
                                      env_extra={'PLAN': 'pro'})
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('api_pool=api_pool_off: api_pool off (plan pro carries no API credit)',
                      doc['selection_detail'])
        doc, _ = Selection().run_case(now=MID, budget='60', claude=snapshot(92, now),
                                      env_extra={'PLAN': 'max_20x'})
        self.assertIn('budget=$60.00 (API_CREDIT_MONTHLY_USD override (plan ultra',
                      doc['selection_detail'])



class CreditExhausted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.marker = os.path.join(self.d, 'exhausted.json')
        self.ledger = os.path.join(self.d, 'ledger.jsonl')

    def tearDown(self):
        self.tmp.cleanup()





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
        self.assertIn('api: no balance', r.stderr)

    def test_worker_record_writes_marker(self):
        r = KeyAndGate().bash(
            'RUNDIR="$TEST_TMP/run"; mkdir -p "$RUNDIR"; RUNID=dsp-7; MODEL=opus\n'
            "printf '%s\\n' '" + json.dumps(CREDIT_RESULT) + "' > \"$RUNDIR/stream.jsonl\"\n"
            ': > "$RUNDIR/claude.err"\n'
            'api_pool_record\n'
            'echo "note=$API_RUN_COST_NOTE"\n'
            'cat "$API_CREDIT_EXHAUSTED_MARKER"\n')
        self.assertIn('a successful probe reopens the lane', r.stdout)
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




if __name__ == '__main__':
    unittest.main()
