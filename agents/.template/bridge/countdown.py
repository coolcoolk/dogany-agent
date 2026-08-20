"""Transient countdown primitive (DGN-594).

One transient (NON-pinned) message per countdown, edited in place every
`cadence` seconds to show remaining time + a fixed-width progress bar, then
finalized with a done line. Edit channel only (channel 1): milestone
notification pushes (60s/30s/start) are a SEPARATE push path (channel 2) and
are deliberately out of scope here.

Notification (DGN-932, owner lock 2026-08-19): the FIRST send of the tick
bubble notifies by default (class "countdown" -> loud: the set-start signal);
every later tick is an in-place edit, notification-free at the Telegram
level. This supersedes the original DGN-594 always-silent first send; an
instance can restore it via NOTIFY_POLICY="countdown=silent".

In-process API (spec: worklog 2026-08-06-dgn594-countdown-helper-spec):
    handle = start_countdown(bot, chat_id, seconds, label, cadence=10)
    handle.cancel()   # early stop + final cleanup edit

Cross-process trigger (domain agents run in separate processes; mirrors the
dashboard.md file-driven pattern):
    <bot_data_dir>/countdown/<id>.json =
        {"seconds": int, "label": str, "cadence": int (optional, default 10),
         "icon": str (optional), "done_icon": str (optional),
         "glyph": str preset name OR [filled, empty] pair (optional)}
        The appearance fields (DGN-780b) are optional and free-form: an unsafe
        or unknown value falls back silently to the default look, never
        rejecting the countdown.
    - file appears  -> CountdownDriver starts a countdown targeting the OWNER
      chat. Any chat_id inside the file is IGNORED: the control file is
      agent-writable, so trusting it would let a writer target arbitrary
      chats (owner resolution mirrors DashboardSync._owner_chat_id).
    - file deleted  -> that countdown is cancelled (cleanup edit).
    - countdown end -> the driver removes its own control file and writes a
      <id>.done marker (consumer watches for it, then deletes it).
    Absent directory = feature dormant (cheap poll, no error), mirroring the
    DASHBOARD_FILE dormancy style. Malformed files are skipped (kept on disk,
    warned once) -- a partial write parses fine on a later tick.

Failure discipline: fail-open everywhere. A failed send never starts a loop;
a failed edit ends the countdown quietly; the session flow and the dashboard/
worktable lifecycle are never touched. Edits go through the shared
EditRateGuard (bridge/edit_guard.py); the default 10s cadence is safely above
the 3s minimum edit interval, and flood/interval backoff simply skips a tick.
"""

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Set, Tuple

import telegram.error
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from bridge import messages, ownership
from bridge.config import config, notify_silent
from bridge.edit_guard import EditOutcome, EditRateGuard

logger = logging.getLogger(__name__)

COUNTDOWN_DIR: Path = config.bot_data_dir / "countdown"

POLL_INTERVAL = 3.0     # seconds between control-dir scans (dashboard style)
DEFAULT_CADENCE = 10    # seconds between in-place edits (spec default)
MAX_SECONDS = 24 * 3600  # sanity cap on a control-file countdown

# Fixed-width DRAINING progress bar (DGN-780): filled cells = time
# REMAINING, so the bar empties right->left as the countdown runs (matches
# the "remaining" meaning the body carries).
BAR_CELLS = 10

# Named glyph PRESETS (DGN-780). A preset is just a shorthand name for a
# (filled, empty) pair; a caller may pass an arbitrary pair directly instead
# (DGN-780b free-form). The config knob COUNTDOWN_GLYPH_SET selects a preset
# name; bridge/config.py normalizes it to this allowlist, and the .get()
# fallback below is defense in depth.
GLYPH_SETS: Dict[str, Tuple[str, str]] = {
    "dot": ("●", "○"),          # owner-locked default (filled, empty)
    "block-line": ("█", "─"),
    "square": ("■", "□"),
}
DEFAULT_GLYPH_SET = "dot"
# Default-set aliases kept for importers/tests.
BAR_FILLED, BAR_EMPTY = GLYPH_SETS[DEFAULT_GLYPH_SET]

# Default icons (DGN-780b). Leading icon on the body line, done-line icon.
# A caller may override either; an unsafe override falls back to these.
DEFAULT_ICON = "⏳"
DEFAULT_DONE_ICON = "✅"

