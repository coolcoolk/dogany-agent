"""Domain session-pin wire contract, shared by the CLI and dashboard sync.

The desired store is a set of renewable leases. Close removes a lease;
expiry is the backstop for a publisher that died without closing. Neither
this store nor its bridge state ever participates in day-card composition.
"""
import json
import math
import re
from pathlib import Path

DESIRED_FILE = "session-pins.json"
STATE_FILE = "session-pin-state.json"
DEFAULT_TTL = 6 * 3600
DOMAIN_RE = re.compile(r"[a-z0-9_-]{1,32}\Z")


def domain_valid(domain):
    return (isinstance(domain, str) and DOMAIN_RE.fullmatch(domain)
            and domain not in ("board", "legacy"))


def buttons_valid(rows):
    """Same callback keyboard shape as strict push: reject, never repair."""
    if not isinstance(rows, list):
        raise ValueError("buttons must be rows of {text,callback_data}")
    for row in rows:
        if not isinstance(row, list) or not row:
            raise ValueError("empty or invalid button row")
        for button in row:
            if not isinstance(button, dict) or set(button) != {"text", "callback_data"}:
                raise ValueError("buttons require exactly text and callback_data")
            label, callback = button["text"], button["callback_data"]
            if not (isinstance(label, str) and label.strip()
                    and isinstance(callback, str)
                    and 0 < len(callback.encode("utf-8")) <= 64):
                raise ValueError("invalid label or callback_data (1..64 bytes)")
    return rows


def read_sessions(path):
    """Missing means no live sessions; corruption must not trigger deletion."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("invalid session-pin store")
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        raise ValueError("invalid session-pin sessions")
    for domain, entry in sessions.items():
        if not domain_valid(domain) or not isinstance(entry, dict):
            raise ValueError("invalid session-pin domain")
        if not isinstance(entry.get("text"), str) or not entry["text"].strip():
            raise ValueError("session pin requires nonempty text")
        expiry = entry.get("expires_at")
        if type(expiry) not in (int, float) or not math.isfinite(expiry):
            raise ValueError("invalid session-pin expiry")
        buttons_valid(entry.get("buttons", []))
    return sessions
