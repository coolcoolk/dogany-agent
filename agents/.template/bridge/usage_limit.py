"""DGN-1906: Claude Code usage-limit result handling.

The CLI reports a session / weekly / model limit as an is_error result whose
text is "You've hit your session limit · resets 7:40pm (Asia/Seoul)". This
module owns the pure parts: recognising that text, parsing the reset time,
and the per-chat limit window (one notice per window, requests queued for
replay after reset). The bot layer owns sending and replay.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# "You've hit your session limit", "...weekly limit", "...Opus limit",
# older "5-hour limit reached". Bounded so a long answer that merely
# mentions limits never matches.
_LIMIT_RE = re.compile(
    r"(?:hit your [\w .\-]{0,30}?limit|(?:usage|\d+-hour|weekly|session) limit reached)",
    re.IGNORECASE,
)
_RESET_RE = re.compile(
    r"resets?\s+(?:(?P<mon>[A-Za-z]{3})[a-z]*\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
_MAX_LEN = 400
FALLBACK_WINDOW_S = 3600
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}


def is_usage_limit_text(text: Optional[str]) -> bool:
    t = (text or "").strip()
    return 0 < len(t) <= _MAX_LEN and _LIMIT_RE.search(t) is not None


def parse_reset(text: str, now: Optional[float] = None) -> Tuple[Optional[float], str]:
    """Return (reset_epoch or None, display label) from the limit text.

    The label is the "7:40pm (Asia/Seoul)" tail ("" when absent). The epoch
    is the next occurrence of that wall-clock time in the stated zone.
    """
    now = time.time() if now is None else now
    m = _RESET_RE.search(text or "")
    if not m:
        return None, ""
    label = text[m.start("h") if not m.group("mon") else m.start("mon"):m.end()].strip()
    try:
        tz = ZoneInfo(m.group("tz")) if m.group("tz") else datetime.now().astimezone().tzinfo
    except Exception:
        return None, label
    hour = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    minute = int(m.group("m") or 0)
    base = datetime.fromtimestamp(now, tz)
    try:
        if m.group("mon") and m.group("mon").lower() in _MONTHS:
            cand = base.replace(month=_MONTHS[m.group("mon").lower()],
                                day=int(m.group("day")), hour=hour,
                                minute=minute, second=0, microsecond=0)
            if cand.timestamp() <= now:
                cand = cand.replace(year=cand.year + 1)
        else:
            cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand.timestamp() <= now:
                cand += timedelta(days=1)
    except ValueError:
        return None, label
    return cand.timestamp(), label


@dataclass
class _Window:
    until: float
    gen: int = 0
    notified: bool = False
    queue: List[Any] = field(default_factory=list)


class LimitGate:
    """Per-chat limit window: one notice per window, queued requests.

    A queued request is opaque here (the bot queues the turn as it would
    have run). Each window carries a generation so a replay scheduled for
    an earlier window never drains a later one."""

    def __init__(self) -> None:
        self._w: Dict[int, _Window] = {}
        self._gen = 0

    def active_until(self, chat_id: int, now: Optional[float] = None) -> Optional[float]:
        now = time.time() if now is None else now
        w = self._w.get(chat_id)
        return w.until if w and w.until > now else None

    def hit(self, chat_id: int, text: str, user_message: Any,
            now: Optional[float] = None) -> Tuple[bool, float, str]:
        """Record a limit hit and queue the request. Returns
        (send_notice, window_until, reset_label). send_notice is True only
        for the first hit of a window. A window that ended before its
        replay drained it hands its queue to the new window (DGN-1967)."""
        now = time.time() if now is None else now
        epoch, label = parse_reset(text, now)
        w = self._w.get(chat_id)
        if w is None or w.until <= now:
            self._gen += 1
            w = _Window(
                until=epoch if epoch and epoch > now else now + FALLBACK_WINDOW_S,
                gen=self._gen,
                queue=list(w.queue) if w is not None else [],
            )
            self._w[chat_id] = w
        if user_message:
            w.queue.append(user_message)
        send = not w.notified
        w.notified = True
        return send, w.until, label

    def generation(self, chat_id: int) -> Optional[int]:
        w = self._w.get(chat_id)
        return w.gen if w else None

    def enqueue(self, chat_id: int, user_message: Any) -> None:
        w = self._w.get(chat_id)
        if w is not None and user_message:
            w.queue.append(user_message)

    def drain(self, chat_id: int, gen: Optional[int] = None) -> List[Any]:
        """Pop the chat's window and return its queue. With gen, only the
        window of that generation is drained ([] once a newer one exists)."""
        w = self._w.get(chat_id)
        if w is None or (gen is not None and w.gen != gen):
            return []
        del self._w[chat_id]
        return list(w.queue)


limit_gate = LimitGate()
