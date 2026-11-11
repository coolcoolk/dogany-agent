#!/usr/bin/env python3
# test-lane: fast    # temp HOME + stub security + loopback HTTP, measured ~2s
"""DGN-1981: the framework secret store and the readers that use it.

Offline: a temp HOME / DOGANY_SECRET_DIR for the file backend, a stub
`security` (records argv + stdin into a temp log, keeps items in a temp dir)
for the keychain backend, a loopback HTTP server for the verify call. No real
keychain, no network.

Covers: file backend round trip + 0700/0600 + loose perms refused; keychain
backend writes via `security -i` stdin (value never in argv), presence check
never asks for the value, locked keychain -> keychain_locked, not found;
invalid names/values refused; CLI exit codes and no value on stderr; the
verify call (ok / rejected / unknown, key only in the header); the replaced
readers (dispatch-detached.sh, api_credit_pool reconcile, browser-auth-open,
claude-usage) carry no hand-rolled `security find-generic-password` left.
"""
import http.server
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / 'routines'
LIB = ROUTINES / 'lib'
sys.path.insert(0, str(LIB))
import secret_store as ss  # noqa: E402
import secret_verify as sv  # noqa: E402

KEY = 'sk-ant-api03-FIXTURE-dgn1981-never-print-me'

STUB_SECURITY = r'''#!/usr/bin/env python3
import os, sys
d = os.environ['STUB_KC_DIR']
log = os.path.join(d, 'calls.log')
args = sys.argv[1:]
stdin = sys.stdin.read() if args[:1] == ['-i'] else ''
with open(log, 'a') as fh:
    fh.write('ARGV ' + ' '.join(args) + '\n')
if os.environ.get('STUB_KC_LOCKED'):
    sys.stderr.write('security: SecKeychainItemCreateFromContent: User interaction is not allowed.\n')
    sys.exit(36)
def item(s, a):
    return os.path.join(d, 'item-' + s.replace(' ', '_') + '@' + a)
def opt(xs, k):
    return xs[xs.index(k) + 1] if k in xs else ''
if args[:1] == ['-i']:
    import shlex
    for line in stdin.splitlines():
        xs = shlex.split(line)
        if xs and xs[0] == 'add-generic-password':
            open(item(opt(xs, '-s'), opt(xs, '-a')), 'w').write(opt(xs, '-w'))
    sys.exit(0)
cmd, rest = args[0], args[1:]
s, a = opt(rest, '-s'), opt(rest, '-a')
prefix = 'item-' + s.replace(' ', '_') + '@'
hits = sorted(p for p in os.listdir(d)
              if p == prefix + a or ('-a' not in rest and p.startswith(prefix)))
if cmd == 'find-generic-password':
    if not hits:
        sys.stderr.write('security: The specified item could not be found in the keychain.\n')
        sys.exit(44)
    if '-w' in rest:
        sys.stdout.write(open(os.path.join(d, hits[0])).read() + '\n')
    sys.exit(0)
if cmd == 'delete-generic-password':
    if not hits:
        sys.exit(44)
    os.unlink(os.path.join(d, hits[0]))
    sys.exit(0)
sys.exit(1)
'''


class Env:
    """A temp HOME with both backends pinned under it."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dgn1981-')
        self.home = self.tmp.name
        self.kc = os.path.join(self.home, 'kc')
        os.makedirs(self.kc)
        self.security = os.path.join(self.home, 'security')
        Path(self.security).write_text(STUB_SECURITY)
        os.chmod(self.security, 0o755)
        self.secrets = os.path.join(self.home, '.dogany', 'secrets')

    def env(self, **extra):
        e = dict(os.environ, HOME=self.home, STUB_KC_DIR=self.kc,
                 DOGANY_RESOURCE_SETTINGS_DIR=os.path.join(self.home, "resource-settings"),
                 DOGANY_SECURITY_BIN=self.security, DOGANY_SECRET_DIR=self.secrets)
        e.pop('DOGANY_SECRET_BACKEND', None)
        e.update(extra)
        return e

    def apply(self, **extra):
        self._saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env(**extra))

    def restore(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self.tmp.cleanup()

    def calls(self):
        p = os.path.join(self.kc, 'calls.log')
        return Path(p).read_text() if os.path.exists(p) else ''

    def all_text_outside_store(self):
        """Every file under HOME except the store's own entries and the stub."""
        out = []
        for dp, _, fns in os.walk(self.home):
            for fn in fns:
                p = os.path.join(dp, fn)
                if dp == self.secrets or fn.startswith('item-') or p == self.security:
                    continue
                out.append(Path(p).read_text(errors='replace'))
        return '\n'.join(out)


