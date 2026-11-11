"""Read legacy gws credentials; never write to the legacy store."""
import base64
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import time

from . import store, transport
from .errors import GoogleAuthError


def read_gws(directory=None):
    # Legacy import is the only feature requiring cryptography.
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise GoogleAuthError('auth_unknown') from None
    directory = directory or Path.home() / '.config' / 'gws'
    for attempt in range(4):
        try:
            key = base64.b64decode((directory / '.encryption_key').read_bytes())
            blob = (directory / 'credentials.enc').read_bytes()
            credentials = json.loads(AESGCM(key).decrypt(blob[:12], blob[12:], None))
            obj = json.loads((directory / 'client_secret.json').read_text())
            return credentials, obj.get('installed') or obj.get('web') or obj
        except InvalidTag:
            if attempt == 3:
                raise GoogleAuthError('import_busy') from None
            time.sleep((1, 2, 4)[attempt])
        except FileNotFoundError:
            if attempt:
                raise GoogleAuthError('import_busy') from None
            return None
        except (OSError, ValueError, KeyError):
            raise GoogleAuthError('auth_unknown') from None


def import_gws(dry_run=False):
    with nullcontext() if dry_run else store.lock():
        source = read_gws()
        if not source:
            return {'imported': False, 'account': None, 'client': None, 'class': 'no_account'}
        credentials, legacy_client = source
        tok = transport.token({'grant_type': 'refresh_token', 'refresh_token': credentials['refresh_token'],
                               'client_id': legacy_client['client_id'], 'client_secret': legacy_client['client_secret']}, log_error=not dry_run)
        code, identity, _ = transport.request_json('GET', transport.USERINFO_URL,
                                                 headers={'Authorization': 'Bearer ' + tok['access_token']})
        if code != 200 or not isinstance(identity, dict) or not identity.get('sub'):
            raise GoogleAuthError('auth_unknown')
        sub = store.key(identity['sub'])
        from . import client
        kind = 'dogany' if legacy_client['client_id'] == client()['client_id'] else 'foreign'
        result = {'imported': False, 'account': identity.get('email'), 'client': kind, 'class': 'dry_run' if dry_run else 'exists'}
        if dry_run:
            return result
        path = store.root() / 'accounts' / (sub + '.json')
        if path.exists():
            return result
        ref = 'accounts/client-' + hashlib.sha256(legacy_client['client_id'].encode()).hexdigest() + '.client'
        store.write(store.root() / ref, {k: legacy_client[k] for k in ('client_id', 'client_secret')}, exclusive=True)
        rec = {'sub': sub, 'email': identity.get('email'), 'client_id': legacy_client['client_id'],
               'client_ref': ref, 'refresh_token': tok.get('refresh_token', credentials['refresh_token']),
               'granted': tok.get('scope', credentials.get('scope', '')).split(),
               'source': 'gws-import', 'created': time.time(), 'updated': time.time()}
        if store.write(path, rec, exclusive=True):
            try:
                store.first_default(sub)
            except GoogleAuthError:
                path.unlink(missing_ok=True)
                raise
            result.update(imported=True, **{'class': 'ok'})
    return result
