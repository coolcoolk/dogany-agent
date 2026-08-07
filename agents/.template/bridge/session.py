"""Session persistence and lifecycle.

JSON store at PROJECT_ROOT/.telegram_bot/sessions.json, keyed by
telegram_session:{user_id}, guarded by an asyncio lock, full rewrite on change.
The SessionManager layers reply-mode normalization, last-message tracking, and
the 24h auto-new-session rule on top of the raw store.
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


session_manager = SessionManager()
