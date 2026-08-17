"""Telegram bot: handlers, commands, callbacks, queue, send logic.

PTB v20 Application with a manual lifecycle that restarts polling on network
blips (launchd owns crash-restart). Per-user serialized queue (max 3 in-flight,
/stop priority), allowlist + stale-message drop, marker-aware sending.
"""

import asyncio
import html
import json
import logging
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import telegram.error
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyParameters,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from bridge import btw as btw_module
from bridge import fastpath, heartbeat, messages, model_state
from bridge.i18n import skill_display_name
from bridge.config import (
    AUTO_RESUME,
    AUTO_RESUME_MAX,
    BRIDGE_INFLIGHT_DEBOUNCE_S,
    BRIDGE_INFLIGHT_INTERRUPT_NOTICE,
    PROCESS_TIMEOUT,
    REPLY_LINK_ENABLED,
    REPLY_LINK_LATENCY_S,
    config,
)
from bridge import ownership
from bridge.formatting import (
    IMAGE_EXTS,
    balance_telegram_html,
    code_segment_html,
    html_to_plain_text,
    markdown_to_telegram_html,
    rebalance_html_chunks,
    resolve_send_paths,
    sanitize_message_for_telegram,
    split_into_segments,
    split_paths_by_scope,
    split_text,
    strip_display_markers,
    strip_link_preview_marker,
    strip_send_markers,
    strip_toolcall_markup,
)
from bridge.health import PollingConflict, PollingRestart, polling_watchdog
from bridge.image_send import (
    classify_send_error,
    downscale_for_photo,
    photo_send_verdict,
    probe_image_dimensions,
)
from bridge.options import (
    OPTIONS_MARKER,
    build_option_keyboard,
    extract_options,
    resolve_choice,
    strip_consumed_options,
    strip_options_marker,
)
from bridge.permissions import (
    ALLOW_OUTSIDE_ONCE_TOKEN,
    DENY_OUTSIDE_TOKEN,
    extract_outside_paths,
    extract_protected_paths,
    outside_path_deny_message,
)
from bridge.sdk_bridge import ChatResponse, PROJECT_ROOT, sdk_bridge
from bridge.session import session_manager
from bridge.dashboard import DashboardSync
from bridge.countdown import CDN_DONE_PREFIX, CountdownDriver

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

logger = logging.getLogger(__name__)

# DGN-919: single source of truth for the slash-command menu.
# Order here drives BOTH the BotCommand popup menu AND the /help numbered list.
# Hidden commands must NOT appear here.  Accurate per-command status:
#   /start     -- CommandHandler registered in _setup_handlers, off-menu.
#   /usageretry -- CommandHandler registered in _setup_handlers, off-menu.
#   /claim     -- NO CommandHandler; intercepted by _check_access via the
#                 MODE_CLAIM ownership path (catch-all _handle_claim_attempt).
#                 A post-ownership "/claim x" falls through to the catch-all
#                 MessageHandler and is forwarded to the model as a slash cmd.
#   kill       -- no handler at all; must not appear anywhere.
COMMAND_MENU_SPEC = [
    ("new",      lambda: messages.CMD_DESC_NEW),
    ("stop",     lambda: messages.CMD_DESC_STOP),
    ("btw",      lambda: messages.CMD_DESC_BTW),
    ("usage",    lambda: messages.CMD_DESC_USAGE),
    ("queue",    lambda: messages.CMD_DESC_QUEUE),
    ("model",    lambda: messages.CMD_DESC_MODEL),
    ("skills",   lambda: messages.CMD_DESC_SKILLS),
    ("resume",   lambda: messages.CMD_DESC_RESUME),
    ("authsync", lambda: messages.CMD_DESC_AUTHSYNC),
    ("help",     lambda: messages.CMD_DESC_HELP),
]

STALE_MESSAGE_SECONDS = 20 * 60
# DGN-616: cap on concurrent turns per user for the CONTROL path only
# (/stop, /new, /model, opt: and resume: callbacks route through
# _enqueue_user_task). Regular messages (text/voice/photo/document) are
# serialized to exactly ONE in-flight turn via the coalescing path; keeping
# this at 3 lets a control command run immediately even while that turn runs.
MAX_INFLIGHT_MESSAGES = 3
# DGN-616: memory-safety cap on the per-user coalescing buffer. Normal usage
# never approaches this; only a runaway flood past it surfaces a notice.
COALESCE_MAX = 50
# DGN-801 (grill M3): retry backoff for the fast-path exit-0 push. State is
# already committed when this send runs, so the push must be GUARANTEED-ish:
# len+1 total attempts, then a death-notice -- never a model fallback.
FASTPATH_PUSH_RETRY_DELAYS = (1.0, 3.0)
STALE_AUDIO_SECONDS = 24 * 60 * 60
MIN_UPTIME = 30
MAX_RAPID_CRASHES = 5
# Runtime getUpdates Conflict handling (token contention, e.g. canary cutover).
CONFLICT_BACKOFF_BASE = 5  # seconds, base backoff before re-initializing polling
CONFLICT_BACKOFF_MAX = 30  # seconds, cap for incremental backoff
CONFLICT_SUSTAINED_SECONDS = 300  # log an error if conflict persists past this
# DGN-140: in-process getUpdates heartbeat stall detection (layer 1; layer 2 is
# the external bridge/watchdog.sh). The heartbeat beats on every getUpdates
# round trip (success or transport timeout); silence past the threshold means
# the polling task itself is dead while the process lives (zombie polling).
HEARTBEAT_STALL_SECONDS = 120  # no poll beat for this long -> restart polling
STALL_STREAK_RESET_SECONDS = 600  # healthy gap that resets the stall-restart streak
STALL_STREAK_SUSPECT = 3  # more consecutive stall restarts than this -> CRITICAL log
# Telegram delivers an album as N separate photo updates sharing one
# media_group_id. Buffer same-group photos for this debounce window, then flush
# them as a single task. 1.5s >> real album inter-arrival (~100ms), big margin.
MEDIA_GROUP_DEBOUNCE = 1.5
# An out-of-root / protected-path one-time approval expires this many seconds
# after the deny prompt was shown, so a stale grant can never authorize a much
# later call (F7 hardening).
OUTSIDE_APPROVAL_TTL = 600  # 10 minutes
# DGN-376: auto link previews are OFF on every outbound text send by default.
# A reply opts back in with a standalone "link_preview::" line (stripped before
# sending), which restores Telegram's default preview for that reply's text.
LINK_PREVIEW_OFF = LinkPreviewOptions(is_disabled=True)

_MODEL_LABELS = {
    "sonnet": "Claude Sonnet",
    "opus": "Claude Opus",
    "haiku": "Claude Haiku",
    "fable": "Claude Fable",
}

# DGN-192: performance ordering for the /model picker -- strongest first.
# Unknown names sort last (rank 99 in the sort key) alphabetically.
_MODEL_PERF_RANK = {"fable": 0, "opus": 1, "sonnet": 2, "haiku": 3}


def _model_whitelist() -> List[str]:
    """Env-driven allowed model names (BRIDGE_MODELS, comma-separated).

    Lets time-limited models (e.g. fable) be enabled without a code edit. Falls
    back to sonnet only. Full 'claude-*' ids are always accepted separately by
    the caller, so they need not appear here.
    """
    raw = os.getenv("BRIDGE_MODELS", "sonnet")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or ["sonnet"]


# Backwards-compatible list of (name, label) for the inline model picker.
MODELS = [(n, _MODEL_LABELS.get(n, n)) for n in _model_whitelist()]

# Hardcoded last-rung fallback when nothing else in the chain yields a model.
DEFAULT_MODEL = "sonnet"


def _known_models() -> List[str]:
    """Short names accepted for persistence/resolution: the env whitelist plus
    the built-in labels (full 'claude-*' ids are accepted by the validator)."""
    return list(dict.fromkeys([*_model_whitelist(), *_MODEL_LABELS.keys()]))


def _fmt_error(e: Exception) -> str:
    """Friendly string for user-visible error messages.

    telegram.error.TimedOut is a transient network blip -- replace the raw
    'Timed out' string with a short friendly message instead of leaking the
    library exception text.
    """
    if isinstance(e, telegram.error.TimedOut):
        return messages.NETWORK_TIMEOUT
    return str(e)


