#!/usr/bin/env python3
# test-lane: fast
"""DGN-1978: console usage is the primary API-credit truth.

Offline: a local fake cost-report endpoint, temp ledgers / state / in-flight
dirs, fixture streams -- no network, keychain or workers. Covers:
  - the cost report fetched per decision behind a 15-min cache; a failed
    fetch keeps the last good figure, marked stale with its age, and backs
    off before the next try;
  - without an Admin key the pool says "estimated" plainly (an owner-read
    figure or the ledger), same guards;
  - a timed-out / cancelled claude-api attempt is charged from the stream's
    cumulative usage (estimated), never no row, and never twice;
  - the pace guard counts in-flight pool runs at their per-model median;
  - r2: the report lags, so it counts for closed days only (per day never
    below the ledger) and the ledger covers the open day; an owner seed
    beats a lower figure until the report closes the seed's day.
"""
import calendar
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

os.environ['TZ'] = 'UTC'
time.tzset()

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / 'routines'
LIB = ROUTINES / 'lib' / 'api_credit_pool.py'
sys.path.insert(0, str(ROUTINES / 'lib'))
import api_credit_pool as pool  # noqa: E402
import runtime_loss as rl  # noqa: E402

DAY10 = calendar.timegm((2026, 10, 10, 12, 0, 0))   # ~30.6% of October elapsed
ADMIN = 'sk-ant-admin01-FIXTURE-never-print-me'


def write_rows(path, rows):
    with open(path, 'w') as fh:
        for r in rows:
            fh.write(json.dumps(r) + '\n')


def rows(path):
    with open(path) as fh:
        return [json.loads(x) for x in fh if x.strip()]


def assistant(mid, usage, model='claude-opus-5-5'):
    return {'type': 'assistant', 'message': {'id': mid, 'model': model,
                                             'content': [{'type': 'text', 'text': 'x'}],
                                             'usage': usage}}


