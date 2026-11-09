"""service.calendar.google -- Google backend over service.google.call (W5).

conn.call    == kit adapter.gws()          (dict, or GwsError)
conn.delete  == kit adapter.gws_delete()   (True, or GwsError)
conn.http    == kit http_direct._call      ((status, json); 412 -> (412, None))

Error parity (D10+): a Google error answer becomes the GwsError the kit got
from the gws CLI for the same answer -- same http_code, same reason, same
message text ("API error <code> <reason>: <message>" for call,
"API error <code>: <message>" for delete). See errors.gws_error_fields.

Seam guarantee (spec 1.1, D17): nothing but a GwsError leaves call / delete /
http, and health() never raises:
  NeedsConsent                    -> CalendarPaused("needs_consent")
  GoogleAuthError(cls) pause/unknown -> CalendarPaused(cls)
  GoogleAuthError(cls) transient  -> GwsError(503, cls)
  GoogleHttpError(code, reason)   -> GwsError(code, reason) (gws rule)
  OSError / timeout (transport)   -> GwsError(503, "network")
  anything else                   -> GwsError(500, "internalError"), the
                                     answer gws itself gives on an internal
                                     failure (exit 5); 500 is retryable.
KeyboardInterrupt / SystemExit are not caught.

service.google is imported lazily; tests point GOOGLE_MODULE at the fake
(conformance/google_stub.py) -- W1's real module is the default.

English/ASCII only.
"""

import importlib

from . import verbs
from .errors import (BackendUnavailable, CalendarPaused, GwsError,
                     RETRY_CLASSES, gws_error_fields)

# Test seam: a module object with the service.google surface. None = import
# service.google on first use.
GOOGLE_MODULE = None

CAPABILITIES = {"calendar_create_sources": ["google"], "event_color": True,
                "tasks_kind": "google_tasks", "change_feed": "native",
                "offline_write": False, "attendees_write": True,
                "window": None}
REQUIRED_SCOPES = ("calendar",)
# status().paused values that do not pause an existing mirror: cap_suspected
# only stops offering NEW consents (spec 6.1).
_NON_PAUSING = ("cap_suspected",)


def google_module():
    if GOOGLE_MODULE is not None:
        return GOOGLE_MODULE
    try:
        return importlib.import_module("service.google")
    except Exception as e:
        raise BackendUnavailable("google_unavailable",
                                 "service.google import failed: %s"
                                 % type(e).__name__)


def _auth_cls(exc):
    cls = getattr(exc, "cls", None)
    if not isinstance(cls, str) and exc.args and isinstance(exc.args[0], str):
        cls = exc.args[0]
    return cls if isinstance(cls, str) and cls else "auth_unknown"


