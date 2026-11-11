#!/usr/bin/env python3
# test-lane: fast
# test-component: dispatch-pools
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-new-component: dispatch_pools.py is a new per-turn reader (DGN-2345); the usage-snapshot suite owns the Claude line only and is a shell harness
# test-covers: agents/.template/routines/lib/dispatch_pools.py agents/.template/routines/codex-ratelimit.py agents/.template/memory-engine/memory.py
"""Offline pool fixtures, both caller surfaces, cache safety and hook timing."""
import builtins
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

ROUTINES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTINES / 'lib'))
import dispatch_pools as pools
import usage_summary


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Pools(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dgn2345-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = self.root / 'workspace'
        self.home = self.root / 'home'
        self.shared = self.home / '.dogany'
        self.dispatch = self.ws / '.telegram_bot/dispatch'
        self.now = time.time()
        # Replace, rather than extend, the caller's environment. In particular,
        # a developer's HOME and API-credit book overrides must never select the
        # books used by this fixture or by the copied CLI below.
        self.env = patch.dict(os.environ, {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'HOME': str(self.home), 'DOGANY_SHARED_HOME': str(self.shared),
            'DOGANY_RESOURCE_SETTINGS_DIR': str(self.root / 'resource-settings'),
            'CODEX_HOME': str(self.home / '.codex'),
            # HOME does not isolate the macOS login keychain.
            'DOGANY_SECRET_BACKEND': 'file',
            'DOGANY_USAGE_SNAPSHOT_DIR': str(self.shared / 'usage'),
            'DOGANY_USAGE_SNAPSHOT_TTL_SEC': '300',
            'CLOUD_CREDIT_UNTIL': '2099-11-05',
            'RESET_PASSES_CLAUDE': '', 'RESET_PASSES_CODEX': '',
            'API_CREDIT_MONTHLY_USD': '100', 'PLAN': 'max_5x',
            'LOOP_CAP_FIVE_HOUR': '90', 'LOOP_CAP_SEVEN_DAY': '95',
            'PYTHONDONTWRITEBYTECODE': '1',
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.data = {
            'five_hour': {'utilization': 95, 'resets_at': '2099-10-29T12:00:00Z'},
            'seven_day': {'utilization': 97, 'resets_at': '2099-11-02T12:00:00Z'},
            'iguana_necktie': {'limit_dollars': 100, 'used_dollars': 36,
                               'remaining_dollars': 64, 'resets_at': '2099-11-05T00:00:00Z'},
        }
        self.write(self.shared / 'usage/snapshot.json', self.data)
        self.write(self.home / '.claude.json', {'oauthAccount': {'accountUuid': 'claude-fixture'}})
        self.write(self.home / '.codex/auth.json', {'tokens': {'account_id': 'codex-fixture'}})
        for runtime in ('claude', 'codex'):
            account = pools.api.key_fingerprint(runtime + '-fixture')
            base = self.shared / 'reset-passes' / runtime
            self.write(base / ('.book-reset-passes-' + runtime + '.json'),
                       {'components': {runtime + '.account': account}})
            self.write(base / account / 'passes.json',
                       {'spec': '1|2099-10-30:weekly' if runtime == 'codex' else ''})
        self.write(self.dispatch / 'api-credit-exhausted.json',
                   {'month': pools.api.month_key(self.now)})
        self.write(self.shared / 'dispatcher/lanes.json', {
            'codex@' + pools.api.key_fingerprint('codex-fixture'): {'cap': 8, 'at': self.now},
            'claude-code@' + pools.api.key_fingerprint('claude-fixture'): {'cap': 3, 'at': self.now},
            'codex@another-account': {'cap': 99, 'at': self.now},
        })
        self.quota(43)

    def write(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def test_ultra_pool_open_from_weekly(self):
        self.data["five_hour"] = None
        self.data["seven_day"]["utilization"] = 51
        self.write(self.shared / "usage/snapshot.json", self.data)
        self.assertEqual(pools.read_pools(self.ws)["pools"]["claude"]["state"], "open")

    def test_ultra_pool_closed_from_weekly(self):
        self.data["five_hour"] = None
        self.write(self.shared / "usage/snapshot.json", self.data)
        self.assertEqual(pools.read_pools(self.ws)["pools"]["claude"]["state"], "closed")

    def quota(self, used):
        pools.publish_codex({'ok': True, 'used_percent': used,
                             'resets_at': self.now + 86400, 'window_duration_mins': 10080,
                             'raw': 'DO-NOT-CACHE-RAW-OR-CREDENTIALS'})

    def view(self):
        return pools.read_pools(self.ws)

    def test_claude_closed_codex_open_and_pass_expiry(self):
        v = self.view()
        self.assertEqual(v['pools']['claude']['state'], 'closed')
        self.assertEqual(v['pools']['claude']['passes']['count'], 0)
        self.assertIn('codex', v['open'])
        self.assertEqual(v['verdict'], 'parallel OK')
        self.assertEqual(v['cap'], 8)
        self.assertEqual(v['pools']['codex']['passes']['count'], 1)
        self.assertIn('expires 10-30', pools.render(v))
        self.assertIn('api no balance', pools.render(v))
        print('FIXTURE open:', pools.render(v))
        print('FIXTURE ko:', pools.render(v, 'ko'))

    def test_all_closed_hold_even_with_held_pass(self):
        self.quota(99)
        marker = self.ws / '.claude/.cloud-credit-exhausted'
        marker.parent.mkdir(parents=True)
        marker.touch()
        v = self.view()
        self.assertTrue(all(p['state'] == 'closed' for p in v['pools'].values()))
        self.assertEqual((v['verdict'], v['open'], v['cap']), ('hold', [], 0))
        print('FIXTURE closed:', pools.render(v))

    def test_zero_and_stale_lane_caps_do_not_claim_parallel_capacity(self):
        path = self.shared / 'dispatcher/lanes.json'
        lanes = pools.read_json(path)
        for row in lanes.values():
            row['cap'] = 0
        self.write(path, lanes)
        marker = self.ws / '.claude/.cloud-credit-exhausted'
        marker.parent.mkdir(parents=True)
        marker.touch()
        self.assertEqual(self.view()['verdict'], 'hold')
        for row in lanes.values():
            row['at'] = self.now - 901
        self.write(path, lanes)
        v = self.view()
        self.assertEqual(v['pools']['codex']['state'], 'open')
        self.assertIsNone(v['cap'])

    def test_missing_cache_is_unknown(self):
        shutil.rmtree(self.shared)
        shutil.rmtree(self.dispatch)
        v = self.view()
        self.assertTrue(all(p['state'] == 'unknown' for p in v['pools'].values()))
        self.assertIsNone(v['cap'])
        self.assertEqual(v['open'], [])
        print('FIXTURE missing:', pools.render(v))

    def test_last_known_codex_reading_shows_age(self):
        # DGN-2345 r3: a stale-but-recent reading is shown with its age, not as unknown.
        original = pools.read_json(pools.codex_cache_path())
        self.write(pools.codex_cache_path(), dict(original, fetched_at=self.now - 720))
        v = self.view()
        self.assertEqual(v['pools']['codex']['state'], 'open')
        self.assertIn('12m old', pools.render(v))
        self.write(pools.codex_cache_path(), dict(original, fetched_at=self.now - 60))
        v = self.view()
        self.assertNotIn(' old', pools.render(v).split('codex', 1)[1].split(')', 1)[0])

    def test_stale_reset_auth_change_and_invalid_cache(self):
        original = pools.read_json(pools.codex_cache_path())
        for change in ({'fetched_at': self.now - 86401}, {'fetched_at': self.now + 60},
                       {'resets_at': self.now - 1}, {'used_percent': True},
                       {'used_percent': 101}, {'ok': False}, {'window_duration_mins': 300},
                       {'auth_stamp': ['changed']}):
            self.write(pools.codex_cache_path(), dict(original, **change))
            self.assertEqual(self.view()['pools']['codex']['state'], 'unknown', change)
        self.write(pools.codex_cache_path(), original)
        snap = self.shared / 'usage/snapshot.json'
        os.utime(snap, (self.now - 301, self.now - 301))
        v = self.view()
        self.assertEqual(v['pools']['claude']['state'], 'unknown')
        self.assertNotIn('cloud', v['pools'])
        self.data['seven_day']['resets_at'] = '2000-01-01T00:00:00Z'
        self.write(snap, self.data)
        self.assertEqual(self.view()['pools']['claude']['state'], 'unknown')

    def test_read_only_no_credentials_network_or_recovery(self):
        self.write(self.dispatch / 'api-inflight/dead.json', {'pid': -1, 'model': 'claude-sonnet-4-6'})
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        def guard(original):
            def opened(file, mode='r', *args, **kwargs):
                self.assertFalse(any(k in str(file) for k in ('auth.json', '.credentials.json')))
                self.assertFalse(any(k in mode for k in ('w', 'a', '+', 'x')))
                return original(file, mode, *args, **kwargs)
            return opened
        with patch('subprocess.run', side_effect=AssertionError('live process')), \
             patch('builtins.open', guard(builtins.open)), patch('io.open', guard(io.open)), \
             patch.object(pools.api, '_recover_attempt', side_effect=AssertionError('recovery')):
            self.assertIn('codex', self.view()['open'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertNotIn('DO-NOT-CACHE', pools.codex_cache_path().read_text())
        pools.publish_codex({'ok': False, 'raw': 'secret'})
        self.assertEqual(self.view()['pools']['codex']['state'], 'unknown')

    def test_exhausted_marker_survives_month_rollover(self):
        self.write(self.dispatch / 'api-credit-exhausted.json', {'month': '2000-01'})
        self.assertEqual(self.view()['pools']['api']['state'], 'closed')
        self.assertIn('api no balance', pools.render(self.view()))

    def test_api_credit_reset_follows_configured_cycle(self):
        marker = self.dispatch / 'api-credit-exhausted.json'
        (self.dispatch / 'api-credit-ledger.jsonl').touch()
        now = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
        with patch.object(pools.api, '_RESET_DAY', None):
            for day, expected in (('15', '2026-11-15'), ('', '2026-12-01')):
                with self.subTest(reset_day=day), \
                     patch.dict(os.environ, {'API_CREDIT_RESET_DAY': day}):
                    self.write(marker, {'month': '2000-01'})
                    before = marker.read_bytes()
                    api = pools.read_pools(self.ws, now)['pools']['api']
                    # A read-only surface cannot confirm a refill or reopen.
                    self.assertEqual(api['remaining'], 0)
                    self.assertEqual(api['state'], 'closed')
                    self.assertEqual(marker.read_bytes(), before)
                    self.assertEqual(time.strftime('%Y-%m-%d', time.localtime(api['reset_at'])),
                                     expected)
                    with patch('subprocess.run') as run:
                        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({
                            'type': 'result', 'subtype': 'success', 'is_error': False}), '')
                        verdict = pools.api.probe_credit('fixture-key', str(self.dispatch / 'probe.json'),
                                                        str(marker), now, max_age=-1)
                    self.assertEqual(verdict, ('payable', False))
                    api = pools.read_pools(self.ws, now)['pools']['api']
                    self.assertEqual(api['remaining'], 100)
                    self.assertEqual(api['state'], 'open')

    def test_snapshot_default_matches_usage_wrapper_with_custom_shared_home(self):
        with patch.dict(os.environ, {'DOGANY_SHARED_HOME': str(self.root / 'other'),
                                     'DOGANY_USAGE_SNAPSHOT_DIR': ''}):
            self.assertEqual(self.view()['pools']['claude']['state'], 'closed')

    def test_damaged_nested_book_keeps_both_context_lines(self):
        self.write(self.shared / 'reset-passes/claude/.book-reset-passes-claude.json',
                   {'components': []})
        context = usage_summary.snapshot_context(self.ws)
        self.assertIn('owner_usage_line=', context.splitlines()[0])
        self.assertIn('[Dispatch pools]', context.splitlines()[1])
        self.assertEqual(self.view()['open'], [])

    def test_failed_claude_lookup_still_displays_other_pools(self):
        dest = self.ws / 'routines'
        shutil.copytree(ROUTINES / 'lib', dest / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROUTINES / 'claude-usage.sh', dest)
        (self.shared / 'usage/snapshot.json').unlink()
        (self.shared / 'usage/backoff-until').write_text(str(int(time.time()) + 300))
        result = subprocess.run(['bash', str(dest / 'claude-usage.sh')],
                                env=dict(os.environ, LOCALE='en'), capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        # Owner card: the agent pool line never shows; the API line + rule do.
        self.assertNotIn('[Dispatch pools]', result.stdout)
        self.assertTrue(result.stdout.rstrip().endswith('\u2500' * 27), result.stdout)

    def test_hook_and_cli_json_english_korean(self):
        # An empty ledger is explicit book evidence for a configured pool; the
        # stale marker leaves this month's pool open with its full amount.
        self.write(self.dispatch / 'api-credit-exhausted.json', {'month': '2000-01'})
        (self.dispatch / 'api-credit-ledger.jsonl').touch()
        hook = usage_summary.snapshot_context(self.ws)
        self.assertEqual(hook.splitlines()[0], usage_summary._claude_snapshot_context())
        self.assertIn('[Dispatch pools]', hook.splitlines()[1])
        self.assertIn('parallel OK (codex', hook)
        # Copy code only; fixtures and all CLI writes stay under TMPDIR.
        dest = self.ws / 'routines'
        shutil.copytree(ROUTINES / 'lib', dest / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROUTINES / 'claude-usage.sh', dest)
        def run(*args, locale='en'):
            result = subprocess.run(['bash', str(dest / 'claude-usage.sh'), *args],
                                    env=dict(os.environ, LOCALE=locale), capture_output=True,
                                    text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout
        doc = json.loads(run('--json'))
        self.assertEqual(doc['owner_usage_line'], usage_summary.owner_usage_line(self.data))
        self.assertEqual(doc['five_hour'], self.data['five_hour'])
        self.assertNotIn('Dispatch pools', json.dumps(doc))
        en, ko = run(), run(locale='ko')
        self.assertNotIn('[Dispatch pools]', en)
        self.assertNotIn('[\ubc30\uc815 \uc790\uc6d0]', ko)
        api = (self.view().get('pools') or self.view()).get('api') or {}
        self.assertIn('remaining', api)
        self.assertIn('API credit $', en)
        self.assertIn('API \ud06c\ub808\ub527 $', ko)
        reset = time.localtime(api['reset_at'])
        self.assertIn('resets %d-%02d' % (reset.tm_mon, reset.tm_mday), en)
        self.assertIn('%d-%02d \ub9ac\uc14b' % (reset.tm_mon, reset.tm_mday), ko)
        # No rule between the Claude windows and the API credit line.
        lines = en.splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith('API credit'))
        self.assertNotEqual(lines[i - 1], '\u2500' * 27)
        self.assertEqual(lines[i + 1], '\u2500' * 27)
        # Exercise the real hook import path without an inherited lib path.
        code = ('import importlib.util; p=' + repr(str(ROUTINES.parent / 'memory-engine/memory.py'))
                + '; s=importlib.util.spec_from_file_location("m",p); '
                'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                'print(m._hook_usage_context())')
        hook = subprocess.run([sys.executable, '-B', '-c', code], env=os.environ,
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertIn('[Dispatch pools]', hook.stdout)
        self.assertIn('parallel OK (codex', hook.stdout)

    def test_cache_published_by_actual_quota_reader(self):
        reader = load('codex_quota_fixture', ROUTINES / 'codex-ratelimit.py')
        result = {'ok': True, 'used_percent': 21, 'resets_at': self.now + 86400,
                  'window_duration_mins': 10080}
        with patch.object(reader, 'probe_rate_limits', return_value=result), \
             patch('sys.stdout', io.StringIO()), self.assertRaises(SystemExit) as exit_result:
            reader.main(['--json'])
        self.assertEqual(exit_result.exception.code, 0)
        self.assertEqual(self.view()['pools']['codex']['used'], 21)

    def test_hook_timing(self):
        baseline = Path(os.environ.get('DOGANY_RUN_SCRATCH', tempfile.gettempdir())) / 'usage_summary_before.py'
        before = load('usage_before', baseline).snapshot_context if baseline.exists() else usage_summary._claude_snapshot_context
        after = lambda: usage_summary.snapshot_context(self.ws)
        timings = []
        for fn in (before, after):
            samples = []
            for _ in range(100):
                start = time.perf_counter()
                fn()
                samples.append((time.perf_counter() - start) * 1000)
            timings.append((statistics.median(samples), max(samples)))
        print('TIMING hook ms median/max before=%.3f/%.3f after=%.3f/%.3f delta=%.3f' %
              (*timings[0], *timings[1], timings[1][0] - timings[0][0]))
        self.assertLess(timings[1][0] - timings[0][0], 200)
        cold = []
        paths = (baseline if baseline.exists() else ROUTINES / 'lib/usage_summary.py',
                 ROUTINES / 'lib/usage_summary.py')
        for index, path in enumerate(paths):
            method = "_claude_snapshot_context" if index == 0 and not baseline.exists() else "snapshot_context"
            code = ('import importlib.util,sys,time; sys.path.insert(0,'
                    + repr(str(ROUTINES / 'lib')) + '); start=time.perf_counter(); '
                    's=importlib.util.spec_from_file_location("u",' + repr(str(path)) + '); '
                    'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                    'm.' + method + '(); print((time.perf_counter()-start)*1000)')
            samples = []
            for _ in range(10):
                result = subprocess.run([sys.executable, '-B', '-c', code],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                samples.append(float(result.stdout))
            cold.append((statistics.median(samples), max(samples)))
        print('TIMING cold import+hook ms median/max before=%.3f/%.3f after=%.3f/%.3f delta=%.3f' %
              (*cold[0], *cold[1], cold[1][0] - cold[0][0]))
        # Gate the median; print maxima to expose scheduling outliers.
        self.assertLess(cold[1][0] - cold[0][0], 200)


if __name__ == '__main__':
    unittest.main()
