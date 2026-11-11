#!/usr/bin/env python3
# test-lane: fast
"""DGN-1978: interrupted attempts remain charged across partial streams and death.

Offline fixtures; no provider calls, credentials, or real workers.
Protocol: https://code.claude.com/docs/en/agent-sdk/streaming-output
https://platform.claude.com/docs/en/build-with-claude/streaming
"""
import calendar
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import api_credit_pool as pool

NOW = calendar.timegm((2026, 10, 10, 12, 0, 0))
MODEL = 'claude-opus-5-5'


def start(mid='m1', parent=None):
    return {'type': 'stream_event', 'parent_tool_use_id': parent, 'event': {
        'type': 'message_start', 'message': {'id': mid, 'model': MODEL, 'usage': {
            'input_tokens': 1000, 'output_tokens': 1,
            'cache_creation_input_tokens': 10000,
            'cache_creation': {'ephemeral_5m_input_tokens': 0,
                               'ephemeral_1h_input_tokens': 10000}}}}}


def delta(output, parent=None):
    return {'type': 'stream_event', 'parent_tool_use_id': parent,
            'event': {'type': 'message_delta', 'usage': {'output_tokens': output}}}


class InterruptedUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = str(self.root / 'ledger.jsonl')
        self.stream = str(self.root / 'stream.jsonl')
        self.inflight = str(self.root / 'inflight')

    def write(self, events):
        Path(self.stream).write_text(''.join(json.dumps(e) + '\n' for e in events))
        os.utime(self.stream, (NOW, NOW))

    def record(self, cause='timed-out'):
        return pool.record_row(self.ledger, self.stream, 'run', MODEL, NOW,
                               cause=cause, attempt='1')

    def test_partial_cumulative_usage_and_ttl_survive_truncation(self):
        self.write([start(), delta(100), delta(500)])
        with open(self.stream, 'a') as f:
            f.write('{"type": "stream_event",')
        row = self.record()
        self.assertAlmostEqual(row['cost_usd'], (1000 * 4 + 10000 * 8 + 500 * 20) / 1e6)
        self.assertTrue(row['estimated'])
        self.assertEqual(row['basis'], 'usage')
        self.assertTrue(self.record('canceled')['duplicate'])
        self.assertEqual(len(Path(self.ledger).read_text().splitlines()), 1)

    def test_assistant_blocks_do_not_replace_or_double_partial_counters(self):
        block = {'type': 'assistant', 'message': {'id': 'm1', 'model': MODEL,
                                                 'usage': {'output_tokens': 10}}}
        self.write([start(), block, delta(500), block, delta(100)])
        self.assertAlmostEqual(self.record()['cost_usd'], 0.094)

    def test_interleaved_message_lanes_keep_their_own_cumulative_usage(self):
        self.write([start(), start('m2', 'tool'), delta(200, 'tool'), delta(500)])
        self.assertAlmostEqual(self.record()['cost_usd'], 0.094 + 0.088)

    def test_exact_result_plus_interrupted_tail(self):
        self.write([start(), delta(500), {'type': 'result', 'total_cost_usd': 2},
                    start('m2'), delta(500)])
        row = self.record()
        self.assertAlmostEqual(row['cost_usd'], 2.094)
        self.assertTrue(row['estimated'])

    def test_completed_results_remain_exact(self):
        self.write([start(), delta(500), {'type': 'result', 'total_cost_usd': 2},
                    start('m2'), delta(500), {'type': 'result', 'total_cost_usd': 3},
                    {'type': 'rate_limit_event'}])
        row = self.record('')
        self.assertEqual(row['cost_usd'], 5)
        self.assertNotIn('estimated', row)

    def test_unusable_counters_and_missing_interrupted_stream_never_zero(self):
        for events in ([], [start() | {'event': {'type': 'message_start', 'message': {
                'id': 'm1', 'usage': {'input_tokens': -1, 'output_tokens': 'NaN'}}}}]):
            with self.subTest(events=events):
                self.write(events)
                row = pool.record_row(self.ledger, self.stream, str(events), MODEL,
                                      NOW, cause='canceled', attempt='1')
                self.assertEqual(row['cost_usd'], pool.DEFAULT_RUN_USD)
                self.assertTrue(row['estimated'])
        Path(self.stream).unlink()
        self.assertIsNone(pool.record_row(self.ledger, self.stream, 'not-started', MODEL, NOW))
        self.assertGreater(self.record()['cost_usd'], 0)

    def test_dead_worker_is_charged_before_next_admission_and_only_once(self):
        self.write([start(), delta(4000000)])  # > day 10 pace line
        pool.inflight_add(self.inflight, 'run', MODEL, 99999999, NOW,
                          self.stream, '1', self.ledger)
        with patch.object(pool, '_pid_alive', return_value=False):
            st = pool.pool_state(200, self.ledger, 6, NOW, inflight=self.inflight)
            self.assertGreater(st['spent'], st['allowed'])
            self.assertFalse(pool.admit(st)[0])
            self.assertEqual(st['inflight_n'], 0)
            self.assertEqual(pool.inflight_list(self.inflight), [])
        row = json.loads(Path(self.ledger).read_text())
        self.assertEqual(row['cause'], 'worker-lost')
        self.assertEqual(row['month'], pool.month_key(NOW))
        self.assertTrue(self.record()['duplicate'])

    def test_live_usage_larger_than_median_reserves_observed_spend(self):
        self.write([start(), delta(4000000)])
        pool.inflight_add(self.inflight, 'run', MODEL, os.getpid(), NOW,
                          self.stream, '1', self.ledger)
        st = pool.pool_state(200, self.ledger, 6, NOW, inflight=self.inflight)
        self.assertGreater(st['inflight_usd'], st['allowed'])
        self.assertFalse(pool.admit(st)[0])
        self.assertFalse(Path(self.ledger).exists())

    def test_killed_wrapper_with_live_cli_keeps_reservation(self):
        self.write([start(), delta(500)])
        Path(self.root, 'claude.pid').write_text(str(os.getpid()))
        pool.inflight_add(self.inflight, 'run', MODEL, 99999999, NOW,
                          self.stream, '1', self.ledger)
        with patch.object(pool, '_pid_alive', side_effect=lambda pid: int(pid) == os.getpid()):
            self.assertEqual(len(pool.inflight_list(self.inflight)), 1)
        self.assertFalse(Path(self.ledger).exists())

    def test_launcher_finalization_recovers_dead_attempt(self):
        self.write([start(), delta(500)])
        pool.inflight_add(self.inflight, 'run', MODEL, 99999999, NOW,
                          self.stream, '1', self.ledger)
        with patch.object(pool, '_pid_alive', return_value=False):
            self.assertEqual(pool.main(['inflight', 'remove', '--dir', self.inflight,
                                        '--runid', 'run']), 0)
        self.assertAlmostEqual(json.loads(Path(self.ledger).read_text())['cost_usd'], 0.094)
        self.assertFalse(Path(self.inflight, 'run.json').exists())

    def test_failed_recovery_keeps_reservation(self):
        self.write([start()])
        pool.inflight_add(self.inflight, 'run', MODEL, 99999999, NOW,
                          self.stream, '1', self.ledger)
        with patch.object(pool, '_pid_alive', return_value=False), \
                patch.object(pool, 'record_row', side_effect=OSError('unwritable')):
            self.assertEqual(len(pool.inflight_list(self.inflight)), 1)
        self.assertTrue(Path(self.inflight, 'run.json').exists())

    def test_exhaustion_does_not_advertise_estimated_credit_remaining(self):
        marker = str(self.root / 'exhausted.json')
        Path(marker).write_text(json.dumps({'month': pool.month_key(NOW),
                                          'evidence': 'billing_error'}))
        st = pool.pool_state(200, self.ledger, 6, NOW, marker=marker)
        self.assertEqual((st['remaining'], st['headroom']), (0, 0))
        self.assertFalse(pool.admit(st)[0])

    def test_worker_exit_backstop_charges_active_attempt(self):
        self.write([start(), delta(500)])
        dispatch = LIB.parent / 'dispatch-detached.sh'
        env = dict(os.environ, DISPATCH_TEST='1', HOME=self.tmp.name,
                   TEST_ROOT=str(LIB.parents[1]), TEST_TMP=self.tmp.name)
        script = '''source "$TEST_ROOT/routines/dispatch-detached.sh"
api_pool_account() { :; }
finalize_run() { :; }
RUNDIR="$TEST_TMP"; RUNID=run; MODEL=opus; RUNTIME=claude-api
API_CREDIT_LEDGER="$TEST_TMP/ledger.jsonl"
API_CREDIT_EXHAUSTED_MARKER="$TEST_TMP/exhausted.json"
API_POOL_ATTEMPT=1; API_POOL_IN_ATTEMPT=1
worker_on_exit
worker_on_exit
'''
        r = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        row = json.loads(Path(self.ledger).read_text())
        self.assertEqual(row['cause'], 'worker-exit')
        self.assertAlmostEqual(row['cost_usd'], 0.094)
        self.assertIn('api_stream_args=(--include-partial-messages)', dispatch.read_text())


if __name__ == '__main__':
    unittest.main()