# DGN-915: callback_data prefix for the completion-affordance button.
# Tapping it clears the inline keyboard from the done message (removes dead-air).
# Format: CDN_DONE_PREFIX + str(message_id).
# NOTE (DGN-922): the message_id suffix is encoded in the callback_data but the
# current handler (bot.py _handle_callback cdn:done branch) does NOT use it for
# an anti-stale check -- it simply calls edit_message_reply_markup(None) and lets
# the Telegram API return an error for any message that is already gone.  The
# suffix is preserved for future use (e.g. session cross-check) and does not
# harm anything.  The stale-message drop that previously killed these taps is
# bypassed by the skip_stale=True path added in DGN-922 FIX 2.
CDN_DONE_PREFIX = "cdn:done:"

# Markdown-risk characters forbidden in any caller-supplied icon/glyph
# (DGN-780b). These are the Telegram/markdown control chars that would break
# or hijack the PLAIN message surface. Emoji carry none of these and pass.
_MARKDOWN_RISK_CHARS = set("*_`#>|[]~")


# --- validation (call-supplied icons/glyphs) ---

def _is_safe_glyph(value: object) -> bool:
    """True when `value` is a non-empty str safe on the PLAIN message surface.

    Rejects: non-strings, empty/whitespace-only, any markdown-risk char, and
    anything that would trip the scaffold-leak guard (a newline could open a
    fresh line matching a harness signature). Emoji pass. This is the single
    gate applied to every caller-supplied icon and glyph.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    if any(ch in _MARKDOWN_RISK_CHARS for ch in value):
        return False
    if "\n" in value or "\r" in value:
        return False
    return True


# --- completion affordance (DGN-915) ---

def _build_done_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """Single-button keyboard for the countdown completion affordance.

    The callback_data encodes the message_id so a stale tap (button from a
    previous session) is detectable by the handler without session state.
    The handler edits the reply_markup away on tap; if the message is already
    gone Telegram returns an error which the handler swallows fail-soft.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            text=messages.COUNTDOWN_DONE_BUTTON,
            callback_data=f"{CDN_DONE_PREFIX}{message_id}",
        )
    ]])


# --- rendering ---

def _format_remaining(remaining: float) -> str:
    """m:ss with unpadded minutes (spec example: "2:30").

    Whole seconds are rounded UP so a still-running countdown never displays
    0:00 -- the zero moment belongs to the final done edit.
    """
    total = max(0, math.ceil(remaining))
    return f"{total // 60}:{total % 60:02d}"


def _config_glyphs() -> Tuple[str, str]:
    """(filled, empty) for the configured preset; allowlist miss -> default."""
    name = getattr(config, "countdown_glyph_set", DEFAULT_GLYPH_SET)
    return GLYPH_SETS.get(name, GLYPH_SETS[DEFAULT_GLYPH_SET])


def _resolve_glyphs(glyph: object = None) -> Tuple[str, str]:
    """Priority resolver call > config > default for the (filled, empty) pair.

    `glyph` may be:
      - None                -> fall through to config preset, then dot.
      - a preset NAME (str) -> that preset if known, else config/dot.
      - a (filled, empty) pair (tuple/list of two) -> used verbatim IF both
        pass _is_safe_glyph; any unsafe member -> silent fallback to config.
    """
    if glyph is None:
        return _config_glyphs()
    if isinstance(glyph, str):
        if glyph in GLYPH_SETS:
            return GLYPH_SETS[glyph]
        return _config_glyphs()  # unknown name: safe fallback
    if isinstance(glyph, (tuple, list)) and len(glyph) == 2:
        filled, empty = glyph
        if _is_safe_glyph(filled) and _is_safe_glyph(empty):
            return filled, empty
        return _config_glyphs()  # unsafe custom pair: silent safe fallback
    return _config_glyphs()


def _resolve_icon(icon: object, default: str) -> str:
    """Call-supplied icon if safe, else the default. Priority call > default."""
    if icon is None:
        return default
    return icon if _is_safe_glyph(icon) else default


def _render_bar(
    remaining_frac: float, glyph: object = None, cells: int = BAR_CELLS
) -> str:
    """Draining bar: filled cells = ceil(remaining_frac * cells).

    Ceil keeps a still-running countdown from ever showing a fully drained
    bar (parity with _format_remaining never showing 0:00); the tiny epsilon
    stops float noise from bumping an exact multiple up one cell. `glyph`
    goes through _resolve_glyphs (call > config > default).
    """
    frac = min(1.0, max(0.0, remaining_frac))
    filled = min(cells, max(0, math.ceil(frac * cells - 1e-9)))
    filled_glyph, empty_glyph = _resolve_glyphs(glyph)
    return filled_glyph * filled + empty_glyph * (cells - filled)


