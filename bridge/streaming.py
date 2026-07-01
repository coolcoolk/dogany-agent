"""Progressive draft-message streaming.

Accumulates assistant text and edits a Telegram draft message in place, updating
when enough characters arrived OR enough time elapsed. Overflows past 4000 chars
into a new draft. Control markers are stripped from the bubble. RetryAfter is
backed off; "message is not modified" is treated as success.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, List, Optional

from telegram import Bot
from telegram.error import RetryAfter, TelegramError

from bridge.config import config
from bridge.formatting import strip_display_markers

logger = logging.getLogger(__name__)

_OVERFLOW_LIMIT = 4000


@dataclass
class DraftState:
    message_id: int
    text: str
    last_update_time: float


class StreamingMessageHandler:
    """Manages the lifecycle of streaming draft messages for one turn."""

    def __init__(self, bot: Bot, chat_id: int, user_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.user_id = user_id
        self.drafts: List[DraftState] = []
        self.accumulated_text = ""
        self.min_chars = config.draft_update_min_chars
        self.min_interval = config.draft_update_interval
        self._finalized = False

    async def _retry_with_backoff(self, operation, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                return await operation()
            except RetryAfter as e:
                if attempt == max_retries - 1:
                    raise
                wait = float(getattr(e, "retry_after", 2 ** attempt))
                logger.warning("Rate limited, waiting %.1fs (retry %d)", wait, attempt + 1)
                await asyncio.sleep(wait)

    @staticmethod
    def _is_not_modified(error: Exception) -> bool:
        return "message is not modified" in str(error).lower()

    @staticmethod
    def _message_id(message: Any) -> Optional[int]:
        mid = getattr(message, "message_id", None)
        return mid if isinstance(mid, int) else None

    async def create_draft(self, text: str) -> Optional[DraftState]:
        content = strip_display_markers(text) or "..."
        try:
            sent = await self._retry_with_backoff(
                lambda: self.bot.send_message(chat_id=self.chat_id, text=content)
            )
            mid = self._message_id(sent)
            if mid is None:
                raise RuntimeError("send_message returned no message_id")
            draft = DraftState(message_id=mid, text=text, last_update_time=time.time())
            self.drafts.append(draft)
            return draft
        except Exception as e:
            logger.error("Failed to create draft: %s", e)
            return None

    async def update_draft(self, draft: DraftState, new_text: str) -> bool:
        try:
            await self._retry_with_backoff(
                lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=draft.message_id,
                    text=strip_display_markers(new_text),
                )
            )
            draft.text = new_text
            draft.last_update_time = time.time()
            return True
        except TelegramError as e:
            if self._is_not_modified(e):
                draft.text = new_text
                draft.last_update_time = time.time()
                return True
            logger.error("Failed to update draft %s: %s", draft.message_id, e)
            return False

    def should_update(self, draft: DraftState, new_char_count: int) -> bool:
        return (
            new_char_count >= self.min_chars
            or (time.time() - draft.last_update_time) >= self.min_interval
        )

    @staticmethod
    def _find_split_boundary(text: str, max_length: int = _OVERFLOW_LIMIT) -> int:
        if len(text) <= max_length:
            return len(text)
        search_start = max(0, max_length - 200)
        para = text.rfind("\n\n", search_start, max_length)
        if para > search_start:
            return para + 2
        line = text.rfind("\n", search_start, max_length)
        if line > search_start:
            return line + 1
        return max_length

    async def handle_overflow(self) -> bool:
        if not self.drafts:
            return False
        current = self.drafts[-1]
        split_point = self._find_split_boundary(self.accumulated_text)
        current.text = self.accumulated_text[:split_point]
        await self.finalize_draft(current)
        remaining = self.accumulated_text[split_point:]
        self.accumulated_text = remaining
        if remaining:
            await self.create_draft(remaining)
        return True

    async def update_if_needed(self, new_chunk: str) -> bool:
        if self._finalized:
            return False
        self.accumulated_text += new_chunk
        if len(self.accumulated_text) >= _OVERFLOW_LIMIT:
            await self.handle_overflow()
            return True
        if not self.drafts:
            await self.create_draft(self.accumulated_text)
            return True
        current = self.drafts[-1]
        chars_since = len(self.accumulated_text) - len(current.text)
        if self.should_update(current, chars_since):
            await self.update_draft(current, self.accumulated_text)
            return True
        return False

    async def finalize_draft(self, draft: DraftState) -> bool:
        try:
            await self._retry_with_backoff(
                lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=draft.message_id,
                    text=strip_display_markers(draft.text),
                )
            )
            return True
        except TelegramError as e:
            if self._is_not_modified(e):
                return True
            logger.error("Failed to finalize draft %s: %s", draft.message_id, e)
            return False

    async def finalize_all(self) -> bool:
        if self._finalized:
            return False
        self._finalized = True
        if self.drafts and self.accumulated_text:
            self.drafts[-1].text = self.accumulated_text
        for draft in self.drafts:
            await self.finalize_draft(draft)
        return True

    async def cancel(self) -> bool:
        if self._finalized:
            return False
        self._finalized = True
        for draft in self.drafts:
            try:
                await self._retry_with_backoff(
                    lambda: self.bot.delete_message(
                        chat_id=self.chat_id, message_id=draft.message_id
                    )
                )
            except TelegramError as e:
                logger.error("Failed to delete draft %s: %s", draft.message_id, e)
        self.drafts.clear()
        self.accumulated_text = ""
        return True
