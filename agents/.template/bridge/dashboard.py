"""Pinned live-dashboard message sync (DGN-214).

Mirrors an agent-authored dashboard file into ONE pinned Telegram message in
the owner chat by editing the same message in place. The bridge side is
deliberately dumb -- watch the file, sync the text, keep the pin alive. All
content decisions (sections, ordering, length shaping, and the freshness
stamp on the last line) belong to the generator that writes the file. The
bridge never writes or preserves the stamp itself: a dead generator must show
up as a visibly stale timestamp, not a fresh-looking lie.

Activation is expressed by file presence: <bot_data_dir>/dashboard.md absent
means the feature is dormant (the poll keeps checking cheaply; the file
appearing later activates it without a restart). DASHBOARD_ENABLED=false in
config disables the sync task entirely.

Lifecycle: one DashboardSync task per polling iteration -- created next to
the watchdog task inside _run_async's try and cancelled in the SAME finally,
so the reconnect loop never leaves a zombie task holding a dead Bot object.
A dark dashboard during Conflict backoff is intended behavior.

Edit discipline (flood-budget friendliness):
  - mtime poll ~3s; changes coalesce via a dirty flag, the newest file
    content is read at edit time.
  - minimum interval between edits >= 3s per chat.
  - while the owner's turn task is in flight, edits are deferred and flushed
    on the first tick after the turn ends (the content is turn-generated, so
    deferring is also semantically consistent -- and it avoids competing with
    the streaming finalize burst).

Pin lifecycle: no probe edit at startup. When an edit fails with "message to
edit not found" or "message can't be edited" (token-swap collision), the
message is recreated: send + pin(disable_notification) + state save, guarded
by a recreate cooldown (armed on success only) against chained recreation.
An old message_id (e.g. left behind by a bot-token swap) gets a best-effort
unpin; failures are ignored. "message is not modified" is normal flow
(streaming._is_not_modified precedent). Terminal chat-level errors ("chat
not found", Forbidden) clear the state instead of poisoning it.

Owner safety: the stored chat_id is revalidated against the CURRENT owner on
every dirty tick; a mismatch (owner.lock reclaim / allowed_user_ids change)
discards the state so dashboard content never leaks into an ex-owner chat.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

import telegram.error
from telegram import Bot

from bridge import ownership
from bridge.config import config

logger = logging.getLogger(__name__)

DASHBOARD_FILE: Path = config.bot_data_dir / "dashboard.md"
# Flat placement beside poll_heartbeat / last_model.json (existing convention).
STATE_FILE: Path = config.bot_data_dir / "dashboard_state.json"

POLL_INTERVAL = 3.0       # seconds between mtime checks (spec: ~3s)
MIN_EDIT_INTERVAL = 3.0   # min seconds between edits per chat (spec: >=3s)
RECREATE_COOLDOWN = 60.0  # min seconds between send+pin recreations
# Telegram's 4096 message limit counts UTF-16 code units. The GENERATOR owns
# the smart, section-aware length cut (pending decisions preserved first);
# this bridge-side value only backs a dumb tail-cut safeguard so an oversized
# file can never break the edit call.
MAX_UTF16_UNITS = 3900


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _tail_cut(text: str) -> str:
    """Dumb tail-cut safeguard for oversized content.

    Content reaching the bridge should already fit (the generator guarantees
    length); this only prevents a hard Telegram rejection. Cutting on a raw
    UTF-16 boundary may split a surrogate pair -- decode ignores the orphan.
    """
    if _utf16_len(text) <= MAX_UTF16_UNITS:
        return text
    encoded = text.encode("utf-16-le")[: MAX_UTF16_UNITS * 2]
    return encoded.decode("utf-16-le", errors="ignore")


def _is_not_modified(error: Exception) -> bool:
    # Same normal-flow detection as streaming.StreamingMessageHandler.
    return "message is not modified" in str(error).lower()


def _needs_recreate(error: Exception) -> bool:
    """Edit errors proving the stored message is no longer OURS to edit.

    "message to edit not found": message deleted or id from another bot.
    "message can't be edited": after a bot-token swap the stored message_id
    can collide with a message the new bot may see but not edit. Our own
    plain-text messages are always editable by us, so on a healthy dashboard
    this string can never occur -- safe as a recreate trigger.
    """
    text = str(error).lower()
    return (
        "message to edit not found" in text
        or "message can't be edited" in text
    )


def _is_chat_gone(error: Exception) -> bool:
    """Terminal chat-level failure: the stored chat itself is unusable."""
    return "chat not found" in str(error).lower()


class DashboardSync:
    """Async task syncing dashboard.md into the pinned owner-chat message."""

    def __init__(
        self,
        bot: Bot,
        turn_active: Callable[[int], bool],
        dashboard_path: Path = DASHBOARD_FILE,
        state_path: Path = STATE_FILE,
    ) -> None:
        self._bot = bot
        # turn_active(user_id) -> True while that user's turn is in flight.
        # Private owner chat means chat_id == user_id.
        self._turn_active = turn_active
        self._dashboard_path = Path(dashboard_path)
        self._state_path = Path(state_path)
        self._chat_id: Optional[int] = None
        self._message_id: Optional[int] = None
        self._last_synced_mtime: Optional[float] = None
        self._dirty = False
        self._last_edit: Optional[float] = None      # monotonic
        self._last_recreate: Optional[float] = None  # monotonic
        self._flood_until: float = 0.0               # monotonic deadline
        self._load_state()

    # --- state file ({chat_id, message_id}, atomic writes) ---

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001 - corrupt state -> fresh start
            logger.warning("dashboard_state.json unreadable (%s); ignoring", e)
            return
        chat_id = data.get("chat_id") if isinstance(data, dict) else None
        message_id = data.get("message_id") if isinstance(data, dict) else None
        # type() is int, not isinstance: bools ARE ints in Python, and a
        # corrupt {"chat_id": true} must not load as chat_id=1.
        if type(chat_id) is int and type(message_id) is int:
            self._chat_id = chat_id
            self._message_id = message_id
        # A loaded chat_id is provisional: _tick revalidates it against the
        # current owner before any network call (owner-change safety).

    def _clear_state(self) -> None:
        """Discard chat/message state (memory + disk) so the next dirty tick
        re-bootstraps from the current owner via the recreate path."""
        self._chat_id = None
        self._message_id = None
        try:
            self._state_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("dashboard state clear failed: %s", e)

    def _save_state(self) -> None:
        """Atomic write, tmp + os.replace (heartbeat.touch precedent)."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"chat_id": self._chat_id, "message_id": self._message_id},
                ensure_ascii=True,
            )
            tmp = self._state_path.with_name(self._state_path.name + ".tmp")
            tmp.write_text(payload, encoding="ascii")
            os.replace(tmp, self._state_path)
        except Exception as e:  # noqa: BLE001 - state write must not kill the task
            logger.warning("dashboard state persist failed: %s", e)

    # --- owner resolution ---

    def _owner_chat_id(self) -> Optional[int]:
        """Bootstrap chat id via ownership precedence; None = stay dormant.

        Private chat: chat_id == user_id. An unclaimed or locked-out bot has
        no owner to pin a dashboard for, so the feature stays dormant.
        """
        mode, owner_id = ownership.resolve_owner(
            config.allowed_user_ids, config.bot_data_dir
        )
        if mode == ownership.MODE_AUTHORITATIVE:
            return config.allowed_user_ids[0]
        if mode == ownership.MODE_OWNER_LOCK:
            return owner_id
        return None

    # --- main loop ---

    async def run(self) -> None:
        if not config.dashboard_enabled:
            logger.info("Dashboard sync disabled (DASHBOARD_ENABLED=false)")
            return
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - sync must never die mid-loop
                logger.warning("Dashboard tick failed: %s", e)

    async def _tick(self) -> None:
        try:
            mtime = os.path.getmtime(self._dashboard_path)
        except OSError:
            return  # file absent = feature dormant (keep polling cheaply)
        if self._last_synced_mtime is None or mtime != self._last_synced_mtime:
            self._dirty = True
        if not self._dirty:
            return

        now = time.monotonic()
        if now < self._flood_until:
            return  # flood wait pending; stay dirty
        if self._last_edit is not None and (now - self._last_edit) < MIN_EDIT_INTERVAL:
            return  # min edit interval; stay dirty, coalesce

        chat_id = self._owner_chat_id()
        if chat_id is None:
            return  # unclaimed bot = dormant
        if self._chat_id is not None and self._chat_id != chat_id:
            # Owner changed (owner.lock reclaim / allowed_user_ids edit):
            # NEVER keep editing content into the ex-owner's chat. Discard
            # the stale state; the recreate path re-bootstraps next tick.
            logger.warning(
                "Dashboard state chat %s != current owner %s; resetting",
                self._chat_id, chat_id,
            )
            self._clear_state()
            return  # stay dirty

        if self._turn_active(chat_id):
            return  # defer during the owner's turn; flushed right after it ends

        # Generator writes atomically, so an mtime change means a complete
        # file; read defensively anyway.
        try:
            text = self._dashboard_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        if not text.strip():
            return  # empty content: nothing to render (Telegram rejects "")
        await self._sync(chat_id, _tail_cut(text), mtime)

    # --- sync / recreate ---

    def _mark_synced(self, mtime: float, now: float) -> None:
        self._dirty = False
        self._last_synced_mtime = mtime
        self._last_edit = now

    async def _sync(self, chat_id: int, text: str, mtime: float) -> None:
        now = time.monotonic()
        if self._message_id is not None:
            try:
                await self._bot.edit_message_text(
                    text=text, chat_id=chat_id, message_id=self._message_id
                )
                self._mark_synced(mtime, now)
                return
            except telegram.error.RetryAfter as e:
                self._flood_until = now + float(getattr(e, "retry_after", 5.0))
                return  # stay dirty; retried after the flood wait
            except telegram.error.BadRequest as e:
                if _is_not_modified(e):
                    self._mark_synced(mtime, now)  # normal flow, not an error
                    return
                if _is_chat_gone(e):
                    # Terminal chat-level failure: clear state instead of
                    # poisoning it forever; recreate re-bootstraps from the
                    # current owner on a later tick.
                    logger.warning("Dashboard chat unusable (%s); state reset", e)
                    self._clear_state()
                    return  # stay dirty
                if not _needs_recreate(e):
                    # Content revision Telegram refuses: skip it (the next
                    # file change retries) instead of hot-looping the API.
                    logger.warning("Dashboard edit rejected: %s", e)
                    self._mark_synced(mtime, now)
                    return
                # Message no longer editable by us -> fall through to recreate.
            except telegram.error.Forbidden as e:
                # Bot blocked/kicked: terminal for this chat state as well.
                logger.warning("Dashboard chat forbidden (%s); state reset", e)
                self._clear_state()
                return  # stay dirty
            except telegram.error.NetworkError as e:
                logger.debug("Dashboard edit transient failure: %s", e)
                return  # stay dirty; transient, retried next tick
            except telegram.error.TelegramError as e:
                logger.warning("Dashboard edit failed: %s", e)
                self._mark_synced(mtime, now)  # skip revision, no hot loop
                return

        await self._recreate(chat_id, text, mtime, now)

    async def _recreate(
        self, chat_id: int, text: str, mtime: float, now: float
    ) -> None:
        """Send a fresh dashboard message, pin it, persist state.

        Guarded by a cooldown so a persistently failing edit path cannot spam
        the chat with chained recreations.
        """
        if (
            self._last_recreate is not None
            and (now - self._last_recreate) < RECREATE_COOLDOWN
        ):
            return  # cooldown; stay dirty, retry later

        old_message_id = self._message_id
        try:
            message = await self._bot.send_message(chat_id=chat_id, text=text)
        except telegram.error.RetryAfter as e:
            self._flood_until = now + float(getattr(e, "retry_after", 5.0))
            return
        except telegram.error.TelegramError as e:
            if isinstance(e, telegram.error.Forbidden) or _is_chat_gone(e):
                self._clear_state()  # terminal chat-level: do not poison state
            logger.warning("Dashboard recreate send failed: %s", e)
            return  # stay dirty; retried on a later tick

        # Cooldown armed only on SUCCESS: it guards against chained
        # recreations spamming the chat -- a failed send posted nothing, so
        # delaying the first healthy recreate would be pure loss.
        self._last_recreate = now
        self._chat_id = chat_id
        self._message_id = message.message_id
        self._save_state()

        try:
            await self._bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message.message_id,
                disable_notification=True,
            )
        except telegram.error.TelegramError as e:
            # Message exists and is tracked; a failed pin only loses the
            # always-on-top affordance. Edits keep working (message_id is
            # the source of truth, the pin is an accessibility device).
            logger.warning("Dashboard pin failed (continuing unpinned): %s", e)

        if old_message_id is not None:
            # A stale pin can survive e.g. a bot-token swap; best-effort
            # unpin of the previous message, failures ignored.
            try:
                await self._bot.unpin_chat_message(
                    chat_id=chat_id, message_id=old_message_id
                )
            except telegram.error.TelegramError:
                pass

        self._mark_synced(mtime, now)
