#!/usr/bin/env python3
"""DGN-1981: the named key slots and the one tiny verify call per slot.

A slot names WHERE a key lives in the secret store (secret_store.py) and HOW
to check it with one read-only request before it is kept:

  api-credit  Anthropic API key (the claude-api credit pool worker).
              GET {base}/v1/models?limit=1
  api-admin   Anthropic Admin API key (the optional org cost report the
              credit pool reconciles from).
              GET {base}/v1/organizations/cost_report?starting_at=<month start>&limit=1
  codex-api-key  OpenAI key for Codex login. No separate HTTP probe: the
                completion callback runs Codex login and checks its status.

verify() returns one word: "ok" (2xx), "rejected" (401/403: the key is wrong
or lacks the right kind), or "unknown" (network down, timeout, any other
status) -- unknown never blocks storing. The key travels only in the
x-api-key header; urllib errors are swallowed whole (their text can carry
request detail), so nothing about the key can reach a log or an exception.
Base URL: $DOGANY_SECRET_VERIFY_BASE_URL (tests), default api.anthropic.com.
"""

import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urlencode

VERIFY_TIMEOUT_S = 10.0
_DEFAULT_BASE = "https://api.anthropic.com"


@dataclass(frozen=True)
class Slot:
    name: str
    service: str
    account: str
    kind: str  # verify kind


# Service/account pairs are the ones the readers already use
# (dispatch-detached.sh API_CREDIT_* / API_ADMIN_*, api_credit_pool.py).
SLOTS: Dict[str, Slot] = {
    "api-credit": Slot("api-credit", "dogany-anthropic-api-credit", "dogany", "anthropic_api"),
    "api-admin": Slot("api-admin", "dogany-anthropic-admin", "dogany", "anthropic_admin"),
    "codex-api-key": Slot("codex-api-key", "dogany-codex-api-key", "dogany", "codex_api"),
}


def slot(name: str) -> Optional[Slot]:
    return SLOTS.get((name or "").strip().lower())


def _base_url() -> str:
    # D7 (DGN-1981): a key is only ever sent to the provider. The override
    # exists for tests and is honored for a loopback host only, so a stray
    # environment value can never redirect a real key elsewhere.
    override = (os.environ.get("DOGANY_SECRET_VERIFY_BASE_URL") or "").rstrip("/")
    if override:
        from urllib.parse import urlparse
        host = (urlparse(override).hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            return override
    return _DEFAULT_BASE


def _request_path(kind: str, now: Optional[datetime] = None) -> Optional[str]:
    if kind == "anthropic_api":
        return "/v1/models?limit=1"
    if kind == "anthropic_admin":
        now = now or datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-01T00:00:00Z")
        return "/v1/organizations/cost_report?" + urlencode({"starting_at": start, "limit": 1})
    return None


def verify(kind: str, key: str, timeout: float = VERIFY_TIMEOUT_S) -> str:
    path = _request_path(kind)
    if path is None or not key:
        return "unknown"
    req = urllib.request.Request(
        _base_url() + path,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        return "unknown"
    finally:
        del req
    if 200 <= status < 300:
        return "ok"
    if status in (401, 403):
        return "rejected"
    return "unknown"