class FakeConsole:
    """Daily buckets {UTC day: cents}; '' = an empty bucket (no results)."""
    def __init__(self, days=None):
        self.seen, self.fail = [], False
        self.days = days or {'2026-10-01': '10417', '2026-10-10': ''}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append(self.path)
                if outer.fail:
                    self.send_response(503)
                    self.end_headers()
                    return
                data = [{'starting_at': f'{d}T00:00:00Z', 'ending_at': '',
                         'results': [{'amount': c, 'currency': 'USD'}] if c else []}
                        for d, c in sorted(outer.days.items())]
                body = json.dumps({'data': data, 'has_more': False,
                                   'next_page': None}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.srv.server_port}'
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ConsolePrimary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'reconcile.json')
        self.ledger = os.path.join(self.tmp.name, 'ledger.jsonl')
        self.console = FakeConsole()

    def tearDown(self):
        self.console.close()
        self.tmp.cleanup()

    def reconcile(self, now, key=ADMIN):
        env = dict(os.environ, TZ='UTC', NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
        return subprocess.run([sys.executable, str(LIB), 'reconcile', '--state', self.state,
                               '--base-url', self.console.url, '--now', str(now)],
                              input=key, capture_output=True, text=True, env=env)

    def state_of(self, now, admin=True):
        return pool.pool_state(200.0, self.ledger, 6.0, now, reconcile=self.state,
                               admin_key=admin)

    def test_fetch_cached_15_min_then_refetched(self):
        r = self.reconcile(DAY10)
        self.assertIn('org_spent=$104.17', r.stdout)
        self.assertNotIn(ADMIN, r.stdout + r.stderr)
        self.assertEqual(len(self.console.seen), 1)
        self.assertIn('fresh', self.reconcile(DAY10 + 14 * 60).stdout)
        self.assertEqual(len(self.console.seen), 1)            # cached
        st = self.state_of(DAY10 + 14 * 60)
        self.assertEqual(st['truth'], 'console')
        self.assertAlmostEqual(st['spent'], 104.17)
        self.assertIn('truth=console', pool._pool_txt(st))
        # r2: the open day (the report's latest bucket) is the ledger's,
        # rows before and after the fetch alike.
        write_rows(self.ledger, [{'ts': DAY10 - 60, 'month': '2026-10', 'cost_usd': 30.0},
                                 {'ts': DAY10 + 60, 'month': '2026-10', 'cost_usd': 2.0}])
        self.assertAlmostEqual(self.state_of(DAY10 + 120)['spent'], 136.17)
        # Past 15 min: the next decision fetches again.
        self.console.days = {'2026-10-01': '11000', '2026-10-10': ''}
        self.assertIn('org_spent=$110.00', self.reconcile(DAY10 + 16 * 60).stdout)
        self.assertEqual(len(self.console.seen), 2)
        self.assertAlmostEqual(self.state_of(DAY10 + 16 * 60)['spent'], 142.0)

    def test_failed_fetch_is_stale_with_age_and_backs_off(self):
        self.reconcile(DAY10)
        self.console.fail = True
        r = self.reconcile(DAY10 + 20 * 60)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.console.seen), 2)
        st = self.state_of(DAY10 + 20 * 60)
        self.assertEqual(st['truth'], 'console-stale')
        self.assertAlmostEqual(st['spent'], 104.17)            # last good figure
        self.assertIn('STALE 20m old', st['spent_src'])
        self.assertIn('503', st['spent_src'])
        # Within the retry backoff: no request at all.
        r = self.reconcile(DAY10 + 21 * 60)
        self.assertEqual(r.returncode, 1)
        self.assertIn('retry after', r.stdout)
        self.assertEqual(len(self.console.seen), 2)
        # Endpoint back: fresh again.
        self.console.fail = False
        self.reconcile(DAY10 + 23 * 60)
        self.assertEqual(self.state_of(DAY10 + 23 * 60)['truth'], 'console')

    def test_no_admin_key_is_estimated(self):
        # Ledger alone.
        st = self.state_of(DAY10, admin=False)
        self.assertEqual(st['truth'], 'estimated')
        self.assertIn('no admin key', st['spent_src'])
        # An owner-read figure (no cost-report source) + ledger after it:
        # still estimated, and it gates the pool the same way.
        Path(self.state).write_text(json.dumps({'month': '2026-10', 'fetched_at': DAY10 - 600,
                                                'org_spent_usd': 104.17,
                                                'source': 'owner-reported'}))
        write_rows(self.ledger, [{'ts': DAY10 - 60, 'month': '2026-10', 'cost_usd': 1.0}])
        st = self.state_of(DAY10, admin=False)
        self.assertEqual(st['truth'], 'estimated')
        self.assertAlmostEqual(st['spent'], 105.17)
        self.assertIn('owner-reported + ledger since', st['spent_src'])
        rt, v, d = pool.decide(200.0, self.ledger, 'claude-code', 'x', True, None, [], 6.0,
                               DAY10, reconcile=self.state, admin_key=False)
        self.assertEqual((rt, v), ('claude-code', 'api_not_needed'))
        self.assertIn('truth=estimated', d)
        # With an admin key, the first fetch replaces the owner figure at once.
        self.assertIn('org_spent=$104.17', self.reconcile(DAY10).stdout)
        self.assertEqual(json.loads(Path(self.state).read_text())['source'], 'cost_report')

    def test_bash_passes_admin_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DISPATCH_TEST='1', TEST_ROOT=str(ROOT), TZ='UTC', PLAN='',
                       API_CREDIT_MONTHLY_USD='200', DOGANY_API_POOL_NOW=str(DAY10),
                       ANTHROPIC_API_KEY_FILE=os.path.join(tmp, 'k'), TEST_TMP=tmp, HOME=tmp,
                       API_ADMIN_BASE_URL=self.console.url,
                       NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
            Path(tmp, 'k').write_text('sk-ant-api03-FIXTURE\n')
            env.pop('ANTHROPIC_ADMIN_KEY_FILE', None)
            script = ('source "$TEST_ROOT/routines/dispatch-detached.sh"\n'
                      'DISPATCH_DIR="$TEST_TMP/dispatch"\n'
                      'API_CREDIT_LEDGER="$TEST_TMP/ledger.jsonl"\n'
                      'API_CREDIT_EXHAUSTED_MARKER="$TEST_TMP/exhausted.json"\n'
                      'API_CREDIT_RECONCILE_STATE="$TEST_TMP/reconcile.json"\n'
                      'API_CREDIT_INFLIGHT_DIR="$TEST_TMP/inflight"\n'
                      'uname() { echo Linux; }\n'
                      'claude_usage_snapshot() { return 1; }\n'
                      'api_pool_verdict claude-code x\n')
            r = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
            self.assertIn('truth=estimated', r.stdout, r.stderr)
            self.assertIn('no admin key', r.stdout)
            self.assertEqual(self.console.seen, [])
            Path(tmp, 'admin').write_text(ADMIN + '\n')
            env['ANTHROPIC_ADMIN_KEY_FILE'] = os.path.join(tmp, 'admin')
            r = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
            self.assertIn('spent=$104.17 [org cost report', r.stdout, r.stderr)
            self.assertIn('truth=console', r.stdout)
            self.assertIn('api_not_needed', r.stdout)            # over pace -> kept
            self.assertNotIn(ADMIN, r.stdout + r.stderr)


