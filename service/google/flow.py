"""PKCE consent, a single exchange shared by loopback and bridge completion."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from . import client, scope_urls, store, transport
from .errors import GoogleAuthError, render

TTL = 600
SUCCESS_PAGE = render('google.login.connected_page').encode()
ERROR_PAGE = render('google.login.failed_page').encode()


def _origin(origin):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+:-?[0-9]+', origin):
        raise GoogleAuthError('flow_unknown')
    return origin


def _b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def prepare(scopes, origin, port, account=None):
    """Prepare under lock; start() already owns a listening socket."""
    _origin(origin)
    with store.lock():
        store.probe()
        cap = store.read(store.root() / 'cap.json', {})
        if cap.get('until', 0) > time.time():
            raise GoogleAuthError('cap_suspected')
        rec = store.record(account) if account else None
        if account and not rec:
            raise GoogleAuthError('flow_unknown')
        credentials = client()
        requested = set(scope_urls(scopes))
        if rec:
            requested.update(rec['granted'])
        state, verifier, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = time.time() + TTL
        pending = {'verifier': verifier, 'scopes': sorted(requested), 'port': port,
                   'origin': origin, 'account_expected': rec['sub'] if rec else account,
                   'expires_at': expires, 'created': time.time(), 'done': False,
                   'nonce': nonce, 'callback_hit': False}
        # Persist a trusted instance path only when the caller names that instance.
        project = os.environ.get('PROJECT_ROOT')
        if project and Path(project).resolve().name == origin.split(':')[0]:
            pending['instance_path'] = str(Path(project).resolve())
        store.write(store.root() / 'pending' / (state + '.json'), pending, exclusive=True)
        params = {'client_id': credentials['client_id'], 'redirect_uri': 'http://127.0.0.1:%d/' % port,
                  'response_type': 'code', 'scope': ' '.join(sorted(requested)),
                  'state': state, 'nonce': nonce, 'access_type': 'offline',
                  'code_challenge_method': 'S256',
                  'code_challenge': _b64(hashlib.sha256(verifier.encode()).digest())}
        if rec and rec['email']:
            params['login_hint'] = rec['email']
        if rec and rec['client_id'] == credentials['client_id']:
            params['include_granted_scopes'] = 'true'
        else:
            params['prompt'] = 'consent'
        return {'flow_id': state, 'url': transport.AUTHORIZE_URL + '?' + urlencode(params), 'expires_at': expires}


def start(scopes, origin, account=None):
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', 0))
        sock.listen(5)
        result = prepare(scopes, origin, sock.getsockname()[1], account)
        # Pass the bound socket: no release/rebind race and no secret in argv.
        subprocess.Popen([sys.executable, '-m', 'service.google', '_listen',
                          result['flow_id'], str(sock.fileno())],
                         cwd=str(Path(__file__).resolve().parents[2]),
                         pass_fds=(sock.fileno(),), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return result
    except OSError:
        raise GoogleAuthError('network') from None
    finally:
        sock.close()


def _identity(tok, pending):
    try:
        payload = tok['id_token'].split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        # Tokens arrive directly from the HTTPS token endpoint, never from a caller.
        if (claims.get('aud') != client()['client_id'] or
                claims.get('iss') not in ('https://accounts.google.com', 'accounts.google.com') or
                claims.get('nonce') != pending['nonce'] or claims.get('exp', 0) <= time.time()):
            raise ValueError()
        return store.key(claims['sub']), claims.get('email')
    except (KeyError, ValueError, TypeError, IndexError):
        raise GoogleAuthError('auth_unknown') from None


def _relay(pending, result):
    """Use the existing inbox route; no direct owner or operator notification."""
    if not pending.get('instance_path'):
        return
    inbox = Path(pending['instance_path']) / '.telegram_bot' / 'session-inbox'
    try:
        inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = 'google-connect-' + secrets.token_hex(12) + '.md'
        store.write(inbox / name,
                    '[google-connect-result] class=%s sub=%s origin=%s\n' %
                    (result['class'], result.get('sub') or '-', pending['origin']), plain=True)
    except (OSError, GoogleAuthError):
        store.log('completion relay unavailable')


def finish(url, origin, callback=False):
    _origin(origin)
    try:
        parsed = urlsplit(url.strip())
        query = parse_qs(parsed.query)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError()
        if len(query.get('state', [])) != 1:
            raise ValueError()
        state = store.key(query['state'][0])
        port = parsed.port
    except (ValueError, GoogleAuthError):
        raise GoogleAuthError('flow_unknown') from None
    with store.lock():
        path = store.root() / 'pending' / (state + '.json')
        pending = store.read(path)
        if not pending or pending['origin'] != origin or port != pending['port']:
            raise GoogleAuthError('flow_unknown')
        if pending['done']:
            return {'ok': False, 'class': 'flow_done'}
        if pending['expires_at'] <= time.time():
            pending['callback_hit'] = True
            store.write(path, pending)
            raise GoogleAuthError('flow_expired')
        pending.update(done=True, callback_hit=True)
        # Claim durably before exchange. No second caller can redeem this code.
        store.write(path, pending)
        tok = None
        try:
            if 'error' in query:
                raise GoogleAuthError({'access_denied': 'consent_denied',
                                       'disallowed_useragent': 'browser_blocked'}.get(query['error'][0], 'auth_unknown'))
            if len(query.get('code', [])) != 1:
                raise GoogleAuthError('flow_unknown')
            credentials = client()
            tok = transport.token({'grant_type': 'authorization_code', 'code': query['code'][0],
                                   'code_verifier': pending['verifier'],
                                   'redirect_uri': 'http://127.0.0.1:%d/' % pending['port'],
                                   'client_id': credentials['client_id'], 'client_secret': credentials['client_secret']})
            sub, email = _identity(tok, pending)
            if pending['account_expected'] and pending['account_expected'] != sub:
                raise GoogleAuthError('account_mismatch')
            granted = scope_urls(tok.get('scope', '').split())
            # Do not infer identity scopes when checking a consent response.
            actual = {('https://www.googleapis.com/auth/userinfo.email' if s == 'email' else s) for s in tok.get('scope', '').split()}
            if not set(pending['scopes']).issubset(actual):
                raise GoogleAuthError('scope_partial')
            previous = store.record(sub)
            refresh = tok.get('refresh_token')
            if not refresh and previous and previous['client_id'] == credentials['client_id']:
                refresh = previous['refresh_token']
            if not refresh:
                raise GoogleAuthError('auth_unknown')
            rec = {'sub': sub, 'email': email, 'client_id': credentials['client_id'],
                   'client_ref': 'dogany', 'refresh_token': refresh, 'granted': granted,
                   'source': 'dogany', 'created': previous['created'] if previous else time.time(), 'updated': time.time()}
            record_path = store.root() / 'accounts' / (sub + '.json')
            # Roll back the record if the first-default write fails.
            store.write(record_path, rec)
            try:
                store.first_default(sub)
            except GoogleAuthError:
                if previous:
                    store.write(record_path, previous)
                else:
                    record_path.unlink(missing_ok=True)
                raise
            (store.root() / 'accounts' / (sub + '.atok')).unlink(missing_ok=True)
            (store.root() / 'paused' / sub).unlink(missing_ok=True)
            if not previous:
                store.log('first_consent %s %s' % (hashlib.sha256(sub.encode()).hexdigest()[:12], time.strftime('%Y-%m-%d', time.gmtime())))
            result = {'ok': True, 'class': 'ok', 'sub': sub}
        except (GoogleAuthError, OSError, ValueError, KeyError, TypeError) as exc:
            cls = exc.cls if isinstance(exc, GoogleAuthError) else ('store_unwritable' if isinstance(exc, OSError) else 'auth_unknown')
            # Revoking a partial incremental grant can revoke the existing grant too.
            if tok and cls != 'scope_partial':
                transport.revoke(tok.get('refresh_token') or tok.get('access_token'))
            result = {'ok': False, 'class': 'flow_expired' if cls == 'token_revoked' else cls}
        pending['result'] = result
        # The durable claim already prevents replay if this diagnostic write fails.
        try:
            store.write(path, pending)
        except GoogleAuthError:
            store.log('completion result write failed')
        _relay(pending, result)
        return result


def listen(state, fd):
    pending = store.read(store.root() / 'pending' / (store.key(state) + '.json'))
    if not pending:
        return

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                query = parse_qs(urlsplit(self.path).query)
                if query.get('state') != [state]:
                    raise GoogleAuthError('flow_unknown')
                result = finish('http://127.0.0.1:%d%s' % (pending['port'], self.path), pending['origin'], callback=True)
                page = SUCCESS_PAGE if result['ok'] else ERROR_PAGE
            except (GoogleAuthError, ValueError):
                page = ERROR_PAGE
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'none'")
            self.end_headers()
            self.wfile.write(page)

    server = HTTPServer(('127.0.0.1', pending['port']), Handler, bind_and_activate=False)
    server.socket.close()
    server.socket = socket.socket(fileno=fd)
    server.timeout = 1
    try:
        while time.time() < pending['expires_at']:
            if store.read(store.root() / 'pending' / (state + '.json'), {}).get('done'):
                break
            server.handle_request()
    finally:
        server.server_close()
    # The bridge claims the prompt through _expired-prompt; do not consume it here.


def expired_prompt(origin):
    """Atomically claim the once-per-chat blocked-screen prompt (bridge renders)."""
    _origin(origin)
    with store.lock():
        now = time.time()
        flows = [store.read(p) for p in (store.root() / 'pending').glob('*.json')]
        recent = sorted((p for p in flows if p and p['origin'] == origin and p.get('created', 0) > now - 86400), key=lambda p: p['created'])
        consecutive = 0
        for p in reversed(recent):
            if p['expires_at'] > now or p.get('callback_hit'):
                break
            consecutive += 1
        ledger = store.root() / ('prompt-' + hashlib.sha256(origin.encode()).hexdigest() + '.json')
        prior = store.read(ledger, {})
        if consecutive < 2 or prior.get('since', 0) > now - 86400:
            return None
        store.write(ledger, {'since': now, 'origin': origin})
        return 'google.login.blocked_screen_ask'


def record_blocked_screen(origin, blocked):
    _origin(origin)
    with store.lock():
        ledger = store.read(store.root() / ('prompt-' + hashlib.sha256(origin.encode()).hexdigest() + '.json'), {})
        if ledger.get('since', 0) < time.time() - 86400:
            raise GoogleAuthError('flow_unknown')
        if blocked:
            store.write(store.root() / 'cap.json', {'until': time.time() + 86400, 'origin': origin})
            store.log('operator cap_suspected')
    return {'ok': True, 'class': 'cap_suspected' if blocked else 'ok', 'operator_alert': bool(blocked)}
