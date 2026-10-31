#!/usr/bin/env python3
# test-lane: fast
"""DGN-1978 r3: one shared API-credit ledger per account on the host.

Measured 2026-10-10: instance A $42.57 + instance B $67.30 = $109.87 ~ Console
$104.17 -- one credit account, two per-instance ledgers, each pacing against
the full budget. Offline (temp HOME, fixture key files and streams, no
network / keychain / workers). Covers:
  - two instances with the same key append -> both read the sum, and the
    status line names the account scope;
  - the one-time migration merges each instance's old ledger (deduped by
    runid + attempt), leaves a pointer note, and is idempotent; marker and
    reconcile state move account-wide;
  - concurrent appends are safe (flock) and one attempt is charged once;
  - the pace rule uses the shared sum;
  - the key is never stored, only its fingerprint names the dir.
"""
import calendar
import json
import multiprocessing
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

DAY10 = calendar.timegm((2026, 10, 10, 12, 0, 0))   # ~30.6% of October elapsed
KEY = 'sk-ant-api03-R3-FIXTURE-never-store-me'


def rows(path):
    with open(path) as fh:
        return [json.loads(x) for x in fh if x.strip()]


def write_rows(path, rs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        for r in rs:
            fh.write(json.dumps(r) + '\n')


def result_stream(path, usd):
    with open(path, 'w') as fh:
        fh.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
        fh.write(json.dumps({'type': 'result', 'total_cost_usd': usd}) + '\n')


class Host:
    """One temp HOME (the Mac) holding any number of agent instances."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.home = os.path.join(tmp, 'home')
        os.makedirs(self.home)
        self.keyfile = os.path.join(tmp, 'key')
        Path(self.keyfile).write_text(KEY + '\n')
        self.fp = pool.key_fingerprint(KEY)
        self.adir = os.path.join(self.home, '.dogany', 'api-credit', self.fp)

    def dispatch(self, name):
        d = os.path.join(self.tmp, name, '.telegram_bot', 'dispatch')
        os.makedirs(d, exist_ok=True)
        return d

    def bash(self, inst, body, **extra):
        env = dict(os.environ, DISPATCH_TEST='1', TEST_ROOT=str(ROOT), TZ='UTC', PLAN='',
                   HOME=self.home, ANTHROPIC_API_KEY_FILE=self.keyfile,
                   API_CREDIT_MONTHLY_USD='200', DOGANY_API_POOL_NOW=str(DAY10),
                   INST=self.dispatch(inst))
        env.pop('ANTHROPIC_ADMIN_KEY_FILE', None)
        env.update(extra)
        # DISPATCH_DIR and the four legacy defaults are re-derived from it,
        # as a real instance would have them at source time.
        script = ('source "$TEST_ROOT/routines/dispatch-detached.sh"\n'
                  'DISPATCH_DIR="$INST"\n'
                  'API_CREDIT_LEDGER="$DISPATCH_DIR/api-credit-ledger.jsonl"\n'
                  'API_CREDIT_EXHAUSTED_MARKER="$DISPATCH_DIR/api-credit-exhausted.json"\n'
                  'API_CREDIT_RECONCILE_STATE="$DISPATCH_DIR/api-credit-reconcile.json"\n'
                  'API_CREDIT_INFLIGHT_DIR="$DISPATCH_DIR/api-inflight"\n'
                  'API_CREDIT_LEGACY_LEDGER="$API_CREDIT_LEDGER"\n'
                  'API_CREDIT_LEGACY_MARKER="$API_CREDIT_EXHAUSTED_MARKER"\n'
                  'API_CREDIT_LEGACY_RECONCILE="$API_CREDIT_RECONCILE_STATE"\n'
                  'API_CREDIT_LEGACY_INFLIGHT="$API_CREDIT_INFLIGHT_DIR"\n'
                  'uname() { echo Linux; }\n'
                  'claude_usage_snapshot() { return 1; }\n' + body)
        return subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)

    def record(self, inst, runid, usd):
        rundir = os.path.join(self.tmp, inst, 'run-' + runid)
        os.makedirs(rundir, exist_ok=True)
        result_stream(os.path.join(rundir, 'stream.jsonl'), usd)
        return self.bash(inst, f'RUNDIR="{rundir}"; RUNID={runid}; MODEL=opus\n'
                               'API_POOL_ATTEMPT=1; api_pool_record\n'
                               'echo "LEDGER=$API_CREDIT_LEDGER"\n')

    def all_files_text(self):
        out = []
        for dp, _, fs in os.walk(self.tmp):
            for f in fs:
                p = os.path.join(dp, f)
                if p != self.keyfile:
                    out.append(Path(p).read_text(errors='replace'))
        return '\n'.join(out)


class SharedLedger(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.h = Host(self.t.name)

    def tearDown(self):
        self.t.cleanup()

    def test_two_instances_append_both_see_sum(self):
        ra = self.h.record('agentA', 'dsp-a1', 42.57)
        rb = self.h.record('agentB', 'dsp-b1', 67.30)
        shared = os.path.join(self.h.adir, 'ledger.jsonl')
        self.assertIn(f'LEDGER={shared}', ra.stdout, ra.stderr)
        self.assertIn(f'LEDGER={shared}', rb.stdout, rb.stderr)
        self.assertEqual(sorted(r['runid'] for r in rows(shared)), ['dsp-a1', 'dsp-b1'])
        for inst in ('agentA', 'agentB'):
            r = self.h.bash(inst, 'api_pool_gate >/dev/null 2>&1; echo "$API_POOL_NOTE"\n')
            self.assertIn('spent=$109.87', r.stdout, r.stderr)
            self.assertIn('scope=[account pool, all agents on this Mac; key '
                          + self.h.fp + ']', r.stdout)
        # the read-only surface (/passes) follows the instance pointer
        os.environ['API_CREDIT_MONTHLY_USD'] = '200'
        try:
            st = pool.summary(os.path.join(self.h.tmp, 'agentA'), DAY10)
        finally:
            del os.environ['API_CREDIT_MONTHLY_USD']
        self.assertAlmostEqual(st['spent'], 109.87)
        self.assertIn('account pool', st['scope'])

    def test_pace_uses_shared_sum(self):
        # Alone, A's $42.57 is under the pace line (~$67.24 on day 10 of a
        # $200 month) -> pace spend; with B's $67.30 in the same account
        # ledger, both instances see $109.87 and hold.
        self.h.record('agentA', 'dsp-a1', 42.57)
        r = self.h.bash('agentA', 'api_pool_verdict claude-code x\n')
        self.assertIn('api_pace_spend', r.stdout, r.stderr)
        self.h.record('agentB', 'dsp-b1', 67.30)
        for inst in ('agentA', 'agentB'):
            r = self.h.bash(inst, 'api_pool_verdict claude-code x\n')
            self.assertIn('claude-code\tapi_not_needed', r.stdout, r.stderr)
            self.assertIn('spent=$109.87', r.stdout)

    def test_key_never_stored(self):
        self.h.record('agentA', 'dsp-a1', 1.0)
        self.h.bash('agentA', 'api_pool_verdict claude-code x\n')
        self.assertEqual(len(self.h.fp), 12)
        self.assertTrue(os.path.isdir(self.h.adir))
        self.assertNotIn(KEY, self.h.all_files_text())

    def test_no_key_keeps_instance_books(self):
        r = self.h.bash('agentA', 'api_pool_account; echo "LEDGER=$API_CREDIT_LEDGER"\n',
                        ANTHROPIC_API_KEY_FILE='/nonexistent/key')
        self.assertIn('LEDGER=' + self.h.dispatch('agentA') + '/api-credit-ledger.jsonl',
                      r.stdout, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.h.home, '.dogany')))

    def test_caller_override_left_alone(self):
        r = self.h.bash('agentA', 'API_CREDIT_LEDGER=/fixture/ledger.jsonl\n'
                                  'api_pool_account; echo "L=$API_CREDIT_LEDGER M=$API_CREDIT_EXHAUSTED_MARKER"\n')
        self.assertIn('L=/fixture/ledger.jsonl M=' + self.h.adir + '/exhausted.json', r.stdout)


class Migration(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.h = Host(self.t.name)
        self.a, self.b = self.h.dispatch('agentA'), self.h.dispatch('agentB')
        m = '2026-10'
        write_rows(os.path.join(self.a, 'api-credit-ledger.jsonl'), [
            {'ts': DAY10 - 7200, 'month': m, 'runid': 'r1', 'attempt': '1', 'cost_usd': 10.0},
            {'ts': DAY10 - 3600, 'month': m, 'runid': 'r2', 'attempt': '1', 'cost_usd': 5.0}])
        # r2 is in both (a copied workspace); r2 attempt 2 is a separate charge
        write_rows(os.path.join(self.b, 'api-credit-ledger.jsonl'), [
            {'ts': DAY10 - 3600, 'month': m, 'runid': 'r2', 'attempt': '1', 'cost_usd': 5.0},
            {'ts': DAY10 - 3000, 'month': m, 'runid': 'r2', 'attempt': '2', 'cost_usd': 2.0},
            {'ts': DAY10 - 1800, 'month': m, 'runid': 'r3', 'attempt': '1', 'cost_usd': 7.0}])
        Path(self.b, 'api-credit-exhausted.json').write_text(json.dumps(
            {'month': m, 'ts': DAY10 - 60, 'runid': 'r3', 'evidence': 'credit balance is too low'}))
        Path(self.a, 'api-credit-reconcile.json').write_text(json.dumps(
            {'month': m, 'source': 'owner-reported', 'org_spent_usd': 20.0,
             'fetched_at': DAY10 - 7300}))

    def tearDown(self):
        self.t.cleanup()

    def test_merge_dedupes_and_leaves_pointer(self):
        _, fp, ra = pool.resolve_account(KEY, self.a, DAY10, self.h.home)
        _, _, rb = pool.resolve_account(KEY, self.b, DAY10, self.h.home)
        self.assertEqual(fp, self.h.fp)
        self.assertEqual((ra['merged'], ra['duplicates']), (2, 0))
        self.assertEqual((rb['merged'], rb['duplicates']), (2, 1))
        shared = os.path.join(self.h.adir, 'ledger.jsonl')
        got = sorted((r['runid'], r['attempt']) for r in rows(shared))
        self.assertEqual(got, [('r1', '1'), ('r2', '1'), ('r2', '2'), ('r3', '1')])
        self.assertAlmostEqual(pool.month_spent(shared, DAY10), 24.0)
        for d in (self.a, self.b):
            old = rows(os.path.join(d, 'api-credit-ledger.jsonl'))
            self.assertEqual(len(old), 1)
            self.assertEqual(old[0]['migrated_to'], self.h.adir)
            self.assertEqual(pool.month_spent(os.path.join(d, 'api-credit-ledger.jsonl'), DAY10), 0)
            self.assertTrue(os.path.exists(os.path.join(d, 'api-credit-ledger.jsonl.pre-migration')))
            self.assertEqual(json.loads(Path(d, 'api-credit-account.json').read_text())['dir'],
                             self.h.adir)
        # account-wide marker + reconcile state
        p = pool.account_paths(self.h.adir)
        self.assertEqual(pool.exhausted_marker(p['marker'], DAY10)['runid'], 'r3')
        self.assertEqual(pool.owner_seed(pool.reconcile_doc(p['reconcile'], DAY10))[0], 20.0)
        self.assertIsNone(pool.exhausted_marker(os.path.join(self.b, 'api-credit-exhausted.json'),
                                                DAY10))
        # idempotent
        _, _, again = pool.resolve_account(KEY, self.a, DAY10, self.h.home)
        self.assertEqual((again['merged'], again['duplicates']), (0, 0))
        self.assertEqual(len(rows(shared)), 4)

    def test_late_legacy_row_merged_next_use(self):
        pool.resolve_account(KEY, self.a, DAY10, self.h.home)
        # an old-code worker still running at the cut-over appends to its file
        with open(os.path.join(self.a, 'api-credit-ledger.jsonl'), 'a') as fh:
            fh.write(json.dumps({'ts': DAY10, 'month': '2026-10', 'runid': 'r9',
                                 'attempt': '1', 'cost_usd': 3.0}) + '\n')
        _, _, res = pool.resolve_account(KEY, self.a, DAY10 + 60, self.h.home)
        self.assertEqual(res['merged'], 1)
        shared = os.path.join(self.h.adir, 'ledger.jsonl')
        self.assertAlmostEqual(pool.month_spent(shared, DAY10), 18.0)

    def test_bash_migrates_on_first_use(self):
        r = self.h.bash('agentA', 'api_pool_verdict claude-code x\n')
        self.assertIn('spent=$', r.stdout, r.stderr)
        r = self.h.bash('agentB', 'api_pool_verdict claude-code x\n')
        # B's marker (credit too low) is now the account's
        self.assertIn('api_credit_exhausted', r.stdout, r.stderr)
        r = self.h.bash('agentA', 'api_pool_verdict claude-code x\n')
        self.assertIn('api_credit_exhausted', r.stdout, r.stderr)


def _hammer(args):
    ledger, stream, worker, n = args
    for i in range(n):
        pool.record_row(ledger, stream, f'w{worker}-{i}', 'opus', DAY10, attempt='1')
        pool.record_row(ledger, stream, 'same-run', 'opus', DAY10, attempt='1')


class Concurrency(unittest.TestCase):
    def test_concurrent_appends_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, 'acct', 'ledger.jsonl')
            stream = os.path.join(tmp, 'stream.jsonl')
            result_stream(stream, 0.5)
            ctx = multiprocessing.get_context('fork')
            with ctx.Pool(8) as p:
                p.map(_hammer, [(ledger, stream, w, 25) for w in range(8)])
            got = rows(ledger)          # every line parses: no torn writes
            self.assertEqual(len(got), 8 * 25 + 1)
            self.assertEqual(sum(1 for r in got if r['runid'] == 'same-run'), 1)
            self.assertAlmostEqual(pool.month_spent(ledger, DAY10), (8 * 25 + 1) * 0.5)


if __name__ == '__main__':
    unittest.main()
