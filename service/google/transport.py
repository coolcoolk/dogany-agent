"""Small stdlib transport, injectable in fixture tests."""
import json
import socket
from urllib import error, parse, request
from .errors import GoogleAuthError, token_error
from . import store

TOKEN_URL = 'https://oauth2.googleapis.com/token'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'


def request_json(method, url, body=None, headers=None, form=False):
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = (parse.urlencode(body) if form else json.dumps(body)).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=15) as response:
            code, raw = response.status, response.read().decode('utf-8', errors='replace')
    except error.HTTPError as exc:
        code, raw = exc.code, exc.read().decode('utf-8', errors='replace')
    except (error.URLError, OSError, socket.timeout):
        raise GoogleAuthError('network') from None
    try:
        payload = json.loads(raw) if raw.strip() else None
    except ValueError:
        payload = None
    return code, payload, raw


def token(body, log_error=True):
    code, payload, raw = request_json('POST', TOKEN_URL, body, form=True)
    if code >= 400 or not isinstance(payload, dict) or 'access_token' not in payload:
        if log_error:
            store.log(raw)
        raise token_error(code, payload)
    return payload


def revoke(token):
    if token:
        try:
            request_json('POST', REVOKE_URL, {'token': token}, form=True)
        except GoogleAuthError:
            store.log('revoke network failure')