def cli(env, *args, stdin=''):
    return subprocess.run([sys.executable, '-B', str(LIB / 'secret_store.py'), *args],
                          input=stdin, capture_output=True, text=True, env=env)


class FileBackend(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.apply(DOGANY_SECRET_BACKEND='file')

    def tearDown(self):
        self.e.restore()

    def test_round_trip_and_modes(self):
        self.assertIsNone(ss.get('svc', 'acct'))
        self.assertFalse(ss.has('svc', 'acct'))
        ss.set('svc', 'acct', KEY)
        self.assertEqual(ss.get('svc', 'acct'), KEY)
        self.assertTrue(ss.has('svc', 'acct'))
        d = Path(self.e.secrets)
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        files = list(d.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        ss.set('svc', 'acct', KEY + 'x')  # overwrite in place
        self.assertEqual(ss.get('svc', 'acct'), KEY + 'x')
        self.assertEqual(len(list(d.iterdir())), 1)
        self.assertTrue(ss.delete('svc', 'acct'))
        self.assertFalse(ss.delete('svc', 'acct'))
        self.assertIsNone(ss.get('svc', 'acct'))

    def test_loose_permissions_refused(self):
        ss.set('svc', 'acct', KEY)
        f = next(Path(self.e.secrets).iterdir())
        os.chmod(f, 0o644)
        with self.assertRaises(ss.SecretStoreError) as cm:
            ss.get('svc', 'acct')
        self.assertEqual(cm.exception.code, 'insecure_perms')
        self.assertNotIn(KEY, str(cm.exception))
        os.chmod(f, 0o600)
        os.chmod(self.e.secrets, 0o755)
        with self.assertRaises(ss.SecretStoreError):
            ss.get('svc', 'acct')

    def test_invalid_input_refused(self):
        for bad in ('', 'has space', 'quo"te', "it's", 'back\\slash', 'tab\tx', 'kéy', 'x' * 5000):
            with self.assertRaises(ss.SecretStoreError) as cm:
                ss.set('svc', 'acct', bad)
            self.assertEqual(cm.exception.code, 'invalid_value')
        for name in ('', '../evil', 'a/b', 'x' * 200):
            with self.assertRaises(ss.SecretStoreError) as cm:
                ss.set(name, 'acct', KEY)
            self.assertEqual(cm.exception.code, 'invalid_name')
        self.assertFalse(os.path.exists(self.e.secrets))

    def test_cli(self):
        env = self.e.env(DOGANY_SECRET_BACKEND='file')
        r = cli(env, 'get', '--service', 'svc', '--account', 'acct')
        self.assertEqual((r.returncode, r.stdout), (1, ''))
        r = cli(env, 'set', '--service', 'svc', '--account', 'acct', stdin=KEY + '\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout + r.stderr, '')
        r = cli(env, 'has', '--service', 'svc', '--account', 'acct')
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        r = cli(env, 'get', '--service', 'svc', '--account', 'acct')
        self.assertEqual((r.returncode, r.stdout), (0, KEY))
        r = cli(env, 'set', '--service', 'svc', '--account', 'acct', stdin='bad value"' + KEY)
        self.assertEqual(r.returncode, 2)
        self.assertIn('invalid_value', r.stderr)
        self.assertNotIn(KEY, r.stdout + r.stderr)
        r = cli(env, 'delete', '--service', 'svc', '--account', 'acct')
        self.assertEqual(r.returncode, 0)
        r = cli(env, 'backend')
        self.assertEqual(r.stdout.strip(), 'file')
        self.assertNotIn(KEY, self.e.all_text_outside_store())


class KeychainBackend(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.apply(DOGANY_SECRET_BACKEND='keychain')

    def tearDown(self):
        self.e.restore()

    def test_set_goes_through_stdin_never_argv(self):
        ss.set('dogany-anthropic-admin', 'dogany', KEY)
        self.assertEqual(ss.get('dogany-anthropic-admin', 'dogany'), KEY)
        calls = self.e.calls()
        self.assertIn('ARGV -i', calls)
        self.assertNotIn(KEY, calls)  # argv log only: the value rode stdin
        self.assertFalse(os.path.exists(self.e.secrets))  # nothing on disk

    def test_has_never_asks_for_the_value(self):
        ss.set('svc', 'acct', KEY)
        before = self.e.calls().count(' -w')
        self.assertTrue(ss.has('svc', 'acct'))
        self.assertFalse(ss.has('svc', 'other'))
        self.assertEqual(self.e.calls().count(' -w'), before)

    def test_not_found_and_delete(self):
        self.assertIsNone(ss.get('svc', 'acct'))
        self.assertFalse(ss.delete('svc', 'acct'))
        ss.set('svc', 'acct', KEY)
        self.assertTrue(ss.delete('svc', 'acct'))
        self.assertIsNone(ss.get('svc', 'acct'))

    def test_any_account_read(self):
        ss.set('Claude Code-credentials', 'someone', KEY)
        self.assertEqual(ss.get('Claude Code-credentials'), KEY)

    def test_locked_keychain_is_named(self):
        os.environ['STUB_KC_LOCKED'] = '1'
        for fn in (lambda: ss.set('svc', 'acct', KEY), lambda: ss.get('svc', 'acct')):
            with self.assertRaises(ss.SecretStoreError) as cm:
                fn()
            self.assertEqual(cm.exception.code, 'keychain_locked')
            self.assertNotIn(KEY, str(cm.exception))
        r = cli(self.e.env(DOGANY_SECRET_BACKEND='keychain', STUB_KC_LOCKED='1'),
                'set', '--service', 'svc', '--account', 'acct', stdin=KEY)
        self.assertEqual(r.returncode, 3)
        self.assertIn('keychain_locked', r.stderr)
        self.assertNotIn(KEY, r.stdout + r.stderr)

    def test_missing_security_tool(self):
        os.environ['DOGANY_SECURITY_BIN'] = os.path.join(self.e.home, 'nope')
        with self.assertRaises(ss.SecretStoreError) as cm:
            ss.get('svc', 'acct')
        self.assertEqual(cm.exception.code, 'store_failed')


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    seen = []

    def do_GET(self):
        type(self).seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, *a):
        pass


class Verify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(('127.0.0.1', 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.srv.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self._saved = dict(os.environ)
        os.environ.update(DOGANY_SECRET_VERIFY_BASE_URL=self.base,
                          NO_PROXY='127.0.0.1', no_proxy='127.0.0.1')
        for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
            os.environ.pop(k, None)
        _Handler.seen = []

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_outcomes(self):
        for status, want in ((200, 'ok'), (401, 'rejected'), (403, 'rejected'),
                             (500, 'unknown'), (429, 'unknown')):
            _Handler.status = status
            self.assertEqual(sv.verify('anthropic_api', KEY, timeout=5), want)
        path, headers = _Handler.seen[0]
        self.assertEqual(path, '/v1/models?limit=1')
        self.assertEqual(headers.get('x-api-key'), KEY)
        self.assertNotIn(KEY, path)
        _Handler.status = 200
        self.assertEqual(sv.verify('anthropic_admin', KEY, timeout=5), 'ok')
        self.assertTrue(_Handler.seen[-1][0].startswith('/v1/organizations/cost_report?'))

    def test_unreachable_is_unknown(self):
        os.environ['DOGANY_SECRET_VERIFY_BASE_URL'] = 'http://127.0.0.1:9'
        self.assertEqual(sv.verify('anthropic_api', KEY, timeout=2), 'unknown')
        self.assertEqual(sv.verify('nope', KEY), 'unknown')

    def test_slots_match_the_readers(self):
        dd = (ROUTINES / 'dispatch-detached.sh').read_text()
        for slot in sv.SLOTS.values():
            reader = ((LIB / 'codex_login.py').read_text()
                      if slot.name == 'codex-api-key' else dd)
            self.assertIn(f'"{slot.service}"', reader)
        self.assertEqual(sv.slot(' API-Credit ').service, 'dogany-anthropic-api-credit')
        self.assertIsNone(sv.slot('nope'))


class Readers(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.tmp.cleanup()

    def test_no_hand_rolled_keychain_reads_left(self):
        for rel in ('dispatch-detached.sh', 'browser-auth-open.sh', 'lib/api_credit_pool.py'):
            text = (ROUTINES / rel).read_text()
            self.assertNotRegex(text, r'(?m)^[^#]*security find-generic-password', rel)
            self.assertIn('secret_store', text, rel)
        cu = (ROUTINES / 'claude-usage.sh').read_text()
        # one fallback line for a copy shipped without lib/ (the OSS root)
        self.assertEqual(len(re.findall(r'(?m)^[^#]*security find-generic-password', cu)), 1)
        self.assertIn('lib/secret_store.py', cu)

    def _dispatch(self, body, **env):
        script = ('source "$TEST_ROOT/routines/dispatch-detached.sh"\n' + body)
        e = self.e.env(DISPATCH_TEST='1', TEST_ROOT=str(ROOT), DOGANY_SECRET_BACKEND='file')
        e.pop('ANTHROPIC_API_KEY_FILE', None)
        e.pop('ANTHROPIC_ADMIN_KEY_FILE', None)
        e.update(env)
        return subprocess.run(['bash', '-c', script], env=e, text=True, capture_output=True)

    def test_dispatch_reads_the_store(self):
        r = self._dispatch('api_key_present || echo absent\n')
        self.assertIn('absent', r.stdout)
        r = cli(self.e.env(DOGANY_SECRET_BACKEND='file'), 'set', '--service',
                'dogany-anthropic-api-credit', '--account', 'dogany', stdin=KEY)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = cli(self.e.env(DOGANY_SECRET_BACKEND='file'), 'set', '--service',
                'dogany-anthropic-admin', '--account', 'dogany', stdin=KEY + 'A')
        r = self._dispatch('api_key_present && echo present\n'
                           'k="$(api_key_read)"; [[ "$k" == "' + KEY + '" ]] && echo match\n'
                           'a="$(api_admin_key_read)"; [[ "$a" == "' + KEY + 'A" ]] && echo admin\n')
        self.assertIn('present', r.stdout)
        self.assertIn('match', r.stdout)
        self.assertIn('admin', r.stdout)
        self.assertNotIn(KEY, r.stdout + r.stderr)

    def test_legacy_key_file_still_read(self):
        kf = os.path.join(self.e.home, 'legacy-key')
        Path(kf).write_text(KEY + '\n')
        r = self._dispatch('k="$(api_key_read)"; [[ "$k" == "' + KEY + '" ]] && echo match\n',
                           ANTHROPIC_API_KEY_FILE=kf)
        self.assertIn('match', r.stdout)

    def test_pool_reconcile_falls_back_to_the_store(self):
        e = self.e.env(DOGANY_SECRET_BACKEND='file')
        state = os.path.join(self.e.home, 'reconcile.json')
        args = [sys.executable, '-B', str(LIB / 'api_credit_pool.py'), 'reconcile',
                '--state', state, '--base-url', 'http://127.0.0.1:9']
        r = subprocess.run(args, input='', capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 1)
        self.assertIn('no admin key', r.stdout)
        cli(e, 'set', '--service', 'dogany-anthropic-admin', '--account', 'dogany', stdin=KEY)
        r = subprocess.run(args, input='', capture_output=True, text=True, env=e)
        self.assertNotIn('no admin key', r.stdout)  # it tried the fetch with the stored key
        self.assertNotIn(KEY, r.stdout + r.stderr)

    def test_browser_auth_open_reads_the_store(self):
        ab = os.path.join(self.e.home, 'agent-browser')
        Path(ab).write_text('#!/bin/sh\necho "AB $*"; [ -n "$AGENT_BROWSER_ENCRYPTION_KEY" ] && echo HASKEY\nexit 0\n')
        os.chmod(ab, 0o755)
        e = self.e.env(DOGANY_SECRET_BACKEND='file', AGENT_BROWSER_NAMESPACE='ns1',
                       BROWSER_AUTH_AGENT_BROWSER=ab)
        script = str(ROUTINES / 'browser-auth-open.sh')
        r = subprocess.run(['bash', script, 'fixture', 'https://example.invalid', '--check-text', 'ok'], env=e,
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('could not be loaded from the secret store', r.stderr)
        cli(e, 'set', '--service', 'agent-browser-enc-key', '--account', 'ns1', stdin=KEY)
        r = subprocess.run(['bash', script, 'fixture', 'https://example.invalid', '--check-text', 'ok'], env=e,
                           capture_output=True, text=True, timeout=30)
        self.assertNotIn('could not be loaded', r.stderr)
        self.assertIn('HASKEY', r.stdout)  # the key reached agent-browser's env
        self.assertNotIn(KEY, r.stdout + r.stderr)



class VerifyBaseOverride(unittest.TestCase):
    """D7 (DGN-1981): the base-URL override is honored for loopback only."""
    def setUp(self):
        self._saved = os.environ.get('DOGANY_SECRET_VERIFY_BASE_URL')

    def tearDown(self):
        if self._saved is None:
            os.environ.pop('DOGANY_SECRET_VERIFY_BASE_URL', None)
        else:
            os.environ['DOGANY_SECRET_VERIFY_BASE_URL'] = self._saved

    def test_non_loopback_override_ignored(self):
        os.environ['DOGANY_SECRET_VERIFY_BASE_URL'] = 'https://example.invalid'
        self.assertEqual(sv._base_url(), 'https://api.anthropic.com')

    def test_loopback_override_honored(self):
        os.environ['DOGANY_SECRET_VERIFY_BASE_URL'] = 'http://127.0.0.1:9/'
        self.assertEqual(sv._base_url(), 'http://127.0.0.1:9')

if __name__ == '__main__':
    unittest.main(verbosity=1)