class TimeoutRows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.ledger = os.path.join(self.d, 'ledger.jsonl')
        self.stream = os.path.join(self.d, 'stream.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def write_stream(self, events):
        with open(self.stream, 'w') as fh:
            for ev in events:
                fh.write(json.dumps(ev) + '\n')

    def test_timeout_row_is_estimated_from_stream_usage(self):
        u1 = {'input_tokens': 1000, 'output_tokens': 200, 'cache_read_input_tokens': 500000,
              'cache_creation_input_tokens': 20000}
        u2 = {'input_tokens': 10, 'output_tokens': 4000,
              'cache_creation': {'ephemeral_5m_input_tokens': 0,
                                 'ephemeral_1h_input_tokens': 10000}}
        # Two blocks of message m1 repeat its usage: counted once.
        self.write_stream([{'type': 'system', 'subtype': 'init'},
                           assistant('m1', u1), assistant('m1', u1),
                           assistant('m2', u2, model='claude-sonnet-5-5')])
        want = ((1000 * 4 + 200 * 20 + 500000 * 0.20 + 20000 * 1.25 * 4) +
                (10 * 2 + 4000 * 10 + 10000 * 2.0 * 2)) / 1e6
        r = subprocess.run([sys.executable, str(LIB), 'record', '--ledger', self.ledger,
                            '--stream', self.stream, '--runid', 'dsp-1', '--model', 'opus',
                            '--cause', 'timed-out', '--attempt', '1', '--now', str(DAY10)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('estimated stream usage (2 msgs)', r.stdout)
        (row,) = rows(self.ledger)
        self.assertAlmostEqual(row['cost_usd'], round(want, 6))
        self.assertGreater(row['cost_usd'], 0)
        self.assertEqual((row['estimated'], row['basis'], row['cause'], row['attempt']),
                         (True, 'usage', 'timed-out', '1'))
        # Same attempt again (cancel trap racing the loop): charged once.
        self.assertIn('already charged', subprocess.run(
            [sys.executable, str(LIB), 'record', '--ledger', self.ledger, '--stream',
             self.stream, '--runid', 'dsp-1', '--model', 'opus', '--attempt', '1',
             '--now', str(DAY10)], capture_output=True, text=True).stdout)
        self.assertEqual(len(rows(self.ledger)), 1)
        # A finished stream still uses the exact result figure.
        self.write_stream([assistant('m3', u1), {'type': 'result', 'total_cost_usd': 0.42}])
        row = pool.record_row(self.ledger, self.stream, 'dsp-2', 'opus', DAY10, attempt='1')
        self.assertEqual(row['cost_usd'], 0.42)
        self.assertNotIn('estimated', row)

    def test_no_usage_yet_falls_back_to_the_median_never_zero(self):
        write_rows(self.ledger, [{'ts': 1, 'month': '2026-10', 'model': 'opus', 'cost_usd': c}
                                 for c in (2.0, 6.0, 9.0)] +
                   [{'ts': 1, 'month': '2026-10', 'model': 'haiku', 'cost_usd': 0.1}])
        self.write_stream([{'type': 'system', 'subtype': 'init'}])
        row = pool.record_row(self.ledger, self.stream, 'dsp-3', 'claude-opus-5-5', DAY10,
                              cause='canceled', attempt='1')
        self.assertEqual((row['cost_usd'], row['basis'], row['estimated']), (6.0, 'median', True))
        # A median fallback row never feeds the next median.
        self.assertEqual(pool.expected_run_cost(self.ledger, 'opus')[0], 6.0)
        self.assertEqual(pool.expected_run_cost(os.path.join(self.d, 'none'), 'opus'),
                         (pool.DEFAULT_RUN_USD, 'default (no ledger history)'))
        self.assertEqual(pool.model_rates('claude-mystery-9')[0], 'unknown')

    def test_worker_records_timeout_and_cancel(self):
        u = {'input_tokens': 100, 'output_tokens': 100}
        self.write_stream([assistant('m1', u)])
        env = dict(os.environ, DISPATCH_TEST='1', TEST_ROOT=str(ROOT), TZ='UTC', TEST_TMP=self.d, HOME=self.d)
        script = ('source "$TEST_ROOT/routines/dispatch-detached.sh"\n'
                  'DISPATCH_DIR="$TEST_TMP/dispatch"\n'
                  'API_CREDIT_LEDGER="$TEST_TMP/ledger.jsonl"\n'
                  'API_CREDIT_EXHAUSTED_MARKER="$TEST_TMP/exhausted.json"\n'
                  'API_CREDIT_INFLIGHT_DIR="$TEST_TMP/inflight"\n'
                  'RUNDIR="$TEST_TMP"; RUNID=dsp-9; MODEL=opus\n'
                  'API_POOL_ATTEMPT=1; api_pool_record timed-out\n'
                  'API_POOL_ATTEMPT=2; api_pool_record canceled\n'
                  'api_pool_record canceled\n'
                  'api_pool_inflight add; ls "$API_CREDIT_INFLIGHT_DIR"\n'
                  'api_pool_inflight remove; ls "$API_CREDIT_INFLIGHT_DIR" | wc -l\n')
        r = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
        self.assertIn('estimated stream usage', r.stdout, r.stderr)
        got = rows(self.ledger)
        self.assertEqual([(x['cause'], x['attempt']) for x in got],
                         [('timed-out', '1'), ('canceled', '2')])
        self.assertIn('dsp-9.json', r.stdout)
        self.assertTrue(r.stdout.strip().endswith('0'))
        # The cancel trap and the finalize hook are wired.
        src = (ROUTINES / 'dispatch-detached.sh').read_text()
        trap = src[src.index('worker_on_term() {'):src.index('do_worker() {')]
        self.assertIn('api_pool_record canceled', trap)
        fin = src[src.index('finalize_run() {'):]
        self.assertIn('api_pool_inflight remove', fin[:2000])


class InflightGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.ledger = os.path.join(self.d, 'ledger.jsonl')
        self.infl = os.path.join(self.d, 'inflight')
        # Spent 50 of an allowed ~67.2 (200 x 30.6% + 6): under pace alone.
        write_rows(self.ledger, [{'ts': 1, 'month': '2026-10', 'model': 'opus', 'cost_usd': 10.0}
                                 for _ in range(5)])

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self):
        return pool.decide(200.0, self.ledger, 'claude-code', 'x', True, None, [], 6.0,
                           DAY10, inflight=self.infl)

    def test_running_workers_count_before_admitting(self):
        rt, v, _ = self.decide()
        self.assertEqual((rt, v), ('claude-api', 'api_pace_spend'))
        pool.inflight_add(self.infl, 'dsp-a', 'opus', os.getpid(), DAY10)
        rt, v, d = self.decide()                   # 50 + 10 = 60 < 67.2
        self.assertEqual((rt, v), ('claude-api', 'api_pace_spend'))
        self.assertIn('in_flight=1 (~$10.00) committed=$60.00', d)
        pool.inflight_add(self.infl, 'dsp-b', 'claude-opus-5-5', os.getpid(), DAY10)
        rt, v, d = self.decide()                   # 50 + 20 = 70 >= 67.2
        self.assertEqual((rt, v), ('claude-code', 'api_not_needed'))
        self.assertIn('in flight $20.00', d)
        # The loss ranker vetoes the pool the same way.
        p = pool.pool_state(200.0, self.ledger, 6.0, DAY10, inflight=self.infl)
        self.assertEqual(pool.admit(p)[0], False)
        why = rl.api_loss(200.0, self.ledger, 6.0, DAY10, None, None, '', self.infl)
        self.assertIsInstance(why, str)
        self.assertIn('over pace', why)

    def test_dead_worker_drops_out(self):
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        pool.inflight_add(self.infl, 'dsp-dead', 'opus', dead.pid, DAY10)
        pool.inflight_add(self.infl, 'dsp-live', 'opus', os.getpid(), DAY10)
        self.assertEqual([r['runid'] for r in pool.inflight_list(self.infl)], ['dsp-live'])
        self.assertFalse(os.path.exists(os.path.join(self.infl, 'dsp-dead.json')))
        pool.inflight_remove(self.infl, 'dsp-live')
        self.assertEqual(pool.inflight_list(self.infl), [])

    def test_inflight_past_budget_holds_even_at_month_end(self):
        end = calendar.timegm((2026, 10, 30, 12, 0, 0))
        write_rows(self.ledger, [{'ts': 1, 'month': '2026-10', 'model': 'opus',
                                  'cost_usd': 95.0}])
        for i in range(2):
            pool.inflight_add(self.infl, f'dsp-{i}', 'opus', os.getpid(), end)
        rt, v, d = pool.decide(100.0, self.ledger, 'claude-code', 'x', True, None, [],
                               None, end, inflight=self.infl)
        self.assertEqual((rt, v), ('claude-code', 'api_inflight_hold'))
        self.assertIn('>= budget', d)


DAY11 = DAY10 + 86400


class LaggingReport(unittest.TestCase):
    """r2 (measured 2026-10-10 14:59): the cost report said $7.73 for the
    month while the Console said $104.17 and the ledger held $42.57 for that
    day alone -- the report lags, so it is trusted for closed days only."""
    LAG = {'2026-10-08': '200', '2026-10-09': '500', '2026-10-10': '73'}   # $7.73

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, 'reconcile.json')
        self.ledger = os.path.join(self.tmp.name, 'ledger.jsonl')
        self.console = FakeConsole(dict(self.LAG))
        write_rows(self.ledger, [
            {'ts': DAY10 - 2 * 86400, 'month': '2026-10', 'cost_usd': 1.0},    # 10-08
            {'ts': DAY10 - 86400, 'month': '2026-10', 'cost_usd': 20.0},       # 10-09
            {'ts': DAY10 - 3600, 'month': '2026-10', 'cost_usd': 40.0},        # 10-10
            {'ts': DAY10 - 1800, 'month': '2026-10', 'cost_usd': 2.57,
             'estimated': True, 'cause': 'timed-out'}])                        # 10-10

    def tearDown(self):
        self.console.close()
        self.tmp.cleanup()

    def cli(self, *args, key=None):
        env = dict(os.environ, TZ='UTC', NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
        return subprocess.run([sys.executable, str(LIB), *args], input=key or '',
                              capture_output=True, text=True, env=env)

    def reconcile(self, now):
        return self.cli('reconcile', '--state', self.state, '--base-url', self.console.url,
                        '--now', str(now), key=ADMIN)

    def state_of(self, now, admin=True):
        return pool.pool_state(200.0, self.ledger, 6.0, now, reconcile=self.state,
                               admin_key=admin)

    def test_lagging_report_plus_ledger_today(self):
        r = self.reconcile(DAY10)
        self.assertIn('org_spent=$7.73', r.stdout)
        self.assertIn('closed=$7.00 before 2026-10-10', r.stdout)
        st = self.state_of(DAY10)
        # closed: max(2, 1) + max(5, 20) ; open 10-10: ledger 42.57 (estimated
        # row included), the report's $0.73 for today ignored.
        self.assertAlmostEqual(st['spent'], 2.0 + 20.0 + 42.57)
        self.assertEqual(st['truth'], 'console')
        self.assertIn('days before 10-10', st['spent_src'])
        self.assertIn('ledger from 10-10', st['spent_src'])
        # Never below the ledger for the same window.
        self.assertGreaterEqual(st['spent'], pool.month_spent(self.ledger, DAY10))
        # A lower month figure than the ledger can never reopen the pool.
        self.assertGreater(st['spent'], 7.73)

    def test_owner_seed_beats_a_lower_computed_figure(self):
        r = self.cli('seed', '--state', self.state, '--usd', '104.17',
                     '--at', str(DAY10 - 300), '--now', str(DAY10))
        self.assertIn('owner-reported org_spent=$104.17', r.stdout, r.stderr)
        # No admin key: seed + ledger after it.
        st = self.state_of(DAY10, admin=False)
        self.assertEqual(st['truth'], 'estimated')
        self.assertAlmostEqual(st['spent'], 104.17)
        # The fetch keeps the seed; computed 64.57 < seed -> seed wins.
        self.reconcile(DAY10)
        doc = json.loads(Path(self.state).read_text())
        self.assertEqual(doc['source'], 'cost_report')
        self.assertEqual(doc['owner_seed']['org_spent_usd'], 104.17)
        write_rows(self.ledger, rows(self.ledger) +
                   [{'ts': DAY10 + 60, 'month': '2026-10', 'cost_usd': 1.0}])
        st = self.state_of(DAY10 + 120)
        self.assertAlmostEqual(st['spent'], 105.17)
        self.assertEqual(st['truth'], 'console')
        self.assertIn('owner-reported + ledger since', st['spent_src'])
        self.assertIn('$65.57', st['spent_src'])          # the lower computed figure
        # Over pace (allowed ~67.2) -> the pool stays closed.
        rt, v, d = pool.decide(200.0, self.ledger, 'claude-code', 'x', True, None, [], 6.0,
                               DAY10 + 120, reconcile=self.state, admin_key=True)
        self.assertEqual((rt, v), ('claude-code', 'api_not_needed'))
        # A seed written after a fetch keeps the report's days.
        self.cli('seed', '--state', self.state, '--usd', '110', '--at', str(DAY10 + 200),
                 '--now', str(DAY10 + 200))
        doc = json.loads(Path(self.state).read_text())
        self.assertIn('days', doc)
        self.assertAlmostEqual(self.state_of(DAY10 + 200)['spent'], 110.0)

    def test_seed_never_lowers_the_ledger(self):
        self.cli('seed', '--state', self.state, '--usd', '1.0', '--at', str(DAY10),
                 '--now', str(DAY10))
        st = self.state_of(DAY10, admin=False)
        self.assertAlmostEqual(st['spent'], 63.57)
        self.assertIn('ledger', st['spent_src'])
        self.assertNotIn('owner-reported', st['spent_src'])

    def test_report_catching_up_supersedes_the_seed(self):
        self.cli('seed', '--state', self.state, '--usd', '104.17',
                 '--at', str(DAY10 - 300), '--now', str(DAY10))
        self.reconcile(DAY10)
        # Next day, before the refetch: the cached report still has 10-10
        # open -> the seed still stands.
        write_rows(self.ledger, rows(self.ledger) +
                   [{'ts': DAY11, 'month': '2026-10', 'cost_usd': 3.0}])
        self.assertAlmostEqual(self.state_of(DAY11)['spent'], 107.17)
        # The report closes 10-10 (a newer closed day than the seed's).
        self.console.days = dict(self.LAG, **{'2026-10-10': '6000', '2026-10-11': ''})
        self.assertIn('closed=$67.00 before 2026-10-11', self.reconcile(DAY11).stdout)
        st = self.state_of(DAY11)
        # max(2,1) + max(5,20) + max(60, 42.57) + ledger 10-11 3.0; the seed
        # (104.17 + 3.0 since) no longer counts.
        self.assertAlmostEqual(st['spent'], 85.0)
        self.assertNotIn('owner-reported', st['spent_src'])
        self.assertEqual(st['truth'], 'console')

    def test_r1_state_without_days_is_refetched(self):
        Path(self.state).write_text(json.dumps(
            {'month': '2026-10', 'fetched_at': DAY10 - 60, 'org_spent_usd': 7.73,
             'source': 'cost_report'}))
        st = self.state_of(DAY10)
        self.assertAlmostEqual(st['spent'], 63.57)       # ledger, not the lagging 7.73
        self.assertIn('org_spent=$7.73', self.reconcile(DAY10).stdout)
        self.assertEqual(len(self.console.seen), 1)       # not "fresh"


class Summary(unittest.TestCase):
    def test_summary_reads_the_workspace(self):
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, 'config'))
            Path(ws, 'config', 'agent.conf').write_text('PLAN=max_20x\n')
            d = os.path.join(ws, '.telegram_bot', 'dispatch')
            os.makedirs(d)
            Path(d, 'api-credit-reconcile.json').write_text(json.dumps(
                {'month': '2026-10', 'fetched_at': DAY10 - 300, 'org_spent_usd': 104.17,
                 'source': 'cost_report', 'days': {'2026-10-09': 104.17, '2026-10-10': 0},
                 'open_from': '2026-10-10'}))
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith('API_CREDIT') and k != 'PLAN'}
            r = subprocess.run([sys.executable, str(LIB), 'summary', '--workspace', ws,
                                '--now', str(DAY10)], capture_output=True, text=True, env=env)
            doc = json.loads(r.stdout)
            self.assertEqual((doc['budget'], doc['truth']), (200.0, 'console'))
            self.assertAlmostEqual(doc['remaining'], 95.83)
            self.assertEqual(doc['truth_age'], 300)
            Path(ws, 'config', 'agent.conf').write_text('PLAN=pro\n')
            r = subprocess.run([sys.executable, str(LIB), 'summary', '--workspace', ws,
                                '--now', str(DAY10)], capture_output=True, text=True, env=env)
            self.assertIn('off', json.loads(r.stdout))


if __name__ == '__main__':
    unittest.main()
