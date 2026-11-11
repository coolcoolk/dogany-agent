#!/usr/bin/env python3
# test-lane: manual devkit-owned suite; run against extracted-devkit/framework
# test-covers: agents/.template/routines/lib/secret_store.py
"""DGN-2102: account fallback through dispatch reconciliation and usage summary.

Only fixture credentials, a fake security executable, temporary files and a
loopback cost report are used. No real keychain or provider is contacted.
"""
import calendar
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / 'lib'
sys.path.insert(0, str(LIB))
import secret_store as ss


def fixture_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = fixture_module('secret_fixtures', 'test-dgn1981-secret-store.py')
console = fixture_module('console_fixtures', 'test-dgn1978-console-truth.py')
KEY = 'sk-ant-admin01-FIXTURE-dgn2102-private'
OTHER = KEY + '-other'
SERVICES = ('dogany-anthropic-admin', 'dogany-anthropic-api-credit')
NOW = calendar.timegm((2026, 10, 10, 12, 0, 0))


class AccountFallback(unittest.TestCase):
    def setUp(self):
        self.e = fixtures.Env()
        self.e.apply(DOGANY_SECRET_BACKEND='keychain')
        for name in ('ANTHROPIC_ADMIN_KEY_FILE', 'ANTHROPIC_API_KEY_FILE',
                     'CLAUDE_CONFIG_DIR', 'API_CREDIT_MONTHLY_USD'):
            os.environ.pop(name, None)

    def tearDown(self):
        self.assertNotIn(KEY, self.e.all_text_outside_store())
        self.e.restore()

    def test_exact_fallback_missing_and_presence(self):
        for backend in ('keychain', 'file'):
            for service in SERVICES:
                with self.subTest(backend=backend, service=service):
                    os.environ['DOGANY_SECRET_BACKEND'] = backend
                    self.assertIsNone(ss.get(service, 'dogany'))
                    self.assertFalse(ss.has(service, 'dogany'))
                    # A prefix of the canonical account is not an exact match.
                    ss.set(service, 'dogany-other', OTHER)
                    ss.set(service, 'dogany', KEY)
                    self.assertTrue(ss.get(service, 'dogany') == KEY)
                    self.assertTrue(ss.has(service, 'dogany'))
                    self.assertTrue(ss.delete(service, 'dogany'))
                    start = len(self.e.calls())
                    self.assertTrue(ss.has(service, 'dogany'))
                    self.assertNotIn(' -w', self.e.calls()[start:])
                    self.assertTrue(ss.get(service, 'dogany') == OTHER)
                    # Lookup does not migrate, and delete never removes fallback.
                    self.assertFalse(ss.delete(service, 'dogany'))
                    self.assertTrue(ss.get(service, 'dogany-other') == OTHER)
                    self.assertTrue(ss.delete(service, 'dogany-other'))
                    self.assertIsNone(ss.get(service, 'dogany'))
                    self.assertFalse(ss.has(service, 'dogany'))

    def test_keychain_call_order_and_cli(self):
        for service in SERVICES:
            ss.set(service, 'legacy-account', KEY)
            start = len(self.e.calls())
            result = fixtures.cli(dict(os.environ), 'get', '--service', service,
                                  '--account', 'dogany')
            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.stdout == KEY)
            self.assertEqual(result.stderr, '')
            self.assertEqual(self.e.calls()[start:].splitlines(), [
                f'ARGV find-generic-password -s {service} -a dogany -w',
                f'ARGV find-generic-password -s {service} -w'])
            ss.set(service, 'dogany', OTHER)
            start = len(self.e.calls())
            self.assertTrue(ss.get(service, 'dogany') == OTHER)
            self.assertEqual(self.e.calls()[start:].splitlines(), [
                f'ARGV find-generic-password -s {service} -a dogany -w'])
        result = fixtures.cli(dict(os.environ), 'get', '--service', 'missing',
                              '--account', 'dogany')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (1, '', ''))

    def test_other_services_and_explicit_accounts_stay_exact(self):
        for backend in ('file', 'keychain'):
            ss.set('dogany-codex-api-key', 'other', KEY, backend)
            self.assertIsNone(ss.get('dogany-codex-api-key', 'dogany', backend))
            self.assertFalse(ss.has('dogany-codex-api-key', 'dogany', backend))
            ss.set(SERVICES[0], 'legacy-account', KEY, backend)
            self.assertIsNone(ss.get(SERVICES[0], 'different-user', backend))

    def test_locked_keychain_does_not_fall_back(self):
        ss.set(SERVICES[0], 'legacy-account', KEY)
        os.environ['STUB_KC_LOCKED'] = '1'
        for operation in ('get', 'has'):
            start = len(self.e.calls())
            result = fixtures.cli(dict(os.environ), operation, '--service', SERVICES[0],
                                  '--account', 'dogany')
            self.assertEqual(result.returncode, 3)
            self.assertIn('keychain_locked', result.stderr)
            self.assertNotIn(KEY, result.stdout + result.stderr)
            self.assertEqual(len(self.e.calls()[start:].splitlines()), 1)

    def test_file_fallback_preserves_private_permissions(self):
        os.environ['DOGANY_SECRET_BACKEND'] = 'file'
        ss.set(SERVICES[0], 'legacy-account', KEY)
        path = ss._entry_path(SERVICES[0], 'legacy-account')
        for operation in (ss.get, ss.has):
            path.chmod(0o644)
            with self.assertRaises(ss.SecretStoreError) as error:
                operation(SERVICES[0], 'dogany')
            self.assertEqual(error.exception.code, 'insecure_perms')
            path.chmod(0o600)
        path.unlink()
        path.symlink_to(Path(self.e.home) / 'missing-target')
        with self.assertRaises(ss.SecretStoreError):
            ss.get(SERVICES[0], 'dogany')

    def test_dispatch_to_usage_summary(self):
        endpoint = console.FakeConsole({'2026-10-09': '10083', '2026-10-10': ''})
        self.addCleanup(endpoint.close)
        for account in ('dogany', 'legacy-account', None):
            with self.subTest(account=account):
                for service in SERVICES:
                    for old in ('dogany', 'legacy-account'):
                        ss.delete(service, old)
                    if account:
                        ss.set(service, account, KEY)
                workspace = Path(self.e.home) / (account or 'missing')
                dispatch = workspace / '.telegram_bot' / 'dispatch'
                dispatch.mkdir(parents=True)
                env = dict(os.environ, DISPATCH_TEST='1', TEST_ROOT=str(fixtures.ROOT),
                           TEST_DISPATCH=str(dispatch), DOGANY_API_POOL_NOW=str(NOW),
                           API_ADMIN_BASE_URL=endpoint.url, API_CREDIT_MONTHLY_USD='100',
                           NO_PROXY='127.0.0.1', no_proxy='127.0.0.1', TZ='UTC')
                # Bash reads code on stdin: no fixture key enters its argv.
                script = '''source "$TEST_ROOT/routines/dispatch-detached.sh"
DISPATCH_DIR="$TEST_DISPATCH"
API_CREDIT_RECONCILE_STATE="$DISPATCH_DIR/api-credit-reconcile.json"
api_key_present && echo onboarded
api_pool_reconcile
'''
                start = len(endpoint.seen)
                result = subprocess.run(['bash'], input=script, env=env,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual('onboarded' in result.stdout, bool(account))
                self.assertEqual(len(endpoint.seen) - start, 1 if account else 0)
                self.assertNotIn(KEY, result.stdout + result.stderr)
                # This is the same summary command consumed by /usage.
                args = [sys.executable, '-B', str(LIB / 'api_credit_pool.py'), 'summary',
                        '--workspace', str(workspace), '--now', str(NOW)]
                result = subprocess.run(args, env=env, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(KEY, result.stdout + result.stderr + ' '.join(result.args))
                doc = json.loads(result.stdout)
                if account:
                    # Report truth is named console in the existing UI contract.
                    self.assertEqual(doc['truth'], 'console')
                    self.assertAlmostEqual(doc['spent'], 100.83)
                    self.assertTrue(doc['exhausted'])
                    self.assertEqual(doc['remaining'], 0.0)
                    self.assertIn('org cost report', doc['spent_src'])
                else:
                    self.assertEqual(doc['truth'], 'estimated')
                    self.assertEqual(doc['remaining'], 100.0)


if __name__ == '__main__':
    unittest.main()
