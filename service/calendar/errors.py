"""service.calendar.errors -- the seam's exception family (DGN-1854 W5, H3).

GwsError is a vendored copy of the kit's adapter.GwsError (same constructor,
same `http_code` / `reason` attributes), so the kit can either ALIAS it
(`GwsError = service.calendar.GwsError`, the recommended path) or SUBCLASS it
and bind the subclass on a connector (`conn.error_class = adapter.GwsError`);
every error the connector raises is then an instance of the kit's class and
all existing `except GwsError` branches keep working.

Contract (locked spec 1.1): every failure leaving conn.call/delete/http is a
GwsError carrying the (http_code, reason) the gws CLI produced for the same
Google answer. gws_error_fields() is the recorded gws rule (gws 0.13.2,
conformance/fixtures/gws_recorded.json):

  body is JSON with an "error" object:
      code    = error.code when it is an int, else the HTTP status
      reason  = error.errors[0].reason when present, else "unknown"
      message = error.message when it is a string, else "Unknown error"
  body is JSON with "error" not an object:   code=HTTP, reason "unknown",
                                             message "Unknown error"
  anything else (non-JSON, empty, no "error" key):
                                             code=HTTP, reason "httpError",
                                             message = the raw body text

English/ASCII only.
"""

import json

__all__ = ["GwsError", "CalendarPaused", "BackendUnavailable",
           "PAUSE_CLASSES", "RETRY_CLASSES", "gws_error_fields"]


class GwsError(RuntimeError):
    """Byte-compatible with the kit's adapter.GwsError."""

    def __init__(self, msg, code=0, reason=""):
        super().__init__(msg)
        self.http_code = code
        self.reason = reason


class CalendarPaused(GwsError):
    """A pause class (spec 6): the kit stops the drain, requeues the row with
    attempts unchanged, and sends one calendar_paused_<cls> notice. code 0 is
    outside the kit's RETRYABLE_HTTP set on purpose."""

    def __init__(self, cls, msg=None):
        super().__init__(msg or "calendar paused: %s" % cls, code=0,
                         reason=cls)
        self.cls = cls


class BackendUnavailable(GwsError):
    """open() could not give a connector (not_built, dual_target, ...).
    A GwsError too, so even a misplaced open() inside the kit's gws() never
    reaches the silent generic-Exception branch (adapter.py:3820)."""

    def __init__(self, cls, msg=None):
        super().__init__(msg or "calendar backend unavailable: %s" % cls,
                         code=0, reason=cls)
        self.cls = cls


# Auth classes that pause the mirror (spec 6 "kit mirror effect: pause", plus
# needs_consent for a never-connected store, spec 7). Any auth class that is
# neither here nor in RETRY_CLASSES also pauses: an unknown auth state must
# never burn outbox attempts into outbox_exhausted.
PAUSE_CLASSES = ("needs_consent", "token_revoked", "client_invalid",
                 "store_unwritable", "account_mismatch")
# Transient classes: retry as HTTP 503 (spec 6 "retry (503)"); 503 is in the
# kit's RETRYABLE_HTTP set.
RETRY_CLASSES = ("network", "google_5xx", "store_busy")


def gws_error_fields(http_status, body):
    """(code, reason, message) exactly as gws 0.13.2 prints them.

    `body` is the raw response text (str/bytes), an already-parsed JSON
    value, or None. A parsed value is re-serialized for the httpError message
    (json.dumps default separators), which equals the raw text Google sends
    for the shapes recorded."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        raw = body
        try:
            parsed = json.loads(body) if body.strip() else None
            is_json = body.strip() != ""
        except ValueError:
            parsed, is_json = None, False
    elif body is None:
        raw, parsed, is_json = "", None, False
    else:
        parsed, is_json = body, True
        try:
            raw = json.dumps(body)
        except (TypeError, ValueError):
            raw = str(body)
    if is_json and isinstance(parsed, dict) and "error" in parsed:
        err = parsed["error"]
        if not isinstance(err, dict):
            return http_status, "unknown", "Unknown error"
        code = err.get("code")
        if type(code) is not int:
            code = http_status
        message = err.get("message")
        if not isinstance(message, str):
            message = "Unknown error"
        reason = "unknown"
        errs = err.get("errors")
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            r = errs[0].get("reason")
            if isinstance(r, str):
                reason = r
        return code, reason, message
    return http_status, "httpError", raw
