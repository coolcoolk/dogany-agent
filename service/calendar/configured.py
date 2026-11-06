"""Settings-selected, user-installed connector drivers (no bundled CalDAV).

A driver is an installed Python module:factory accepting its settings dict.
It implements the same call/delete/http/health methods as GoogleConnector.
Factories must be local constructors; open/status never connect to a server.
"""
import copy
import importlib
import re

from .errors import BackendUnavailable
from .google import GoogleConnector
from .settings import CAPABILITY_KEYS

DRIVER = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")


class ConfiguredConnector(GoogleConnector):
    """Reuse the seam's exception boundary and kit error-class binding."""
    def __init__(self, backend, entry, driver):
        super().__init__(None)
        self.backend = backend
        self._account = entry["account"]
        self._driver = driver
        caps = driver.capabilities
        if (not isinstance(caps, dict) or set(caps) != CAPABILITY_KEYS
                or not isinstance(caps["calendar_create_sources"], list)
                or any(type(caps[k]) is not bool for k in
                       ("event_color", "offline_write", "attendees_write"))):
            raise BackendUnavailable("invalid_backend")
        self._caps = copy.deepcopy(caps)
        if not all(callable(getattr(driver, key, None)) for key in
                   ("call", "delete", "http", "health")):
            raise BackendUnavailable("invalid_backend")

    @property
    def account_key(self):
        return "%s:%s" % (self.backend, self._account)

    @property
    def capabilities(self):
        return copy.deepcopy(self._caps)

    def call(self, *args, body=None):
        return self._guard(lambda: self._driver.call(*args, body=body), "call")

    def delete(self, *args):
        return self._guard(lambda: self._driver.delete(*args), "delete")

    def http(self, method, url, params=None, body=None, etag=None):
        return self._guard(lambda: self._driver.http(
            method, url, params=params, body=body, etag=etag), "http")

    def health(self):
        try:
            result = self._driver.health()
            return result if result is None or isinstance(result, str) else "invalid_backend"
        except Exception:
            return "backend_unavailable"


def open_connector(name, entry):
    ref = entry.get("driver", "")
    if not ref:
        raise BackendUnavailable("backend_not_available")
    if not DRIVER.fullmatch(ref):
        raise BackendUnavailable("invalid_backend")
    module, factory = ref.split(":")
    try:
        build = getattr(importlib.import_module(module), factory)
        return ConfiguredConnector(name, entry, build(copy.deepcopy(entry)))
    except BackendUnavailable:
        raise
    except Exception:
        # Never include a driver's error or settings: either may hold credentials.
        raise BackendUnavailable("backend_not_available") from None