class GoogleConnector(object):
    backend = "google"

    def __init__(self, gmod, sub=None, required_scopes=REQUIRED_SCOPES):
        self._g = gmod
        self._sub = sub
        self._required_scopes = tuple(required_scopes)
        self._error_class = GwsError
        self._paused_class = CalendarPaused

    # -- identity / flags -------------------------------------------------
    @property
    def account_key(self):
        return ("google:%s" % self._sub) if self._sub else None

    @property
    def capabilities(self):
        caps = dict(CAPABILITIES)
        caps["calendar_create_sources"] = list(
            CAPABILITIES["calendar_create_sources"])
        return caps

    @property
    def error_class(self):
        return self._error_class

    @error_class.setter
    def error_class(self, cls):
        """Bind the kit's GwsError subclass so raised errors are instances of
        it. Must subclass service.calendar.GwsError (alias needs no bind)."""
        if not (isinstance(cls, type) and issubclass(cls, GwsError)):
            raise TypeError("error_class must subclass service.calendar."
                            "GwsError")
        self._error_class = cls
        if cls is GwsError:
            self._paused_class = CalendarPaused
        else:
            self._paused_class = type("CalendarPaused", (CalendarPaused, cls),
                                      {"__module__": __name__})

    # -- error construction -----------------------------------------------
    def _err(self, msg, code=0, reason=""):
        return self._error_class(msg, code=code, reason=reason)

    def _rebrand(self, e):
        """A GwsError from inside the seam, re-issued as the bound class."""
        if isinstance(e, self._error_class):
            return e
        if isinstance(e, CalendarPaused):
            return self._paused_class(e.cls, str(e))
        return self._err(str(e), code=e.http_code, reason=e.reason)

    def _translate(self, exc, kind):
        """Any non-GwsError exception -> the one GwsError the seam raises."""
        g = self._g
        needs = getattr(g, "NeedsConsent", None)
        auth = getattr(g, "GoogleAuthError", None)
        http = getattr(g, "GoogleHttpError", None)
        if isinstance(needs, type) and isinstance(exc, needs):
            return self._paused_class("needs_consent")
        if isinstance(auth, type) and isinstance(exc, auth):
            cls = _auth_cls(exc)
            if cls in RETRY_CLASSES:
                return self._err("google transient: %s" % cls, code=503,
                                 reason=cls)
            return self._paused_class(cls)
        if (isinstance(http, type) and isinstance(exc, http)) or (
                type(getattr(exc, "code", None)) is int
                and hasattr(exc, "reason")):
            return self._http_error(exc, kind)
        if isinstance(exc, (OSError, TimeoutError)):
            return self._err("google transport: %s" % type(exc).__name__,
                             code=503, reason="network")
        return self._err("calendar seam internal error: %s"
                         % type(exc).__name__, code=500,
                         reason="internalError")

    def _http_error(self, exc, kind):
        status = exc.code
        if hasattr(exc, "body"):
            code, reason, message = gws_error_fields(status, exc.body)
        else:
            code = status
            reason = getattr(exc, "reason", None)
            if not isinstance(reason, str) or not reason:
                reason = "unknown"
            message = getattr(exc, "message", None)
            if not isinstance(message, str):
                message = str(exc)
        return self._api_error(code, reason, message, kind)

    def _api_error(self, code, reason, message, kind):
        if kind == "delete":
            return self._err("API error %s: %s" % (code, message), code=code,
                             reason=reason)
        if kind == "http":
            return self._err("direct HTTP %s: %s %s" % (code, reason, message),
                             code=code, reason=reason)
        return self._err("API error %s %s: %s" % (code, reason, message),
                         code=code, reason=reason)

    def _guard(self, fn, kind):
        try:
            return fn()
        except GwsError as e:
            raise self._rebrand(e)
        except Exception as e:  # seam guarantee: translate everything
            raise self._translate(e, kind)

    def _send(self, method, url, params, body, etag, scope):
        res = self._g.call(method, url, params=params, body=body, etag=etag,
                           scopes=[scope])
        if not (isinstance(res, tuple) and len(res) == 2
                and type(res[0]) is int):
            raise self._err("calendar seam: malformed transport answer",
                            code=500, reason="internalError")
        return res

    # -- verbs ------------------------------------------------------------
    def call(self, *gws_args, body=None):
        def run():
            req = verbs.parse(gws_args, body, kind="call")
            status, data = self._send(req["method"], req["url"],
                                      req["params"], req["body"], None,
                                      req["scope"])
            if status >= 400:  # only 412 comes back as a value
                code, reason, message = gws_error_fields(status, data)
                raise self._api_error(code, reason, message, "call")
            # kit gws(): empty stdout -> "gws no output"; error key -> raise
            if data is None:
                raise self._err("gws no output: \nArgs: %s" % (gws_args,))
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                err = data["error"]
                raise self._err("API error %s %s: %s" % (
                    err.get("code", 0), err.get("reason", ""),
                    err.get("message", "")),
                    code=err.get("code", 0), reason=err.get("reason", ""))
            return data
        return self._guard(run, "call")

    def delete(self, *gws_args):
        def run():
            req = verbs.parse(gws_args, None, kind="delete")
            status, data = self._send(req["method"], req["url"],
                                      req["params"], None, None, req["scope"])
            if status >= 400:
                code, reason, message = gws_error_fields(status, data)
                raise self._api_error(code, reason, message, "delete")
            return True
        return self._guard(run, "delete")

    def http(self, method, url, params=None, body=None, etag=None):
        def run():
            m = verbs.check_http(method, url)
            if params is not None and not isinstance(params, dict):
                raise self._err("unsupported verb: http params", code=0,
                                reason=verbs.UNSUPPORTED)
            status, data = self._send(m, url, params, body, etag, "calendar")
            if status == 412:
                return 412, None
            if status >= 400:
                code, reason, message = gws_error_fields(status, data)
                raise self._api_error(code, reason, message, "http")
            return status, data
        return self._guard(run, "http")

    def health(self):
        """None, or the pause class that must stop the drain (spec 6.2).
        Local reads only (service.google.status); never raises."""
        try:
            st = self._g.status()
        except Exception as e:
            auth = getattr(self._g, "GoogleAuthError", None)
            if isinstance(auth, type) and isinstance(e, auth):
                cls = _auth_cls(e)
                return None if cls in RETRY_CLASSES else cls
            return None
        try:
            paused = st.get("paused")
            if isinstance(paused, str) and paused and \
                    paused not in _NON_PAUSING:
                return paused
            if not st.get("connected"):
                return "needs_consent"
            granted = st.get("granted") or []
            if any(s not in granted for s in self._required_scopes):
                return "needs_consent"
            sub = st.get("sub")
            if self._sub and sub and sub != self._sub:
                return "account_mismatch"
        except Exception:
            return None
        return None


def open_connector(required_scopes=REQUIRED_SCOPES):
    """Build the Google connector for the store's default account."""
    gmod = google_module()
    sub = None
    try:
        st = gmod.status()
        if isinstance(st, dict) and st.get("connected"):
            sub = st.get("sub") if isinstance(st.get("sub"), str) else None
    except Exception:
        sub = None
    return GoogleConnector(gmod, sub, required_scopes=required_scopes)