def render_countdown(
    label: str,
    remaining: float,
    total: float,
    icon: object = None,
    glyph: object = None,
) -> str:
    """Body line: `{icon} {label}  {m:ss}  {bar}` (locale catalog).

    icon/glyph are optional caller overrides; both resolve call > config >
    default and fall back silently on an unsafe value (DGN-780b).
    """
    remaining_frac = remaining / total if total > 0 else 0.0
    return messages.COUNTDOWN_BODY.format(
        icon=_resolve_icon(icon, DEFAULT_ICON),
        label=label,
        remaining=_format_remaining(remaining),
        bar=_render_bar(remaining_frac, glyph),
    )


def render_done(label: str, done_icon: object = None) -> str:
    """Done line: `{done_icon} {label} done/wanryo` (locale catalog)."""
    return messages.COUNTDOWN_DONE.format(
        done_icon=_resolve_icon(done_icon, DEFAULT_DONE_ICON),
        label=label,
    )


def _next_boundary(remaining: float, cadence: float) -> float:
    """Next cadence boundary STRICTLY below `remaining`, floored at 0.

    Deadline-anchored snap math (DGN-780): e.g. remaining 24.3s at cadence 5
    -> 20. An exact-boundary remaining steps down a full cadence (20 -> 15).
    0 means the deadline itself is the next wake (final edit owns 0:00).
    The epsilon keeps float noise in the quotient (e.g. 0.8/0.2 ->
    4.000000000000001) from re-yielding the current boundary.
    """
    return max(0.0, (math.ceil(remaining / cadence - 1e-9) - 1) * cadence)


# --- one running countdown (the handle) ---

