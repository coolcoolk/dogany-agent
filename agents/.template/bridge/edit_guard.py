"""Shared Telegram edit-message rate guard (DGN-594).

Extracted from DashboardSync so every in-place message editor in the bridge
(dashboard sync, countdown helper) shares ONE flood/interval discipline
instead of growing per-feature copies.

State per guarded message stream:
  - min interval between edits (MIN_EDIT_INTERVAL default, spec: >=3s),
  - flood_until: monotonic backoff deadline armed by Telegram RetryAfter,
  - last_edit: monotonic time of the last accounted edit.

The guard is deliberately dumb about error taxonomy beyond the two universal
cases (RetryAfter -> backoff, "message is not modified" -> benign success).
Callers with richer needs (DashboardSync's recreate / chat-gone / forbidden
handling) keep their own try/except around bot.edit_message_text and use only
the state primitives (ready / note_retry_after / note_edit); simple callers
(countdown) use the async edit() wrapper, which NEVER raises on Telegram
errors (fail-open by contract).
"""

import enum
import logging
import time
from typing import Optional

import telegram.error

logger = logging.getLogger(__name__)

# Minimum seconds between edits per chat (spec: >=3s). Single source of
# truth; bridge.dashboard re-exports this name for compatibility.
MIN_EDIT_INTERVAL = 3.0

# Fallback backoff when a RetryAfter error carries no usable retry_after.
DEFAULT_RETRY_AFTER = 5.0


def _is_not_modified(error: Exception) -> bool:
    # Same normal-flow detection as streaming.StreamingMessageHandler.
    return "message is not modified" in str(error).lower()


class EditOutcome(enum.Enum):
    """Result of a guarded edit; only FAILED means the edit is truly lost."""

    OK = "ok"                       # edit applied
    NOT_MODIFIED = "not_modified"   # content identical: benign success
    FLOOD = "flood"                 # RetryAfter: backoff armed, retry later
    FAILED = "failed"               # other Telegram error (fail-open)


class EditRateGuard:
    """Flood/interval gate for repeated in-place edits of one message stream."""

    def __init__(self, min_interval: float = MIN_EDIT_INTERVAL) -> None:
        self.min_interval = min_interval
        self.flood_until: float = 0.0           # monotonic deadline
        self.last_edit: Optional[float] = None  # monotonic

    def ready(self, now: Optional[float] = None) -> bool:
        """True when an edit may be attempted (no flood wait, interval met)."""
        if now is None:
            now = time.monotonic()
        if now < self.flood_until:
            return False
        if self.last_edit is not None and (now - self.last_edit) < self.min_interval:
            return False
        return True

    def note_retry_after(
        self, retry_after: float, now: Optional[float] = None
    ) -> None:
        """Arm the flood backoff deadline from a Telegram RetryAfter."""
        if now is None:
            now = time.monotonic()
        self.flood_until = now + retry_after

    def note_edit(self, now: Optional[float] = None) -> None:
        """Record an edit (or an accounted skip) for interval pacing."""
        if now is None:
            now = time.monotonic()
        self.last_edit = now

    async def edit(
        self,
        bot,
        chat_id: int,
        message_id: int,
        text: str,
        **kwargs,
    ) -> EditOutcome:
        """Guarded bot.edit_message_text; never raises on Telegram errors.

        Does NOT gate on ready() itself -- pacing is the caller's decision
        (a caller may deliberately spend a final edit regardless of pacing).
        """
        now = time.monotonic()
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id, **kwargs
            )
        except telegram.error.RetryAfter as e:
            self.note_retry_after(
                float(getattr(e, "retry_after", DEFAULT_RETRY_AFTER)), now
            )
            return EditOutcome.FLOOD
        except telegram.error.BadRequest as e:
            if _is_not_modified(e):
                self.note_edit(now)  # counts for pacing: Telegram saw a call
                return EditOutcome.NOT_MODIFIED
            logger.warning("Guarded edit failed: %s", e)
            return EditOutcome.FAILED
        except telegram.error.TelegramError as e:
            logger.warning("Guarded edit failed: %s", e)
            return EditOutcome.FAILED
        self.note_edit(now)
        return EditOutcome.OK
