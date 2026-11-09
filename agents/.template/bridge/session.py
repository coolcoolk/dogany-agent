"""Session persistence and lifecycle.

JSON store at PROJECT_ROOT/.telegram_bot/sessions.json, keyed by
telegram_session:{user_id}, guarded by an asyncio lock, full rewrite on change.
The SessionManager layers reply-mode normalization, last-message tracking, and
the 24h auto-new-session rule on top of the raw store.

DGN-1403 context-fill rotation: the bridge records each turn's context fill
(record_context_fill) and context_rotation decides, at the next message,
whether that fill rotates the session.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from bridge.config import config

logger = logging.getLogger(__name__)

VALID_REPLY_MODES = {"text", "voice"}
DEFAULT_REPLY_MODE = "text"
LAST_USER_MESSAGE_AT_KEY = "last_user_message_at"
CONTEXT_FILL_KEY = "context_fill"

# Rotation reasons. Only ROTATE_HARD can carry an owner notice, and only
# when config.session_rotate_notice is on (DGN-2226).
ROTATE_IDLE = "idle"
ROTATE_HARD = "hard"
ROTATE_SOFT = "soft"

_FILL_USAGE_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def usage_fill_tokens(usage: Any) -> Optional[int]:
    """Context fill of one model call: input + cache_read + cache_creation.

    None when the usage dict is missing or carries none of the three keys --
    a missing signal, never a zero fill."""
    if not isinstance(usage, dict):
        return None
    total, seen = 0, False
    for key in _FILL_USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
            seen = True
    return total if seen else None


def context_window_of(model_usage: Any, model: Optional[str], default: int) -> int:
    """The model's context window from ResultMessage.model_usage
    ({model: {"contextWindow": N}}); `default` when not reported."""
    windows = {}
    if isinstance(model_usage, dict):
        for name, entry in model_usage.items():
            value = entry.get("contextWindow") if isinstance(entry, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                windows[str(name)] = int(value)
    if model:
        if model in windows:
            return windows[model]
        for name, value in windows.items():
            if name.startswith(model) or model.startswith(name):
                return value
    if len(windows) == 1:
        return next(iter(windows.values()))
    return int(default)


class SessionStore:
    """Process-local cache backed by a single JSON file, async-locked."""

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._path = config.session_store_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.error("Failed to load session store: %s", e)
                self._data = {}

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save session store: %s", e)

    @staticmethod
    def _key(user_id: int) -> str:
        return f"telegram_session:{user_id}"

    async def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self._lock:
            return self._data.get(self._key(user_id))

    async def set(self, user_id: int, data: Dict[str, Any]) -> None:
        async with self._lock:
            self._data[self._key(user_id)] = data
            self._save()

    async def update(self, user_id: int, updates: Dict[str, Any]) -> None:
        async with self._lock:
            data = self._data.get(self._key(user_id), {})
            data.update(updates)
            self._data[self._key(user_id)] = data
            self._save()

    async def delete(self, user_id: int) -> None:
        async with self._lock:
            self._data.pop(self._key(user_id), None)
            self._save()


class SessionManager:
    """Higher-level session operations over the store."""

    def __init__(self) -> None:
        self.store = SessionStore()

    @staticmethod
    def normalize_reply_mode(mode: Optional[str]) -> str:
        normalized = str(mode or DEFAULT_REPLY_MODE).strip().lower()
        return normalized if normalized in VALID_REPLY_MODES else DEFAULT_REPLY_MODE

    async def _ensure_reply_mode(
        self, user_id: int, session: Dict[str, Any]
    ) -> Dict[str, Any]:
        current = session.get("reply_mode")
        normalized = self.normalize_reply_mode(current)
        if current != normalized:
            session["reply_mode"] = normalized
            await self.store.set(user_id, session)
        return session

    async def get_session(self, user_id: int) -> Dict[str, Any]:
        session = await self.store.get(user_id) or {}
        return await self._ensure_reply_mode(user_id, session)

    async def update_session(self, user_id: int, data: Dict[str, Any]) -> None:
        payload = dict(data)
        if "reply_mode" in payload:
            payload["reply_mode"] = self.normalize_reply_mode(payload.get("reply_mode"))
        await self.store.update(user_id, payload)

    async def clear_session(self, user_id: int) -> None:
        await self.store.delete(user_id)

    # --- timestamps / auto-new-session ---

    @staticmethod
    def _normalize_ts(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _parse_ts(cls, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return cls._normalize_ts(parsed)

    @staticmethod
    def _auto_new_interval() -> Optional[timedelta]:
        hours = config.auto_new_session_after_hours
        if hours is None:
            return None
        return timedelta(hours=float(hours))

    async def get_last_user_message_at(self, user_id: int) -> Optional[datetime]:
        session = await self.get_session(user_id)
        return self._parse_ts(session.get(LAST_USER_MESSAGE_AT_KEY))

    async def set_last_user_message_at(
        self, user_id: int, at: Optional[datetime] = None
    ) -> None:
        ts = self._normalize_ts(at or datetime.now(timezone.utc))
        await self.update_session(user_id, {LAST_USER_MESSAGE_AT_KEY: ts.isoformat()})

    async def should_start_new_session(
        self, user_id: int, now: Optional[datetime] = None
    ) -> bool:
        interval = self._auto_new_interval()
        if interval is None:
            return False
        last = await self.get_last_user_message_at(user_id)
        if last is None:
            return False
        current = self._normalize_ts(now or datetime.now(timezone.utc))
        return current - last > interval

    # --- DGN-1403 context fill ---

    async def record_context_fill(
        self,
        user_id: int,
        session_id: Optional[str],
        tokens: Optional[int],
        window: Optional[int],
        compacted: bool = False,
        at: Optional[datetime] = None,
    ) -> None:
        """Persist the latest turn's context fill, bound to its session id.

        Missing usage (tokens None) records nothing. A compaction seen in the
        same session stays recorded until the session rotates."""
        if not session_id or tokens is None or not window or window <= 0:
            return
        prev = (await self.get_session(user_id)).get(CONTEXT_FILL_KEY)
        if isinstance(prev, dict) and prev.get("session_id") == session_id:
            compacted = compacted or bool(prev.get("compacted"))
        ts = self._normalize_ts(at or datetime.now(timezone.utc))
        await self.update_session(user_id, {CONTEXT_FILL_KEY: {
            "session_id": session_id,
            "tokens": int(tokens),
            "window": int(window),
            "ratio": round(int(tokens) / int(window), 4),
            "compacted": bool(compacted),
            "at": ts.isoformat(),
        }})


def context_rotation(
    session: Dict[str, Any], now: Optional[datetime] = None
) -> Optional[str]:
    """Context-fill rotation for the next message: ROTATE_HARD, ROTATE_SOFT
    or None.

    Reads only the fill recorded for the CURRENT session id (a fill left by
    an earlier session is inert). HARD: ratio >= hard ratio. SOFT: ratio >=
    soft ratio (or a compaction already happened) and the gap since the last
    user message >= the soft idle minutes. No recorded fill -> None."""
    fill = session.get(CONTEXT_FILL_KEY)
    sid = session.get("session_id")
    if not isinstance(fill, dict) or not sid or fill.get("session_id") != sid:
        return None
    ratio = fill.get("ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return None
    hard = config.session_rotate_hard_ratio
    if hard is not None and ratio >= hard:
        return ROTATE_HARD
    soft = config.session_rotate_soft_ratio
    if soft is None or not (ratio >= soft or fill.get("compacted")):
        return None
    last = SessionManager._parse_ts(session.get(LAST_USER_MESSAGE_AT_KEY))
    if last is None:
        return None
    current = SessionManager._normalize_ts(now or datetime.now(timezone.utc))
    gap = timedelta(minutes=float(config.session_rotate_soft_idle_minutes))
    return ROTATE_SOFT if current - last >= gap else None


def rotation_log_line(
    user_id: int,
    trigger: str,
    old_session: Dict[str, Any],
    new_session_id: Optional[str],
) -> str:
    """DGN-2226: one traceable log line per rotation (idle / hard / soft).

    fill is the ratio recorded for the OLD session ("none" when no fill was
    recorded). The SessionStart continuity recap runs in the CLI child, so
    the bridge cannot know at this point whether it was injected: recap is
    always "unknown"."""
    fill = old_session.get(CONTEXT_FILL_KEY)
    ratio = fill.get("ratio") if isinstance(fill, dict) else None
    return (
        "session rotation for user %s: trigger=%s fill=%s old=%s -> new=%s recap=unknown"
        % (user_id, trigger, "none" if ratio is None else ratio,
           old_session.get("session_id") or "none", new_session_id or "none")
    )


session_manager = SessionManager()