class Countdown:
    """One running transient countdown; the object itself is the handle."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        seconds: float,
        label: str,
        cadence: float = DEFAULT_CADENCE,
        guard: Optional[EditRateGuard] = None,
        icon: object = None,
        done_icon: object = None,
        glyph: object = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._seconds = float(seconds)
        self._label = label
        # Guard against zero/negative cadence: the loop sleeps this long.
        self._cadence = max(float(cadence), 0.001)
        self._guard = guard or EditRateGuard()
        # Free-form appearance (DGN-780b). Stored raw; the render/resolve layer
        # validates and falls back safely on every use, so an unsafe value is
        # inert here.
        self._icon = icon
        self._done_icon = done_icon
        self._glyph = glyph
        self._stop = asyncio.Event()
        self._message_id: Optional[int] = None
        self.task: Optional[asyncio.Task] = None
        self.finished = False
        self.completed = False

    def cancel(self) -> None:
        """Early stop: the loop wakes, does one cleanup edit, and ends."""
        self._stop.set()

    async def _run(self) -> None:
        try:
            await self._run_inner()
        except asyncio.CancelledError:
            raise  # bridge shutdown: no farewell edit against a dying Bot
        except Exception as e:  # noqa: BLE001 - fail-open, never leak upward
            logger.warning("Countdown %r died (fail-open): %s", self._label, e)
        finally:
            self.finished = True

    async def _run_inner(self) -> None:
        try:
            message = await self._bot.send_message(
                chat_id=self._chat_id,
                text=render_countdown(
                    self._label, self._seconds, self._seconds,
                    icon=self._icon, glyph=self._glyph,
                ),
                # DGN-932 mechanism A: first-tick notification is policy-
                # driven (class "countdown", default LOUD = set-start signal,
                # owner lock 2026-08-19 -- supersedes the DGN-594 hardcoded
                # silence); all later ticks are silent in-place edits.
                disable_notification=notify_silent("countdown"),
            )
        except telegram.error.TelegramError as e:
            logger.warning("Countdown send failed (fail-open): %s", e)
            return
        self._message_id = message.message_id

        # Deadline-anchored boundary snap (DGN-780). A fixed cadence nap let
        # the editMessageText round-trip (~0.5-1s) accumulate every cycle
        # (cadence + edit latency), sliding the display off the cadence grid
        # (14/8/3 instead of 15/10/5). Instead, snap each wake to the next
        # cadence boundary re-anchored to the deadline: any latency eaten by
        # an edit just shortens the next sleep, so drift self-corrects and
        # the display walks exact boundaries (30 -> 25 -> 20 -> ... -> 5).
        deadline = time.monotonic() + self._seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.completed = True
                break
            next_target = _next_boundary(remaining, self._cadence)
            wake_at = deadline - next_target
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(0.0, wake_at - time.monotonic()),
                )
                break  # cancelled early -> cleanup edit below
            except asyncio.TimeoutError:
                pass  # boundary reached
            if next_target <= 0:
                self.completed = True
                break  # deadline: 0:00 belongs to the final done edit
            if not self._guard.ready():
                continue  # flood/interval backoff: skip this tick silently
            outcome = await self._guard.edit(
                self._bot,
                self._chat_id,
                self._message_id,
                render_countdown(
                    self._label, next_target, self._seconds,
                    icon=self._icon, glyph=self._glyph,
                ),
            )
            if outcome is EditOutcome.FAILED:
                return  # fail-open: end quietly, session flow unaffected

        # Final cleanup edit. Natural completion (DGN-915): emit the done
        # text WITH a tappable completion affordance button so the owner has
        # a one-tap path forward instead of dead-air. Cancel path: plain done
        # text with no button (the timer was interrupted, no next-step offered).
        # Deliberately not gated on ready(): one closing edit is worth spending;
        # failure still just ends quietly (fail-open discipline unchanged).
        done_text = render_done(self._label, done_icon=self._done_icon)
        if self.completed:
            # DGN-915: affordance emit with fail-soft fallback to plain done.
            # If the keyboard edit fails (Telegram error, message gone), we
            # retry as a plain done edit so the timer ALWAYS terminates cleanly.
            outcome = await self._guard.edit(
                self._bot,
                self._chat_id,
                self._message_id,
                done_text,
                reply_markup=_build_done_keyboard(self._message_id),
            )
            if outcome is EditOutcome.FAILED:
                # Affordance failed: fall back to plain done (still terminates).
                await self._guard.edit(
                    self._bot,
                    self._chat_id,
                    self._message_id,
                    done_text,
                )
        else:
            # Cancelled countdown: plain done, no affordance.
            await self._guard.edit(
                self._bot,
                self._chat_id,
                self._message_id,
                done_text,
            )


def start_countdown(
    bot: Bot,
    chat_id: int,
    seconds: float,
    label: str,
    cadence: float = DEFAULT_CADENCE,
    guard: Optional[EditRateGuard] = None,
    icon: object = None,
    done_icon: object = None,
    glyph: object = None,
) -> Countdown:
    """Create the transient message loop as a background task; returns handle.

    icon/done_icon/glyph are optional free-form overrides (DGN-780b): omit
    them for the default appearance (hourglass/check, config-or-dot bar). An
    unsafe value falls back silently at render time.
    """
    countdown = Countdown(
        bot, chat_id, seconds, label, cadence=cadence, guard=guard,
        icon=icon, done_icon=done_icon, glyph=glyph,
    )
    countdown.task = asyncio.create_task(countdown._run())
    return countdown


# --- cross-process driver (control-file polling) ---

class CountdownDriver:
    """Polls <bot_data_dir>/countdown/*.json and drives Countdown lifecycles."""

    def __init__(
        self,
        bot: Bot,
        control_dir: Path = COUNTDOWN_DIR,
        turn_active: Optional[Callable[[int], bool]] = None,
    ) -> None:
        self._bot = bot
        self._dir = Path(control_dir)
        self._active: Dict[str, Countdown] = {}
        self._warned_bad: Set[str] = set()  # malformed-file log dedup
        # turn_active(user_id) -> True while that user's turn is in flight
        # (DGN-950 seam 2; mirrors DashboardSync). Private owner chat means
        # chat_id == user_id. None (standalone/test use) = no deferral.
        self._turn_active = turn_active

    def _owner_chat_id(self) -> Optional[int]:
        """Owner chat via ownership precedence; None = stay dormant.

        Mirrors DashboardSync._owner_chat_id: private chat means
        chat_id == user_id, and an unclaimed/locked-out bot has no owner to
        count down for.
        """
        mode, owner_id = ownership.resolve_owner(
            config.allowed_user_ids, config.bot_data_dir
        )
        if mode == ownership.MODE_AUTHORITATIVE:
            return config.allowed_user_ids[0]
        if mode == ownership.MODE_OWNER_LOCK:
            return owner_id
        return None

    async def run(self) -> None:
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - driver must never die
                    logger.warning("Countdown tick failed: %s", e)
        finally:
            # Reconnect-loop hygiene (dashboard precedent): never leave
            # countdown tasks holding a dead Bot object. Hard-cancel; the
            # control files stay, so a restart re-arms them from scratch.
            for countdown in self._active.values():
                if countdown.task is not None and not countdown.task.done():
                    countdown.task.cancel()
            self._active.clear()

    async def _tick(self) -> None:
        # 1) Reap finished countdowns; completion removes our own file.
        for cid, countdown in list(self._active.items()):
            if countdown.finished:
                if countdown.completed:
                    self._emit_done(cid)
                del self._active[cid]
                self._remove_file(cid)

        # 2) Scan control files; absent dir = feature dormant.
        try:
            entries = [p for p in self._dir.iterdir() if p.suffix == ".json"]
        except OSError:
            entries = []
        present = {p.stem for p in entries}
        self._warned_bad &= present  # forget warnings for vanished files

        # 3) Delete-cancel: an active countdown whose file vanished.
        for cid, countdown in list(self._active.items()):
            if cid not in present:
                countdown.cancel()  # cleanup edit runs inside its own task
                del self._active[cid]

        # 4) New control files start new countdowns (owner chat only).
        for path in entries:
            cid = path.stem
            if cid in self._active:
                continue
            spec = self._read_spec(path)
            if spec is None:
                if cid not in self._warned_bad:
                    self._warned_bad.add(cid)
                    logger.warning("Countdown control file invalid: %s", path)
                continue  # kept on disk; a partial write parses next tick
            self._warned_bad.discard(cid)
            chat_id = self._owner_chat_id()
            if chat_id is None:
                continue  # unclaimed bot = dormant; retried while file exists
            if self._turn_active is not None and self._turn_active(chat_id):
                # DGN-950 seam 2: defer during the owner's turn, exactly like
                # DashboardSync -- a rest-timer send racing the in-flight
                # model reply inverts the message order (timer lands before
                # the queued next-step answer). The control file stays on
                # disk, so the countdown starts (flushes) on the first tick
                # after the turn ends.
                continue
            seconds, label, cadence, icon, done_icon, glyph = spec
            self._active[cid] = start_countdown(
                self._bot, chat_id, seconds, label, cadence=cadence,
                icon=icon, done_icon=done_icon, glyph=glyph,
            )

    @staticmethod
    def _spec_glyph(raw: object) -> object:
        """Normalize a control-file glyph field to what _resolve_glyphs wants.

        Accepts a preset NAME (str) or a 2-element list/tuple (JSON arrays
        parse to list) as a custom (filled, empty) pair; anything else -> None
        (fall through to config/default). Safety of a custom pair is enforced
        later by _resolve_glyphs, so no charset check is needed here.
        """
        if isinstance(raw, str):
            return raw
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return tuple(raw)
        return None

    def _read_spec(
        self, path: Path
    ) -> Optional[Tuple[int, str, int, object, object, object]]:
        """Parse+validate a control file; None = invalid (skip, do not start).

        Any chat_id key in the file is deliberately ignored (owner-only
        targeting). type() is int, not isinstance: bool is an int subclass
        (dashboard state-load precedent). The optional icon/done_icon/glyph
        appearance fields (DGN-780b) are passed through raw: the render layer
        validates and falls back safely, so a bad value never rejects the
        countdown -- it just yields the default look.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        seconds = data.get("seconds")
        label = data.get("label")
        cadence = data.get("cadence", DEFAULT_CADENCE)
        if type(seconds) is not int or not 0 < seconds <= MAX_SECONDS:
            return None
        if not isinstance(label, str) or not label.strip():
            return None
        if type(cadence) is not int or cadence <= 0:
            return None
        icon = data.get("icon")
        done_icon = data.get("done_icon")
        glyph = self._spec_glyph(data.get("glyph"))
        return seconds, label.strip(), cadence, icon, done_icon, glyph

    def _emit_done(self, cid: str) -> None:
        try:
            (self._dir / f"{cid}.done").write_text(
                json.dumps({"ended_at": datetime.now(timezone.utc).isoformat()}),
                encoding="utf-8",
            )
        except OSError:
            pass  # fail-open: consumer will not see a marker, not fatal

    def _remove_file(self, cid: str) -> None:
        try:
            (self._dir / f"{cid}.json").unlink()
        except OSError:
            pass  # already gone / dir vanished: converged either way
