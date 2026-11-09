"""service.calendar -- calendar connection seam (DGN-1854 W5, locked spec 1.1).

    conn = service.calendar.open(instance)     # raises BackendUnavailable(cls)
    conn.backend        -> "google"            # "apple" reserved (annex)
    conn.account_key    -> "google:<sub>"      # None while not connected
    conn.capabilities   -> frozen v2.7 flags (google.CAPABILITIES)
    conn.call(*gws_args, body=None) -> dict    # == kit adapter.gws()
    conn.delete(*gws_args) -> bool             # == kit adapter.gws_delete()
    conn.http(method, url, params=None, body=None, etag=None)
                                    -> (status, json)  # == http_direct._call
    conn.health() -> None | pause_cls          # local reads, never raises

Only GwsError subclasses leave call/delete/http (CalendarPaused for pause
classes). The kit aliases GwsError (or subclasses it and sets
conn.error_class). `instance` is the instance root; None = $PROJECT_ROOT.

Google is the provided backend. Instance config/calendar.json selects user
drivers and read-only ICS sources (2026-10-26 scope amendment).
CLI: python3 -m service.calendar status|resolve --json.

English/ASCII only.
"""

from . import google as _google
from . import resolve as _resolve
from . import settings as _settings
from . import configured as _configured
from .subscriptions import Subscription
from .errors import (BackendUnavailable, CalendarPaused, GwsError,
                     PAUSE_CLASSES, RETRY_CLASSES, gws_error_fields)

__all__ = ["open", "status", "resolve", "sources", "BackendUnavailable",
           "CalendarPaused", "GwsError", "PAUSE_CLASSES", "RETRY_CLASSES",
           "gws_error_fields"]


def _google_status():
    try:
        st = _google.google_module().status()
    except Exception:
        return None
    return st if isinstance(st, dict) else None


def resolve(instance=None):
    """Backend choice for `instance` (see resolve.py). Never raises."""
    return _resolve.resolve(instance, google_status=_google_status())


def open(instance=None):  # noqa: A001 -- frozen API name
    cfg = _settings.read(instance)
    r = _resolve.resolve(instance, google_status=_google_status(), configuration=cfg)
    if r["class"]:
        raise BackendUnavailable(r["class"])
    conn = (_google.open_connector(required_scopes=_resolve.required_google_scopes(instance))
            if r["backend"] == "google" else
            _configured.open_connector(r["backend"], cfg["backends"][r["backend"]]))
    conn.subscriptions = tuple(Subscription(feed) for feed in cfg["subscriptions"])
    return conn


def sources(instance=None):
    """Read sources remain usable even when a write backend is unavailable."""
    return tuple(Subscription(feed) for feed in _settings.read(instance)["subscriptions"])


def status(instance=None):
    """Seam status for preflight / shell routines. Never a token, never an
    item body. Never raises."""
    gst = _google_status() or {}
    r = _resolve.resolve(instance, google_status=gst)
    out = {
        "backend": r["backend"],
        "class": r["class"],
        "requested": r["requested"],
        "target": r["target"],
        "offered": r["offered"],
        "unavailable": r["unavailable"],
        "connected": bool(gst.get("connected")) if r["backend"] == "google" else False,
        "account": gst.get("account") if r["backend"] == "google" and gst.get("connected") else None,
        "account_key": None,
        "granted": list(gst.get("granted") or []) if r["backend"] == "google" else [],
        "health": None,
        "capabilities": None,
        "subscriptions": r.get("subscriptions", []),
    }
    if r["backend"]:
        try:
            conn = open(instance)
        except GwsError as e:
            out["class"] = e.reason
            out["backend"] = None
            return out
        out["account_key"] = conn.account_key
        out["health"] = conn.health()
        out["capabilities"] = conn.capabilities
        if r["backend"] != "google":
            out["connected"] = out["health"] is None
    return out