class TelegramBot:
    def __init__(self) -> None:
        self.application: Optional[Application] = None
        self._conflict_event: Optional[asyncio.Event] = None
        self._runtime_active_sessions: set[int] = set()
        self._user_run_tasks: Dict[int, set[asyncio.Task]] = {}
        self._user_queue_locks: Dict[int, asyncio.Lock] = {}
        self._active_tasks: Dict[int, asyncio.Task] = {}
        # DGN-616: per-user coalescing buffer. Messages land here in arrival
        # order and the in-flight turn's done-callback drains the buffer,
        # merging everything into ONE combined follow-up turn. Control commands
        # never touch this buffer -- they run immediately via
        # _enqueue_user_task. Each entry: (text, arrival_ts, update).
        # DGN-911: this buffer is no longer the DEFAULT in-flight path -- it is
        # fed by the explicit /queue command and by the debounce-expiry hand-off
        # below (and it remains the fail-safe landing zone for every path).
        self._user_pending_texts: Dict[int, List[tuple]] = {}
        # DGN-911: default in-flight policy = debounce-interrupt. A REGULAR
        # message (text/voice/photo/document) arriving while a turn is in
        # flight lands here and (re)arms the per-user debounce timer. On
        # expiry the buffer moves into _user_pending_texts, the in-flight turn
        # is soft-interrupted (DGN-581), and the finishing turn's drain path
        # dispatches the merged messages as ONE new turn. Same entry shape as
        # _user_pending_texts: (text, arrival_ts, update).
        self._debounce_texts: Dict[int, List[tuple]] = {}
        self._debounce_timers: Dict[int, asyncio.Task] = {}
        self._audio_dir = config.bot_data_dir / "audio"
        # Inbound photos land here: ephemeral runtime buffer, pruned after 7 days
        # by the cleanup cron. photo input restored.
        self._image_dir = config.bot_data_dir / "images"
        # media_group_id -> buffered album state, flushed as one task.
        self._media_groups: Dict[str, dict] = {}
        self._media_group_lock = asyncio.Lock()
        # DGN-555: per-chat newest incoming user message_id, recorded on every
        # accepted inbound message. The reply-link policy compares it against a
        # turn's triggering message_id at send time to detect interleave.
        self._last_incoming_mid: Dict[int, int] = {}
        # Inbound documents land here: files worth keeping (PDF, code, data).
        # Kept indefinitely (not pruned by cleanup).
        self._inbox_dir = PROJECT_ROOT / "files" / "inbox"
        from bridge.voice import AudioProcessor, build_transcriber

        self._audio_processor = AudioProcessor(ffmpeg_path=config.ffmpeg_path)
        self._build_transcriber = build_transcriber
        self._transcriber = None
        # DGN-140: consecutive heartbeat-stall restart streak (loud-log guard).
        self._stall_restart_count = 0
        self._last_stall_restart: Optional[float] = None
        # DGN-902: /btw fork table -- ephemeral side conversations.
        self._btw_forks = btw_module.BtwForkManager()
        # DGN-922 FIX 4: btw fork tasks are tracked SEPARATELY from
        # _user_run_tasks so they do NOT participate in the in-flight /
        # debounce decision for regular messages.  A normal message arriving
        # while only a fork is running must dispatch immediately (5s reaction
        # contract); tracking the fork in _user_run_tasks was causing it to
        # block the main conversation for up to BTW_TURN_TIMEOUT=120s.
        self._btw_fork_tasks: Dict[int, set] = {}

    # --- lifecycle ---

    def build(self) -> None:
        # Explicit HTTP timeouts. PTB defaults (read_timeout=5s) are SHORTER than
        # the long-poll timeout, so every getUpdates would raise TimedOut and the
        # bot never finishes starting. The get_updates request needs a read_timeout
        # comfortably longer than the long-poll interval.
        request = HTTPXRequest(
            connection_pool_size=8,
            connect_timeout=5.0,
            read_timeout=10.0,
            write_timeout=10.0,
            pool_timeout=3.0,
        )
        # DGN-140: heartbeat-wrapped transport for getUpdates only, so the
        # stall detector and the external watchdog see real polling liveness.
        get_updates_request = heartbeat.HeartbeatHTTPXRequest(
            connection_pool_size=4,
            connect_timeout=5.0,
            read_timeout=35.0,
            pool_timeout=5.0,
        )
        self.application = (
            Application.builder()
            .token(config.telegram_bot_token)
            .concurrent_updates(True)
            .request(request)
            .get_updates_request(get_updates_request)
            .build()
        )
        self._setup_handlers()
        self.application.add_error_handler(self._error_handler)

    def run(self) -> None:
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        rapid_crash_count = 0
        # First wall-clock time of an ongoing Conflict streak; reset on recovery.
        conflict_since: Optional[float] = None
        conflict_backoff = CONFLICT_BACKOFF_BASE
        # DGN-140 MAJOR-1: drop pending updates ONLY on the very first polling
        # start of this process. In-process re-inits (PollingRestart/Conflict)
        # must NOT drop -- messages sent during the gap would be lost silently.
        first_boot = True
        while not stop_event.is_set():
            if not self.application:
                self.build()
            self._conflict_event = asyncio.Event()
            start_time = time.time()
            try:
                await self.application.initialize()
            except telegram.error.InvalidToken:
                raise SystemExit("Invalid Telegram Bot Token. Check TELEGRAM_BOT_TOKEN.")
            except telegram.error.Conflict:
                # Conflict during init = transient token contention, not a fatal
                # misconfiguration. Back off and retry rather than exiting.
                logger.warning("Conflict during init (token contention), backing off")
                await self._graceful_shutdown(force=True)
                await asyncio.sleep(conflict_backoff)
                conflict_backoff = min(CONFLICT_BACKOFF_MAX, conflict_backoff * 2)
                continue
            except telegram.error.NetworkError as e:
                logger.warning("Network error during init: %s, retrying", e)
                await self._graceful_shutdown(force=True)
                await asyncio.sleep(5)
                continue

            await self._on_ready()
            watchdog_task = None
            inbox_task = None
            dashboard_task = None
            countdown_task = None
            try:
                await self.application.start()
                await self.application.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=first_boot,
                    # DGN-140: never give up re-establishing getUpdates after
                    # network loss (e.g. laptop sleep/wake).
                    bootstrap_retries=-1,
                    error_callback=self._on_polling_error,
                )
                first_boot = False
                logger.info("Bot is running")
                heartbeat.touch()
                watchdog_task = asyncio.create_task(
                    polling_watchdog(
                        self.application,
                        stop_event,
                        on_recovery=self._notify_outage_recovered,
                    )
                )
                # DGN-217: session-inbox watcher -- external crons drop a
                # summary file, we inject it as a background turn into the
                # owner's live session. Same lifecycle as the watchdog task.
                inbox_task = asyncio.create_task(self._session_inbox_loop())
                # Pinned live-dashboard sync: polls dashboard.md and edits
                # the owner's pinned message in place. Same lifecycle as the
                # watchdog and inbox tasks (cancelled in the same finally).
                dashboard_task = asyncio.create_task(
                    DashboardSync(
                        bot=self.application.bot,
                        turn_active=self._user_turn_active,
                    ).run()
                )
                # Transient countdown driver (DGN-594): polls the countdown
                # control dir and edits transient owner-chat messages in
                # place. Same lifecycle as the tasks above (cancelled in the
                # same finally; its own finally reaps live countdown tasks).
                countdown_task = asyncio.create_task(
                    CountdownDriver(bot=self.application.bot).run()
                )
                await self._wait_for_polling_exit(stop_event)
            except PollingConflict:
                # PTB swallows Conflict in its retry loop (bot stays alive but
                # receives no updates). We surface it here, back off, and cleanly
                # re-initialize polling. This does NOT count as a rapid crash:
                # contention is expected during cutover.
                uptime = time.time() - start_time
                if uptime >= MIN_UPTIME:
                    # Polling ran healthy for a while before this Conflict, so
                    # treat it as a fresh streak (prior contention had recovered).
                    conflict_since = None
                    conflict_backoff = CONFLICT_BACKOFF_BASE
                if conflict_since is None:
                    conflict_since = time.time()
                elapsed = time.time() - conflict_since
                if elapsed >= CONFLICT_SUSTAINED_SECONDS:
                    logger.error(
                        "getUpdates Conflict sustained for %ds; another instance "
                        "still holds this bot token", int(elapsed)
                    )
                else:
                    logger.warning(
                        "getUpdates Conflict, backing off %ds before restart",
                        conflict_backoff,
                    )
                await self._graceful_shutdown(force=True)
                await asyncio.sleep(conflict_backoff)
                conflict_backoff = min(CONFLICT_BACKOFF_MAX, conflict_backoff * 2)
                continue
            except PollingRestart:
                conflict_since = None
                conflict_backoff = CONFLICT_BACKOFF_BASE
                uptime = time.time() - start_time
                if uptime < MIN_UPTIME:
                    rapid_crash_count += 1
                    if rapid_crash_count >= MAX_RAPID_CRASHES:
                        raise SystemExit(f"Polling restarted {MAX_RAPID_CRASHES} times rapidly.")
                else:
                    rapid_crash_count = 0
                logger.warning("Polling restart triggered")
                continue
            except telegram.error.NetworkError as e:
                logger.warning("Network error at runtime: %s", e)
                await self._graceful_shutdown(force=True)
                continue
            else:
                # Clean exit of the wait loop (stop requested): reset conflict state.
                conflict_since = None
                conflict_backoff = CONFLICT_BACKOFF_BASE
            finally:
                if watchdog_task and not watchdog_task.done():
                    watchdog_task.cancel()
                    try:
                        await watchdog_task
                    except (asyncio.CancelledError, PollingRestart):
                        pass
                if inbox_task and not inbox_task.done():
                    inbox_task.cancel()
                    try:
                        await inbox_task
                    except asyncio.CancelledError:
                        pass
                if dashboard_task and not dashboard_task.done():
                    dashboard_task.cancel()
                    try:
                        await dashboard_task
                    except asyncio.CancelledError:
                        pass
                if countdown_task and not countdown_task.done():
                    countdown_task.cancel()
                    try:
                        await countdown_task
                    except asyncio.CancelledError:
                        pass
                await self._graceful_shutdown()
        logger.info("Bot stopped")

    def _on_polling_error(self, error: telegram.error.TelegramError) -> None:
        """PTB updater error_callback (must be sync, must not raise).

        PTB's network retry loop catches Conflict and retries indefinitely
        without stopping the updater, so the run loop never notices. We flag it
        via an event so _wait_for_polling_exit can raise PollingConflict and
        trigger a clean backoff+restart instead of silently going zombie.

        DGN-140 MINOR: RetryAfter (flood wait) means Telegram answered -- the
        polling loop is alive but told to back off. Beat the heartbeat so a
        long flood-wait cannot misfire the stall detector.
        """
        if isinstance(error, telegram.error.RetryAfter):
            heartbeat.touch()
        if isinstance(error, telegram.error.Conflict) and self._conflict_event:
            self._conflict_event.set()

    async def _wait_for_polling_exit(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if self._conflict_event and self._conflict_event.is_set():
                raise PollingConflict()
            if (
                self.application
                and self.application.updater
                and not self.application.updater.running
            ):
                logger.warning("Polling exited unexpectedly, restarting")
                raise PollingRestart()
            if heartbeat.stalled(HEARTBEAT_STALL_SECONDS):
                self._note_stall_restart()
                logger.warning(
                    "getUpdates heartbeat stalled >%ds (polling task presumed "
                    "dead), restarting polling",
                    HEARTBEAT_STALL_SECONDS,
                )
                raise PollingRestart()
            await asyncio.sleep(1)

    def _note_stall_restart(self) -> None:
        """Track consecutive heartbeat-stall restarts (DGN-140 MINOR).

        A stall restart should be rare; a tight streak of them means the
        heartbeat wiring itself is suspect (e.g. beats never arriving even
        though polling works). Loud CRITICAL log only -- no behavior change.
        A healthy gap (> STALL_STREAK_RESET_SECONDS) resets the streak.
        """
        now = time.monotonic()
        if (
            self._last_stall_restart is not None
            and (now - self._last_stall_restart) > STALL_STREAK_RESET_SECONDS
        ):
            self._stall_restart_count = 0
        self._stall_restart_count += 1
        self._last_stall_restart = now
        if self._stall_restart_count > STALL_STREAK_SUSPECT:
            logger.critical(
                "heartbeat wiring suspect: %d consecutive stall restarts",
                self._stall_restart_count,
            )

    async def _session_inbox_loop(self) -> None:
        """DGN-217: poll <bot_data_dir>/session-inbox/ and inject each file as
        a background turn into the owner's live session.

        Contract with writers (cron scripts): write to a temp name, then
        mv/rename to *.md in this dir -- rename is atomic, so a half-written
        file is never picked up. One file per tick keeps injected turns
        serialized. Injection is refused (file kept, retried next tick) while
        the owner has a turn in flight. If no live stream exists yet (fresh
        restart at a quiet hour, no owner message to create one), it is
        bootstrapped here first (DGN-399) so a queued turn resumes on its own.
        Delivery of the turn's OUTPUT rides the existing no-pending proactive
        path; a turn ending in bare NO_PUSH reaches the session but not the
        owner chat.
        """
        inbox_dir = config.bot_data_dir / "session-inbox"
        poll_secs = 20
        while True:
            await asyncio.sleep(poll_secs)
            try:
                if not inbox_dir.is_dir():
                    continue
                files = sorted(p for p in inbox_dir.glob("*.md") if p.is_file())
                if not files:
                    continue
                owner_ids = config.allowed_user_ids
                if not owner_ids:
                    continue  # claim mode, owner unknown yet
                owner_id = owner_ids[0]
                if self._user_turn_active(owner_id):
                    continue  # defer: never race a live turn
                path = files[0]
                try:
                    text = path.read_text(encoding="utf-8").strip()
                except Exception as e:
                    logger.error("session-inbox read failed for %s: %s", path, e)
                    continue
                if not text:
                    path.unlink(missing_ok=True)
                    continue
                # DGN-399: bootstrap the owner's live stream if none exists yet
                # (fresh restart at a quiet hour, no owner message to create it).
                # Idempotent -- a no-op refresh when a stream already exists.
                # In a private chat the chat_id equals the owner's user id.
                session = await session_manager.get_session(owner_id)
                await sdk_bridge.ensure_owner_stream(
                    owner_id,
                    session.get("model"),
                    owner_id,
                    self._proactive_push,
                )
                ok = await sdk_bridge.inject_background_turn(owner_id, text)
                if ok:
                    path.unlink(missing_ok=True)
                    logger.info("session-inbox injected: %s", path.name)
                # not ok -> stream missing or busy; keep the file, retry next tick
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # The watcher must never die silently on a transient error.
                logger.error("session-inbox loop error: %s", e)

    async def _graceful_shutdown(self, force: bool = False) -> None:
        if not self.application:
            return
        try:
            if not force:
                await asyncio.wait_for(self._do_graceful_stop(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            logger.warning("Graceful shutdown issue, forcing cleanup")
        finally:
            self.application = None

    async def _do_graceful_stop(self) -> None:
        if self.application.updater and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()

    async def _on_ready(self) -> None:
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._announce_ownership_mode()
        try:
            await self._audio_processor.cleanup_stale_audio_files(
                self._audio_dir, STALE_AUDIO_SECONDS
            )
        except Exception:
            pass
        await self._set_bot_commands()

    def _announce_ownership_mode(self) -> None:
        """Log the effective ownership mode; in claim mode, surface the claim code.

        Born-locked: an empty allowed_user_ids with no owner.lock does NOT allow
        all -- it enters claim mode and prints a one-time code the first user
        sends back as '/claim <code>' to take ownership.
        """
        mode, owner_id = ownership.resolve_owner(
            config.allowed_user_ids, config.bot_data_dir
        )
        if mode == ownership.MODE_AUTHORITATIVE:
            logger.info("Ownership: authoritative allowed_user_ids (claim mode off)")
        elif mode == ownership.MODE_OWNER_LOCK:
            logger.info("Ownership: owner.lock -> sole owner id %s", owner_id)
        elif mode == ownership.MODE_LOCKED_OUT:
            logger.warning(messages.OWNER_LOCK_MISSING_LOG)
        else:  # MODE_CLAIM
            code = ownership.ensure_claim_code(config.bot_data_dir)
            line = messages.CLAIM_CODE_LOG.format(code=code)
            # Print prominently to stdout AND log so it is visible however the
            # bot was launched (foreground console or launchd log file).
            print(line, flush=True)
            logger.warning(line)

    def _setup_handlers(self) -> None:
        app = self.application
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("new", self._cmd_new))
        app.add_handler(CommandHandler("model", self._cmd_model))
        app.add_handler(CommandHandler("resume", self._cmd_resume))
        app.add_handler(CommandHandler("stop", self._cmd_stop))
        app.add_handler(CommandHandler("queue", self._cmd_queue))
        app.add_handler(CommandHandler("skills", self._cmd_skills))
        app.add_handler(CommandHandler("usage", self._cmd_usage))
        # DGN-841 A: STALE-gate-free slash route for the usage-defer manual
        # retry (buttons on aged messages die at the 20-min gate). Group 0
        # registration also keeps the catch-all skill forwarder below from
        # swallowing it (it skips names owned by group-0 CommandHandlers).
        app.add_handler(CommandHandler("usageretry", self._cmd_usage_retry))
        app.add_handler(CommandHandler("authsync", self._cmd_authsync))
        app.add_handler(CommandHandler("btw", self._cmd_btw))
        app.add_handler(CommandHandler("help", self._cmd_help))
        # Catch-all: any other /foo is forwarded to the agent as a slash command
        # (the dedicated /skill and /command handlers were dropped -- this already
        # covers them).
        app.add_handler(MessageHandler(filters.COMMAND, self._handle_skill_command), group=1)
        app.add_handler(MessageHandler(filters.VOICE, self._handle_voice_message), group=2)
        app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo_message), group=2)
        app.add_handler(
            MessageHandler(filters.Document.ALL, self._handle_document_message), group=2
        )
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message),
            group=2,
        )
        app.add_handler(CallbackQueryHandler(self._handle_callback))

    async def _set_bot_commands(self) -> None:
        # DGN-919: build the BotCommand list from COMMAND_MENU_SPEC so order
        # and copy are always in sync with the /help output.
        commands = [BotCommand(cmd, desc_fn()) for cmd, desc_fn in COMMAND_MENU_SPEC]
        try:
            # Self-heal: clear stale scoped menus (e.g. left by a previous
            # bot setup). Scoped entries override the default scope and
            # would shadow the menu registered below.
            for scope in (
                BotCommandScopeAllPrivateChats(),
                BotCommandScopeAllGroupChats(),
                BotCommandScopeAllChatAdministrators(),
            ):
                try:
                    await self.application.bot.delete_my_commands(scope=scope)
                except Exception as e:
                    logger.warning(
                        "Failed to clear scoped commands (%s): %s", scope.type, e
                    )
            await self.application.bot.set_my_commands(commands)
        except Exception as e:
            logger.warning("Failed to set bot commands: %s", e)

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # PTB-level catch for exceptions in a handler BODY before the turn is
        # enqueued (e.g. access check / media-group scheduling crash). DGN-163:
        # emit the bounded turn-death notice instead of a raw traceback so a
        # consumed update still yields one user-visible, no-internals message.
        logger.error("Unhandled exception:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    update.effective_chat.id,
                    messages.TURN_FAILED,
                )
            except Exception:
                pass

    # --- access control ---

    async def _check_access(self, update: Update, *, skip_stale: bool = False) -> bool:
        # DGN-922 FIX 2: skip_stale=True exempts the caller from the 20-min
        # stale-message drop.  Used exclusively for cdn:done: callbacks so a
        # countdown completion affordance button stays tappable regardless of
        # how long the countdown ran (MAX_SECONDS = 24 h).  All other callers
        # pass the default skip_stale=False and see the original gate unchanged.
        if not skip_stale:
            msg = update.message or (update.callback_query and update.callback_query.message)
            if msg and msg.date:
                age = (datetime.now(timezone.utc) - msg.date).total_seconds()
                if age > STALE_MESSAGE_SECONDS:
                    return False
        user = update.effective_user
        if not user:
            return False

        mode, owner_id = ownership.resolve_owner(
            config.allowed_user_ids, config.bot_data_dir
        )

        if mode == ownership.MODE_CLAIM:
            # Born-locked: the ONLY accepted action is a correct '/claim <code>'
            # text message. Everything else is silently dropped (no reply at all,
            # no NO_PERMISSION) so we never leak that the bot exists/is claimable.
            return await self._handle_claim_attempt(update, user.id)

        if mode == ownership.MODE_LOCKED_OUT:
            # Claimed before, owner.lock gone: deny all, do NOT reopen claim mode.
            # Silent drop (no reply) -- an anomalous recovery state, not normal use.
            logger.warning(messages.OWNER_LOCK_MISSING_LOG)
            return False

        if mode == ownership.MODE_AUTHORITATIVE:
            allowed = user.id in config.allowed_user_ids
        else:  # MODE_OWNER_LOCK
            allowed = user.id == owner_id

        if not allowed:
            if update.message:
                await update.message.reply_text(messages.NO_PERMISSION)
            elif update.callback_query:
                await update.callback_query.answer(
                    messages.NO_PERMISSION_CALLBACK, show_alert=True
                )
            return False
        self._note_incoming_message(update.message)
        return True

    def _note_incoming_message(self, message) -> None:
        """DGN-555: record the newest accepted incoming user message_id per chat.

        Read by _reply_link_id at send time: a recorded id newer than a turn's
        triggering message means user messages interleaved during the turn.
        Callback queries never reach here (update.message is None for them), so
        button taps do not count as incoming user messages.
        """
        if message is None:
            return
        mid = getattr(message, "message_id", None)
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if not isinstance(mid, int) or not isinstance(chat_id, int):
            return
        prev = self._last_incoming_mid.get(chat_id)
        if prev is None or mid > prev:
            self._last_incoming_mid[chat_id] = mid

    async def _handle_claim_attempt(self, update: Update, user_id: int) -> bool:
        """Claim-mode interception. Returns False in every case so NO inbound
        message in claim mode ever reaches normal processing / the model.

        A correct '/claim <code>' text message makes the sender the owner and
        gets the one success reply. Every other message (wrong code, /start,
        random text, callbacks) is silently dropped with no reply.
        """
        text = update.message.text if update.message else None
        if text and ownership.verify_and_claim(text, user_id, config.bot_data_dir):
            try:
                await update.message.reply_text(messages.CLAIM_SUCCESS)
            except Exception as e:
                logger.warning("claim success reply failed for user %s: %s", user_id, e)
        return False

    # --- session helpers ---

    async def _save_session_id(self, user_id: int, response: ChatResponse) -> None:
        if response.session_id:
            await session_manager.update_session(
                user_id, {"session_id": response.session_id}
            )
            self._runtime_active_sessions.add(user_id)

    def _effective_session_id(self, user_id: int, session: dict) -> Optional[str]:
        """Cross-process guard: persisted id only honored if active this run."""
        session_id = session.get("session_id")
        if not session_id:
            return None
        if user_id not in self._runtime_active_sessions:
            return None
        return session_id

    # --- permission callback (wired into SDK) ---

    async def _permission_callback(
        self, chat_id: int, user_id: int, tool_name: str, tool_input: Any
    ):
        if tool_name == "AskUserQuestion":
            return PermissionResultDeny(message=messages.ASK_USER_QUESTION_DENY)

        return await self._guard_paths(user_id, tool_name, tool_input)

    async def _guard_paths(self, user_id: int, tool_name: str, tool_input: Any):
        """Shared path guard: protected-zone check runs BEFORE the inside-root
        shortcut, then the out-of-root check. Both funnel through the same
        one-time confirm bound to the specific resolved paths (F2/F7).
        """
        # PROTECTED ZONE FIRST: the secrets dir / any .env lives inside
        # PROJECT_ROOT, so it must be caught before the inside-root pass-through.
        protected = extract_protected_paths(tool_name, tool_input, PROJECT_ROOT)
        outside = extract_outside_paths(
            tool_name, tool_input, PROJECT_ROOT, config.extra_allowed_roots
        )
        guarded = list(dict.fromkeys(protected + outside))  # union, order-stable
        if not guarded:
            return PermissionResultAllow()
        if await self._consume_outside_approval_once(user_id, guarded):
            return PermissionResultAllow()
        session = await session_manager.get_session(user_id)
        session["pending_outside_paths"] = guarded[:5]
        session["pending_outside_at"] = time.time()
        await session_manager.update_session(user_id, session)
        return PermissionResultDeny(message=outside_path_deny_message(guarded))

    async def _consume_outside_approval_once(
        self, user_id: int, requested_paths: List[str]
    ) -> bool:
        """Consume a one-time grant ONLY if it authorizes exactly these paths.

        F7 hardening: the grant is bound to the specific paths shown in the deny
        prompt. It is honored only when every path in this call is a subset of
        the approved set, and only within OUTSIDE_APPROVAL_TTL. Otherwise the
        grant is left untouched (this call is denied) so an approval for path A
        can never silently authorize a later call for path B.
        """
        session = await session_manager.get_session(user_id)
        if not session.get("outside_path_approved_once"):
            return False
        granted_at = session.get("outside_path_approved_at", 0)
        approved = set(session.get("outside_path_approved_paths") or [])
        expired = (time.time() - granted_at) > OUTSIDE_APPROVAL_TTL
        subset = bool(requested_paths) and set(requested_paths).issubset(approved)
        if expired or not subset:
            if expired:
                # Clear a stale grant so it cannot be reused later.
                session["outside_path_approved_once"] = False
                session.pop("outside_path_approved_paths", None)
                session.pop("outside_path_approved_at", None)
                await session_manager.update_session(user_id, session)
            return False
        session["outside_path_approved_once"] = False
        session.pop("outside_path_approved_paths", None)
        session.pop("outside_path_approved_at", None)
        session.pop("pending_outside_paths", None)
        session.pop("pending_outside_at", None)
        await session_manager.update_session(user_id, session)
        return True

    async def _maybe_capture_outside_approval(self, user_id: int, text: str) -> bool:
        """Consume an outside-path approval/deny reply while one is pending.

        Returns True when a LIVE (non-expired) outside-approval prompt was
        pending for this user at message time -- whether or not this message was
        an allow/deny decision token. The caller uses that to seal DGN-801 B4
        double-consumption: while an outside approval is being captured, a bare
        numeric token ("1") that also grants the approval must NOT additionally
        be parsed as a fast-path set-result (Skull kept bare-numeric a valid
        fast-path input, so the ambiguity is resolved on the approval side; it
        exists only while a prompt pends, and only the bridge knows that state).
        Returns False when no prompt is pending, or the prompt had expired (both
        leave fast-path free to fire).
        """
        session = await session_manager.get_session(user_id)
        pending = session.get("pending_outside_paths")
        if not pending:
            return False
        # Expire a stale deny prompt: an approval reply that arrives after the
        # TTL no longer grants anything -- and no longer suppresses fast-path.
        pending_at = session.get("pending_outside_at", 0)
        if (time.time() - pending_at) > OUTSIDE_APPROVAL_TTL:
            session.pop("pending_outside_paths", None)
            session.pop("pending_outside_at", None)
            await session_manager.update_session(user_id, session)
            return False
        normalized = text.strip().lower()
        allow = ALLOW_OUTSIDE_ONCE_TOKEN.lower() in normalized or normalized in {
            "1", "allow", "yes", "y"
        }
        deny = DENY_OUTSIDE_TOKEN.lower() in normalized or normalized in {
            "2", "deny", "no", "n"
        }
        if allow:
            session["outside_path_approved_once"] = True
            # Bind the grant to exactly the paths that were shown (F7).
            session["outside_path_approved_paths"] = list(pending)
            session["outside_path_approved_at"] = time.time()
            session.pop("pending_outside_paths", None)
            session.pop("pending_outside_at", None)
            await session_manager.update_session(user_id, session)
            return True
        elif deny:
            session["outside_path_approved_once"] = False
            session.pop("outside_path_approved_paths", None)
            session.pop("outside_path_approved_at", None)
            session.pop("pending_outside_paths", None)
            session.pop("pending_outside_at", None)
            await session_manager.update_session(user_id, session)
            return True
        # A pending prompt exists but this message is not a decision token. It
        # is still ambiguous whether the user meant to answer the prompt, so
        # (B4) fast-path is suppressed while ANY approval is pending: signal
        # that a prompt is live so the caller skips fast-path this turn.
        return True

    # --- per-user queue ---

    def _get_user_queue_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._user_queue_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_queue_locks[user_id] = lock
        return lock

    def _user_turn_active(self, user_id: int) -> bool:
        """True while ANY turn task for this user is in flight.

        Gate for background work (session-inbox injection, deferred pinned
        edits): source is _user_run_tasks (the full in-flight set), NOT
        _active_tasks -- that dict holds a single slot per user, so with
        concurrent turns a short turn overwrites a long turn's registration
        and pops it on completion, opening the gate while the long turn is
        still streaming.
        """
        return bool(self._prune_user_tasks(user_id))

    def _prune_user_tasks(self, user_id: int) -> set[asyncio.Task]:
        tasks = self._user_run_tasks.setdefault(user_id, set())
        tasks.difference_update({t for t in tasks if t.done()})
        return tasks

    def _track_user_task(self, user_id: int, task: asyncio.Task) -> None:
        tasks = self._prune_user_tasks(user_id)
        tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            self._user_run_tasks.get(user_id, set()).discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("Background task failed for user %s: %s", user_id, e, exc_info=True)

        task.add_done_callback(_on_done)

    def _track_btw_fork_task(self, user_id: int, task: asyncio.Task) -> None:
        """DGN-922 FIX 4: track a btw fork task in the SEPARATE fork set.

        Fork tasks MUST NOT live in _user_run_tasks -- doing so makes
        _prune_user_tasks truthy while the fork runs, which causes regular
        messages to enter the debounce path and block for up to
        BTW_TURN_TIMEOUT=120s (violates the DGN-911 5s-reaction contract).

        The fork set is pruned the same way as _user_run_tasks: done tasks
        are removed on completion via a done-callback.  _clear_user_queue
        cancels both sets so /stop still terminates outstanding forks.
        """
        fork_tasks = self._btw_fork_tasks.setdefault(user_id, set())
        # Prune stale entries (done tasks from prior forks) before adding.
        fork_tasks.difference_update({t for t in fork_tasks if t.done()})
        fork_tasks.add(task)

        def _on_fork_done(t: asyncio.Task) -> None:
            self._btw_fork_tasks.get(user_id, set()).discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(
                    "btw fork task failed for user %s: %s", user_id, e, exc_info=True
                )

        task.add_done_callback(_on_fork_done)

    def _clear_user_queue(self, user_id: int) -> int:
        tasks = self._prune_user_tasks(user_id)
        cleared = len(tasks)
        for t in list(tasks):
            t.cancel()
        tasks.clear()
        # DGN-922 FIX 4: also cancel outstanding btw fork tasks on /stop.
        fork_tasks = self._btw_fork_tasks.get(user_id, set())
        for ft in list(fork_tasks):
            if not ft.done():
                ft.cancel()
        fork_tasks.clear()
        # DGN-616: /stop also discards any buffered-but-not-yet-run messages so a
        # stopped turn does not leave a phantom merged follow-up queued behind it.
        self._user_pending_texts.pop(user_id, None)
        # DGN-911: same discard policy for the debounce buffer + its timer.
        self._clear_inflight_debounce(user_id)
        return cleared

    def _clear_inflight_debounce(self, user_id: int) -> None:
        """DGN-911: discard the debounce buffer and disarm the pending timer.

        Used by the /stop paths (both soft-interrupt and hard teardown): a stop
        means "drop what has not run yet", mirroring the DGN-616 pending-buffer
        discard above.
        """
        timer = self._debounce_timers.pop(user_id, None)
        if timer and not timer.done():
            timer.cancel()
        self._debounce_texts.pop(user_id, None)

    @staticmethod
    def _bundle_texts(items: List[tuple]) -> str:
        """DGN-616: merge buffered regular messages into one combined turn input.

        Single item -> returned verbatim (zero-cost, no wrapper). Multiple items
        -> arrival order preserved, each prefixed with its [HH:MM:SS] receipt
        time and joined by a clear "---" separator so the brain reads them as one
        message composed of several parts. Any attachment path-reference lines
        already baked into each item's text ride along unchanged, so photos /
        docs / voice transcripts buffered mid-turn all reach the merged turn.
        """
        if len(items) == 1:
            return items[0][0]
        parts = []
        for text, ts, _update in items:
            stamp = ts.astimezone(timezone.utc).strftime("%H:%M:%S") if ts else ""
            parts.append(f"[{stamp}] {text}" if stamp else text)
        return "\n---\n".join(parts)

    async def _enqueue_text_task(
        self,
        user_id: int,
        text: str,
        ts,
        update,
        *,
        failure_message=None,
        coalesce: bool = False,
    ) -> None:
        """Entry point for a REGULAR message (text/voice/photo/document).

        Idle: dispatch immediately as a single turn -- zero added latency, no
        debounce. The turn's done-callback drains whatever buffers while it
        runs into ONE merged follow-up turn.

        In flight, DEFAULT (DGN-911, coalesce=False): debounce-interrupt.
        The message lands in the per-user debounce buffer and (re)arms the
        BRIDGE_INFLIGHT_DEBOUNCE_S timer; on expiry the in-flight turn is
        soft-interrupted (DGN-581) and the buffered messages run as ONE merged
        new turn (see _debounce_expire).

        In flight, coalesce=True (/queue command): legacy DGN-616 coalescing --
        append to the pending buffer, never interrupt; the in-flight turn's
        done-callback merges the buffer into the next turn.

        failure_message selects the DGN-163 death-notice variant for an
        immediate single-message dispatch (e.g. the voice/photo/doc-specific
        text). A merged drain turn is a mix of message kinds, so it always uses
        the generic TURN_FAILED.
        """
        async with self._get_user_queue_lock(user_id):
            if self._prune_user_tasks(user_id):
                if coalesce:
                    buf = self._user_pending_texts.setdefault(user_id, [])
                    if len(buf) >= COALESCE_MAX:
                        await self._notify_coalesce_cap(user_id, update)
                        return
                    buf.append((text, ts, update))
                    return
                # DGN-911 default: buffer + (re)arm the debounce window.
                buf = self._debounce_texts.setdefault(user_id, [])
                if len(buf) >= COALESCE_MAX:
                    # Memory-safety valve: only a runaway flood reaches here.
                    await self._notify_coalesce_cap(user_id, update)
                    return
                buf.append((text, ts, update))
                self._reset_inflight_debounce(user_id)
                return
        # Idle: dispatch immediately. The done-callback drains any messages that
        # arrive while this turn runs.
        await self._dispatch_text_turn(
            user_id, text, ts, update, failure_message=failure_message
        )

    async def _notify_coalesce_cap(self, user_id: int, update) -> None:
        """Memory-safety cap notice (DGN-616), shared by both buffers."""
        chat = update.effective_chat
        chat_id = chat.id if chat else user_id
        try:
            await self.application.bot.send_message(chat_id, messages.QUEUE_BUSY)
        except Exception as e:
            logger.error(
                "coalesce-cap notice send failed for user %s: %s", user_id, e
            )

    def _reset_inflight_debounce(self, user_id: int) -> None:
        """DGN-911: (re)arm the per-user in-flight debounce timer.

        Called under the user's queue lock. Each new arrival inside the window
        cancels the previous timer and starts a fresh one (continuous-typing
        protection): the interrupt only fires after
        BRIDGE_INFLIGHT_DEBOUNCE_S seconds of input silence.
        """
        old = self._debounce_timers.pop(user_id, None)
        if old and not old.done():
            old.cancel()
        self._debounce_timers[user_id] = asyncio.create_task(
            self._debounce_expire(user_id)
        )

    async def _debounce_expire(self, user_id: int) -> None:
        """DGN-911: debounce window closed -- interrupt and re-dispatch.

        Fail-safe ordering: the debounce buffer moves into _user_pending_texts
        BEFORE the interrupt attempt, so whatever happens next (interrupt ok /
        nothing to interrupt / interrupt raises) the DGN-616 drain machinery
        guarantees delivery -- either the interrupted turn's done-path drains
        it now, or a naturally-finishing turn drains it later (graceful
        degradation to legacy coalescing). Messages are never dropped.

        The move + in-flight check + interrupt all run under the user's queue
        lock, so no new turn can dispatch and no drain can pop in between: the
        interrupt can only target the turn that was in flight when the window
        closed, never a successor turn carrying these very messages.

        Notice policy (owner decision 2026-08-17): SILENT by default. The
        BRIDGE_INFLIGHT_INTERRUPT_NOTICE flag is wired for a future opt-in;
        it reuses the confirmed /stop copy (no dedicated auto-interrupt text
        exists yet).
        """
        try:
            await asyncio.sleep(BRIDGE_INFLIGHT_DEBOUNCE_S)
        except asyncio.CancelledError:
            # Reset by a newer arrival, flushed by a finishing turn's drain,
            # or discarded by /stop.
            raise
        interrupted = False
        dispatch_idle = False
        newest_update = None
        try:
            async with self._get_user_queue_lock(user_id):
                self._debounce_timers.pop(user_id, None)
                items = self._debounce_texts.pop(user_id, None)
                if not items:
                    return
                newest_update = items[-1][2]
                # Fail-safe hand-off FIRST: from here on the coalescing drain
                # path owns delivery no matter what the interrupt does.
                #
                # MINOR: sort after extend so that pre-existing /queue entries
                # (coalesced before this window) and the debounce items are
                # merged in chronological order. Without this the interrupted
                # turn's done-path drains _user_pending_texts via the
                # else-branch of _drain_pending_texts (no sort) and a /queue
                # item at t2 can precede a debounce item at t1.
                pending_buf = self._user_pending_texts.setdefault(user_id, [])
                pending_buf.extend(items)
                if len(pending_buf) > 1:
                    fallback_ts = datetime.min.replace(tzinfo=timezone.utc)

                    def _expire_arrival_key(item, _fb=fallback_ts):
                        ts_val = item[1]
                        if ts_val is None:
                            return _fb
                        if ts_val.tzinfo is None:
                            return ts_val.replace(tzinfo=timezone.utc)
                        return ts_val

                    pending_buf.sort(key=_expire_arrival_key)
                if self._prune_user_tasks(user_id):
                    try:
                        interrupted = await sdk_bridge.interrupt(user_id)
                    except Exception as e:
                        logger.error(
                            "DGN-911 auto-interrupt failed for user %s: %s -- "
                            "degrading to coalescing merge on turn end",
                            user_id,
                            e,
                        )
                    # interrupted False here means the in-flight task carries
                    # no interruptible SDK turn (already settling, or a
                    # control task). Either way its done-path / a later turn
                    # drains the buffer -- legacy DGN-616 behavior.
                else:
                    # Turn ended during the window without a drain pass (the
                    # drain path normally flushes this buffer first; belt and
                    # braces). Dispatch outside the lock.
                    dispatch_idle = True
        except Exception as e:
            # Buffered messages already sit in _user_pending_texts (or still in
            # _debounce_texts if the lock section itself failed) -- both are
            # drained by later turns, so log loudly but never drop.
            logger.error(
                "DGN-911 debounce-expire failed for user %s: %s",
                user_id,
                e,
                exc_info=True,
            )
            return
        if dispatch_idle:
            await self._drain_pending_texts(user_id)
            return
        if interrupted and BRIDGE_INFLIGHT_INTERRUPT_NOTICE and newest_update:
            # Opt-in notice only; default is silence (owner-confirmed UX).
            try:
                chat = newest_update.effective_chat
                chat_id = chat.id if chat else user_id
                await self.application.bot.send_message(
                    chat_id, messages.STOP_INTERRUPTED
                )
            except Exception as e:
                logger.error(
                    "DGN-911 interrupt notice send failed for user %s: %s",
                    user_id,
                    e,
                )

    async def _dispatch_text_turn(
        self, user_id: int, text: str, ts, update, *, failure_message=None
    ) -> None:
        """DGN-616: run one regular turn, then drain the coalescing buffer.

        Wraps _process_user_message_text in the DGN-163 turn-death safety net
        (via _enqueue_user_task) and, on completion, flushes any messages that
        buffered while it ran into a single merged follow-up turn. Regular turns
        thus stay strictly serial per user: exactly one ResultMessage settles
        before the next merged turn dispatches.
        """
        chat = update.effective_chat
        chat_id = chat.id if chat else user_id

        async def run_task() -> None:
            try:
                await self._process_user_message_text(update, user_id, text)
            finally:
                await self._drain_pending_texts(user_id)

        async def on_overflow() -> None:
            # Unreachable in practice: the coalescing path only dispatches when
            # idle, so the control-path inflight cap is never hit here. Buffer as
            # a defensive fallback rather than drop.
            self._user_pending_texts.setdefault(user_id, []).append((text, ts, update))

        await self._enqueue_user_task(
            user_id,
            run_task,
            on_overflow,
            chat_id=chat_id,
            failure_message=failure_message,
        )

    async def _drain_pending_texts(self, user_id: int) -> None:
        """DGN-616: flush the coalescing buffer into ONE merged follow-up turn.

        Runs from the finishing turn's done path. Pops every buffered message,
        merges them (order preserved), and dispatches a single new turn seeded
        with the newest update for chat/reply context. That turn will itself
        drain again on completion, so a buffer that keeps filling drains in a
        loop -- always one merged turn at a time, never concurrent.

        DGN-911: the debounce buffer flushes here too. A turn that ends
        NATURALLY inside an open debounce window must not leave those messages
        waiting for the timer (idle == immediate dispatch, and a later idle
        arrival could otherwise race ahead of them). The timer is cancelled;
        if its expiry task already started, the pop below wins or loses the
        buffer atomically under the lock -- never both, never neither.
        """
        async with self._get_user_queue_lock(user_id):
            pending = self._user_pending_texts.pop(user_id, None) or []
            debounced = self._debounce_texts.pop(user_id, None) or []
            timer = self._debounce_timers.pop(user_id, None)
        if timer and not timer.done():
            timer.cancel()
        if debounced:
            # Stable chronological merge across the two buffers; entries
            # without a timestamp keep their relative position (sort key
            # normalizes None/naive so mixed inputs can never raise).
            fallback = datetime.min.replace(tzinfo=timezone.utc)

            def _arrival_key(item):
                ts_val = item[1]
                if ts_val is None:
                    return fallback
                if ts_val.tzinfo is None:
                    return ts_val.replace(tzinfo=timezone.utc)
                return ts_val

            items = sorted(pending + debounced, key=_arrival_key)
        else:
            items = pending
        if not items:
            return
        merged = self._bundle_texts(items)
        # Seed the merged turn from the newest buffered update so replies attach
        # to the latest message and failure notices route to the right chat.
        newest_update = items[-1][2]
        newest_ts = items[-1][1]
        await self._dispatch_text_turn(user_id, merged, newest_ts, newest_update)

    async def _turn_death_notice(
        self,
        user_id: int,
        chat_id: Optional[int],
        failure_message: str,
    ) -> None:
        """DGN-163 safety net: emit ONE user-visible notice for a turn that would
        otherwise die silently (any exception between "update accepted" and the
        first user reply).

        Never sends a raw traceback. If partial output already streamed for this
        turn, route to the softer "reply may be incomplete" variant instead of
        claiming the message was dropped. The send itself is best-effort: wrapped
        so a failing notice-send logs and returns rather than crash-looping.
        """
        if chat_id is None:
            chat_id = user_id
        try:
            if sdk_bridge.user_has_streamed_output(user_id):
                text = messages.TURN_INCOMPLETE
            else:
                text = failure_message
        except Exception:
            text = failure_message
        try:
            await self.application.bot.send_message(chat_id, text)
        except Exception as e:
            logger.error(
                "turn-death notice send failed for user %s chat %s: %s",
                user_id, chat_id, e,
            )

    async def _enqueue_user_task(
        self,
        user_id: int,
        run_task: Callable[[], Awaitable[None]],
        on_overflow: Callable[[], Awaitable[None]],
        *,
        chat_id: Optional[int] = None,
        failure_message: Optional[str] = None,
    ) -> bool:
        # DGN-163: wrap every enqueued turn so ANY escaping exception (handler
        # crash, download failure, mid-turn death) still produces one bounded
        # user-visible notice. Without this, run_task exceptions only reached the
        # done-callback logger and the consumed update produced zero output.
        death_message = failure_message or messages.TURN_FAILED
        accepted: Optional[asyncio.Task] = None
        async with self._get_user_queue_lock(user_id):
            tasks = self._prune_user_tasks(user_id)
            if len(tasks) < MAX_INFLIGHT_MESSAGES:
                async def wrapped() -> None:
                    self._active_tasks[user_id] = asyncio.current_task()
                    try:
                        await run_task()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(
                            "turn died for user %s: %s", user_id, e, exc_info=True
                        )
                        await self._turn_death_notice(user_id, chat_id, death_message)
                    finally:
                        self._active_tasks.pop(user_id, None)

                accepted = asyncio.create_task(wrapped())
                self._track_user_task(user_id, accepted)
        if not accepted:
            await on_overflow()
            return False
        return True

    # --- fast-path interceptor (DGN-801) ---

    async def _try_fastpath(self, user_id: int, text: str, update) -> bool:
        """Offer a message to the domain fast-path handler before any SDK turn.

        Returns True when the message was CLAIMED by the fast-path task (the
        caller must not enqueue an SDK turn for it); False routes it to the
        normal coalescing path. Domain-agnostic: the enable flag, a cheap
        numeric-short prefilter, and the idle gate are the only bridge-side
        signals -- the real verdict is the handler's (exit 2 FALLBACK degrades
        any misdetection back to the model, fail-safe, never a drop).

        Serialization (grill F4): concurrent_updates(True) runs handlers
        concurrently, so the fast-path fires ONLY under the user's queue lock
        and ONLY when that user is fully idle (no in-flight turn). The
        fast-path task registers in _user_run_tasks while the lock is held, so
        any message arriving during it buffers behind it via the DGN-616
        coalescing path (exactly one serialization point), and a second
        fast-path can never run concurrently for the same user.
        """
        if not fastpath.enabled():
            return False
        if not fastpath.prefilter(text):
            return False
        chat = update.effective_chat
        chat_id = chat.id if chat else user_id

        async def run_task() -> None:
            try:
                result = await fastpath.run_handler(text)
                if result.processed:
                    # exit 0 = commit witness: push the rendered body and
                    # suppress the model turn. An empty body is a handler
                    # contract violation, but state may be committed, so it
                    # still must NOT fall back to the model -- surface the
                    # committed-but-silent case via the death notice instead.
                    if result.body:
                        await self._fastpath_push_guaranteed(chat_id, result.body)
                    else:
                        logger.error(
                            "fast-path handler exit 0 with empty body for user %s",
                            user_id,
                        )
                        await self._fastpath_push_death_notice(chat_id)
                else:
                    # FALLBACK / timeout / crash -> the normal SDK turn runs on
                    # the original text inside THIS task (still serialized).
                    await self._process_user_message_text(update, user_id, text)
            finally:
                await self._drain_pending_texts(user_id)

        async with self._get_user_queue_lock(user_id):
            if self._prune_user_tasks(user_id):
                # Turn in flight -> let the default in-flight path handle this
                # message (DGN-911 debounce-interrupt via _enqueue_text_task).
                return False

            async def wrapped() -> None:
                self._active_tasks[user_id] = asyncio.current_task()
                try:
                    await run_task()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        "fast-path turn died for user %s: %s",
                        user_id, e, exc_info=True,
                    )
                    await self._turn_death_notice(
                        user_id, chat_id, messages.TURN_FAILED
                    )
                finally:
                    self._active_tasks.pop(user_id, None)

            task = asyncio.create_task(wrapped())
            self._track_user_task(user_id, task)
        return True

    async def _fastpath_push_guaranteed(self, chat_id: int, content: str) -> None:
        """Grill M3: deliver a fast-path exit-0 body with retries, never silence.

        The handler already committed state when this runs, so a failed send
        must NOT re-route the message to the model (double-processing risk).
        Retry with backoff; on final failure push a short death-notice through
        the raw send path so "recorded but screen not updated" is never silent.
        Distinct from _send_guaranteed (B3 raw send): the fast-path body is a
        rendered table, so it must go through _send_smart formatting.
        """
        last_error: Optional[Exception] = None
        for attempt, delay in enumerate((0.0, *FASTPATH_PUSH_RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._send_smart(chat_id, content)
                return
            except Exception as e:
                last_error = e
                logger.warning(
                    "fast-path push attempt %d failed for chat %s: %s",
                    attempt + 1, chat_id, e,
                )
        logger.error(
            "fast-path push exhausted retries for chat %s: %s", chat_id, last_error
        )
        await self._fastpath_push_death_notice(chat_id)

    async def _fastpath_push_death_notice(self, chat_id: int) -> None:
        """Best-effort minimal-path notice: input recorded, screen update lost."""
        try:
            if self.application is not None:
                await self.application.bot.send_message(
                    chat_id, messages.FASTPATH_PUSH_FAILED
                )
        except Exception as e:
            logger.error(
                "fast-path push death-notice send failed for chat %s: %s",
                chat_id, e,
            )

    # --- commands ---

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update):
            return
        user = update.effective_user
        await update.message.reply_text(messages.WELCOME.format(name=user.first_name))

    async def _cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        await sdk_bridge.cancel_user_streaming(user_id)
        session = await session_manager.get_session(user_id)
        session["session_id"] = None
        session["new_session"] = True
        await session_manager.update_session(user_id, session)
        self._runtime_active_sessions.discard(user_id)
        await update.message.reply_text(messages.NEW_SESSION)

    def _get_real_model(self, session: dict) -> str:
        # Resolution chain (DGN-162): explicit session override > persisted
        # last-session model > workspace settings > global settings > default.
        # The resolver persists whatever concrete value wins so the next fresh
        # session inherits the model this one actually used.
        return model_state.resolve_session_model(
            override=session.get("model"),
            known=_known_models(),
            fallback=DEFAULT_MODEL,
        )

    async def _cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        session = await session_manager.get_session(user_id)
        if context.args:
            name = context.args[0]
            allowed = _model_whitelist()
            # Accept whitelisted short names and any full 'claude-*' id; reject
            # unknown short names with the allowed list. Switching restarts the
            # conversation, so warn in the reply.
            if name not in allowed and not name.startswith("claude-"):
                await update.message.reply_text(
                    messages.MODEL_UNKNOWN.format(name=name, allowed=", ".join(allowed))
                )
                return
            # DGN-192: switching to the already-active model is a no-op --
            # never reset the session for a switch that changes nothing.
            if name == self._get_real_model(session):
                label = _MODEL_LABELS.get(name, name)
                await update.message.reply_text(
                    messages.MODEL_ALREADY_ACTIVE.format(label=label)
                )
                return
            session["model"] = name
            session["session_id"] = None
            session["new_session"] = True
            await session_manager.update_session(user_id, session)
            # DGN-162: a user-initiated switch becomes the new last-session model.
            model_state.persist_model(name, _known_models())
            self._runtime_active_sessions.discard(user_id)
            label = _MODEL_LABELS.get(name, name)
            await update.message.reply_text(
                messages.MODEL_SWITCHED.format(label=label)
            )
            return
        current = self._get_real_model(session)
        models = list(MODELS)
        if current not in dict(models):
            models.append((current, current))
        # DGN-192: strongest model first (fable > opus > sonnet > haiku).
        models.sort(key=lambda x: (_MODEL_PERF_RANK.get(x[0], 99), x[0]))
        buttons = [
            [InlineKeyboardButton(
                f"{label} (current)" if name == current else label,
                callback_data=f"model:{name}",
            )]
            for name, label in models
        ]
        await update.message.reply_text(
            messages.MODEL_SELECT, reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """DGN-581: ESC-parity stop. Soft interrupt first -- stop the in-flight
        turn in place and drain the queue while the session, client, and CLI
        subprocess stay alive. Hard teardown is the fallback when the interrupt
        has nothing to catch or fails, so /stop is never a silent no-op.

        The soft path must NOT cancel the user's run tasks: they resolve on
        their own via the drained futures, and cancelling them would trigger
        process_message's CancelledError handler, which hard-stops the stream.
        """
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        # DGN-911/DGN-922 FIX 1: a stop discards BOTH the debounce buffer and
        # the /queue coalescing buffer.  The comment at _clear_user_queue and
        # in the now-reconciled block below has always said "stop discards
        # buffered messages"; the soft path previously only cleared the debounce
        # window, leaving _user_pending_texts alive.  A dying turn's finally-
        # drain would then fire _drain_pending_texts on those surviving entries
        # and dispatch a ghost merged turn -- violating the "stop drops the
        # queue" contract.  Mirror the same two-part discard done by
        # _clear_user_queue: pending buffer + debounce window.
        self._user_pending_texts.pop(user_id, None)
        self._clear_inflight_debounce(user_id)
        try:
            if await sdk_bridge.interrupt(user_id):
                await update.message.reply_text(messages.STOP_INTERRUPTED)
                return
        except Exception as e:
            logger.error(
                "Soft interrupt failed for user %s: %s -- falling back to hard teardown",
                user_id,
                e,
            )
        # Nothing to interrupt (or the interrupt send failed on a stuck turn):
        # legacy hard-stop semantics.
        await self._hard_stop(update, user_id)

    async def _cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """DGN-911: /queue <msg> -- explicit no-interrupt enqueue.

        Routes the message through the legacy DGN-616 coalescing path: while a
        turn is in flight it appends to the pending buffer and merges into the
        NEXT turn when the current one finishes on its own -- never interrupts.
        When idle it simply dispatches immediately (same as a plain message),
        so /queue is always safe to use.
        """
        if not await self._check_access(update):
            return
        message = update.message
        user_id = update.effective_user.id
        args = context.args or []
        text = " ".join(args).strip()
        if not text:
            await message.reply_text(messages.QUEUE_USAGE)
            return
        await self._enqueue_text_task(
            user_id, text, self._message_ts(message), update, coalesce=True
        )

    async def _hard_stop(self, update: Update, user_id: int) -> None:
        """Legacy hard teardown: cancel run tasks, disconnect the stream (kill
        the CLI subprocess on a stuck disconnect), clear the queue."""
        await sdk_bridge.cancel_user_streaming(user_id)
        active = self._active_tasks.get(user_id)
        task_cancelled = False
        if active and not active.done():
            active.cancel()
            task_cancelled = True
        killed = await sdk_bridge.stop(user_id)
        cleared = self._clear_user_queue(user_id)
        reply = messages.STOP_PAUSED if (task_cancelled or killed or cleared) else messages.STOP_NOTHING
        await update.message.reply_text(reply)

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        sessions = self._list_sessions(limit=10)
        if not sessions:
            await update.message.reply_text(messages.NO_SESSION_HISTORY)
            return
        session = await session_manager.get_session(user_id)
        session["resume_list"] = [(sid, msg) for sid, msg, _ in sessions]
        await session_manager.update_session(user_id, session)
        lines = [messages.SESSION_HISTORY_HEADER, ""]
        for i, (_sid, msg, _mtime) in enumerate(sessions, 1):
            lines.append(f"{i}. {msg}")
        lines.append("")
        lines.append(messages.RESUME_HINT)
        await update.message.reply_text("\n".join(lines))

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update):
            return
        # DGN-919: build help text from COMMAND_MENU_SPEC so menu and help list
        # always share the same order and copy (drift-proof single source).
        lines = [messages.HELP_TEXT_HEADER]
        for i, (cmd, desc_fn) in enumerate(COMMAND_MENU_SPEC, start=1):
            lines.append(f"{i}. /{cmd} - {desc_fn()}")
        lines.append("")
        lines.append(messages.HELP_TEXT_FOOTER)
        await update.message.reply_text("\n".join(lines))

    async def _cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Run routines/claude-usage.sh and reply with its report.

        No model call / no session: run the script directly (like /skills),
        capture stdout, HTML-escape it, and wrap it in <pre> so the ASCII bars
        and tables keep their alignment in Telegram. Long reports are split.
        """
        if not await self._check_access(update):
            return
        script = PROJECT_ROOT / "routines" / "claude-usage.sh"
        if not script.is_file():
            await update.message.reply_text(messages.USAGE_SCRIPT_MISSING)
            return

        def _run() -> str:
            # Pass the active locale so the script localizes its labels
            # (ko/en) to match the bridge UI.
            run_env = {**os.environ, "LOCALE": config.locale}
            proc = subprocess.run(
                [str(script)],
                capture_output=True,
                text=True,
                timeout=12,
                env=run_env,
            )
            out = proc.stdout or ""
            if not out.strip():
                out = (proc.stderr or "").strip() or "(no output)"
            return out

        try:
            output = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            await update.message.reply_text(messages.USAGE_TIMEOUT)
            return
        except Exception as e:
            await update.message.reply_text(
                messages.USAGE_FAILED.format(error=str(e))
            )
            return

        escaped = html.escape(output)
        for part in split_text(escaped):
            # DGN-891: balance guard (no-op here) + tag-stripped fallback so
            # the plain degrade never shows escaped entities.
            body = balance_telegram_html(f"<pre>{part}</pre>")
            try:
                await update.message.reply_text(body, parse_mode="HTML")
            except Exception:
                await update.message.reply_text(html_to_plain_text(body))

    async def _cmd_authsync(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """DGN-759: /authsync -- sync macOS Keychain Claude credential with
        ~/.claude/.credentials.json after a host re-login.

        Reuses token-sync.sh from the dogany-relogin-rebind skill (DGN-394):
          status subcommand: compare hashes, exit 0=MATCH, 1=MISMATCH, 2=ERROR, 3=NOT-APPLICABLE
          sync   subcommand: overwrite keychain from file, verify MATCH

        Non-macOS hosts: token-sync.sh exits 3 (NOT-APPLICABLE); we surface a
        graceful no-op message. Missing script: clear error, no crash.
        """
        if not await self._check_access(update):
            return

        # Locate token-sync.sh in the dogany-relogin-rebind skill (canonical
        # global install path). Climb the ladder: reuse, do NOT reimplement.
        skill_script = (
            Path.home() / ".claude" / "skills" / "dogany-relogin-rebind" / "token-sync.sh"
        )
        if not skill_script.is_file():
            await update.message.reply_text(messages.AUTHSYNC_SCRIPT_MISSING)
            return

        await update.message.reply_text(messages.AUTHSYNC_RUNNING)

        def _run_status() -> subprocess.CompletedProcess:
            return subprocess.run(
                [str(skill_script), "status"],
                capture_output=True,
                text=True,
                timeout=15,
            )

        def _run_sync() -> subprocess.CompletedProcess:
            return subprocess.run(
                [str(skill_script), "sync"],
                capture_output=True,
                text=True,
                timeout=15,
            )

        try:
            status_result = await asyncio.to_thread(_run_status)
        except subprocess.TimeoutExpired:
            await update.message.reply_text(
                messages.AUTHSYNC_ERROR.format(error="status check timed out")
            )
            return
        except Exception as e:
            await update.message.reply_text(messages.AUTHSYNC_ERROR.format(error=str(e)))
            return

        # exit 3 = NOT-APPLICABLE (non-macOS platform)
        if status_result.returncode == 3:
            await update.message.reply_text(messages.AUTHSYNC_NOT_APPLICABLE)
            return

        # exit 2 = ERROR from token-sync.sh
        if status_result.returncode == 2:
            detail = (status_result.stderr or status_result.stdout or "").strip()
            await update.message.reply_text(
                messages.AUTHSYNC_ERROR.format(error=detail or "unknown error")
            )
            return

        # exit 0 = MATCH; nothing to do
        if status_result.returncode == 0:
            await update.message.reply_text(messages.AUTHSYNC_MATCH)
            return

        # exit 1 = MISMATCH; run sync
        await update.message.reply_text(messages.AUTHSYNC_MISMATCH_SYNCING)
        try:
            sync_result = await asyncio.to_thread(_run_sync)
        except subprocess.TimeoutExpired:
            await update.message.reply_text(
                messages.AUTHSYNC_ERROR.format(error="sync timed out")
            )
            return
        except Exception as e:
            await update.message.reply_text(messages.AUTHSYNC_ERROR.format(error=str(e)))
            return

        if sync_result.returncode == 0:
            await update.message.reply_text(messages.AUTHSYNC_SYNC_OK)
        elif sync_result.returncode == 3:
            # Sync returned not-applicable (platform changed mid-run, unlikely)
            await update.message.reply_text(messages.AUTHSYNC_NOT_APPLICABLE)
        else:
            await update.message.reply_text(messages.AUTHSYNC_SYNC_FAILED)

    async def _cmd_btw(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """DGN-902: /btw -- fork the current session context for a side question.

        Reads the current main session_id, creates an ephemeral SDK client with
        fork_session=True so it inherits the main session history but writes to
        a new isolated session. The response is sent as a Telegram reply-to the
        /btw message, marked with the 💭 bubble prefix.

        Reply-to threading: if the user replies to a 💭 bubble, that incoming
        message is routed to the same fork session (keyed by the anchor
        message_id), not to the main history.
        """
        if not await self._check_access(update):
            return

        message = update.message
        user_id = update.effective_user.id
        chat = update.effective_chat
        chat_id = chat.id if chat else user_id

        # Extract the question from command args (text after "/btw ").
        args = context.args or []
        question = " ".join(args).strip()
        if not question:
            await message.reply_text(messages.BTW_NO_QUESTION)
            return

        # Gate: require an active main session to fork from.
        session = await session_manager.get_session(user_id)
        main_session_id = self._effective_session_id(user_id, session)
        if not main_session_id:
            await message.reply_text(messages.BTW_NO_SESSION)
            return

        # Send an acknowledgement reply-to the /btw message immediately.
        # We capture its message_id to anchor the fork state.
        try:
            thinking_msg = await message.reply_text(
                messages.BTW_THINKING,
                reply_parameters=ReplyParameters(message_id=message.message_id),
            )
        except Exception as e:
            logger.error("btw thinking reply failed for user %s: %s", user_id, e)
            await message.reply_text(messages.BTW_FORK_FAILED)
            return

        anchor_mid = thinking_msg.message_id

        # Register the fork state BEFORE running the turn so reply-to routing
        # is live as soon as the anchor bubble exists.
        fork = btw_module.BtwForkState(
            anchor_message_id=anchor_mid,
            spawned_from_session_id=main_session_id,
        )
        self._btw_forks.register_fork(user_id, fork)

        # Run the fork turn in an enqueued task so it does not block the bot's
        # main handler loop. The turn is independent of the main session queue.
        async def run_fork_task() -> None:
            try:
                response = await self._btw_forks.run_fork_turn(user_id, fork, question)
            except asyncio.TimeoutError:
                logger.warning("btw fork turn timed out for user %s", user_id)
                response = messages.BTW_FORK_FAILED
            except Exception as e:
                logger.error("btw fork turn failed for user %s: %s", user_id, e)
                response = messages.BTW_FORK_FAILED

            if not response:
                response = messages.BTW_FORK_FAILED

            # Prepend the 💭 marker.
            marked = f"{messages.BTW_MARKER}\n\n{response}"

            # DGN-920: format through the shared prose->HTML helper so markdown
            # in the fork response renders (** -> <b>, code blocks, etc.) the
            # same way the normal send path does (DGN-891 tag balancing included).
            # Fail-soft: if formatting raises, fall back to the raw marked text
            # so the answer is delivered even without formatting.
            try:
                formatted = balance_telegram_html(sanitize_message_for_telegram(marked))
                use_html = True
            except Exception as fmt_err:
                logger.warning(
                    "btw formatting failed for user %s (falling back to raw): %s",
                    user_id,
                    fmt_err,
                )
                formatted = marked
                use_html = False

            # DGN-922 FIX 3: split long output so a >4096-char fork response
            # never fails silently.  Mirror the main send path: split_text
            # chunks at paragraph > line > hard-cut boundaries, then
            # rebalance_html_chunks ensures every chunk is independently
            # valid HTML (no tag straddling the split point).  Each chunk gets
            # its own html_to_plain_text degrade fallback so a Telegram HTML
            # rejection never stalls with the anchor bubble stuck on "생각 중...".
            chunks: List[str] = split_text(formatted) if formatted else [formatted]
            if use_html:
                chunks = rebalance_html_chunks(chunks)
            parse_mode_val = "HTML" if use_html else None

            async def _send_chunk(text_chunk: str, *, reply_to_mid: int) -> Optional[int]:
                """Send one chunk; returns the new message_id or None on total failure."""
                parse_mode = parse_mode_val
                try:
                    sent = await self.application.bot.send_message(
                        chat_id=chat_id,
                        text=text_chunk,
                        parse_mode=parse_mode,
                        reply_parameters=ReplyParameters(message_id=reply_to_mid),
                    )
                    return sent.message_id
                except Exception as html_err:
                    if not use_html:
                        logger.error(
                            "btw chunk send failed for user %s: %s", user_id, html_err
                        )
                        return None
                    # HTML rejected: degrade to plain text.
                    try:
                        sent = await self.application.bot.send_message(
                            chat_id=chat_id,
                            text=html_to_plain_text(text_chunk),
                            parse_mode=None,
                            reply_parameters=ReplyParameters(message_id=reply_to_mid),
                        )
                        return sent.message_id
                    except Exception as plain_err:
                        logger.error(
                            "btw chunk plain-text fallback also failed for user %s: %s",
                            user_id,
                            plain_err,
                        )
                        return None

            first_chunk = chunks[0]
            rest_chunks = chunks[1:]

            # Try to edit the thinking anchor to the first chunk.
            # On edit failure, fall back to sending a fresh message so the
            # anchor bubble is never left stuck on "생각 중...".
            # Use a local mutable container for the anchor id so inner-function
            # assignment does not create a Python "local before use" shadowing
            # issue with the outer anchor_mid closure variable.
            thread_anchor = [anchor_mid]  # [0] = current anchor message_id
            try:
                await self.application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=thread_anchor[0],
                    text=first_chunk,
                    parse_mode=parse_mode_val,
                )
            except Exception as edit_err:
                logger.warning(
                    "btw edit failed for user %s (will send fresh reply): %s",
                    user_id,
                    edit_err,
                )
                # Edit failed: try HTML send, then plain-text degrade.
                new_mid = await _send_chunk(first_chunk, reply_to_mid=message.message_id)
                if new_mid is not None:
                    # Re-register fork under the new anchor so reply routing works.
                    fork.anchor_message_id = new_mid
                    self._btw_forks.register_fork(user_id, fork)
                    thread_anchor[0] = new_mid
                else:
                    # Both edit and send failed: anchor bubble stays; log and give up.
                    logger.error(
                        "btw: both edit and fresh send failed for user %s; "
                        "anchor bubble may be stuck",
                        user_id,
                    )

            # Send remaining chunks (if any) as follow-up replies to the anchor.
            for extra_chunk in rest_chunks:
                await _send_chunk(extra_chunk, reply_to_mid=thread_anchor[0])

        # DGN-911/DGN-922: drain _user_pending_texts when the fork ends.
        # FIX 4 moves fork tasks to _btw_fork_tasks (out of _user_run_tasks),
        # so a new message no longer stalls behind a running fork.  The drain
        # here is belt-and-braces: if a debounce expire moved items into
        # _user_pending_texts just before the fork finished, the drain fires
        # the merged follow-up immediately instead of waiting for idle.
        async def _fork_task_with_drain() -> None:
            try:
                await run_fork_task()
            finally:
                await self._drain_pending_texts(user_id)

        # DGN-922 FIX 4: fire the fork task in the SEPARATE btw fork set
        # (not _user_run_tasks) so normal messages arriving while the fork
        # runs are NOT forced into the debounce path.  _clear_user_queue
        # still cancels fork tasks on /stop (see _track_btw_fork_task).
        task = asyncio.create_task(_fork_task_with_drain())
        self._track_btw_fork_task(user_id, task)

    async def _maybe_route_btw_reply(
        self,
        user_id: int,
        text: str,
        message,
        update,
    ) -> bool:
        """DGN-902: route a reply-to message into its fork session if applicable.

        Returns True when the message was claimed by a btw fork (caller stops
        further routing). Returns False when the message is not a reply to a
        known fork bubble (caller proceeds with normal routing).

        The check is: does this message have a reply_to_message whose message_id
        is registered as a fork anchor for this user?
        """
        reply_to = getattr(message, "reply_to_message", None)
        if reply_to is None:
            return False
        anchor_mid = getattr(reply_to, "message_id", None)
        if anchor_mid is None:
            return False

        fork = self._btw_forks.lookup_fork(user_id, anchor_mid)
        if fork is None:
            return False

        # Claimed by a btw fork. Route the question to the fork session.
        chat = update.effective_chat
        chat_id = chat.id if chat else user_id

        async def run_fork_continuation() -> None:
            try:
                response = await self._btw_forks.run_fork_turn(user_id, fork, text)
            except asyncio.TimeoutError:
                logger.warning(
                    "btw fork continuation timed out for user %s", user_id
                )
                response = messages.BTW_FORK_FAILED
            except Exception as e:
                logger.error(
                    "btw fork continuation failed for user %s: %s", user_id, e
                )
                response = messages.BTW_FORK_FAILED

            if not response:
                response = messages.BTW_FORK_FAILED

            marked = f"{messages.BTW_MARKER}\n\n{response}"
            # DGN-920: same shared formatting as the initial fork send path.
            # Fail-soft: if formatting raises, send the raw marked text.
            try:
                formatted = balance_telegram_html(sanitize_message_for_telegram(marked))
                use_html = True
            except Exception as fmt_err:
                logger.warning(
                    "btw continuation formatting failed for user %s (raw fallback): %s",
                    user_id,
                    fmt_err,
                )
                formatted = marked
                use_html = False
            # DGN-922 FIX 3 (continuation path): same split + plain-text degrade
            # pattern as the initial fork send path.  A long continuation
            # response must not be silently dropped just because a single
            # send_message call exceeds Telegram's 4096-char limit.
            cont_chunks: List[str] = split_text(formatted) if formatted else [formatted]
            if use_html:
                cont_chunks = rebalance_html_chunks(cont_chunks)
            cont_parse_mode = "HTML" if use_html else None

            reply_to_mid = message.message_id
            for cont_chunk in cont_chunks:
                try:
                    sent = await self.application.bot.send_message(
                        chat_id=chat_id,
                        text=cont_chunk,
                        parse_mode=cont_parse_mode,
                        reply_parameters=ReplyParameters(message_id=reply_to_mid),
                    )
                    # Chain continuation chunks as replies to each other so the
                    # thread stays readable.
                    reply_to_mid = sent.message_id
                except Exception as html_send_err:
                    if not use_html:
                        logger.error(
                            "btw continuation send failed for user %s: %s",
                            user_id,
                            html_send_err,
                        )
                        continue
                    # HTML rejected: degrade to plain text.
                    try:
                        sent = await self.application.bot.send_message(
                            chat_id=chat_id,
                            text=html_to_plain_text(cont_chunk),
                            parse_mode=None,
                            reply_parameters=ReplyParameters(message_id=reply_to_mid),
                        )
                        reply_to_mid = sent.message_id
                    except Exception as cont_plain_err:
                        logger.error(
                            "btw continuation plain-text fallback failed for user %s: %s",
                            user_id,
                            cont_plain_err,
                        )

        # DGN-911/DGN-922 FIX 4: same drain-on-exit pattern as the initial
        # fork task (see above).  Fork continuation is also tracked in the
        # SEPARATE btw fork set, NOT _user_run_tasks, so it does not block
        # the debounce/in-flight decision for normal messages.
        async def _fork_continuation_with_drain() -> None:
            try:
                await run_fork_continuation()
            finally:
                await self._drain_pending_texts(user_id)

        task = asyncio.create_task(_fork_continuation_with_drain())
        self._track_btw_fork_task(user_id, task)
        return True

    @staticmethod
    def _read_skill_frontmatter(skill_md: Path) -> Optional[tuple]:
        """Return (name, description) from a SKILL.md YAML frontmatter, or None.

        Minimal, dependency-free parse: read only the leading '---' fenced block,
        pull 'name:' and 'description:' (supporting '>' / '>-' folded blocks).
        Robust to missing fields; never raises to the caller.
        """
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        if not text.startswith("---"):
            return None
        end = text.find("\n---", 3)
        if end == -1:
            return None
        block = text[3:end]
        lines = block.splitlines()
        name = None
        description = None
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("name:"):
                name = stripped[len("name:"):].strip().strip("'\"")
            elif stripped.startswith("description:"):
                val = stripped[len("description:"):].strip()
                if val in (">", ">-", "|", "|-", ">+", "|+", ""):
                    # YAML block-scalar indicator on the key line (e.g.
                    # "description: >-"): the real text lives on the following
                    # more-indented lines, NOT the indicator token.  Fold them
                    # so callers get the actual description, never a literal
                    # ">-" leak (DGN-618).  This parser is shared by
                    # _collect_skills and any other frontmatter reader, so the
                    # fold must stay correct even though /skills no longer
                    # renders the description.
                    base_indent = len(line) - len(line.lstrip())
                    parts: List[str] = []
                    i += 1
                    while i < len(lines):
                        nxt = lines[i]
                        if not nxt.strip():
                            i += 1
                            continue
                        indent = len(nxt) - len(nxt.lstrip())
                        if indent <= base_indent:
                            break
                        parts.append(nxt.strip())
                        i += 1
                    description = " ".join(parts)
                    continue
                else:
                    description = val.strip("'\"")
            i += 1
        if not name:
            name = skill_md.parent.name
        return (name, description or "")

    def _collect_skills(self, skills_dir: Path) -> List[tuple]:
        """(name, description) for every SKILL.md directly under skills_dir."""
        out: List[tuple] = []
        if not skills_dir.is_dir():
            return out
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                fm = self._read_skill_frontmatter(skill_md)
                if fm:
                    out.append(fm)
        return out

    async def _cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update):
            return
        # Read SKILL.md frontmatter directly -- no model call, no session, no
        # stream teardown (RELIABILITY #4). Project skills live under
        # PROJECT_ROOT/.claude/skills; global under ~/.claude/skills.
        project_skills = self._collect_skills(PROJECT_ROOT / ".claude" / "skills")
        global_skills = self._collect_skills(Path.home() / ".claude" / "skills")
        lines: List[str] = []

        def _fmt(entries: List[tuple]) -> List[str]:
            # DS list rule (DGN-376): top-level bullet "• ".  Show the localized
            # display name only; fall back to "/name" when unmapped.  The long
            # English SKILL.md description is intentionally NOT shown here
            # (DGN-618): it was caveman-English, truncated mid-word, and an
            # English wall for a Korean owner.
            rows = []
            for name, _desc in entries:
                label = skill_display_name(name)
                text = label if label != name else f"/{name}"
                rows.append(f"• {text}")
            return rows

        if project_skills:
            lines.append(f"<b>{messages.SKILLS_HEADER_PROJECT}</b>")
            lines.extend(_fmt(project_skills))
        if global_skills:
            if lines:
                lines.append("")
            lines.append(f"<b>{messages.SKILLS_HEADER_GLOBAL}</b>")
            lines.extend(_fmt(global_skills))
        reply = "\n".join(lines) if lines else messages.SKILLS_NONE
        for part in split_text(reply):
            # DGN-891: balance guard (a split could cut a <b> header pair) +
            # tag-stripped fallback so a plain degrade never leaks tags.
            part = balance_telegram_html(part)
            try:
                await update.message.reply_text(part, parse_mode="HTML")
            except Exception:
                await update.message.reply_text(html_to_plain_text(part))

    async def _handle_skill_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._check_access(update):
            return
        if not update.message or not update.message.text:
            return
        text = update.message.text
        parts = text.split(maxsplit=1)
        cmd_name = parts[0].lstrip("/").split("@")[0]
        for handler in self.application.handlers.get(0, []):
            if isinstance(handler, CommandHandler) and cmd_name in handler.commands:
                return
        args = parts[1] if len(parts) > 1 else ""
        await self._exec_slash_command(update, f"/{cmd_name} {args}".strip())

    async def _exec_slash_command(self, update: Update, slash_cmd: str) -> None:
        message = update.message
        user_id = update.effective_user.id
        chat = update.effective_chat
        app = self.application

        async def run_task() -> None:
            session = await session_manager.get_session(user_id)
            try:
                await message.chat.send_action(action="typing")
            except Exception:
                pass
            try:
                response = await sdk_bridge.process_message(
                    user_message=slash_cmd,
                    user_id=user_id,
                    chat_id=chat.id,
                    session_id=self._effective_session_id(user_id, session),
                    model=session.get("model"),
                    permission_callback=self._permission_callback,
                    typing_callback=lambda: message.chat.send_action(action="typing"),
                    bot=app.bot,
                    proactive_push=self._proactive_push,
                )
                await self._save_session_id(user_id, response)
                await self._reply_smart(
                    message,
                    response.content,
                    force_options=response.has_options,
                    streamed=response.streamed,
                    draft_message_ids=response.draft_message_ids,
                    classifier_injected=getattr(response, "options_classifier_injected", False),
                )
            except Exception as e:
                logger.error("Skill execution failed: %s", e, exc_info=True)
                await message.reply_text(messages.PROCESSING_FAILED.format(error=_fmt_error(e)))
            finally:
                # DGN-911 FATAL fix: drain debounce buffer on every SDK turn
                # completion so the "interrupted turn drains it" invariant holds
                # for all turn types, not only _dispatch_text_turn and fastpath.
                await self._drain_pending_texts(user_id)

        async def on_overflow() -> None:
            await message.reply_text(messages.QUEUE_BUSY)

        await self._enqueue_user_task(
            user_id, run_task, on_overflow, chat_id=chat.id if chat else user_id
        )

    # --- session listing (reads Claude conversation JSONL) ---

    @property
    def _conversations_dir(self) -> Path:
        project_dir_name = re.sub(r"[^A-Za-z0-9]", "-", str(PROJECT_ROOT))
        return Path.home() / ".claude" / "projects" / project_dir_name

    def _list_sessions(self, limit: int = 10):
        import json

        conv_dir = self._conversations_dir
        if not conv_dir.exists():
            return []
        files = sorted(conv_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        results = []
        for f in files[: limit * 2]:
            first = None
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        d = json.loads(line)
                        if d.get("type") != "user":
                            continue
                        msg = d.get("message", {})
                        if msg.get("role") != "user":
                            continue
                        content = msg.get("content", "")
                        text = ""
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c["text"]
                                    break
                        elif isinstance(content, str):
                            text = content
                        text = text.strip()
                        if text and not text.startswith("<"):
                            first = text[:80]
                            break
            except Exception:
                continue
            if first:
                results.append((f.stem, first, f.stat().st_mtime))
            if len(results) >= limit:
                break
        return results

    # --- text / voice handlers ---

    async def _handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._check_access(update):
            return
        message = update.message
        if not message or not message.text:
            return
        user_id = update.effective_user.id
        text = message.text
        session = await session_manager.get_session(user_id)

        resume_list = session.get("resume_list")
        if resume_list and text.strip().isdigit():
            idx = int(text.strip()) - 1
            if 0 <= idx < len(resume_list):
                sid, msg = resume_list[idx]
                session["session_id"] = sid
                session["new_session"] = False
                session.pop("resume_list", None)
                await session_manager.update_session(user_id, session)
                self._runtime_active_sessions.add(user_id)
                await message.reply_text(messages.RESUME_SWITCHED.format(msg=msg))
                return
            await message.reply_text(messages.RESUME_INVALID_NUMBER)
            return
        if resume_list:
            session.pop("resume_list", None)
            await session_manager.update_session(user_id, session)

        approval_pending = await self._maybe_capture_outside_approval(user_id, text)

        # DGN-902: reply-to routing for /btw fork continuations. If the
        # incoming message is a reply to a known 💭 fork bubble, route it into
        # that fork's isolated session instead of the main history. The check
        # is intentionally before fast-path and coalescing -- a fork reply
        # must never land in the main session queue.
        if await self._maybe_route_btw_reply(user_id, text, message, update):
            return

        # DGN-801: deterministic fast-path. On opted-in instances a short
        # numeric-looking message is offered to the domain handler BEFORE an
        # SDK turn spawns; a claimed message is fully handled (or model-fallback
        # dispatched) inside the fast-path task, so stop here. Default OFF:
        # without FASTPATH_HANDLER this is a no-op boolean check.
        # B4 (Option 2): skip fast-path entirely while an outside-approval
        # prompt is live -- a bare numeric token ("1") would otherwise be
        # double-consumed (approval grant AND fast-path set). The approval path
        # already handled it; hand this turn to the normal SDK path.
        if not approval_pending and await self._try_fastpath(user_id, text, update):
            return

        # DGN-911: regular text goes through the default in-flight policy. If a
        # turn is already running for this user, this message opens/extends the
        # debounce window and then soft-interrupts into a new merged turn;
        # explicit /queue keeps the DGN-616 merge-without-interrupt behavior.
        await self._enqueue_text_task(user_id, text, self._message_ts(message), update)

    async def _handle_voice_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._check_access(update):
            return
        message = update.message
        if not message or not message.voice:
            return
        user_id = update.effective_user.id
        voice = message.voice

        # DGN-616: transcribe the voice note here (download I/O is independent of
        # the SDK turn and safe to run while a turn is in flight), then route the
        # transcript through the coalescing path so it merges with any in-flight
        # turn instead of dropping. Attachment-specific failures reply inline as
        # before; a crash in the merged turn is still caught by the DGN-163 net
        # inside _dispatch_text_turn (voice failure_message carried via update).
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        cleanup_paths: List[Path] = []
        try:
            if voice.duration and voice.duration > config.max_voice_duration:
                await message.reply_text(
                    messages.VOICE_TOO_LONG.format(seconds=config.max_voice_duration)
                )
                return
            ext = self._voice_extension(getattr(voice, "mime_type", None))
            source_path = self._audio_dir / f"{user_id}_{int(time.time() * 1000)}.{ext}"
            cleanup_paths.append(source_path)
            try:
                await self._download_file(voice.file_id, source_path)
            except Exception as e:
                logger.error("Voice download failed for user %s: %s", user_id, e)
                await self._turn_death_notice(
                    user_id,
                    update.effective_chat.id if update.effective_chat else user_id,
                    messages.TURN_FAILED_VOICE,
                )
                return
            try:
                audio_path = await self._audio_processor.prepare_for_whisper(
                    source_path, cleanup_paths
                )
            except Exception as e:
                logger.error("Voice conversion failed for user %s: %s", user_id, e)
                await message.reply_text(messages.VOICE_CONVERT_FAILED)
                return
            if self._transcriber is None:
                self._transcriber = self._build_transcriber()
            try:
                self._transcriber.ensure_available()
            except RuntimeError as e:
                logger.error("Local whisper unavailable: %s", e)
                await message.reply_text(messages.VOICE_UNAVAILABLE)
                return
            from bridge.voice import EmptyTranscriptionError, TranscriptionError

            try:
                text = await self._transcriber.transcribe_audio(
                    audio_path, duration_seconds=voice.duration
                )
            except EmptyTranscriptionError:
                await message.reply_text(messages.VOICE_EMPTY)
                return
            except TranscriptionError as e:
                logger.error("Transcription failed for user %s: %s", user_id, e)
                await message.reply_text(messages.VOICE_TRANSCRIBE_FAILED)
                return
            # Surface the transcript preview before enqueueing; the turn body no
            # longer carries voice_input_preview since it may run merged later.
            preview = str(text).strip()
            if preview:
                try:
                    await message.reply_text(f"\U0001f399️ {preview}")
                except Exception:
                    pass
            await self._enqueue_text_task(
                user_id,
                text,
                self._message_ts(message),
                update,
                failure_message=messages.TURN_FAILED_VOICE,
            )
        finally:
            await self._audio_processor.cleanup_audio_files(cleanup_paths)

    async def _handle_photo_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        # inbound image. Save to image dir, then hand the path to the
        # multimodal brain to open with its Read tool (mirrors the legacy bridge).
        if not await self._check_access(update):
            return
        message = update.message
        if not message or not message.photo:
            return
        user_id = update.effective_user.id
        # Telegram sends a photo as ascending-resolution sizes; last = largest.
        photo = message.photo[-1]
        caption = (message.caption or "").strip()
        mgid = message.media_group_id

        if not mgid:
            # Single photo: dispatch immediately (legacy behavior).
            await self._dispatch_photo_task(update, user_id, [photo.file_id], caption)
            return

        # album. Buffer same-group photos and debounce-flush as one task.
        async with self._media_group_lock:
            group = self._media_groups.get(mgid)
            if group is None:
                group = {
                    "user_id": user_id,
                    "update": update,
                    "message": message,
                    "file_ids": [],
                    "caption": "",
                    "timer": None,
                }
                self._media_groups[mgid] = group
            group["file_ids"].append(photo.file_id)
            # Telegram puts the caption only on the first album item; keep first seen.
            if caption and not group["caption"]:
                group["caption"] = caption
            if group["timer"] is not None:
                group["timer"].cancel()
            group["timer"] = asyncio.create_task(self._flush_media_group_after(mgid))

    async def _flush_media_group_after(self, mgid: str) -> None:
        # wait out the debounce; a newer photo for this group cancels us
        # and starts a fresh timer. When we win, pop and dispatch the whole album.
        try:
            await asyncio.sleep(MEDIA_GROUP_DEBOUNCE)
        except asyncio.CancelledError:
            return
        async with self._media_group_lock:
            group = self._media_groups.pop(mgid, None)
        if not group or not group["file_ids"]:
            return
        # DGN-163: this runs as a bare debounce task, so an exception escaping the
        # dispatch would be swallowed by asyncio (silent album loss). Guard it and
        # route to the safety-net notice. The per-download failures are handled
        # inside the enqueued run_task; this catches the rarer pre-enqueue crash.
        try:
            await self._dispatch_photo_task(
                group["update"], group["user_id"], group["file_ids"], group["caption"]
            )
        except Exception as e:
            logger.error("Media-group dispatch failed for user %s: %s", group["user_id"], e)
            upd = group["update"]
            chat_id = upd.effective_chat.id if upd.effective_chat else group["user_id"]
            await self._turn_death_notice(
                group["user_id"], chat_id, messages.TURN_FAILED_PHOTO
            )

    async def _dispatch_photo_task(
        self, update: Update, user_id: int, file_ids: List[str], caption: str
    ) -> None:
        # download 1..N photos (single or album) and hand all paths to the
        # multimodal brain in ONE turn, so an album is read as a single message.
        # DGN-616: download I/O runs here (independent of the SDK turn) and the
        # resulting prompt (path references + caption) routes through the
        # coalescing path so a photo arriving mid-turn merges instead of dropping.
        message = update.message
        self._image_dir.mkdir(parents=True, exist_ok=True)
        paths: List[Path] = []
        last_exc: Optional[Exception] = None
        for idx, fid in enumerate(file_ids):
            dest = self._image_dir / f"{user_id}_{int(time.time() * 1000)}_{idx}.jpg"
            try:
                await self._download_file(fid, dest)
            except Exception as e:
                # Per-photo miss in an album is tolerated (best-effort); the
                # turn still proceeds with whatever downloaded. Only a total
                # wipeout (no paths) is a turn-death, handled below.
                logger.error("Photo download failed for user %s: %s", user_id, e)
                last_exc = e
                continue
            paths.append(dest)
        if not paths:
            # DGN-082/163: every photo failed after retries -> emit the
            # photo-specific notice. No silent loss.
            logger.error(
                "Photo download produced no paths for user %s: %s", user_id, last_exc
            )
            await self._turn_death_notice(
                user_id,
                update.effective_chat.id if update.effective_chat else user_id,
                messages.TURN_FAILED_PHOTO,
            )
            return
        if len(paths) == 1:
            lines = [
                messages.PHOTO_PROMPT_SINGLE,
                messages.PHOTO_PROMPT_PATH.format(path=paths[0]),
            ]
        else:
            lines = [messages.PHOTO_PROMPT_ALBUM.format(count=len(paths))]
            for i, p in enumerate(paths, 1):
                lines.append(messages.PHOTO_PROMPT_ALBUM_PATH.format(index=i, path=p))
        if caption:
            lines.append(messages.USER_CAPTION.format(caption=caption))
        await self._enqueue_text_task(
            user_id,
            "\n".join(lines),
            self._message_ts(message),
            update,
            failure_message=messages.TURN_FAILED_PHOTO,
        )

    async def _handle_document_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        # inbound file/document (PDF, code, uncompressed image, ...).
        if not await self._check_access(update):
            return
        message = update.message
        if not message or not message.document:
            return
        user_id = update.effective_user.id
        doc = message.document
        caption = (message.caption or "").strip()

        # DGN-616: download the file here (independent of the SDK turn) then route
        # the resulting prompt through the coalescing path so a doc arriving
        # mid-turn merges instead of dropping.
        self._inbox_dir.mkdir(parents=True, exist_ok=True)
        # Preserve the original filename; prefix user+ts to avoid clashes.
        safe_name = Path(doc.file_name).name if doc.file_name else "file"
        dest = self._inbox_dir / f"{user_id}_{int(time.time() * 1000)}_{safe_name}"
        try:
            await self._download_file(doc.file_id, dest)
        except Exception as e:
            logger.error("Document download failed for user %s: %s", user_id, e)
            await self._turn_death_notice(
                user_id,
                update.effective_chat.id if update.effective_chat else user_id,
                messages.TURN_FAILED_DOCUMENT,
            )
            return
        lines = [
            messages.DOC_PROMPT,
            messages.DOC_PROMPT_PATH.format(path=dest),
        ]
        if caption:
            lines.append(messages.USER_CAPTION.format(caption=caption))
        await self._enqueue_text_task(
            user_id,
            "\n".join(lines),
            self._message_ts(message),
            update,
            failure_message=messages.TURN_FAILED_DOCUMENT,
        )

    @staticmethod
    def _voice_extension(mime_type: Optional[str]) -> str:
        if not mime_type:
            return "ogg"
        m = mime_type.lower()
        if "amr" in m:
            return "amr"
        if "mp3" in m or "mpeg" in m:
            return "mp3"
        if "wav" in m:
            return "wav"
        if "m4a" in m or "mp4" in m:
            return "m4a"
        return "ogg"

    async def _download_file(
        self, file_id: str, destination: Path, read_timeout: float = 60.0
    ) -> None:
        # file downloads need a longer read_timeout than the shared
        # request default (10s). When the Telegram API is briefly slow, a 10s
        # read_timeout drops inbound photos/documents/voice. 60s rides it out.
        # DGN-082: retry-with-exponential-backoff for transient errors
        # (Telegram 5xx / connection reset / TLS blip). 3 attempts, 1s/3s/9s
        # backoff (sleep after attempt 1 = 1s, after attempt 2 = 3s; the 9s slot
        # is reserved should attempts grow). Mirrors _send_guaranteed. On final
        # failure this raises -- the caller's enqueue safety net (DGN-163) then
        # emits the media-specific turn-death notice, so no inbound is lost.
        _max_attempts = 3
        _backoff_delays = [1.0, 3.0, 9.0]
        last_exc: Exception
        for _attempt in range(1, _max_attempts + 1):
            try:
                tfile = await self.application.bot.get_file(
                    file_id, read_timeout=read_timeout
                )
                await tfile.download_to_drive(
                    custom_path=str(destination), read_timeout=read_timeout
                )
                return
            except Exception as e:
                last_exc = e
                logger.warning(
                    "file download attempt %d/%d failed (file_id=%s): %s",
                    _attempt, _max_attempts, file_id, e,
                )
                if _attempt < _max_attempts:
                    await asyncio.sleep(_backoff_delays[_attempt - 1])
        logger.error(
            "file download FAILED after %d attempts (file_id=%s): %s",
            _max_attempts, file_id, last_exc,
        )
        raise last_exc

    async def _process_user_message_text(
        self,
        update: Update,
        user_id: int,
        text: str,
        voice_input_preview: Optional[str] = None,
    ) -> None:
        message = update.message
        chat = update.effective_chat
        app = self.application
        session = await session_manager.get_session(user_id)
        message_ts = self._message_ts(message)
        try:
            await message.chat.send_action(action="typing")
        except Exception:
            pass
        try:
            new_session = session.pop("new_session", False)
            if await session_manager.should_start_new_session(user_id, now=message_ts):
                session["session_id"] = None
                self._runtime_active_sessions.discard(user_id)
                new_session = True
            # DGN-162: resolve the effective session model through the chain
            # (override > persisted last-session > settings > default) and pin
            # it onto the session so every downstream call sees a concrete,
            # persisted value. A fresh session (no override) thus inherits the
            # model the last session actually used. Pinning the model does NOT
            # by itself restart the conversation -- it only persists the choice.
            resolved_model = self._get_real_model(session)
            model_pinned = session.get("model") != resolved_model
            if model_pinned:
                session["model"] = resolved_model
            if new_session or model_pinned:
                await session_manager.update_session(user_id, session)
            await session_manager.set_last_user_message_at(user_id, message_ts)

            # DGN-162: one user-visible notice per bridge start if a persisted
            # model had to be rejected (corrupt / unknown). Never per-message.
            notice = model_state.take_start_notice()
            if notice:
                try:
                    await message.reply_text(messages.MODEL_STATE_FALLBACK)
                except Exception:
                    pass

            # Surface voice transcript as its own message before the streamed bubble.
            if voice_input_preview:
                preview = str(voice_input_preview).strip()
                if preview:
                    try:
                        await message.reply_text(f"\U0001f399️ {preview}")
                    except Exception:
                        pass

            response = await sdk_bridge.process_message(
                user_message=text,
                user_id=user_id,
                chat_id=chat.id,
                session_id=self._effective_session_id(user_id, session),
                model=session.get("model"),
                new_session=new_session,
                permission_callback=self._permission_callback,
                typing_callback=lambda: message.chat.send_action(action="typing"),
                bot=app.bot,
                proactive_push=self._proactive_push,
            )

            async def resume_caller(cont: str) -> ChatResponse:
                sess = await session_manager.get_session(user_id)
                return await sdk_bridge.process_message(
                    user_message=cont,
                    user_id=user_id,
                    chat_id=chat.id,
                    session_id=self._effective_session_id(user_id, sess),
                    model=sess.get("model"),
                    permission_callback=self._permission_callback,
                    typing_callback=lambda: message.chat.send_action(action="typing"),
                    bot=app.bot,
                    proactive_push=self._proactive_push,
                )

            response = await self._auto_resume_loop(
                user_id=user_id, chat_id=chat.id, response=response, resume_caller=resume_caller
            )
            if getattr(response, "timed_out", False):
                await self._send_resume_notice(chat_id=chat.id, user_id=user_id, response=response)
                return
            await self._save_session_id(user_id, response)
            # DGN-686 MAJOR-1: a transient is_error result auto-retries ONCE
            # here in the caller context (never inside the reader loop). Only
            # if the single retry also fails do we surface the notice + button.
            if getattr(response, "error_kind", None) == "transient":
                logger.warning(
                    "transient is_error for user %s -- auto-retrying once", user_id
                )
                # resume_caller re-runs an arbitrary message via process_message;
                # feed it the ORIGINAL text for a single automatic retry.
                response = await resume_caller(text)
                await self._save_session_id(user_id, response)
            # DGN-686: an is_error result offering a retry gets the LOCKED
            # notice plus a [retry] button that re-runs the same user message.
            if getattr(response, "retry_offer", False):
                await self._send_retry_notice(
                    chat_id=chat.id, user_id=user_id,
                    notice=response.content, user_message=text,
                )
                return
            await self._reply_smart(
                message,
                response.content,
                force_options=response.has_options,
                streamed=response.streamed,
                draft_message_ids=response.draft_message_ids,
                classifier_injected=getattr(response, "options_classifier_injected", False),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Error in chat for user %s: %s", user_id, e, exc_info=True)
            await message.reply_text(messages.GENERIC_ERROR.format(error=_fmt_error(e)))

    @staticmethod
    def _message_ts(message) -> datetime:
        ts = getattr(message, "date", None)
        if ts is None:
            return datetime.now(timezone.utc)
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    # --- timeout / resume (A4) ---

    async def _resolve_resume_sid(self, user_id: int, response) -> Optional[str]:
        """B2: bridge may return None resume sid when the live stream state is lost
        at timeout. Fall back to the session_id persisted by session_manager so
        auto-resume actually fires instead of degrading to silence."""
        sid = getattr(response, "resume_session_id", None)
        if sid:
            return sid
        try:
            sess = await session_manager.get_session(user_id)
            return sess.get("session_id")
        except Exception as e:
            logger.error("resume sid fallback failed for user %s: %s", user_id, e)
            return None

    async def _send_guaranteed(
        self, chat_id: int, text: str, *, reply_markup=None, retries: int = 3
    ) -> bool:
        """B3: at timeout the Telegram HTTP path can be transiently down, and a single
        unguarded send gets swallowed -> total silence. Retry with backoff so at least
        one user-facing message lands; log loudly (never swallow) on final failure."""
        app = self.application
        delay = 1.0
        for i in range(retries):
            try:
                await app.bot.send_message(chat_id, text, reply_markup=reply_markup)
                return True
            except Exception as e:
                logger.warning(
                    "guaranteed send attempt %d/%d failed for chat %s: %s",
                    i + 1, retries, chat_id, e,
                )
                if i < retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
        logger.error(
            "guaranteed send FAILED after %d attempts for chat %s (text head: %r)",
            retries, chat_id, text[:60],
        )
        return False

    async def _auto_resume_loop(self, *, user_id, chat_id, response, resume_caller):
        app = self.application
        attempt = 0
        notified = False
        while (
            getattr(response, "timed_out", False)
            and AUTO_RESUME
            and attempt < AUTO_RESUME_MAX
        ):
            resume_sid = await self._resolve_resume_sid(user_id, response)
            if not resume_sid:
                break
            attempt += 1
            if not notified:
                notified = True
                await self._send_guaranteed(chat_id, messages.STILL_WORKING)
            session = await session_manager.get_session(user_id)
            session["session_id"] = resume_sid
            session["new_session"] = False
            await session_manager.update_session(user_id, session)
            self._runtime_active_sessions.add(user_id)
            try:
                await app.bot.send_chat_action(chat_id, action="typing")
            except Exception:
                pass
            response = await resume_caller(messages.RESUME_CONTINUATION_PROMPT)
        return response

    async def _send_resume_notice(self, *, chat_id: int, user_id: int, response: ChatResponse) -> None:
        resume_sid = await self._resolve_resume_sid(user_id, response)
        if not resume_sid:
            await self._send_guaranteed(chat_id, messages.TIMEOUT_NO_RESUME)
            return
        token = f"{int(time.time())}"
        session = await session_manager.get_session(user_id)
        session["pending_resume"] = {"token": token, "session_id": resume_sid}
        await session_manager.update_session(user_id, session)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(messages.TAP_TO_CONTINUE, callback_data=f"resume:{token}")]]
        )
        await self._send_guaranteed(chat_id, messages.TIMEOUT_TAP_NOTICE, reply_markup=kb)

    async def _send_retry_notice(
        self, *, chat_id: int, user_id: int, notice: str, user_message: str
    ) -> None:
        """DGN-686: deliver a failure notice with a [retry] button.

        Stores the original user message under a one-shot token (same pattern
        as pending_resume) so the retry callback can re-run it verbatim.
        """
        token = f"{int(time.time())}"
        session = await session_manager.get_session(user_id)
        session["pending_retry"] = {"token": token, "user_message": user_message}
        await session_manager.update_session(user_id, session)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                messages.ERROR_RETRY_BUTTON, callback_data=f"retry:{token}"
            )]]
        )
        await self._send_guaranteed(chat_id, notice, reply_markup=kb)

    # --- send logic ---

    def _reply_link_id(self, message) -> Optional[int]:
        """DGN-555: reply-link policy for a turn's final response.

        Returns the triggering user message_id when the final response should
        go out as a Telegram reply -- (a) interleave: a newer user message
        arrived in this chat after the trigger, or (b) latency: the response
        fires more than REPLY_LINK_LATENCY_S seconds after the trigger arrived.
        Otherwise (including an unavailable trigger id) returns None: plain
        send. Tight back-and-forth thus stays link-free.
        """
        if not REPLY_LINK_ENABLED:
            return None
        trigger_id = getattr(message, "message_id", None)
        if not isinstance(trigger_id, int):
            return None
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        last_seen = self._last_incoming_mid.get(chat_id)
        if isinstance(last_seen, int) and last_seen > trigger_id:
            return trigger_id
        age = (datetime.now(timezone.utc) - self._message_ts(message)).total_seconds()
        if age > REPLY_LINK_LATENCY_S:
            return trigger_id
        return None

    async def _reply_smart(
        self,
        message,
        content: str,
        force_options: bool = False,
        streamed: bool = False,
        draft_message_ids: Optional[List[int]] = None,
        classifier_injected: bool = False,
    ) -> None:
        display, has_marker = strip_options_marker(content)
        display, _ = strip_send_markers(display)
        # DGN-376: link previews default OFF; a link_preview:: line opts in.
        display, preview = strip_link_preview_marker(display)
        # DGN-159: last-mile scrub of leaked tool-call markup on the non-streamed
        # / finalized send path (streamed drafts are scrubbed in strip_display_markers).
        display = strip_toolcall_markup(display)
        # DGN-665: an AGENT-AUTHORED [[OPTIONS]] marker renders the choices as
        # buttons only -- drop the consumed numbered run from the display body,
        # but only when the run extracts (buttons will build). On extraction
        # failure the list stays so the user is never left choice-less. A
        # classifier-INJECTED marker (Haiku auto-button) renders buttons but
        # keeps the body list per owner lock.
        authored_marker = has_marker and not classifier_injected
        consumed_options: Optional[List[str]] = None
        if force_options and authored_marker:
            stripped_display, consumed_options = strip_consumed_options(display)
            if consumed_options:
                display = stripped_display
        # DGN-555: selective reply-linking of the final response body.
        reply_to = self._reply_link_id(message)
        has_code = "```" in display
        if not streamed:
            # DGN-665: a list-only body strips to whitespace -- skip the empty
            # bubble; the SELECT_PROMPT + buttons message carries the turn.
            if display.strip():
                await self._send_text_body(message, display, preview, reply_to=reply_to)
        elif has_code and (force_options or draft_message_ids):
            # DGN-085 (comment refreshed in DGN-376 v1.1, finding m4): when code
            # blocks coexist with [[OPTIONS]] buttons or streamed drafts, the
            # plain-text drafts are deleted and the body is re-sent through
            # _send_text_body (HTML code segments + converted prose) so the
            # split is enforced at the bridge layer regardless of agent
            # discipline. Also covers the pre-existing streamed+code case.
            bot = message.get_bot()
            chat_id = message.chat.id
            for mid in (draft_message_ids or []):
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception as e:
                    logger.warning("Failed to delete streamed draft %s: %s", mid, e)
            await self._send_text_body(message, display, preview, reply_to=reply_to)
        elif consumed_options and draft_message_ids:
            # DGN-665: the terminal AssistantMessage live-streams into drafts, so
            # a normal decision-ask is streamed=True with the (unstripped) list
            # baked into the draft. Delete the draft(s) and re-send the stripped
            # body so the list shows only as buttons; skip the send when the body
            # strips to whitespace-only (the SELECT_PROMPT+buttons carries it).
            bot = message.get_bot()
            chat_id = message.chat.id
            for mid in draft_message_ids:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception as e:
                    logger.warning("Failed to delete streamed draft %s: %s", mid, e)
            if display.strip():
                await self._send_text_body(message, display, preview, reply_to=reply_to)
        elif draft_message_ids and display.strip():
            # DGN-376 v1.1 (M1): streamed prose-only reply. The final draft
            # bubble streamed as plain text; re-render it in place as HTML via
            # the same converter the non-streamed path uses. Fallback (multi-
            # bubble reply or edit failure): delete the drafts and re-send
            # through the converting body path so the message is never lost.
            # DGN-555: a reply link cannot ride an in-place edit, so when the
            # link policy fires the edit is skipped and the same delete +
            # re-send fallback delivers the body as a fresh, linked message.
            bot = message.get_bot()
            chat_id = message.chat.id
            if reply_to is not None or not await self._edit_streamed_prose_html(
                bot, chat_id, display, preview, draft_message_ids
            ):
                for mid in draft_message_ids:
                    try:
                        await bot.delete_message(chat_id, mid)
                    except Exception as e:
                        logger.warning("Failed to delete streamed draft %s: %s", mid, e)
                await self._send_text_body(message, display, preview, reply_to=reply_to)
        await self._send_content_artifacts(
            message, content, force_options, options=consumed_options
        )

    async def _send_text_body(
        self,
        message,
        content: str,
        preview: bool = False,
        reply_to: Optional[int] = None,
    ) -> None:
        # DGN-376: preview=True (link_preview:: opt-in) restores Telegram's
        # default preview; otherwise previews are suppressed.
        lp = None if preview else LINK_PREVIEW_OFF
        # DGN-555: only the FIRST sent part of the final body carries the reply
        # link; every subsequent part goes out plain.
        link_pending = reply_to is not None
        for segment, is_code, lang in split_into_segments(content):
            parts = [p for p in split_text(segment) if p.strip()]
            if is_code:
                rendered_parts = [code_segment_html(p, lang) for p in parts]
            else:
                # DGN-376: prose goes out as Telegram HTML; markdown the
                # agents emit is converted, everything else is escaped so
                # stray _ * [ ] < > never mangle the message.
                rendered_parts = [markdown_to_telegram_html(p) for p in parts]
            # DGN-891: a tag span can straddle a split_text boundary; close
            # open tags at each chunk end and re-open them on the next chunk
            # so every send is independently valid HTML (no-op when balanced).
            rendered_parts = rebalance_html_chunks(rendered_parts)
            for rendered in rendered_parts:
                if link_pending:
                    link_pending = False
                    if await self._try_send_linked(message, rendered, lp, reply_to):
                        continue
                    # DGN-555: linked send rejected -> degrade to the plain
                    # (unlinked) sends below; a reply link never fails a turn.
                try:
                    await message.reply_text(
                        rendered,
                        parse_mode="HTML",
                        link_preview_options=lp,
                    )
                except Exception:
                    # DGN-376 v1.1 (m1) + DGN-891: tag-STRIPPED readable
                    # fallback (never raw markdown source, never leaked
                    # tags); keeps the preview suppression of the send it
                    # replaces.
                    await message.reply_text(
                        html_to_plain_text(rendered), link_preview_options=lp
                    )

    async def _try_send_linked(self, message, rendered: str, lp, reply_to: int) -> bool:
        """DGN-555: attempt the reply-linked send of the first body part.

        allow_sending_without_reply lets Telegram itself degrade to a plain
        send when the trigger message is gone; any other rejection returns
        False so the caller re-sends unlinked. Never raises.
        """
        try:
            await message.reply_text(
                rendered,
                parse_mode="HTML",
                link_preview_options=lp,
                reply_parameters=ReplyParameters(
                    message_id=reply_to, allow_sending_without_reply=True
                ),
            )
            return True
        except Exception as e:
            logger.warning("Reply-linked send failed (mid=%s): %s", reply_to, e)
            return False

    async def _edit_streamed_prose_html(
        self,
        bot,
        chat_id: int,
        display: str,
        preview: bool,
        draft_message_ids: List[int],
    ) -> bool:
        """DGN-376 v1.1 (M1): re-render a finalized streamed prose draft as HTML.

        The draft bubble streamed as plain text (unchanged behavior); once the
        turn is final, edit that single bubble in place with the converted HTML
        (markdown_to_telegram_html -- the exact converter the non-streamed path
        uses). Returns True when the draft is already correct or the edit
        succeeded; False tells the caller to fall back to the existing
        delete-drafts + convert-resend path (multi-bubble replies, edit
        failures), which never drops the message.
        """
        if len(draft_message_ids) != 1:
            return False
        if len(split_text(display)) != 1:
            return False
        # DGN-891: balance guard -- no-op on balanced (or tag-free) output,
        # so the converted == display no-op check below is unaffected.
        converted = balance_telegram_html(markdown_to_telegram_html(display))
        if converted == display and not preview:
            # No markdown and nothing to escape: the plain draft already
            # renders exactly this text, so skip the no-op edit (Telegram
            # rejects it with "message is not modified").
            return True
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=draft_message_ids[0],
                text=converted,
                parse_mode="HTML",
                link_preview_options=None if preview else LINK_PREVIEW_OFF,
            )
            return True
        except telegram.error.BadRequest as e:
            if "not modified" in str(e).lower():
                # Escaping-only delta (e.g. & -> &amp;) parses back to the same
                # visible text: the draft is already correct.
                return True
            logger.warning(
                "HTML finalize edit failed for draft %s: %s",
                draft_message_ids[0],
                e,
            )
            return False
        except Exception as e:
            logger.warning(
                "HTML finalize edit failed for draft %s: %s",
                draft_message_ids[0],
                e,
            )
            return False

    async def _send_content_artifacts(
        self,
        message,
        content: str,
        force_options: bool,
        options: Optional[List[str]] = None,
    ) -> None:
        resolved = resolve_send_paths(content, PROJECT_ROOT)
        in_root, outside = split_paths_by_scope(resolved, PROJECT_ROOT)
        await self._send_file_paths(message.chat.id, in_root)
        if outside:
            await self._prompt_outside_file_confirmation(
                message.chat.id, message.chat.id, outside
            )
        if force_options:
            clean, has_marker = strip_options_marker(content)
            if has_marker:
                # DGN-665: prefer the caller's consumed-run extraction so the
                # body strip and the buttons always come from the same run;
                # fall back to a local extraction when it was not computed.
                if options is None:
                    _, options = strip_consumed_options(clean)
                kb = build_option_keyboard(options)
                if kb:
                    await message.reply_text(messages.SELECT_PROMPT, reply_markup=kb)

    async def _proactive_push(
        self,
        chat_id: int,
        content: str,
        has_options: bool,
        classifier_injected: bool = False,
    ) -> None:
        """Deliver main-agent output that arrived with no pending request.

        Invoked by the bridge when the agent emits a turn (e.g. a background-task
        completion report) that the request-response path would otherwise drop.
        Reuses _send_smart so formatting and [[OPTIONS]] buttons behave the same
        as a normal reply. DGN-665: classifier_injected forwards marker
        provenance so a classifier-injected list keeps its body text.
        """
        try:
            await self._send_smart(
                chat_id,
                content,
                force_options=has_options,
                classifier_injected=classifier_injected,
            )
        except Exception as e:
            logger.error("Proactive push delivery failed for chat %s: %s", chat_id, e)

    async def _notify_outage_recovered(self, down_seconds: int) -> None:
        """Tell the user the bot was offline, after the watchdog reconnects.

        Both agents share one machine+network, so a network outage silences them
        with no trace. On recovery we push a one-line notice to the owner chat(s)
        so the silence is explained. In a private chat the chat_id equals
        the user_id, so allowed_user_ids is the right delivery target.
        """
        # outage-recovered user notice disabled per owner request (2026-06-30):
        # too noisy on flaky networks. Recovery is still logged for visibility.
        # DGN-851: the dormant notice copy (messages.OUTAGE_RECOVERED + the
        # i18n outage_recovered entries) was removed as dead weight;
        # re-enabling means restoring the i18n key (ko/en) and a real send
        # here, behind a config knob.
        minutes = max(1, round(down_seconds / 60))
        logger.info("Outage recovered after ~%d min (user notice disabled)", minutes)
        return

    async def _send_smart(
        self,
        chat_id: int,
        content: str,
        force_options: bool = False,
        streamed: bool = False,
        draft_message_ids: Optional[List[int]] = None,
        classifier_injected: bool = False,
    ) -> None:
        # D2 (DGN-515): guard shutdown race -- application is set to None in
        # _graceful_shutdown; queued run_task closures may call this after that.
        if self.application is None:
            raise RuntimeError("Bot application already stopped")
        bot = self.application.bot
        display, has_marker = strip_options_marker(content)
        display, _ = strip_send_markers(display)
        # DGN-376: link previews default OFF; a link_preview:: line opts in.
        display, preview = strip_link_preview_marker(display)
        # DGN-159: last-mile scrub of leaked tool-call markup (proactive / option /
        # resume send path); streamed drafts are scrubbed in strip_display_markers.
        display = strip_toolcall_markup(display)
        # DGN-665: same as _reply_smart -- an AGENT-AUTHORED marker renders the
        # choices as buttons only, so drop the consumed numbered run from the
        # body when it extracts; on extraction failure the list stays. A
        # classifier-injected marker keeps the body list.
        authored_marker = has_marker and not classifier_injected
        options: List[str] = []
        if force_options and authored_marker:
            stripped_display, options = strip_consumed_options(display)
            if options:
                display = stripped_display
        has_code = "```" in display
        if not streamed:
            # DGN-665: a list-only body strips to whitespace -- skip the empty
            # bubble; the SELECT_PROMPT + buttons message carries the turn.
            if display.strip():
                await self._send_text_body_chat(chat_id, display, preview)
        elif has_code and (force_options or draft_message_ids):
            # DGN-085: same backstop as _reply_smart -- code+options coexistence
            # forces clean re-send via HTML segments so code/tables render correctly.
            for mid in (draft_message_ids or []):
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception as e:
                    logger.warning("Failed to delete streamed draft %s: %s", mid, e)
            await self._send_text_body_chat(chat_id, display, preview)
        elif options and draft_message_ids:
            # DGN-665: streamed decision-ask -- the draft baked in the unstripped
            # list. Delete the draft(s) and re-send the stripped body so the list
            # shows only as buttons; skip the send when the body is whitespace.
            for mid in draft_message_ids:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception as e:
                    logger.warning("Failed to delete streamed draft %s: %s", mid, e)
            if display.strip():
                await self._send_text_body_chat(chat_id, display, preview)
        elif draft_message_ids and display.strip():
            # DGN-376 v1.1 (M1): same streamed prose finalize as _reply_smart --
            # edit the single draft bubble to HTML in place; fall back to
            # delete + convert-resend so the message is never lost.
            if not await self._edit_streamed_prose_html(
                bot, chat_id, display, preview, draft_message_ids
            ):
                for mid in draft_message_ids:
                    try:
                        await bot.delete_message(chat_id, mid)
                    except Exception as e:
                        logger.warning("Failed to delete streamed draft %s: %s", mid, e)
                await self._send_text_body_chat(chat_id, display, preview)
        resolved = resolve_send_paths(content, PROJECT_ROOT)
        in_root, _ = split_paths_by_scope(resolved, PROJECT_ROOT)
        await self._send_file_paths(chat_id, in_root)
        if force_options and has_marker:
            # DGN-665: buttons render for ANY marker (authored or classifier-
            # injected). Reuse the consumed-run options when the body was
            # stripped; otherwise (classifier-injected, or a non-adjacent/failed
            # run that kept the body) extract the run just for the buttons so the
            # choices still appear -- the body list stays intact in that case.
            button_options = options or extract_options(display)
            kb = build_option_keyboard(button_options)
            if kb:
                await bot.send_message(chat_id, messages.SELECT_PROMPT, reply_markup=kb)

    async def _send_text_body_chat(self, chat_id: int, content: str, preview: bool = False) -> None:
        bot = self.application.bot
        # DGN-376: preview=True (link_preview:: opt-in) restores Telegram's
        # default preview; otherwise previews are suppressed.
        lp = None if preview else LINK_PREVIEW_OFF
        for segment, is_code, lang in split_into_segments(content):
            parts = [p for p in split_text(segment) if p.strip()]
            if is_code:
                rendered_parts = [code_segment_html(p, lang) for p in parts]
            else:
                # DGN-376: prose goes out as Telegram HTML; markdown the
                # agents emit is converted, everything else is escaped so
                # stray _ * [ ] < > never mangle the message.
                rendered_parts = [markdown_to_telegram_html(p) for p in parts]
            # DGN-891: a tag span can straddle a split_text boundary; close
            # open tags at each chunk end and re-open them on the next chunk
            # so every send is independently valid HTML (no-op when balanced).
            rendered_parts = rebalance_html_chunks(rendered_parts)
            for rendered in rendered_parts:
                try:
                    await bot.send_message(
                        chat_id,
                        rendered,
                        parse_mode="HTML",
                        link_preview_options=lp,
                    )
                except Exception:
                    # DGN-376 v1.1 (m1) + DGN-891: tag-STRIPPED readable
                    # fallback (never raw markdown source, never leaked
                    # tags); keeps the preview suppression of the send it
                    # replaces.
                    await bot.send_message(
                        chat_id, html_to_plain_text(rendered), link_preview_options=lp
                    )

    async def _send_file_paths(self, chat_id: int, paths: List[Path]) -> None:
        bot = self.application.bot
        for p in paths:
            is_image = p.suffix.lower() in IMAGE_EXTS
            send_path = p
            as_document = not is_image
            scaled_tmp: Optional[Path] = None
            if is_image:
                # DGN-649 fix A: sendPhoto rejects oversized dimensions
                # (width+height > 10000 or ratio > 20) with an opaque
                # BadRequest. Normalize BEFORE sending: a ratio-legal overflow
                # is downscaled (Pillow, aspect preserved) and stays a photo;
                # an illegal ratio -- or a failed/unavailable downscale --
                # degrades to a document send, which has no dimension limit
                # and preserves the original pixels. Unprobeable dimensions
                # keep the prior photo-first behavior.
                dims = probe_image_dimensions(p)
                if dims is not None:
                    verdict = photo_send_verdict(*dims)
                    if verdict == "downscale":
                        scaled_tmp = downscale_for_photo(p)
                        if scaled_tmp is not None:
                            send_path = scaled_tmp
                        else:
                            as_document = True
                    elif verdict == "document":
                        as_document = True
                    if as_document or scaled_tmp is not None:
                        logger.info(
                            "Oversized image %s (%dx%d): sending as %s",
                            p, dims[0], dims[1],
                            "document" if as_document else "downscaled photo",
                        )
            last_err = None
            try:
                for attempt in range(2):  # initial try + one retry
                    try:
                        with open(send_path, "rb") as f:
                            if as_document:
                                await bot.send_document(chat_id, document=f)
                            else:
                                await bot.send_photo(chat_id, photo=f)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        kind = classify_send_error(e)
                        logger.warning(
                            "Failed to send file %s (attempt %d/2, %s): %s",
                            send_path, attempt + 1, kind, e,
                        )
                        if attempt == 0:
                            if kind == "dimensions" and not as_document:
                                # DGN-649: probe missed the overflow (unknown
                                # format etc.) -- retry once as document with
                                # the original file, no backoff needed.
                                as_document = True
                                send_path = p
                            else:
                                await asyncio.sleep(1.5)
            finally:
                if scaled_tmp is not None:
                    try:
                        scaled_tmp.unlink()
                    except OSError:
                        pass
            if last_err is not None:
                # Do not let a file silently vanish -- notify the user.
                # DGN-649 fix B: state the real reason, not always "network".
                kind = classify_send_error(last_err)
                if kind == "dimensions":
                    failure_msg = messages.SEND_FILE_FAILED_DIMENSIONS
                elif kind == "too_large":
                    failure_msg = messages.SEND_FILE_FAILED_TOO_LARGE
                elif kind == "network":
                    failure_msg = messages.SEND_FILE_FAILED
                else:
                    failure_msg = messages.SEND_FILE_FAILED_API
                try:
                    await bot.send_message(
                        chat_id,
                        failure_msg.format(filename=p.name),
                    )
                except Exception as notify_err:
                    logger.warning(
                        "Failed to notify send failure for %s: %s", p, notify_err
                    )

    async def _prompt_outside_file_confirmation(
        self, chat_id: int, user_id: int, paths: List[Path]
    ) -> None:
        session = await session_manager.get_session(user_id)
        session["pending_external_files"] = [str(p) for p in paths]
        await session_manager.update_session(user_id, session)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(messages.EXTERNAL_FILE_SEND, callback_data="extsend:allow")],
                [InlineKeyboardButton(messages.EXTERNAL_FILE_CANCEL, callback_data="extsend:deny")],
            ]
        )
        await self.application.bot.send_message(
            chat_id, messages.EXTERNAL_FILE_PROMPT, reply_markup=kb
        )

    # --- callbacks ---

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # DGN-922 FIX 2: cdn:done: callbacks must bypass the 20-min stale gate.
        # Countdowns can run up to MAX_SECONDS=24h; a completion button tapped
        # after 20 min would silently die without this exemption.  Peek at the
        # raw callback_data here (before _check_access) to select the right
        # gate; all other prefixes still go through the normal stale check.
        query = update.callback_query
        cdn_done_tap = bool(
            query and query.data and query.data.startswith(CDN_DONE_PREFIX)
        )
        if not await self._check_access(update, skip_stale=cdn_done_tap):
            return
        # D1 (DGN-515): spinner-dismiss failure must never kill the handler.
        # Telegram API blips (ConnectTimeout, QueryExpired) are non-fatal here.
        try:
            await query.answer()
        except Exception as e:
            logger.warning("query.answer() failed (ignored): %s", e)
        user_id = update.effective_user.id
        chat = update.effective_chat
        app = self.application
        data = query.data
        if data is None:
            return

        if data.startswith("extsend:"):
            session = await session_manager.get_session(user_id)
            pending = session.get("pending_external_files", [])
            session.pop("pending_external_files", None)
            await session_manager.update_session(user_id, session)
            if data == "extsend:deny":
                await query.edit_message_text(messages.EXTERNAL_FILE_CANCELLED)
                return
            if not pending:
                await query.edit_message_text(messages.EXTERNAL_FILE_NONE)
                return
            await query.edit_message_text(messages.EXTERNAL_FILE_CONFIRMED)
            paths: List[Path] = []
            for raw in pending:
                try:
                    resolved = Path(raw).resolve(strict=False)
                    if resolved.is_file() and resolved.stat().st_size < 10 * 1024 * 1024:
                        paths.append(resolved)
                except Exception:
                    continue
            await self._send_file_paths(chat.id, paths)
            return

        if data.startswith("opt:"):
            # DGN-665: recover the full "N. label" from the tapped button's own
            # text -- callback_data is number-only when the label overflowed
            # Telegram's 64-byte limit (Korean labels). Resolve BEFORE the edit,
            # which drops the keyboard, so both the confirmation and the agent
            # user_message carry the full label.
            reply_markup = getattr(query.message, "reply_markup", None)
            inline_keyboard = getattr(reply_markup, "inline_keyboard", None)
            choice = resolve_choice(data, inline_keyboard)
            # DGN-665: a keyboard was present but no button matched -> resolve_choice
            # fell back to the bare number. Warn to diagnose stale/duplicate-tap
            # cases (no warning when the keyboard is simply absent).
            if inline_keyboard and choice == (data.split(":", 1)[1] if ":" in data else data):
                if not any(
                    getattr(btn, "callback_data", None) == data
                    for row in inline_keyboard for btn in row
                ):
                    logger.warning(
                        "opt callback %r matched no button in the keyboard "
                        "(stale or duplicate tap); using bare fallback %r", data, choice
                    )
            await query.edit_message_text(messages.SELECTED.format(choice=choice))
            await self._maybe_capture_outside_approval(user_id, choice)
            chat_id = chat.id

            async def run_task() -> None:
                # D2 (DGN-515): app captured at dispatch time; if shutdown raced
                # and cleared self.application, fail soft so no AttributeError.
                if app is None:
                    logger.warning(
                        "opt run_task skipped: application stopped before execution "
                        "(chat %s)", chat_id
                    )
                    return
                session = await session_manager.get_session(user_id)
                try:
                    await app.bot.send_chat_action(chat_id, action="typing")
                except Exception:
                    pass
                try:
                    response = await sdk_bridge.process_message(
                        user_message=choice,
                        user_id=user_id,
                        chat_id=chat_id,
                        session_id=self._effective_session_id(user_id, session),
                        model=session.get("model"),
                        permission_callback=self._permission_callback,
                        typing_callback=lambda: app.bot.send_chat_action(chat_id, action="typing"),
                        bot=app.bot,
                        proactive_push=self._proactive_push,
                    )
                    await self._save_session_id(user_id, response)
                    await self._send_smart(
                        chat_id,
                        response.content,
                        force_options=response.has_options,
                        streamed=response.streamed,
                        draft_message_ids=response.draft_message_ids,
                        classifier_injected=getattr(response, "options_classifier_injected", False),
                    )
                except Exception as e:
                    logger.error("Option reply failed: %s", e, exc_info=True)
                    await app.bot.send_message(chat_id, messages.PROCESSING_FAILED.format(error=_fmt_error(e)))
                finally:
                    # DGN-911 FATAL fix: drain debounce buffer on every SDK turn
                    # completion (options callback turn is a full SDK turn).
                    await self._drain_pending_texts(user_id)

            async def on_overflow() -> None:
                await app.bot.send_message(chat_id, messages.QUEUE_BUSY)

            await self._enqueue_user_task(
                user_id, run_task, on_overflow, chat_id=chat_id
            )
            return

        if data.startswith("resume:"):
            await self._handle_resume_callback(update, query, user_id, chat)
            return

        if data.startswith("retry:"):
            await self._handle_retry_callback(update, query, user_id, chat)
            return

        if data.startswith("usageretry:"):
            await self._handle_usage_retry_callback(query, data)
            return

        if data.startswith("idrill:"):
            await self._handle_idrill_callback(query, data)
            return

        if data.startswith(CDN_DONE_PREFIX):
            # DGN-915: countdown completion-affordance tap. Remove the inline
            # keyboard from the done message so the affordance clears cleanly.
            # query.answer() already called above (spinner dismiss).
            # Stale tap (message gone / session ended): Telegram returns an
            # error; swallowed fail-soft -- the button is already unreachable.
            # TODO(DGN-915): no next-step trigger exists in the bridge yet.
            # Removing dead-air (dead button) is the sole win here. A future
            # Skull-coordinated step can wire a proactive model-turn from this
            # branch when the domain sequence machinery is in place.
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as e:
                logger.warning("cdn:done keyboard clear failed (ignored): %s", e)
            return

        if data.startswith("model:"):
            model_name = data.split(":", 1)[1]
            session = await session_manager.get_session(user_id)
            label = _MODEL_LABELS.get(model_name, model_name)
            # DGN-192: same-model tap is a no-op switch -- keep the session.
            if model_name == self._get_real_model(session):
                await query.edit_message_text(
                    messages.MODEL_ALREADY_ACTIVE.format(label=label)
                )
                return
            session["model"] = model_name
            session["session_id"] = None
            session["new_session"] = True
            await session_manager.update_session(user_id, session)
            # DGN-162: a user-initiated switch becomes the new last-session model.
            model_state.persist_model(model_name, _known_models())
            self._runtime_active_sessions.discard(user_id)
            await query.edit_message_text(
                messages.MODEL_SWITCHED.format(label=label)
            )
            return

    # --- DGN-918: idrill in-place drilldown capture callback primitive ---

    # arm_id is an opaque 8-char token used as a filename; constrain to
    # chars safe as path components (no traversal, no shell-special chars).
    # The spec says the arming consumer produces hex[:8], so [0-9a-f]{8}
    # is the exact set.
    _IDRILL_ARM_ID_RE = re.compile(r"^[0-9a-f]{8}$")

    # step2 values allowed in the :2:<v> tap.
    _IDRILL_STEP2_VALUES = frozenset({"0", "1", "2", "3", "4+", "skip"})

    # step2 keyboard buttons: labels + callback_data suffix.
    _IDRILL_STEP2_BUTTONS = [
        ("0", "0"), ("1", "1"), ("2", "2"), ("3", "3"), ("4+", "4+"),
    ]
    _IDRILL_STEP2_SKIP_LABEL = "건너뜀"
    _IDRILL_STEP2_SKIP_SUFFIX = "skip"

    @staticmethod
    def _idrill_read_arm(arm_id: str) -> Optional[dict]:
        """Read the arm-id state file; returns dict or None (missing/malformed).

        Path: PROJECT_ROOT/files/program/.idrill-arm/<arm_id>
        arm_id is pre-validated (no traversal); still resolves strictly inside
        the arm directory so any unexpected '/' in a future arm_id format
        change cannot escape the directory.
        """
        cb_dir = PROJECT_ROOT / "files" / "program" / ".idrill-arm"
        arm_path = (cb_dir / arm_id).resolve()
        # Strict containment guard: resolved path must stay inside cb_dir.
        try:
            arm_path.relative_to(cb_dir.resolve())
        except ValueError:
            logger.warning("idrill: arm_id %r resolved outside arm dir", arm_id)
            return None
        try:
            return json.loads(arm_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _idrill_consume_arm(arm_id: str) -> None:
        """Delete the arm-id state file (consume on step2 tap)."""
        arm_path = PROJECT_ROOT / "files" / "program" / ".idrill-arm" / arm_id
        try:
            arm_path.unlink()
        except OSError:
            pass  # already consumed or never existed -- converged

    @staticmethod
    def _idrill_render_confirmation(arm: dict, val1, val2: str) -> str:
        """Render the captured-input confirmation (Addition B spec).

        Priority: arm-file confirm_fmt / confirm_fmt_skip fields (the arming
        consumer owns the text; bridge renders only). Tokens are positional:
        {1} = step1 value, {2} = step2 value; {sec} stays a named extra for
        hold-style arms. Fallback: messages constants.
        Defensive .format: KeyError / IndexError -> fallback, never crash.
        """
        try:
            val1_num = int(val1)
        except (ValueError, TypeError):
            val1_num = val1  # best-effort; confirm_fmt may not need it as int

        is_hold = arm.get("is_hold", False)

        if is_hold:
            # hold-style arm: confirm_fmt.format(sec=<hold_sec>)
            fmt = arm.get("confirm_fmt", "")
            hold_sec = arm.get("hold_sec", val1_num)
            if fmt:
                try:
                    return fmt.format("", val1_num, val2, sec=hold_sec)
                except (KeyError, IndexError):
                    pass
            return messages.IDRILL_LOGGED_DEFAULT.format("", hold_sec)
        elif val2 == "skip":
            fmt = arm.get("confirm_fmt_skip", "")
            if fmt:
                try:
                    return fmt.format("", val1_num, val2, sec=val1_num)
                except (KeyError, IndexError):
                    pass
            return messages.IDRILL_LOGGED_DEFAULT_SKIP.format("", val1_num)
        else:
            fmt = arm.get("confirm_fmt", "")
            if fmt:
                try:
                    return fmt.format("", val1_num, val2, sec=val1_num)
                except (KeyError, IndexError):
                    pass
            return messages.IDRILL_LOGGED_DEFAULT.format("", val1_num)

    # Placeholder tokens a declared argv may carry (contract v2).  The
    # bridge substitutes ONLY these exact-match elements; every other argv
    # element is executed verbatim as declared by the arm file.  Tokens are
    # positional -- the bridge knows no domain meaning for either step.
    _IDRILL_TOKEN_1 = "{1}"
    _IDRILL_TOKEN_2 = "{2}"
    # Substitution whitelist: step1 = plain ASCII integer (length-bounded to
    # block overflow abuse); step2 = single digit 0-4 after 4+ normalization.
    _IDRILL_STEP1_RE = re.compile(r"^[0-9]{1,4}$")
    _IDRILL_STEP2_RE = re.compile(r"^[0-4]$")

    async def _idrill_fire_cmd(self, arm: dict, val1, val2: str) -> bool:
        """Execute the argv declared by the arm file (contract v2), once.

        The bridge is domain-verb agnostic: it never assembles a command.
        The arm file declares the full argv -- `cmd`, or `cmd_skip2` when
        step2 was skipped -- with argv[0] already an absolute path.  The
        bridge only substitutes the "{1}" / "{2}" placeholder elements with
        numeric-whitelisted values and executes the list verbatim:
        list-form subprocess, no shell, no PATH lookup, no cwd change,
        no re-parsing of declared elements.  Returns True on exit 0.
        """
        key = "cmd_skip2" if val2 == "skip" else "cmd"
        declared = arm.get(key)
        if (not isinstance(declared, list) or not declared
                or not all(isinstance(el, str) for el in declared)):
            logger.warning(
                "idrill: arm file %s argv missing/malformed (session=%s)",
                key, arm.get("session_id", "?"),
            )
            return False

        # Whitelist the substitution values: only validated numerics may
        # enter the argv.  Everything else is the file's own declaration
        # and passes through untouched (no re-parsing).
        val1_str = str(val1)
        if not self._IDRILL_STEP1_RE.match(val1_str):
            logger.warning("idrill: step1 value rejected by whitelist: %r", val1_str)
            return False
        val2_sub = None
        if val2 != "skip":
            val2_sub = "4" if val2 == "4+" else val2
            if not self._IDRILL_STEP2_RE.match(val2_sub):
                logger.warning("idrill: step2 value rejected by whitelist: %r", val2)
                return False

        argv = []
        for el in declared:
            if el == self._IDRILL_TOKEN_1:
                argv.append(val1_str)
            elif el == self._IDRILL_TOKEN_2:
                if val2_sub is None:
                    # The skip variant must not carry a {2} slot; an
                    # unsubstitutable token means a malformed declaration.
                    logger.warning(
                        "idrill: %s declares a step2 token but step2 was skipped",
                        key,
                    )
                    return False
                argv.append(val2_sub)
            else:
                argv.append(el)

        def _run() -> subprocess.CompletedProcess:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=15,
            )

        try:
            proc = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            logger.warning(
                "idrill: declared cmd timed out (session=%s)",
                arm.get("session_id"),
            )
            return False
        except Exception as e:
            logger.warning("idrill: declared cmd subprocess error: %s", e)
            return False

        if proc.returncode != 0:
            logger.warning(
                "idrill: declared cmd exited %d for session=%s: %s",
                proc.returncode,
                arm.get("session_id"),
                (proc.stderr or "").strip()[:200],
            )
            return False
        return True

    async def _handle_idrill_callback(self, query, data: str) -> None:
        """DGN-918: dispatch idrill:<arm_id>:1:<val> | :2:<v> callbacks.

        Behavior model turn 0, outbound 0:
          :1:<n>     -> swap current message keyboard to step2 buttons (in-place edit).
          :1:other   -> edit_message_text to free-text prompt (no fire).
          :2:<v>     -> read arm file, fire the declared cmd argv,
                        confirm edit, consume file.

        All paths are fail-soft (mirror D1/D2 patterns in _handle_callback).
        """
        # Parse: idrill:<arm_id>:<step>:<value>
        parts = data.split(":")
        # Minimum structure: idrill : arm_id : step : value  (4 parts min)
        if len(parts) < 4:
            logger.warning("idrill: malformed callback_data %r", data)
            try:
                await query.edit_message_text(messages.IDRILL_ERROR)
            except Exception:
                pass
            return

        arm_id = parts[1]
        action = parts[2]
        # value may contain ':' for future-proofing (rejoin from part 3 on)
        value = ":".join(parts[3:])

        # Security: arm_id is used as a filename; validate strictly.
        if not self._IDRILL_ARM_ID_RE.match(arm_id):
            logger.warning("idrill: arm_id failed validation: %r", arm_id)
            try:
                await query.edit_message_text(messages.IDRILL_ERROR)
            except Exception:
                pass
            return

        # --- step1 tap: :1:<val> ---
        if action == "1":
            if value == "other":
                # Free-text fallback: prompt the user; leave arm file for TTL cleanup.
                try:
                    await query.edit_message_text(messages.IDRILL_OTHER_PROMPT)
                except Exception as e:
                    logger.warning("idrill: edit_message_text (other) failed: %s", e)
                return

            # Normal step1 tap: validate arm file exists, then swap to step2 keyboard.
            arm = self._idrill_read_arm(arm_id)
            if arm is None:
                try:
                    await query.edit_message_text(messages.IDRILL_ARM_EXPIRED)
                except Exception as e:
                    logger.warning("idrill: edit_message_text (expired) failed: %s", e)
                return

            # Build step2 keyboard; embed the step1 value via the arm file.
            step2_rows = [
                [
                    InlineKeyboardButton(
                        lbl, callback_data=f"idrill:{arm_id}:2:{sfx}"
                    )
                    for lbl, sfx in self._IDRILL_STEP2_BUTTONS
                ],
                [
                    InlineKeyboardButton(
                        self._IDRILL_STEP2_SKIP_LABEL,
                        callback_data=f"idrill:{arm_id}:2:{self._IDRILL_STEP2_SKIP_SUFFIX}",
                    )
                ],
            ]
            # Store the chosen step1 value in the arm file so the :2: handler
            # can retrieve it without re-encoding it in callback_data.
            arm["_pending_step1"] = value
            arm_path = (
                PROJECT_ROOT / "files" / "program" / ".idrill-arm" / arm_id
            )
            try:
                arm_path.write_text(json.dumps(arm), encoding="utf-8")
            except OSError as e:
                logger.warning("idrill: arm file update failed: %s", e)
                try:
                    await query.edit_message_text(messages.IDRILL_ERROR)
                except Exception:
                    pass
                return

            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(step2_rows)
                )
            except Exception as e:
                logger.warning(
                    "idrill: edit_message_reply_markup failed for arm_id=%s: %s",
                    arm_id, e,
                )
            return

        # --- step2 tap: :2:<v> ---
        if action == "2":
            if value not in self._IDRILL_STEP2_VALUES:
                logger.warning("idrill: unexpected step2 value %r for arm_id=%s", value, arm_id)
                try:
                    await query.edit_message_text(messages.IDRILL_ERROR)
                except Exception:
                    pass
                return

            # Read and consume the arm file atomically-enough: read, fire,
            # then delete.  A concurrent second tap on the same arm finds the
            # file already deleted -> IDRILL_ARM_EXPIRED (fail-soft, no crash).
            arm = self._idrill_read_arm(arm_id)
            if arm is None:
                # Already consumed by a concurrent tap or expired.
                try:
                    await query.edit_message_text(messages.IDRILL_ARM_EXPIRED)
                except Exception as e:
                    logger.warning("idrill: edit_message_text (step2 expired) failed: %s", e)
                return

            val1 = arm.get("_pending_step1", "?")

            # Fire the declared cmd exactly once (server-side, no model turn).
            ok = await self._idrill_fire_cmd(arm, val1, value)

            # Consume the arm file regardless of fire outcome to prevent
            # double-submission on retry tap.
            self._idrill_consume_arm(arm_id)

            if not ok:
                try:
                    await query.edit_message_text(messages.IDRILL_ERROR)
                except Exception as e:
                    logger.warning("idrill: edit_message_text (error) failed: %s", e)
                return

            confirmation = self._idrill_render_confirmation(arm, val1, value)
            try:
                await query.edit_message_text(confirmation)
            except Exception as e:
                logger.warning(
                    "idrill: edit_message_text (confirm) failed for arm_id=%s: %s",
                    arm_id, e,
                )
            return

        # Unknown step token: log and fail-soft.
        logger.warning("idrill: unknown step %r in callback_data %r", action, data)
        try:
            await query.edit_message_text(messages.IDRILL_ERROR)
        except Exception:
            pass

    async def _handle_resume_callback(self, update, query, user_id, chat) -> None:
        app = self.application
        token = query.data.split(":", 1)[1]
        session = await session_manager.get_session(user_id)
        pending = session.get("pending_resume")
        if not pending or pending.get("token") != token:
            await query.edit_message_text(messages.RESUME_EXPIRED)
            return
        resume_sid = pending.get("session_id")
        session.pop("pending_resume", None)
        if resume_sid:
            session["session_id"] = resume_sid
            session["new_session"] = False
        await session_manager.update_session(user_id, session)
        if resume_sid:
            self._runtime_active_sessions.add(user_id)
        await query.edit_message_text(messages.RESUME_CONTINUING)
        chat_id = chat.id
        continuation = messages.RESUME_CONTINUATION_PROMPT

        async def run_task() -> None:
            sess = await session_manager.get_session(user_id)
            try:
                await app.bot.send_chat_action(chat_id, action="typing")
            except Exception:
                pass
            try:
                response = await sdk_bridge.process_message(
                    user_message=continuation,
                    user_id=user_id,
                    chat_id=chat_id,
                    session_id=self._effective_session_id(user_id, sess),
                    model=sess.get("model"),
                    permission_callback=self._permission_callback,
                    typing_callback=lambda: app.bot.send_chat_action(chat_id, action="typing"),
                    bot=app.bot,
                    proactive_push=self._proactive_push,
                )

                async def resume_caller(cont: str) -> ChatResponse:
                    s = await session_manager.get_session(user_id)
                    return await sdk_bridge.process_message(
                        user_message=cont,
                        user_id=user_id,
                        chat_id=chat_id,
                        session_id=self._effective_session_id(user_id, s),
                        model=s.get("model"),
                        permission_callback=self._permission_callback,
                        typing_callback=lambda: app.bot.send_chat_action(chat_id, action="typing"),
                        bot=app.bot,
                        proactive_push=self._proactive_push,
                    )

                response = await self._auto_resume_loop(
                    user_id=user_id, chat_id=chat_id, response=response, resume_caller=resume_caller
                )
                if getattr(response, "timed_out", False):
                    await self._send_resume_notice(chat_id=chat_id, user_id=user_id, response=response)
                    return
                await self._save_session_id(user_id, response)
                await self._send_smart(
                    chat_id,
                    response.content,
                    force_options=response.has_options,
                    streamed=response.streamed,
                    draft_message_ids=response.draft_message_ids,
                    classifier_injected=getattr(response, "options_classifier_injected", False),
                )
            except Exception as e:
                logger.error("Resume continuation failed: %s", e, exc_info=True)
                await app.bot.send_message(chat_id, messages.RESUME_FAILED.format(error=_fmt_error(e)))
            finally:
                # DGN-911 FATAL fix: drain debounce buffer on every SDK turn
                # completion (resume continuation is a full SDK turn).
                await self._drain_pending_texts(user_id)

        async def on_overflow() -> None:
            await app.bot.send_message(chat_id, messages.QUEUE_BUSY)

        await self._enqueue_user_task(
            user_id, run_task, on_overflow, chat_id=chat_id
        )

    async def _handle_retry_callback(self, update, query, user_id, chat) -> None:
        """DGN-686: [retry] button -- re-run the stored user message verbatim.

        One-shot token (pending_retry); expired/mismatched taps degrade to a
        notice. On a repeated is_error the retry notice + button is offered
        again (bounded only by the user's taps).
        """
        app = self.application
        token = query.data.split(":", 1)[1]
        session = await session_manager.get_session(user_id)
        pending = session.get("pending_retry")
        if not pending or pending.get("token") != token:
            await query.edit_message_text(messages.ERROR_RETRY_EXPIRED)
            return
        user_message = pending.get("user_message", "")
        session.pop("pending_retry", None)
        await session_manager.update_session(user_id, session)
        await query.edit_message_text(messages.ERROR_RETRYING)
        chat_id = chat.id

        async def run_task() -> None:
            sess = await session_manager.get_session(user_id)
            try:
                await app.bot.send_chat_action(chat_id, action="typing")
            except Exception:
                pass
            try:
                response = await sdk_bridge.process_message(
                    user_message=user_message,
                    user_id=user_id,
                    chat_id=chat_id,
                    session_id=self._effective_session_id(user_id, sess),
                    model=sess.get("model"),
                    permission_callback=self._permission_callback,
                    typing_callback=lambda: app.bot.send_chat_action(chat_id, action="typing"),
                    bot=app.bot,
                    proactive_push=self._proactive_push,
                )
                await self._save_session_id(user_id, response)
                if getattr(response, "retry_offer", False):
                    await self._send_retry_notice(
                        chat_id=chat_id, user_id=user_id,
                        notice=response.content, user_message=user_message,
                    )
                    return
                await self._send_smart(
                    chat_id,
                    response.content,
                    force_options=response.has_options,
                    streamed=response.streamed,
                    draft_message_ids=response.draft_message_ids,
                    classifier_injected=getattr(response, "options_classifier_injected", False),
                )
            except Exception as e:
                # DGN-686 MINOR-2: never leak the English detail to the user;
                # log it, show the fixed ko failure notice.
                logger.error("Retry run failed: %s", e, exc_info=True)
                await app.bot.send_message(chat_id, messages.ERROR_GENERIC_RETRY)
            finally:
                # DGN-911 FATAL fix: drain debounce buffer on every SDK turn
                # completion (retry turn is a full SDK turn).
                await self._drain_pending_texts(user_id)

        async def on_overflow() -> None:
            await app.bot.send_message(chat_id, messages.QUEUE_BUSY)

        await self._enqueue_user_task(
            user_id, run_task, on_overflow, chat_id=chat_id
        )

    # --- DGN-835: usage-defer manual retry (7d window) ---

    # Charset guard for the job label riding in callback_data: the label
    # becomes a filename under ~/.dogany/usage-defer/, so no '/', no '..',
    # nothing outside launchd-label characters.
    _USAGE_RETRY_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
    # Preflight threshold: aligned with the automatic-pipeline defer threshold
    # (memory-engine/memory.py CONSOLIDATE_USAGE_EXHAUSTION_PCT = 99.0).
    _USAGE_RETRY_PREFLIGHT_PCT = 99.0

    def _fetch_usage_json(self) -> Optional[dict]:
        """Blocking helper: routines/claude-usage.sh --json -> dict or None."""
        script = PROJECT_ROOT / "routines" / "claude-usage.sh"
        if not script.is_file():
            return None
        try:
            proc = subprocess.run(
                ["bash", str(script), "--json"],
                capture_output=True, text=True, timeout=12,
            )
            if proc.returncode != 0:
                return None
            return json.loads(proc.stdout)
        except Exception:
            return None

    async def _usage_retry_run(self, label: str):
        """DGN-835 shared core for the usageretry BUTTON and the /usageretry
        SLASH command (DGN-841 A: a late button tap dies at the 20-min STALE
        gate, so the slash path -- always a fresh message -- must exist as a
        first-class route). Runs the deferred job's replay file DETACHED --
        never through process_message, so neither entry consumes a live
        conversation turn (the whole point: the 7d window is scarce).

        Safety: label charset-validated (it names a file under the
        user-owned ~/.dogany/usage-defer/); usage PREFLIGHT blocks execution
        while the 7d window is still exhausted (running during saturation
        would just re-fail the job). The replay file self-deletes on launch
        (cron-guard writes it that way), so a repeat attempt degrades to the
        no-replay notice.

        Returns (reply_text, retriable): retriable True = a later attempt
        may succeed (callers keep the button / the user may re-issue).
        """
        if not self._USAGE_RETRY_LABEL_RE.match(label):
            return messages.USAGE_RETRY_BAD_LABEL, False
        replay = Path.home() / ".dogany" / "usage-defer" / f"{label}.replay"
        if not replay.is_file():
            return messages.USAGE_RETRY_NO_REPLAY.format(label=label), False

        usage = await asyncio.to_thread(self._fetch_usage_json)
        if not isinstance(usage, dict):
            return messages.USAGE_RETRY_LOOKUP_FAILED, True
        seven = usage.get("seven_day") or {}
        try:
            sd = float(seven.get("utilization", 0) or 0)
        except (TypeError, ValueError):
            sd = 0.0
        if sd >= self._USAGE_RETRY_PREFLIGHT_PCT:
            reset = str(seven.get("resets_at", "") or "?")
            return (
                messages.USAGE_RETRY_NOT_ENOUGH.format(
                    pct=int(round(sd)), reset=reset
                ),
                True,
            )

        try:
            with open(os.devnull, "wb") as devnull:
                subprocess.Popen(
                    ["/bin/bash", str(replay)],
                    stdout=devnull, stderr=devnull,
                    start_new_session=True,
                )
        except Exception as e:
            logger.error("usage-retry replay launch failed (%s): %s", label, e)
            return messages.USAGE_RETRY_LOOKUP_FAILED, True
        return messages.USAGE_RETRY_STARTED.format(label=label), False

    async def _handle_usage_retry_callback(self, query, data: str) -> None:
        """DGN-835: 'usageretry:<label>' button from a seven_day usage-defer
        notification (cron-guard). NOTE the 20-min STALE gate (_check_access)
        kills taps on aged messages, so this path realistically serves only
        the fresh notification and the reset-time re-notify (DGN-841 B);
        /usageretry (DGN-841 A) covers everything else."""
        label = data.split(":", 1)[1]
        text, retriable = await self._usage_retry_run(label)
        # retriable outcomes KEEP the button for a later tap; terminal
        # outcomes drop the one-shot keyboard.
        keep_kb = getattr(query.message, "reply_markup", None) if retriable else None
        await query.edit_message_text(text, reply_markup=keep_kb)

    async def _cmd_usage_retry(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/usageretry <label> -- DGN-841 A slash fallback for the usage-defer
        manual retry. A freshly typed command always passes the STALE gate
        (its own message is new), unlike a button tap riding an old
        notification. The defer notification body carries this command
        verbatim so the user can copy it after the button expires."""
        if not await self._check_access(update):
            return
        if not update.message:
            return
        args = context.args or []
        if len(args) != 1:
            await update.message.reply_text(messages.USAGE_RETRY_USAGE)
            return
        text, _retriable = await self._usage_retry_run(args[0])
        await update.message.reply_text(text)


bot = TelegramBot()
