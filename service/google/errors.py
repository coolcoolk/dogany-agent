"""Stable owner-facing classes; Google response text never becomes owner copy."""
import re

KEYS = {name: 'google.login.' + key for name, key in {
    'needs_consent': 'start', 'consent_denied': 'denied',
    'scope_partial': 'partial', 'flow_expired': 'expired',
    'flow_unknown': 'expired', 'flow_done': 'done',
    'account_mismatch': 'account_mismatch', 'token_revoked': 'revoked',
    'browser_blocked': 'browser_blocked', 'cap_suspected': 'cap',
    'client_invalid': 'internal', 'network': 'network', 'google_5xx': 'network',
    'store_busy': 'internal', 'store_unwritable': 'internal',
    'import_busy': 'internal', 'auth_unknown': 'internal',
}.items()}


class GoogleAuthError(RuntimeError):
    def __init__(self, cls):
        self.cls = cls if cls in KEYS else 'auth_unknown'
        self.key = KEYS[self.cls]
        super().__init__(self.cls)


class NeedsConsent(GoogleAuthError):
    def __init__(self, missing, account=None):
        self.missing = missing
        self.account = account
        super().__init__('needs_consent')


class GoogleHttpError(RuntimeError):
    def __init__(self, code, reason, body=None):
        self.code = self.http_code = code
        self.reason = reason
        self.body = body
        super().__init__('google_http_error')


def redact(text):
    text = re.sub(r'(?:ya29\.|1//|GOCSPX-)[^\s"&<>]+', '[redacted]', str(text))
    text = re.sub(r'(?i)(?:code|access_token|refresh_token|client_secret|id_token|code_verifier)["\s]*[=:]["\s]*[^\s&,}\"]+', '[redacted]', text)
    return text


def token_error(code, payload):
    error = payload.get('error') if isinstance(payload, dict) else None
    if code >= 500:
        return GoogleAuthError('google_5xx')
    return GoogleAuthError({
        'invalid_grant': 'token_revoked', 'invalid_client': 'client_invalid',
        'unauthorized_client': 'client_invalid', 'access_denied': 'consent_denied',
        'disallowed_useragent': 'browser_blocked',
    }.get(error, 'auth_unknown'))


def render(key, locale='en', **values):
    import json
    from pathlib import Path
    messages = json.loads(Path(__file__).with_name('messages.json').read_text())
    return messages.get(locale, messages['en'])[key].format(**values)
