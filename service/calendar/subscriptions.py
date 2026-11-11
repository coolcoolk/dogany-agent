"""Provider-independent ICS subscriptions. GET only; no write transport.

Return the original iCalendar text, retaining recurrence, timezone and all
extension properties for the consuming kit's parser. Do not expand events.
"""
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import GwsError

MAX_BYTES = 8 * 1024 * 1024


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in ("http", "https"):
            raise GwsError("ICS redirect rejected", reason="invalid_subscription")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Subscription:
    backend = "ics"
    read_only = True

    def __init__(self, entry):
        self.id = entry["id"]
        self._url = entry["url"]

    def read(self):
        try:
            parts = urlsplit(self._url)
            url = urlunsplit(parts._replace(scheme="https")) if parts.scheme == "webcal" else self._url
            req = Request(url, headers={"Accept": "text/calendar"}, method="GET")
            with build_opener(_Redirects()).open(req, timeout=30) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise GwsError("ICS feed exceeds size limit", reason="subscription_too_large")
            text = raw.decode("utf-8-sig")
            lines = text.strip().splitlines()
            if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
                raise GwsError("Invalid iCalendar feed", reason="invalid_subscription")
            return text
        except GwsError:
            raise
        except HTTPError as exc:
            raise GwsError("ICS read failed", code=exc.code, reason="subscription_http") from None
        except (OSError, URLError):
            raise GwsError("ICS read failed", code=503, reason="network") from None
        except Exception:
            raise GwsError("Invalid iCalendar feed", reason="invalid_subscription") from None

    def _readonly(self, *args, **kwargs):
        raise GwsError("ICS subscriptions are read-only", reason="read_only")

    call = delete = http = _readonly
