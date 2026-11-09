"""Framework-owned Google credentials. No consumer receives refresh tokens."""
import json
from pathlib import Path
import time
from urllib.parse import urlencode

from . import store, transport
from .errors import GoogleAuthError, GoogleHttpError, NeedsConsent

SCOPES = {
    'calendar': 'https://www.googleapis.com/auth/calendar',
    'tasks': 'https://www.googleapis.com/auth/tasks',
    'gmail.send': 'https://www.googleapis.com/auth/gmail.send',
    'openid': 'openid', 'email': 'https://www.googleapis.com/auth/userinfo.email',
}


def scope_urls(scopes):
    values = scopes.split() if isinstance(scopes, str) else scopes
    return sorted({SCOPES.get(s, s) for s in values} | {'openid', SCOPES['email']})


def short_scopes(scopes):
    reverse = {v: k for k, v in SCOPES.items()}
    return sorted({reverse.get(s, s) for s in scopes})


def client():
    try:
        obj = json.loads(Path(__file__).with_name('client.json').read_text())
        return obj.get('installed', obj)
    except (OSError, ValueError):
        raise GoogleAuthError('client_invalid') from None


def record_client(rec):
    if rec['client_ref'] == 'dogany':
        result = client()
    else:
        ref = Path(rec['client_ref'])
        if ref.is_absolute() or len(ref.parts) != 2 or ref.parts[0] != 'accounts' or ref.suffix != '.client':
            raise GoogleAuthError('client_invalid')
        result = store.read(store.root() / ref)
    if not result or result.get('client_id') != rec['client_id']:
        raise GoogleAuthError('client_invalid')
    return result


def status(account=None):
    rec = store.record(account)
    cap = store.read(store.root() / 'cap.json', {})
    paused = 'cap_suspected' if cap.get('until', 0) > time.time() else None
    if rec:
        marker = store.read(store.root() / 'paused' / store.key(rec['sub']), {})
        paused = paused or marker.get('cls')
    return {'connected': bool(rec), 'account': rec.get('email') if rec else None,
            'sub': rec.get('sub') if rec else None,
            'granted': short_scopes(rec.get('granted', [])) if rec else [],
            'source': rec.get('source') if rec else None,
            'client': 'foreign' if rec and rec['client_id'] != client()['client_id'] else 'dogany',
            'paused': paused}


def _access_token(scopes, account=None):
    requested = set(scope_urls(scopes))
    with store.lock():
        rec = store.record(account)
        # Imported legacy grants may lack OpenID; resource reads do not require it.
        required = requested - {'openid', SCOPES['email']}
        if not rec or not required.issubset(rec['granted']):
            raise NeedsConsent(short_scopes(required - set(rec['granted'] if rec else [])), account)
        sub = store.key(rec['sub'])
        path = store.root() / 'accounts' / (sub + '.atok')
        cached = store.read(path, {})
        if cached.get('expires_at', 0) > time.time() + 120 and required.issubset(cached.get('granted', [])):
            return cached
        credentials = record_client(rec)
        try:
            tok = transport.token({'grant_type': 'refresh_token', 'refresh_token': rec['refresh_token'],
                                   'client_id': credentials['client_id'], 'client_secret': credentials['client_secret']})
        except GoogleAuthError as exc:
            if exc.cls in ('token_revoked', 'client_invalid'):
                marker = store.root() / 'paused' / sub
                previous = store.read(marker, {})
                if previous.get('cls') != exc.cls:
                    store.write(marker, {'cls': exc.cls, 'since': time.time()})
                    store.log('paused ' + exc.cls)
            raise
        granted = tok.get('scope', ' '.join(rec['granted'])).split()
        if not required.issubset(granted):
            raise NeedsConsent(short_scopes(required - set(granted)), sub)
        if tok.get('refresh_token'):
            rec.update(refresh_token=tok['refresh_token'], updated=time.time())
            store.write(store.root() / 'accounts' / (sub + '.json'), rec)
        cache = {'access_token': tok['access_token'], 'expires_at': time.time() + int(tok.get('expires_in', 3600)), 'granted': granted}
        try:
            store.write(path, cache)
        except GoogleAuthError:
            store.log('access token cache write failed')
        (store.root() / 'paused' / sub).unlink(missing_ok=True)
        return cache


def get_access_token(scopes, account=None):
    return _access_token(scopes, account)['access_token']


def call(method, url, params=None, body=None, etag=None, scopes=None):
    token = get_access_token(scopes or [])
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params, doseq=True)
    headers = {'Authorization': 'Bearer ' + token}
    if etag:
        headers['If-Match'] = etag
    code, payload, raw = transport.request_json(method, url, body, headers)
    if code >= 400 and code != 412:
        try:
            with store.lock():
                store.log(raw)
        except GoogleAuthError:
            pass
        err = payload.get('error', {}) if isinstance(payload, dict) else {}
        entries = err.get('errors', []) if isinstance(err, dict) else []
        reason = entries[0].get('reason', 'unknown') if entries and isinstance(entries[0], dict) else ('unknown' if payload is not None else 'httpError')
        raise GoogleHttpError(code, reason, raw)
    return code, payload


def set_default(sub):
    with store.lock():
        if not store.record(store.key(sub)):
            raise NeedsConsent([], sub)
        store.write(store.root() / 'default', sub, plain=True)
    return {'ok': True}


def disconnect(account=None):
    with store.lock():
        rec = store.record(account)
        if rec:
            sub = store.key(rec['sub'])
            transport.revoke(rec['refresh_token'])
            stamp = str(time.time_ns())
            (store.root() / 'accounts' / (sub + '.json')).replace(store.root() / '_archive' / (sub + '-' + stamp + '.json'))
            (store.root() / 'accounts' / (sub + '.atok')).unlink(missing_ok=True)
            (store.root() / 'paused' / sub).unlink(missing_ok=True)
    return {'ok': True}
