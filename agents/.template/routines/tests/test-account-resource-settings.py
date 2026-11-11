#!/usr/bin/env python3
# test-lane: fast
# test-component: account-resource-settings
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-covers: resource_settings.py api_credit_pool.py dispatch_pools.py dispatch-detached.sh
"""Offline account isolation, migration, passive precedence and real quota gates."""
import calendar
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROUTINES = Path(__file__).resolve().parents[1]
LIB = ROUTINES / 'lib'
sys.path.insert(0, str(LIB))
import resource_settings as settings
import api_credit_pool as api
import dispatch_pools as pools

A = '0123456789ab'
B = 'abcdef012345'


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='resource-settings-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.ws = self.root / 'instance'
        self.home.mkdir()
        self.ws.mkdir()
        env = patch.dict(os.environ, {'HOME': str(self.home), 'PATH': os.environ['PATH'],
                                     'DOGANY_RESOURCE_SETTINGS_DIR': str(self.root / 'settings'),
                                     'TMPDIR': self.tmp.name, 'TZ': 'UTC',
                                     'PYTHONDONTWRITEBYTECODE': '1',
                                     'DOGANY_SECRET_BACKEND': 'file'}, clear=True)
        env.start()
        self.addCleanup(time.tzset)
        self.addCleanup(env.stop)
        time.tzset()
        api.configure_reset_day()
        self.addCleanup(api.configure_reset_day)
        self.books = self.home / '.dogany/api-credit' / A
        self.books.mkdir(parents=True)
        self.write(self.ws / '.telegram_bot/dispatch/api-credit-account.json',
                   {'dir': str(self.books), 'fingerprint': A})

    def write(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def cli(self, *args, input=None):
        return subprocess.run([sys.executable, '-B', str(LIB / 'resource_settings.py'), *args],
                              input=input, text=True, capture_output=True, timeout=10)

    def test_defaults_do_not_write(self):
        before = set(self.root.rglob('*'))
        self.assertEqual(settings.get('api-credit', A)['reset_day'], 1)
        self.assertEqual(settings.get('codex', A)['max_used_percent'], 98)
        self.assertEqual(settings.get('codex', None)['source'], 'default')
        self.assertIn('missing', settings.get('codex', A)['note'])
        self.assertEqual(before, set(self.root.rglob('*')))

    def test_boundaries_and_invalid_values(self):
        for runtime, values in [('api-credit', (1, 28)), ('codex', (50, 100))]:
            field = settings.SPECS[runtime][0]
            for value in values:
                self.assertEqual(settings.set_value(runtime, A, value)[field], value)
            for value in (values[0] - 1, values[1] + 1, True, 90.5, '', 'bad', None, [], {}):
                with self.subTest(runtime=runtime, value=value), self.assertRaises(ValueError):
                    settings.set_value(runtime, A, value)

    def test_accounts_and_runtimes_are_isolated(self):
        settings.set_value('api-credit', A, 6)
        settings.set_value('api-credit', B, 8)
        settings.set_value('codex', A, 90)
        self.assertEqual(settings.get('api-credit', A)['reset_day'], 6)
        self.assertEqual(settings.get('api-credit', B)['reset_day'], 8)
        self.assertEqual(settings.get('codex', B)['max_used_percent'], 98)
        settings.set_value('api-credit', A, 9)
        self.assertEqual(settings.get('codex', A)['max_used_percent'], 90)

    def test_set_preserves_other_fields_and_cleans_temporary_files(self):
        path = settings.store_path('codex', A)
        self.write(path, {'extra': 'keep'})
        settings.set_value('codex', A, 90)
        self.assertEqual(settings.read_json(path)['extra'], 'keep')
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ['settings.json', 'settings.json.lock'])
        with patch.object(settings.os, 'replace', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                settings.set_value('codex', A, 95)
        self.assertEqual(settings.get('codex', A)['max_used_percent'], 90)
        self.assertEqual(len(list(path.parent.iterdir())), 2)

    def test_invalid_and_corrupt_store_fall_back(self):
        path = settings.store_path('codex', A)
        for doc in ({'max_used_percent': 101}, {'max_used_percent': True}, {'max_used_percent': []}, []):
            self.write(path, doc)
            result = settings.get('codex', A)
            self.assertEqual(result['max_used_percent'], 98)
            self.assertEqual(result['source'], 'default')
        path.write_text('{invalid')
        self.assertEqual(settings.get('codex', A)['max_used_percent'], 98)
        self.assertIn('invalid', settings.get('codex', A)['note'])

    def test_one_time_migration_and_shared_precedence(self):
        self.assertEqual(api.configure_reset_day(legacy='6', ledger=self.books / 'ledger.jsonl'), 6)
        self.assertEqual(api.configure_reset_day(legacy='12', ledger=self.books / 'ledger.jsonl'), 6)
        self.assertEqual(api.configure_reset_day(ledger=self.books / 'ledger.jsonl'), 6)
        settings.set_value('api-credit', A, 9)
        self.assertEqual(api.configure_reset_day(legacy='6', ledger=self.books / 'ledger.jsonl'), 9)
        self.assertEqual(api.configure_reset_day(legacy='7', account=B), 7)
        self.assertEqual(settings.get('api-credit', A)['reset_day'], 9)

    def test_invalid_legacy_does_not_create_store(self):
        for raw in ('', '29', '0', 'no', True):
            self.assertIsNone(api.configure_reset_day(legacy=raw, account=A))
        self.assertFalse(settings.store_path('api-credit', A).exists())
        path = settings.store_path('api-credit', A)
        self.write(path, {'reset_day': 99})
        self.assertIsNone(api.configure_reset_day(legacy='6', account=A))
        self.assertEqual(settings.read_json(path)['reset_day'], 99)
        path.write_text('{invalid')
        self.assertIsNone(api.configure_reset_day(legacy='6', account=A))
        self.assertEqual(path.read_text(), '{invalid')

    def test_unresolved_account_does_not_create_shared_settings(self):
        self.assertEqual(api.configure_reset_day(legacy='6', ledger=self.root / 'legacy.jsonl'), 6)
        self.assertFalse(Path(os.environ['DOGANY_RESOURCE_SETTINGS_DIR']).exists())
        result = self.cli('set', 'api-credit', '--reset-day', '6', '--workspace', str(self.root / 'missing'))
        self.assertEqual(result.returncode, 2)
        self.assertIn('account unresolved', result.stderr)
        self.assertFalse(Path(os.environ['DOGANY_RESOURCE_SETTINGS_DIR']).exists())

    def test_concurrent_migration_cannot_overwrite_explicit_setting(self):
        settings.set_value('api-credit', A, 6)
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda day: settings.credit_reset_day(A, day)['reset_day'], range(10, 26)))
        self.assertEqual(results, [6] * 16)
        path = settings.store_path('api-credit', B)
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda day: settings.credit_reset_day(B, day), range(10, 26)))
        self.assertIn(settings.read_json(path)['reset_day'], range(10, 26))
        self.assertEqual(len(list(path.parent.iterdir())), 2)

    def test_credit_cycle_before_and_after_reset(self):
        settings.set_value('api-credit', A, 6)
        api.configure_reset_day(legacy='1', account=A)
        nov2 = calendar.timegm((2026, 11, 2, 12, 0, 0))
        nov6 = calendar.timegm((2026, 11, 6, 0, 0, 0))
        self.assertEqual(api.month_key(nov2), '2026-10-06')
        self.assertEqual(api.cycle_bounds(nov2)[1], nov6)
        self.assertEqual(api.month_key(nov6), '2026-11-06')
        self.assertEqual(api.pool_loss(api.pool_state(100, str(self.books / 'ledger.jsonl'), None, nov2), nov2)[0], 100.0)

    def test_cli_migrates_legacy_conf_only_once(self):
        conf = self.ws / 'config/agent.conf'
        conf.parent.mkdir()
        conf.write_text('PLAN=max_5x\nAPI_CREDIT_RESET_DAY=6\n')
        for day in (6, 12):
            conf.write_text(f'PLAN=max_5x\nAPI_CREDIT_RESET_DAY={day}\n')
            result = subprocess.run([sys.executable, '-B', str(LIB / 'api_credit_pool.py'),
                                     'probe', '--workspace', str(self.ws)],
                                    input='', capture_output=True, text=True, timeout=15)
            # This fixture has no API key; migration happens before the probe.
            self.assertEqual(settings.get('api-credit', A)['reset_day'], 6, result.stderr)
            self.assertIn(f'API_CREDIT_RESET_DAY={day}', conf.read_text())

    def test_store_override_is_shared_with_cli_and_beats_explicit_home(self):
        expected = self.root / 'settings/api-credit' / A / 'settings.json'
        self.assertEqual(settings.store_path('api-credit', A, self.root / 'other-home'), expected)
        result = self.cli('set', 'api-credit', '--account', A, '--reset-day', '8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(settings.get('api-credit', A)['reset_day'], 8)
        self.assertTrue(expected.is_file())
        self.assertFalse((self.home / '.dogany/resource-settings').exists())
        with patch.dict(os.environ, {'DOGANY_RESOURCE_SETTINGS_DIR': ''}):
            self.assertEqual(settings.store_path('api-credit', A),
                             self.home / '.dogany/resource-settings/api-credit' / A / 'settings.json')

    def test_process_override_never_migrates_and_shared_setting_survives(self):
        self.assertEqual(api.configure_reset_day('6', account=A), 6)
        self.assertFalse(settings.store_path('api-credit', A).exists())
        settings.set_value('api-credit', A, 9)
        self.assertEqual(api.configure_reset_day('6', account=A), 6)
        self.assertEqual(settings.get('api-credit', A)['reset_day'], 9)
        self.assertEqual(api.configure_reset_day(account=A), 9)

    def test_env_only_cli_override_cannot_leak_to_next_process(self):
        ledger = str(self.books / 'ledger.jsonl')
        command = [sys.executable, '-B', str(LIB / 'api_credit_pool.py'), 'seed',
                   '--state', str(self.books / 'reconcile.json'), '--usd', '0',
                   '--now', str(calendar.timegm((2026, 11, 10, 12, 0, 0)))]
        for raw_args, env_day in (([], '6'), (['--reset-day', '6'], ''),
                                  (['--reset-day', '6'], '8')):
            result = subprocess.run(command + raw_args,
                                    env=dict(os.environ, API_CREDIT_RESET_DAY=env_day),
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('2026-11-06', result.stdout)
            self.assertFalse(settings.store_path('api-credit', A).exists())
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('2026-11-06', result.stdout)
        self.assertFalse(settings.store_path('api-credit', A).exists())
        self.assertIsNone(api.configure_reset_day(ledger=ledger))

    def test_env_override_and_config_migration_keep_distinct_sources(self):
        conf = self.ws / 'config/agent.conf'
        conf.parent.mkdir()
        conf.write_text('PLAN=max_5x\nAPI_CREDIT_RESET_DAY=12\n')
        with patch.dict(os.environ, {'API_CREDIT_RESET_DAY': '6'}):
            result = subprocess.run([sys.executable, '-B', str(LIB / 'api_credit_pool.py'),
                                     'probe', '--workspace', str(self.ws)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(settings.get('api-credit', A)['reset_day'], 12, result.stderr)
            now = calendar.timegm((2026, 11, 10, 12, 0, 0))
            api.summary(str(self.ws), now)
            self.assertEqual(api.month_key(now), '2026-11-06')
            pools.read_pools(self.ws, now)
            self.assertEqual(api.month_key(now), '2026-11-06')
        self.assertEqual(settings.get('api-credit', A)['reset_day'], 12)
        self.assertEqual(api.configure_reset_day(account=A), 12)

    def test_cli_roundtrip_and_account_switch(self):
        result = self.cli('set', 'api-credit', '--workspace', str(self.ws), '--reset-day', '6')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['reset_day'], 6)
        self.write(self.home / '.codex/auth.json', {'tokens': {'account_id': 'codex-A'}})
        result = self.cli('set', 'codex', '--max-used-percent', '90')
        self.assertEqual(result.returncode, 0, result.stderr)
        account = json.loads(result.stdout)['account']
        result = self.cli('show', 'codex', '--tsv')
        self.assertEqual(result.stdout, '90\taccount resource setting\n')
        self.write(self.home / '.codex/auth.json', {'tokens': {'account_id': 'codex-B'}})
        self.assertEqual(json.loads(self.cli('show', 'codex').stdout)['max_used_percent'], 98)
        self.assertEqual(settings.get('codex', account)['max_used_percent'], 90)
        for args in [('set', 'codex', '--max-used-percent', '49'),
                     ('set', 'api-credit', '--reset-day', '29', '--account', A),
                     ('set', 'codex', '--reset-day', '6'),
                     ('show', 'codex', '--account', '../escape')]:
            self.assertEqual(self.cli(*args).returncode, 2)

    def test_piped_key_is_fingerprinted_and_never_stored(self):
        key = 'fixture-credit-secret'
        result = self.cli('set', 'api-credit', '--reset-day', '6', '--api-key-stdin', input=key)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['account'], api.key_fingerprint(key))
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(key, path.read_text())
        self.assertNotIn(key, result.stdout + result.stderr)
        self.assertEqual(self.cli('set', 'api-credit', '--reset-day', '6', '--api-key-stdin', input='').returncode, 2)

    def test_passive_reader_prefers_store_and_remains_read_only(self):
        now = time.time()
        conf = self.ws / 'config/agent.conf'
        conf.parent.mkdir()
        conf.write_text('PLAN=max_5x\nAPI_CREDIT_RESET_DAY=12\n')
        settings.set_value('api-credit', A, 6)
        settings.set_value('codex', B, 90)
        self.write(self.books / 'ledger.jsonl', {})
        quota = {'ok': True, 'auth_stamp': pools.codex_auth_stamp(), 'fetched_at': now,
                 'used_percent': 91, 'resets_at': now + 86400, 'window_duration_mins': 10080}
        self.write(pools.codex_cache_path(), quota)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(pools, 'pass_state', return_value=({'count': 0, 'expiry': None}, B)), \
             patch.object(pools, 'cached_plan', return_value='max_5x'), \
             patch.object(settings, 'resolve_account', side_effect=AssertionError('credential lookup')):
            view = pools.read_pools(self.ws, now)
            self.assertEqual(view['pools']['codex']['state'], 'closed')
            self.assertEqual(time.localtime(view['pools']['api']['reset_at']).tm_mday, 6)
            quota['used_percent'] = 90
            self.write(pools.codex_cache_path(), quota)
            before[pools.codex_cache_path()] = pools.codex_cache_path().read_bytes()
            self.assertEqual(pools.read_pools(self.ws, now)['pools']['codex']['state'], 'open')
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertIn('API_CREDIT_RESET_DAY=12', conf.read_text())

    def test_passive_legacy_source_does_not_write(self):
        self.assertEqual(api.configure_reset_day(legacy='6', account=A, migrate=False), 6)
        self.assertFalse(settings.store_path('api-credit', A).exists())
        self.assertEqual(api.configure_reset_day(legacy='6', account=A), 6)
        self.assertTrue(settings.store_path('api-credit', A).exists())

    def bash(self, body):
        script = 'source "$TEST_ROUTINES/dispatch-detached.sh"\n' + body
        return subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=15,
                              env=dict(os.environ, DISPATCH_TEST='1', TEST_ROUTINES=str(ROUTINES)))

    def test_real_codex_gate_uses_setting_and_boundary(self):
        self.write(self.home / '.codex/auth.json', {'tokens': {'account_id': 'worker-account'}})
        account = settings.resolve_account('codex')
        settings.set_value('codex', account, 90)
        quota = self.root / 'quota.py'
        quota.write_text('print(\'{"ok":true,"used_percent":91,"window_duration_mins":10080}\')\n')
        result = self.bash('CODEX_RATELIMIT_SCRIPT="' + str(quota) + '"\ncodex_quota_gate\n')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('ceiling: 90%', result.stderr)
        quota.write_text('print(\'{"ok":true,"used_percent":90,"window_duration_mins":10080}\')\n')
        result = self.bash('CODEX_RATELIMIT_SCRIPT="' + str(quota) + '"\ncodex_quota_gate\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('<=90', result.stdout)
        self.assertIn('account resource setting', result.stdout)
        settings.store_path('codex', account).unlink()
        result = self.bash('CODEX_RATELIMIT_SCRIPT="' + str(quota) + '"\ncodex_quota_gate\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('missing; using default 98', result.stdout)
        result = self.bash('RESOURCE_SETTINGS_PY=/missing/helper.py\nCODEX_RATELIMIT_SCRIPT="' + str(quota) + '"\ncodex_quota_gate\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('unreadable; using default 98', result.stdout)

    def test_codex_fallback_note_and_concurrency_reserve(self):
        for malformed in (False, True):
            if malformed:
                self.write(self.home / '.codex/auth.json', {'tokens': {'account_id': 'worker-account'}})
                self.write(settings.store_path('codex', settings.resolve_account('codex')), {'max_used_percent': 12})
            result = self.bash('load_codex_resource_settings\nprintf "%s|%s\\n" "$CODEX_QUOTA_MAX_PERCENT" "$CODEX_RESOURCE_NOTE"')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('98|codex max_used_percent', result.stdout)
            self.assertIn('using default 98', result.stdout)
        settings.set_value('codex', settings.resolve_account('codex'), 90)
        result = self.bash('''codex_quota_remaining() { echo 20; }
reset_pass_mode() { printf 'none\\t-\\t-\\t-\\n'; }
machine_workers() { MACHINE_CAP=20; MACHINE_NOTE=fixture; }
compute_dynamic_cap codex
printf '%s|%s\\n' "$DYNAMIC_CAP_VALUE" "$DYNAMIC_CAP_NOTE"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('reserve=10%', result.stdout)
        self.assertIn('1|', result.stdout)

    def test_onboarding_reset_day_without_dispatch_pointer(self):
        dest = self.ws / 'routines'
        dest.mkdir(parents=True)
        shutil.copytree(LIB, dest / 'lib', ignore=shutil.ignore_patterns('__pycache__'))
        # The base dispatcher delegates API workers and has no api_key_read.
        (dest / 'dispatch-detached.sh').write_text('# fixture core dispatcher\n')
        keyfile = self.root / 'credit.key'
        keyfile.write_text('fixture-onboarding-secret')
        conf = self.ws / 'config/agent.conf'
        conf.parent.mkdir()
        conf.write_text('PLAN=max_5x\n')
        command = ROUTINES.parents[2] / 'skills/dogany-api-credit-connect/check.sh'
        result = subprocess.run(['bash', str(command), 'reset-day', '6'],
                                env=dict(os.environ, DOGANY_ROOT=str(self.ws), ANTHROPIC_API_KEY_FILE=str(keyfile)),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        account = api.key_fingerprint('fixture-onboarding-secret')
        self.assertEqual(settings.get('api-credit', account)['reset_day'], 6)
        self.assertEqual(conf.read_text(), 'PLAN=max_5x\n')
        self.assertNotIn('fixture-onboarding-secret', result.stdout + result.stderr)

    def test_helper_mirror_is_identical(self):
        # The devkit mirror is canonical-only, outside the public package.
        # A canonical checkout with a missing mirror must still fail below.
        if not (ROUTINES.parents[2] / 'extracted-devkit').is_dir():
            self.skipTest('private devkit mirror is not shipped')
        mirror = ROUTINES.parents[2] / 'extracted-devkit/payload/routines/bundle/lib/resource_settings.py'
        self.assertEqual((LIB / 'resource_settings.py').read_bytes(), mirror.read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
