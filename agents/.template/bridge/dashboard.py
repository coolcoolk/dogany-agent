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

Empty content (DGN-541 S1): an empty dashboard.md is the generator's hide
signal.  It drives a mechanical, debounced delete state machine: an empty
read with a live message_id only MARKS "empty pending"; the actual
unpin+delete runs after the empty state persisted EMPTY_DEBOUNCE_SECS
(kills flapping from generator false-empties).  Not-found on delete
converges as success (dead message_id, e.g. a restart that lost the
post-delete state save -- the first tick after boot is always dirty, so a
ghost pin never survives a restart).  Delete failures fail open (state
kept, retried on a later tick).  Re-appearance goes through the normal
recreate path (send+pin, both silent, RECREATE_COOLDOWN unchanged and
separate from the empty debounce).
An old message_id (e.g. left behind by a bot-token swap) is unpinned via the
stale-pin queue below. "message is not modified" is normal flow
(streaming._is_not_modified precedent). Terminal chat-level errors ("chat
not found", Forbidden) clear the state instead of poisoning it.

Daily rotation (DGN-1768): the pinned board drifts back in chat history as
days pass, so the first REAL content change on a new local calendar day (OS
TZ, the instance clock) is sent as a NEW message instead of an edit: send +
pin(disable_notification) through the recreate path, then delete the previous
board message (< 48h old on a daily cadence).  A refused delete falls back to
the existing unpin-only convergence (_is_undeletable etc.): the orphan body
stays, unpinned.  Later changes that day edit the new message.  A day without
a change sends nothing -- there is no scheduled send.  "Real change" is a
content digest compare, not mtime: the always-dirty first tick after a
restart re-edits (usually "not modified") instead of rotating.  The state
file therefore carries {chat_id, message_id, sent_day, digest}.  A legacy
state without them loads with sent_day/digest unknown: the first tick edits as
before (no rotation on the restart alone) and records the digest; the next
real change then rotates, since the unknown send day never equals today.

Explicit fresh request (DGN-1768 r2): a producer that owns the day start
(e.g. a morning brief) runs `status-footer.py --pin-set <owner> ... --fresh`,
which writes its section and then drops ROTATE_REQUEST_FILE next to the state
file.  The next tick treats the marker as dirty and rotates (send + pin +
delete old, same path as above) regardless of day or digest, bypassing the
recreate cooldown (a deliberate one-shot request, not an edit-failure chain),
then consumes the marker -- only after the send succeeded, so a failed send
retries on a later tick.  An empty board consumes it too (nothing to rotate;
the hide path owns it).  `--wait` on the producer blocks until the marker is
gone so its message order holds.  DASHBOARD_ROTATE selects the day rule:
auto (default, the first-change-of-day rule above) | explicit (rotate ONLY on
a fresh request; every other change, a section clear included, edits).  Read
per dirty tick from env / .telegram_bot/.env, then config/agent.conf.

One board pin (DGN-1777): every board id the bridge pinned and has not yet
seen unpinned or deleted is persisted in the state file ("stale_pins", with
its chat and an attempt count).  After a rotation/recreate the old id is
queued BEFORE any unpin call (a crash in between cannot lose it), then every
queued id is unpinned; the live board id is never a target, and nothing the
bridge did not pin itself (owner pins) is ever touched.  An unpin failure is
logged and the id stays queued: the tick retries it every
STALE_UNPIN_RETRY_SECS, up to STALE_UNPIN_MAX_ATTEMPTS, across restarts.
"Not found" and terminal chat errors converge (nothing left to unpin).  The
empty/hide path queues the id too when its unpin failed and the delete was
refused (48h age limit), so the orphan body never stays pinned silently.

Owner-unpinned live board (DGN-1782, dec-201): when the owner unpins the LIVE
board it is re-pinned silently (disable_notification).  Signal: getChat's
pinned_message is "the most recent pinned message by SENDING date" (Bot API),
and message ids grow with send time.  So with top = that id:
  - top == live board id -> the board is pinned, nothing to do;
  - no pinned message, or top < live board id -> the board is NOT pinned (if
    it were, it would outrank every older pin) -> re-pin it;
  - top > live board id -> the owner pinned a newer message; whether the
    board is still pinned below it is unknowable through the Bot API, and a
    re-pin could never make the board the top pin anyway -> leave it alone
    (no churn; the next rotation re-pins a fresh board).
Only the live board id is ever re-pinned; stale ids (always older) and owner
pins are never pinned or unpinned by this check.  Cadence (flood-safe): the
check runs on an idle tick (not dirty, edit guard ready, no owner turn in
flight, no pending hide) at most once per PIN_CHECK_MIN_SECS after a real
content edit, and otherwise at most once per PIN_CHECK_IDLE_SECS.

Owner safety: the stored chat_id is revalidated against the CURRENT owner on
every dirty tick; a mismatch (owner.lock reclaim / allowed_user_ids change)
discards the state so dashboard content never leaks into an ex-owner chat.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import telegram.error
from telegram import Bot

from bridge import ownership
from bridge.config import config, notify_silent
# MIN_EDIT_INTERVAL and _is_not_modified moved to bridge.edit_guard (DGN-594,
# shared with the countdown helper); re-imported here as the compat surface.
from bridge.edit_guard import (  # noqa: F401 - re-exported names
    MIN_EDIT_INTERVAL,
    EditRateGuard,
    _is_not_modified,
)
# DGN-974: the pin surface reuses the SAME converter as the chat path
# (bridge.formatting.sanitize_message_for_telegram -- code fences -> <pre>/
# <code>, prose -> markdown_to_telegram_html) instead of growing a second
# one. balance_telegram_html guards against a straddling/unbalanced tag
# reaching Telegram, same as every other parse_mode="HTML" send site.
from bridge.formatting import balance_telegram_html, sanitize_message_for_telegram
from bridge.machine_gate import RAIL_CONSUMER

logger = logging.getLogger(__name__)

DASHBOARD_FILE: Path = config.bot_data_dir / "dashboard.md"
# Flat placement beside poll_heartbeat / last_model.json (existing convention).
STATE_FILE: Path = config.bot_data_dir / "dashboard_state.json"
# DGN-1768 r2 fresh-rotation request marker, always the state file's sibling
# (status-footer.py --pin-set --fresh writes it, the tick consumes it).
ROTATE_REQUEST_NAME = "dashboard_rotate.request"
AGENT_CONF_FILE: Path = config.bot_data_dir.parent / "config" / "agent.conf"
ROTATE_AUTO = "auto"
ROTATE_EXPLICIT = "explicit"

POLL_INTERVAL = 3.0       # seconds between mtime checks (spec: ~3s)
RECREATE_COOLDOWN = 60.0  # min seconds between send+pin recreations
# Empty state must persist this long before the pinned message is actually
# unpinned+deleted (DGN-541 S1 debounce, spec range [30, 60]).  Separate
# from RECREATE_COOLDOWN by design.
EMPTY_DEBOUNCE_SECS = 45.0
# Telegram's 4096 message limit counts UTF-16 code units. The GENERATOR owns
# the smart, section-aware length cut (pending decisions preserved first);
# this bridge-side value only backs a dumb tail-cut safeguard so an oversized
# file can never break the edit call.
MAX_UTF16_UNITS = 3900
# DGN-1777: retry cadence and cap for unpinning past board ids that failed to
# unpin (persisted queue, see "One board pin" above).
STALE_UNPIN_RETRY_SECS = 300.0
STALE_UNPIN_MAX_ATTEMPTS = 12
# DGN-1782: live-board pin-state check cadence (getChat).  After a real edit
# the check may run once PIN_CHECK_MIN_SECS elapsed since the previous one;
# with no edits it still runs every PIN_CHECK_IDLE_SECS.
PIN_CHECK_MIN_SECS = 60.0
PIN_CHECK_IDLE_SECS = 300.0


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _tail_cut(text: str) -> str:
    """Dumb tail-cut safeguard for oversized content.

    Content reaching the bridge should already fit (the generator guarantees
    length); this only prevents a hard Telegram rejection. DGN-950 seam 1:
    the old raw UTF-16 byte slice could land inside a surrogate pair and
    corrupt the display name sitting on the cut boundary. The cut now walks
    codepoints against the UTF-16 unit budget and slices the str at a
    codepoint boundary, so a multibyte char (hangul, emoji) is either kept
    whole or dropped whole -- never split.
    """
    if _utf16_len(text) <= MAX_UTF16_UNITS:
        return text
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > MAX_UTF16_UNITS:
            return text[:i]
    return text


def _local_day() -> str:
    """Today's local calendar day (OS TZ, the instance clock), ISO format.

    OS-local on purpose, same convention as the memory engine's clock line:
    never hard-code a zone.  Module-level so tests can pin the day.
    """
    return datetime.now().astimezone().date().isoformat()


def _digest(text: str) -> str:
    """Short content digest: tells a real board change from a re-read."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _conf_value(path: Path, key: str) -> str:
    """KEY=value from a conf file (conf_reader.py normalization: first match,
    whitespace + surrounding quotes stripped).  Any read failure -> ""."""
    try:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _rotate_mode() -> str:
    """DASHBOARD_ROTATE: env / .telegram_bot/.env -> config/agent.conf ->
    auto.  Anything but "explicit" is auto (r1 behavior is the default)."""
    value = (
        getattr(config, "dashboard_rotate", "")
        or _conf_value(AGENT_CONF_FILE, "DASHBOARD_ROTATE")
    )
    return ROTATE_EXPLICIT if value.strip().lower() == ROTATE_EXPLICIT else ROTATE_AUTO


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


def _is_message_gone(error: Exception) -> bool:
    """Delete errors proving the message is ALREADY gone -> success.

    "message to delete not found": deleted earlier (e.g. a restart lost the
    post-delete state save) or the id belongs to another bot.  Converging a
    dead message_id here is what keeps the delete path idempotent (DGN-541).
    """
    text = str(error).lower()
    return "message to delete not found" in text or "message not found" in text


def _is_unpin_converged(error: Exception) -> bool:
    """Unpin errors proving there is nothing left for us to unpin (DGN-1777):
    the message is gone, or the chat itself is unusable (blocked / not found).
    """
    if isinstance(error, telegram.error.Forbidden) or _is_chat_gone(error):
        return True
    text = str(error).lower()
    return "message to unpin not found" in text or "message not found" in text


def _is_parse_error(error: Exception) -> bool:
    """True when a BadRequest is Telegram's HTML entity-parse rejection.

    DGN-974: Telegram's parse_mode="HTML" failures are consistently prefixed
    "Can't parse entities" (case varies by client library / server version),
    e.g. "Can't parse entities: unsupported start tag ... at byte offset N".
    Narrowed to that phrase ON PURPOSE: an unrelated BadRequest -- "message is
    not modified", a length limit, a permission issue -- must NOT be caught
    by the plain-text fallback and must keep its pre-existing handling
    (grill finding: a fallback catching too broadly would silently mask a
    genuine, unrelated bug).
    """
    return "can't parse entities" in str(error).lower()


def _is_undeletable(error: Exception) -> bool:
    """Permanent delete refusal: a bot cannot delete-for-everyone a message
    older than 48h.  Retrying never succeeds, so once the best-effort unpin has
    hidden the board we converge and drop the id -- the orphan body stays in
    the chat, unpinned (DGN-541).
    """
    return "can't be deleted for everyone" in str(error).lower()


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
        self._rotate_request = self._state_path.with_name(ROTATE_REQUEST_NAME)
        self._chat_id: Optional[int] = None
        self._message_id: Optional[int] = None
        # DGN-1768: local day the pinned message was SENT, and the digest of
        # the last content it shows.  None = unknown (legacy state / fresh).
        self._sent_day: Optional[str] = None
        self._digest: Optional[str] = None
        self._last_synced_mtime: Optional[float] = None
        self._dirty = False
        # Flood/interval state lives in the shared guard (DGN-594);
        # _last_edit/_flood_until below are property shims over it.
        self._edit_guard = EditRateGuard()
        self._last_recreate: Optional[float] = None  # monotonic
        # First empty read (monotonic) while a message is pinned; None when
        # content is present or no delete is pending (DGN-541 S1 debounce).
        self._empty_since: Optional[float] = None
        # DGN-1777: past board ids we pinned and have not seen unpinned or
        # deleted: [{"chat_id", "message_id", "attempts"}], persisted.
        self._stale_pins: list = []
        self._next_unpin_sweep = 0.0  # monotonic
        # DGN-1782: live-board pin-state check (monotonic; None = never ran).
        self._last_pin_check: Optional[float] = None
        self._pin_check_due = False  # a real edit happened since the last check
        self._load_state()

    # --- shared edit-guard state (single source: EditRateGuard, DGN-594) ---
    # Property shims keep the historical attribute surface (used across this
    # class and by tests) reading/writing the ONE guard state.

    @property
    def _last_edit(self) -> Optional[float]:  # monotonic
        return self._edit_guard.last_edit

    @_last_edit.setter
    def _last_edit(self, value: Optional[float]) -> None:
        self._edit_guard.last_edit = value

    @property
    def _flood_until(self) -> float:  # monotonic deadline
        return self._edit_guard.flood_until

    @_flood_until.setter
    def _flood_until(self, value: float) -> None:
        self._edit_guard.flood_until = value

    # --- state file ({chat_id, message_id, sent_day, digest, stale_pins},
    # atomic writes) ---

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001 - corrupt state -> fresh start
            logger.warning("dashboard_state.json unreadable (%s); ignoring", e)
            return
        if isinstance(data, dict):
            self._stale_pins = self._parse_stale_pins(data.get("stale_pins"))
        chat_id = data.get("chat_id") if isinstance(data, dict) else None
        message_id = data.get("message_id") if isinstance(data, dict) else None
        # type() is int, not isinstance: bools ARE ints in Python, and a
        # corrupt {"chat_id": true} must not load as chat_id=1.
        if type(chat_id) is int and type(message_id) is int:
            self._chat_id = chat_id
            self._message_id = message_id
            # DGN-1768 fields are optional: a legacy file without them keeps
            # loading, with the send day and digest unknown.
            sent_day = data.get("sent_day")
            digest = data.get("digest")
            if isinstance(sent_day, str):
                self._sent_day = sent_day
            if isinstance(digest, str):
                self._digest = digest
        # A loaded chat_id is provisional: _tick revalidates it against the
        # current owner before any network call (owner-change safety).

    @staticmethod
    def _parse_stale_pins(raw: object) -> list:
        """Validate the persisted stale-pin queue; malformed entries dropped."""
        out: list = []
        if not isinstance(raw, list):
            return out
        for item in raw:
            if not isinstance(item, dict):
                continue
            chat_id = item.get("chat_id")
            message_id = item.get("message_id")
            attempts = item.get("attempts", 0)
            if type(chat_id) is int and type(message_id) is int:
                out.append({
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "attempts": attempts if type(attempts) is int else 0,
                })
        return out

    def _clear_state(self) -> None:
        """Discard chat/message state (memory + disk) so the next dirty tick
        re-bootstraps from the current owner via the recreate path.

        The stale-pin queue survives (DGN-1777): those ids are still pinned
        in their chats; the file is kept for them while any remain."""
        self._chat_id = None
        self._message_id = None
        self._sent_day = None
        self._digest = None
        if self._stale_pins:
            self._save_state()
            return
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
                {
                    "chat_id": self._chat_id,
                    "message_id": self._message_id,
                    "sent_day": self._sent_day,
                    "digest": self._digest,
                    "stale_pins": self._stale_pins,
                },
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
        if self._stale_pins:
            sweep_now = time.monotonic()
            if (
                sweep_now >= self._next_unpin_sweep
                and self._edit_guard.ready(sweep_now)
            ):
                await self._unpin_stale(sweep_now)
        try:
            mtime = os.path.getmtime(self._dashboard_path)
        except OSError:
            return  # file absent = feature dormant (keep polling cheaply)
        if self._last_synced_mtime is None or mtime != self._last_synced_mtime:
            self._dirty = True
        if self._fresh_requested():
            self._dirty = True  # DGN-1768 r2: rotate even on unchanged content
        if not self._dirty:
            await self._maybe_check_pin()
            return

        now = time.monotonic()
        if not self._edit_guard.ready(now):
            return  # flood wait pending or min edit interval; stay dirty, coalesce

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
        if "\ufffd" in text:
            # DGN-950 seam 1: a torn read (generator caught mid-write) can
            # cut a multibyte char at a byte boundary; errors="replace" then
            # materializes U+FFFD inside a display name (hangul renders as
            # a replacement char). Never render a broken frame: stay dirty
            # and re-read on a later tick, when the write has completed.
            logger.debug("Dashboard read caught U+FFFD (torn read); deferring")
            return
        if not text.strip():
            # Empty board = generator hide signal: drive the debounced
            # delete state machine instead of leaving a stale pin (DGN-541).
            self._consume_fresh_request()  # nothing to rotate on a hidden board
            await self._handle_empty(chat_id, mtime)
            return
        self._empty_since = None  # content is back; cancel any pending delete
        await self._sync(chat_id, _tail_cut(text), mtime)

    # --- empty content: debounced unpin+delete (DGN-541 S1) ---

    async def _handle_empty(self, chat_id: int, mtime: float) -> None:
        """Debounced unpin+delete of the pinned message on empty content.

        Mechanical only: empty vs non-empty plus timers -- no content
        decisions.  First empty read MARKS "empty pending" (stays dirty, no
        delete); the actual unpin+delete runs only once the empty state has
        persisted EMPTY_DEBOUNCE_SECS.  Not-found converges as success;
        other failures fail open (state kept, retried on a later tick).
        After a completed delete message_id is marked None and the state
        saved, so later Stops do not re-attempt the delete in a loop.
        """
        now = time.monotonic()
        if self._message_id is None:
            # Nothing pinned: empty content is already converged.
            self._empty_since = None
            self._mark_synced(mtime, now)
            return
        if self._empty_since is None:
            self._empty_since = now  # mark pending; stay dirty, no delete yet
            return
        if (now - self._empty_since) < EMPTY_DEBOUNCE_SECS:
            return  # debounce running; stay dirty
        # Unpin first; the delete is the authoritative removal.  A failed
        # unpin is logged and, if the delete is refused too, queued below.
        unpin_failed = False
        try:
            await self._bot.unpin_chat_message(
                chat_id=chat_id, message_id=self._message_id
            )
        except telegram.error.TelegramError as e:
            if not _is_unpin_converged(e):
                unpin_failed = True
                logger.warning(
                    "Dashboard unpin of board %s failed: %s", self._message_id, e
                )
        try:
            await self._bot.delete_message(
                chat_id=chat_id, message_id=self._message_id
            )
        except telegram.error.RetryAfter as e:
            self._edit_guard.note_retry_after(
                float(getattr(e, "retry_after", 5.0)), now
            )
            return  # stay dirty; retried after the flood wait
        except telegram.error.Forbidden as e:
            logger.warning("Dashboard chat forbidden on delete (%s); state reset", e)
            self._clear_state()
            return
        except telegram.error.TelegramError as e:
            if not _is_message_gone(e):
                if _is_chat_gone(e):
                    logger.warning(
                        "Dashboard chat unusable on delete (%s); state reset", e
                    )
                    self._clear_state()
                    return
                if not _is_undeletable(e):
                    # Fail-open: keep state, retry on a later tick, never crash.
                    logger.warning("Dashboard delete failed (retrying later): %s", e)
                    return
                # Undeletable (48h age limit): the best-effort unpin already hid
                # the board, so converge and drop the id -- retrying can never
                # succeed.  The orphan body stays in the chat, unpinned.
                logger.info(
                    "Dashboard message undeletable (age limit); unpinned, converging: %s", e
                )
                if unpin_failed:
                    # DGN-1777: the body stays AND is still pinned -> retry
                    # the unpin from the persisted queue.
                    self._queue_unpin(chat_id, self._message_id)
            # Not-found or undeletable: converge as success.
        self._message_id = None
        self._sent_day = None
        self._digest = None
        self._save_state()
        self._empty_since = None
        self._mark_synced(mtime, now)

    # --- stale board pins (DGN-1777): exactly one board pin per chat ---

    def _queue_unpin(self, chat_id: int, message_id: int) -> None:
        """Remember a board id we pinned that must end up unpinned."""
        for entry in self._stale_pins:
            if entry["chat_id"] == chat_id and entry["message_id"] == message_id:
                return
        self._stale_pins.append(
            {"chat_id": chat_id, "message_id": message_id, "attempts": 0}
        )
        self._save_state()

    def _drop_stale(self, chat_id: int, message_id: int) -> None:
        before = len(self._stale_pins)
        self._stale_pins = [
            e for e in self._stale_pins
            if not (e["chat_id"] == chat_id and e["message_id"] == message_id)
        ]
        if len(self._stale_pins) != before:
            self._save_state()

    async def _unpin_stale(self, now: float) -> None:
        """Unpin every queued past board id; never the live board.

        Success or a converged error drops the entry.  RetryAfter stops the
        sweep (flood guard armed).  Any other failure is LOGGED, counted and
        kept for the next sweep; at STALE_UNPIN_MAX_ATTEMPTS it is dropped
        with a warning so a permanent refusal cannot log forever.
        """
        self._next_unpin_sweep = now + STALE_UNPIN_RETRY_SECS
        changed = False
        for entry in list(self._stale_pins):
            chat_id = entry["chat_id"]
            message_id = entry["message_id"]
            if chat_id == self._chat_id and message_id == self._message_id:
                self._stale_pins.remove(entry)  # the live board stays pinned
                changed = True
                continue
            try:
                await self._bot.unpin_chat_message(
                    chat_id=chat_id, message_id=message_id
                )
            except telegram.error.RetryAfter as e:
                self._edit_guard.note_retry_after(
                    float(getattr(e, "retry_after", 5.0)), now
                )
                break
            except telegram.error.TelegramError as e:
                if _is_unpin_converged(e):
                    logger.info(
                        "Dashboard old board %s: nothing to unpin (%s)", message_id, e
                    )
                else:
                    entry["attempts"] += 1
                    changed = True
                    if entry["attempts"] < STALE_UNPIN_MAX_ATTEMPTS:
                        logger.warning(
                            "Dashboard unpin of old board %s failed (attempt %d/%d,"
                            " retrying): %s",
                            message_id, entry["attempts"],
                            STALE_UNPIN_MAX_ATTEMPTS, e,
                        )
                        continue
                    logger.warning(
                        "Dashboard unpin of old board %s failed %d times; giving"
                        " up (may stay pinned): %s",
                        message_id, entry["attempts"], e,
                    )
            else:
                logger.info("Dashboard old board %s unpinned", message_id)
            self._stale_pins.remove(entry)
            changed = True
        if changed:
            self._save_state()

    # --- live board pin state (DGN-1782): owner unpin -> silent re-pin ---

    async def _maybe_check_pin(self) -> None:
        """Re-pin the live board if the owner unpinned it (see module doc).

        Runs only on an idle tick and within the bounded cadence; respects
        the edit guard (flood wait / min interval) and the owner's turn."""
        if self._message_id is None or self._chat_id is None:
            return
        if self._empty_since is not None:
            return  # hide pending: the empty path owns this board
        now = time.monotonic()
        if self._last_pin_check is not None:
            elapsed = now - self._last_pin_check
            due = elapsed >= PIN_CHECK_IDLE_SECS or (
                self._pin_check_due and elapsed >= PIN_CHECK_MIN_SECS
            )
            if not due:
                return
        if not self._edit_guard.ready(now):
            return
        chat_id = self._owner_chat_id()
        if chat_id is None or chat_id != self._chat_id:
            return  # owner change is handled by the dirty path
        if self._turn_active(chat_id):
            return  # deferred like every other board call
        self._last_pin_check = now
        self._pin_check_due = False
        message_id = self._message_id
        try:
            chat = await self._bot.get_chat(chat_id=chat_id)
        except telegram.error.RetryAfter as e:
            self._edit_guard.note_retry_after(
                float(getattr(e, "retry_after", 5.0)), now
            )
            return
        except telegram.error.TelegramError as e:
            logger.debug("Dashboard pin check failed: %s", e)
            return
        top = getattr(chat, "pinned_message", None)
        top_id = getattr(top, "message_id", None) if top is not None else None
        if top_id == message_id:
            return  # pinned and on top
        if type(top_id) is int and top_id > message_id:
            # A newer (owner) pin outranks the board; its own pin state is
            # not observable, and re-pinning could not change the top.
            logger.debug(
                "Dashboard pin check: newer pin %s above board %s; leaving it",
                top_id, message_id,
            )
            return
        logger.info(
            "Dashboard board %s no longer pinned (top pin: %s); re-pinning",
            message_id, top_id,
        )
        try:
            await self._bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message_id,
                disable_notification=notify_silent("dashboard"),
            )
            self._edit_guard.note_edit(now)
        except telegram.error.RetryAfter as e:
            self._edit_guard.note_retry_after(
                float(getattr(e, "retry_after", 5.0)), now
            )
            self._last_pin_check = None  # retry once the flood wait ends
        except telegram.error.TelegramError as e:
            logger.warning("Dashboard re-pin of board %s failed: %s", message_id, e)

    # --- sync / recreate ---

    def _mark_synced(self, mtime: float, now: float) -> None:
        self._dirty = False
        self._last_synced_mtime = mtime
        self._edit_guard.note_edit(now)

    def _note_shown(self, text: str) -> None:
        """Record the digest of the content the pinned message now shows;
        persisted only when it changed (restart must see the same digest)."""
        digest = _digest(text)
        if digest != self._digest:
            self._digest = digest
            self._save_state()

    def _fresh_requested(self) -> bool:
        return self._rotate_request.exists()

    def _consume_fresh_request(self) -> None:
        try:
            self._rotate_request.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Dashboard fresh request not consumed: %s", e)

    def _rotation_due(self, text: str) -> bool:
        """DGN-1768: first real change on a day other than the send day.

        An unknown digest (legacy state) is never a "real change": the edit
        path runs and records it, so a restart alone cannot rotate.
        DASHBOARD_ROTATE=explicit disables this rule (fresh requests only).
        """
        return (
            _rotate_mode() == ROTATE_AUTO
            and self._message_id is not None
            and self._digest is not None
            and _digest(text) != self._digest
            and self._sent_day != _local_day()
        )

    # --- DGN-974: pin surface HTML render + fail-open send helpers ---

    def _render_pin_html(self, text: str) -> Optional[str]:
        """Convert pin text through the SAME converter the chat path uses.

        Returns the balanced Telegram HTML, or None on ANY conversion
        failure (fail-open): the pin body is generator/owner-authored
        (DGN-967 section ownership -- e.g. an agent-authored code block, or a
        ticket title with arbitrary characters) and must never be able to
        block the pin from updating just because it doesn't convert cleanly.
        """
        try:
            # DGN-1209 R4: the pin body is generator/owner-authored consumer
            # text (no model turn) -> declared consumer rail.
            return balance_telegram_html(
                sanitize_message_for_telegram(text, rail=RAIL_CONSUMER)
            )
        except Exception as e:  # noqa: BLE001 - conversion must never break the pin
            logger.warning(
                "Dashboard HTML render failed (%s); falling back to plain text", e
            )
            return None

    async def _edit_with_fallback(
        self, chat_id: int, message_id: int, text: str
    ) -> None:
        """Edit with the chat-path converter's HTML; fail-open to plain text.

        DGN-974: on a Telegram parse-entity rejection (or a conversion
        failure caught in _render_pin_html), retry the IDENTICAL edit as
        plain text (no parse_mode) -- the pin must never stop updating over
        a formatting problem. Every other exception (RetryAfter, Forbidden,
        any non-parse BadRequest such as "message is not modified",
        NetworkError, generic TelegramError) propagates UNCHANGED so the
        caller's existing except chain handles it exactly as it did before
        parse_mode existed -- a broad catch here would silently mask an
        unrelated, genuine failure.
        """
        html_text = self._render_pin_html(text)
        if html_text is None:
            await self._bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id
            )
            return
        try:
            await self._bot.edit_message_text(
                text=html_text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML",
            )
        except telegram.error.BadRequest as e:
            if not _is_parse_error(e):
                raise
            logger.warning(
                "Dashboard HTML rejected by Telegram (%s); retrying plain text", e
            )
            await self._bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id
            )

    async def _send_with_fallback(
        self, chat_id: int, text: str, disable_notification: bool
    ):
        """Send with the chat-path converter's HTML; fail-open to plain text.

        Same fail-open contract as _edit_with_fallback (DGN-974), for the
        recreate send site. Returns the sent Message.
        """
        html_text = self._render_pin_html(text)
        if html_text is None:
            return await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_notification=disable_notification,
            )
        try:
            return await self._bot.send_message(
                chat_id=chat_id,
                text=html_text,
                parse_mode="HTML",
                disable_notification=disable_notification,
            )
        except telegram.error.BadRequest as e:
            if not _is_parse_error(e):
                raise
            logger.warning(
                "Dashboard HTML rejected by Telegram (%s); retrying plain text", e
            )
            return await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_notification=disable_notification,
            )

    async def _sync(self, chat_id: int, text: str, mtime: float) -> None:
        now = time.monotonic()
        if self._fresh_requested():
            # DGN-1768 r2 explicit request: rotate now, consume on success.
            if await self._recreate(chat_id, text, mtime, now, rotate=True,
                                    force=True):
                self._consume_fresh_request()
            return
        if self._rotation_due(text):
            # New day, first real change: fresh message + pin, old deleted.
            await self._recreate(chat_id, text, mtime, now, rotate=True)
            return
        if self._message_id is not None:
            try:
                await self._edit_with_fallback(chat_id, self._message_id, text)
                self._pin_check_due = True  # DGN-1782: verify the pin soon
                self._note_shown(text)
                self._mark_synced(mtime, now)
                return
            except telegram.error.RetryAfter as e:
                self._edit_guard.note_retry_after(
                    float(getattr(e, "retry_after", 5.0)), now
                )
                return  # stay dirty; retried after the flood wait
            except telegram.error.BadRequest as e:
                if _is_not_modified(e):
                    self._note_shown(text)
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
        self, chat_id: int, text: str, mtime: float, now: float,
        rotate: bool = False, force: bool = False,
    ) -> bool:
        """Send a fresh dashboard message, pin it, persist state.

        Guarded by a cooldown so a persistently failing edit path cannot spam
        the chat with chained recreations.  rotate=True (DGN-1768 daily
        rotation) additionally deletes the previous board message; any delete
        refusal falls back to the unpin-only path below.  force=True (r2
        fresh request) skips the cooldown.  Returns True once the new message
        was sent.
        """
        if (
            not force
            and self._last_recreate is not None
            and (now - self._last_recreate) < RECREATE_COOLDOWN
        ):
            return False  # cooldown; stay dirty, retry later

        old_message_id = self._message_id
        try:
            # Silent send (DGN-541): with conditional display the board
            # re-appears routinely; a notifying send each time would be an
            # alert storm.  Matches the pin's disable_notification below.
            # DGN-932: policy-routed (class "dashboard", default silent).
            # DGN-974: HTML via the chat-path converter, fail-open to plain.
            message = await self._send_with_fallback(
                chat_id,
                text,
                disable_notification=notify_silent("dashboard"),
            )
        except telegram.error.RetryAfter as e:
            self._edit_guard.note_retry_after(
                float(getattr(e, "retry_after", 5.0)), now
            )
            return False
        except telegram.error.TelegramError as e:
            if isinstance(e, telegram.error.Forbidden) or _is_chat_gone(e):
                self._clear_state()  # terminal chat-level: do not poison state
            logger.warning("Dashboard recreate send failed: %s", e)
            return False  # stay dirty; retried on a later tick

        # Cooldown armed only on SUCCESS: it guards against chained
        # recreations spamming the chat -- a failed send posted nothing, so
        # delaying the first healthy recreate would be pure loss.
        self._last_recreate = now
        self._last_pin_check = now  # DGN-1782: freshly pinned below
        self._pin_check_due = False
        self._chat_id = chat_id
        self._message_id = message.message_id
        self._sent_day = _local_day()
        self._digest = _digest(text)
        if old_message_id is not None and old_message_id != message.message_id:
            # DGN-1777: queued (and persisted with the new id) before any
            # unpin, so a crash in between cannot lose the old pinned id.
            self._queue_unpin(chat_id, old_message_id)
        self._save_state()

        try:
            # DGN-932: pin alert follows the same class value as the send.
            # Default (silent) -> no notifications. A loud override on the
            # non-default path produces two alerts (one send + one pin service
            # notification), which is acceptable: the override is deliberate.
            await self._bot.pin_chat_message(
                chat_id=chat_id,
                message_id=message.message_id,
                disable_notification=notify_silent("dashboard"),
            )
        except telegram.error.TelegramError as e:
            # Message exists and is tracked; a failed pin only loses the
            # always-on-top affordance. Edits keep working (message_id is
            # the source of truth, the pin is an accessibility device).
            logger.warning("Dashboard pin failed (continuing unpinned): %s", e)

        if rotate and old_message_id is not None:
            # DGN-1768: yesterday's board is ours and < 48h old -> delete it.
            # Gone already = done; any refusal (undeletable age limit, flood,
            # other) never retries: the new board is live, so fall back to
            # the unpin below and leave the orphan body unpinned.
            try:
                await self._bot.delete_message(
                    chat_id=chat_id, message_id=old_message_id
                )
                self._drop_stale(chat_id, old_message_id)  # deleted = unpinned
            except telegram.error.TelegramError as e:
                if _is_message_gone(e):
                    self._drop_stale(chat_id, old_message_id)
                else:
                    logger.info(
                        "Dashboard rotation: old board not deleted (%s); unpinning", e
                    )

        # The previous board (a stale pin can also survive e.g. a bot-token
        # swap) and any earlier queued id: unpin now, failures logged and
        # retried from the persisted queue (DGN-1777).
        await self._unpin_stale(now)

        self._mark_synced(mtime, now)
        return True
