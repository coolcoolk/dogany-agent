"""DGN-1854 W5 -- local stub of service.google (frozen contract, spec 1.1).

W1 builds the real module in parallel; until it merges, the seam is coded and
tested against this stub, which has exactly the frozen surface:

    get_access_token(scopes, account=None) -> str
    status(account=None) -> dict
    call(method, url, params=None, body=None, etag=None, scopes=...)
        -> (status, json); 412 returned; other >=400 -> GoogleHttpError
    NeedsConsent(missing, account), GoogleAuthError(cls),
    GoogleHttpError(code, reason)

The answers come from a `server` callable (method, url, params, body, etag)
-> (http_status, body) where body is parsed JSON, raw text, or None; the
fake Google in fake_google.py and the recorded-fixture replayer are servers.

GoogleHttpError.reason follows the recorded gws rule (errors.gws_error_fields);
`carry_body=True` also attaches the raw `body`, which lets the seam reproduce
gws's message text and the code-from-JSON quirk. Both modes are tested.

English/ASCII only.
"""

import json
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from service.calendar.errors import gws_error_fields  # noqa: E402


class NeedsConsent(Exception):
    def __init__(self, missing, account=None):
        super().__init__("needs_consent")
        self.missing = missing
        self.account = account
        self.cls = "needs_consent"


class GoogleAuthError(Exception):
    def __init__(self, cls):
        super().__init__(cls)
        self.cls = cls


class GoogleHttpError(Exception):
    def __init__(self, code, reason, message="", body=None, carry_body=False):
        super().__init__("HTTP %s %s" % (code, reason))
        self.code = code
        self.reason = reason
        self.message = message
        if carry_body:
            self.body = body


CONNECTED = {"connected": True, "account": "owner@example.com",
             "sub": "1234567890", "granted": ["calendar", "tasks", "openid",
                                               "email"],
             "source": "dogany", "client": "dogany", "paused": None}


def make_module(server, status_dict=None, carry_body=True, call_raises=None,
                token_raises=None, status_raises=None):
    """A module object with the service.google surface.

    call_raises / token_raises / status_raises: an exception instance (or a
    zero-arg callable returning one) raised by that function instead."""
    mod = types.ModuleType("service_google_stub")
    mod.NeedsConsent = NeedsConsent
    mod.GoogleAuthError = GoogleAuthError
    mod.GoogleHttpError = GoogleHttpError
    mod.calls = []
    st = dict(CONNECTED if status_dict is None else status_dict)

    def _maybe_raise(spec):
        if spec is None:
            return
        exc = spec() if callable(spec) and not isinstance(spec, BaseException) \
            else spec
        raise exc

    def get_access_token(scopes, account=None):
        _maybe_raise(token_raises)
        missing = [s for s in scopes if s not in st.get("granted", [])]
        if not st.get("connected") or missing:
            raise NeedsConsent(missing, account)
        if st.get("paused"):
            raise GoogleAuthError(st["paused"])
        return "fake-access-token"

    def status(account=None):
        _maybe_raise(status_raises)
        return dict(st)

    def call(method, url, params=None, body=None, etag=None,
             scopes=("calendar",)):
        mod.calls.append({"method": method, "url": url, "params": params,
                          "body": body, "etag": etag, "scopes": list(scopes)})
        _maybe_raise(call_raises)
        get_access_token(list(scopes))
        http_status, resp = server(method, url, params, body, etag)
        if http_status == 412:
            return 412, _parsed(resp)
        if http_status >= 400:
            code, reason, message = gws_error_fields(http_status, resp)
            raise GoogleHttpError(http_status, reason, message, body=resp,
                                  carry_body=carry_body)
        return http_status, _parsed(resp)

    mod.get_access_token = get_access_token
    mod.status = status
    mod.call = call
    mod.state = st
    return mod


def _parsed(resp):
    if isinstance(resp, str):
        if not resp.strip():
            return None
        try:
            return json.loads(resp)
        except ValueError:
            return resp
    return resp
