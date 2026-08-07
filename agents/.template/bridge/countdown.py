"""Transient countdown primitive (DGN-594).

One transient (NON-pinned) message per countdown, edited in place every
`cadence` seconds to show remaining time + a fixed-width progress bar, then
finalized with a done line. Silent-edit channel only (channel 1): milestone
notification pushes (60s/30s/start) are a SEPARATE push path (channel 2) and
are deliberately out of scope here.

In-process API (spec: worklog 2026-08-06-dgn594-countdown-helper-spec):
    handle = start_countdown(bot, chat_id, seconds, label, cadence=10)
    handle.cancel()   # early stop + final cleanup edit

Cross-process trigger (domain agents run in separate processes; mirrors the
dashboard.md file-driven pattern):
    <bot_data_dir>/countdown/<id>.json =
        {"seconds": int, "label": str, "cadence": int (optional, default 10)}
    - file appears  -> CountdownDriver starts a countdown targeting the OWNER
      chat. Any chat_id inside the file is IGNORED: the control file is
      agent-writable, so trusting it would let a writer target arbitrary
      chats (owner resolution mirrors DashboardSync._owner_chat_id).
    - file deleted  -> that countdown is cancelled (cleanup edit).
    - countdown end -> the driver removes its own control file.
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
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import telegram.error
from telegram import Bot

from bridge import messages, ownership
from bridge.config import config
from bridge.edit_guard import EditOutcome, EditRateGuard

logger = logging.getLogger(__name__)

COUNTDOWN_DIR: Path = config.bot_data_dir / "countdown"

POLL_INTERVAL = 3.0     # seconds between control-dir scans (dashboard style)
DEFAULT_CADENCE = 10    # seconds between in-place edits (spec default)
MAX_SECONDS = 24 * 3600  # sanity cap on a control-file countdown

# Fixed-width progress bar. Block chars per spec: U+2593 dark shade =
# elapsed cell, U+2591 light shade = remaining cell.
BAR_CELLS = 10
BAR_FILLED = "▓"  # dark shade block (spec glyph)
BAR_EMPTY = "░"   # light shade block (spec glyph)


# --- rendering ---

def _format_remaining(remaining: float) -> str:
    """m:ss with unpadded minutes (spec example: "2:30").

    Whole seconds are rounded UP so a still-running countdown never displays
    0:00 -- the zero moment belongs to the final done edit.
    """
    total = max(0, math.ceil(remaining))
    return f"{total // 60}:{total % 60:02d}"


def _render_bar(elapsed_frac: float, cells: int = BAR_CELLS) -> str:
    frac = min(1.0, max(0.0, elapsed_frac))
    filled = min(cells, int(frac * cells + 1e-9))
    return BAR_FILLED * filled + BAR_EMPTY * (cells - filled)


def render_countdown(label: str, remaining: float, total: float) -> str:
    """Body line: `{label} {m:ss} left/nam-eum  {bar}` (locale catalog)."""
    elapsed_frac = 1.0 - (remaining / total) if total > 0 else 1.0
    return messages.COUNTDOWN_BODY.format(
        label=label,
        remaining=_format_remaining(remaining),
        bar=_render_bar(elapsed_frac),
    )


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
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._seconds = float(seconds)
        self._label = label
        # Guard against zero/negative cadence: the loop sleeps this long.
        self._cadence = max(float(cadence), 0.001)
        self._guard = guard or EditRateGuard()
        self._stop = asyncio.Event()
        self._message_id: Optional[int] = None
        self.task: Optional[asyncio.Task] = None
        self.finished = False

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
                text=render_countdown(self._label, self._seconds, self._seconds),
                disable_notification=True,
            )
        except telegram.error.TelegramError as e:
            logger.warning("Countdown send failed (fail-open): %s", e)
            return
        self._message_id = message.message_id

        deadline = time.monotonic() + self._seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=min(self._cadence, remaining)
                )
                break  # cancelled early -> cleanup edit below
            except asyncio.TimeoutError:
                pass  # cadence tick elapsed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not self._guard.ready():
                continue  # flood/interval backoff: skip this tick silently
            outcome = await self._guard.edit(
                self._bot,
                self._chat_id,
                self._message_id,
                render_countdown(self._label, remaining, self._seconds),
            )
            if outcome is EditOutcome.FAILED:
                return  # fail-open: end quietly, session flow unaffected

        # Final cleanup edit (natural end and cancel share the done line).
        # Deliberately not gated on ready(): one closing edit is worth
        # spending; a flood/failure here still just ends quietly.
        await self._guard.edit(
            self._bot,
            self._chat_id,
            self._message_id,
            messages.COUNTDOWN_DONE.format(label=self._label),
        )


def start_countdown(
    bot: Bot,
    chat_id: int,
    seconds: float,
    label: str,
    cadence: float = DEFAULT_CADENCE,
    guard: Optional[EditRateGuard] = None,
) -> Countdown:
    """Create the transient message loop as a background task; returns handle."""
    countdown = Countdown(
        bot, chat_id, seconds, label, cadence=cadence, guard=guard
    )
    countdown.task = asyncio.create_task(countdown._run())
    return countdown


# --- cross-process driver (control-file polling) ---

class CountdownDriver:
    """Polls <bot_data_dir>/countdown/*.json and drives Countdown lifecycles."""

    def __init__(self, bot: Bot, control_dir: Path = COUNTDOWN_DIR) -> None:
        self._bot = bot
        self._dir = Path(control_dir)
        self._active: Dict[str, Countdown] = {}
        self._warned_bad: Set[str] = set()  # malformed-file log dedup

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
            seconds, label, cadence = spec
            self._active[cid] = start_countdown(
                self._bot, chat_id, seconds, label, cadence=cadence
            )

    def _read_spec(self, path: Path) -> Optional[Tuple[int, str, int]]:
        """Parse+validate a control file; None = invalid (skip, do not start).

        Any chat_id key in the file is deliberately ignored (owner-only
        targeting). type() is int, not isinstance: bool is an int subclass
        (dashboard state-load precedent).
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
        return seconds, label.strip(), cadence

    def _remove_file(self, cid: str) -> None:
        try:
            (self._dir / f"{cid}.json").unlink()
        except OSError:
            pass  # already gone / dir vanished: converged either way
