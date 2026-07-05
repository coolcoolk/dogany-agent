"""Telegram bot: handlers, commands, callbacks, queue, send logic.

PTB v20 Application with a manual lifecycle that restarts polling on network
blips (launchd owns crash-restart). Per-user serialized queue (max 3 in-flight,
/stop priority), allowlist + stale-message drop, marker-aware sending.
"""

import asyncio
import logging
import os
import signal
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

from bridge import heartbeat, messages
from bridge.config import (
    AUTO_RESUME,
    AUTO_RESUME_MAX,
    PROCESS_TIMEOUT,
    config,
)
from bridge import ownership
from bridge.formatting import (
    IMAGE_EXTS,
    code_segment_html,
    resolve_send_paths,
    split_into_segments,
    split_paths_by_scope,
    split_text,
    strip_display_markers,
    strip_send_markers,
    strip_toolcall_markup,
)
from bridge.health import PollingConflict, PollingRestart, polling_watchdog
from bridge.options import (
    OPTIONS_MARKER,
    build_option_keyboard,
    extract_options,
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

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

logger = logging.getLogger(__name__)

STALE_MESSAGE_SECONDS = 20 * 60
MAX_INFLIGHT_MESSAGES = 3
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

_MODEL_LABELS = {
    "sonnet": "Claude Sonnet",
    "opus": "Claude Opus",
    "haiku": "Claude Haiku",
    "fable": "Claude Fable",
}


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


class TelegramBot:
    def __init__(self) -> None:
        self.application: Optional[Application] = None
        self._conflict_event: Optional[asyncio.Event] = None
        self._runtime_active_sessions: set[int] = set()
        self._user_run_tasks: Dict[int, set[asyncio.Task]] = {}
        self._user_queue_locks: Dict[int, asyncio.Lock] = {}
        self._active_tasks: Dict[int, asyncio.Task] = {}
        self._audio_dir = config.bot_data_dir / "audio"
        # Inbound photos land here: ephemeral runtime buffer, pruned after 7 days
        # by the cleanup cron. photo input restored.
        self._image_dir = config.bot_data_dir / "images"
        # media_group_id -> buffered album state, flushed as one task.
        self._media_groups: Dict[str, dict] = {}
        self._media_group_lock = asyncio.Lock()
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
        app.add_handler(CommandHandler("history", self._cmd_history))
        app.add_handler(CommandHandler("skills", self._cmd_skills))
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
        commands = [
            BotCommand("new", messages.CMD_DESC_NEW),
            BotCommand("stop", messages.CMD_DESC_STOP),
            BotCommand("model", messages.CMD_DESC_MODEL),
            BotCommand("resume", messages.CMD_DESC_RESUME),
            BotCommand("history", messages.CMD_DESC_HISTORY),
            BotCommand("skills", messages.CMD_DESC_SKILLS),
            BotCommand("help", messages.CMD_DESC_HELP),
        ]
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
        logger.error("Unhandled exception:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    update.effective_chat.id,
                    messages.INTERNAL_ERROR.format(error=context.error),
                )
            except Exception:
                pass

    # --- access control ---

    async def _check_access(self, update: Update) -> bool:
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
        return True

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

    async def _maybe_capture_outside_approval(self, user_id: int, text: str) -> None:
        session = await session_manager.get_session(user_id)
        pending = session.get("pending_outside_paths")
        if not pending:
            return
        # Expire a stale deny prompt: an approval reply that arrives after the
        # TTL no longer grants anything.
        pending_at = session.get("pending_outside_at", 0)
        if (time.time() - pending_at) > OUTSIDE_APPROVAL_TTL:
            session.pop("pending_outside_paths", None)
            session.pop("pending_outside_at", None)
            await session_manager.update_session(user_id, session)
            return
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
        elif deny:
            session["outside_path_approved_once"] = False
            session.pop("outside_path_approved_paths", None)
            session.pop("outside_path_approved_at", None)
            session.pop("pending_outside_paths", None)
            session.pop("pending_outside_at", None)
            await session_manager.update_session(user_id, session)

    # --- per-user queue ---

    def _get_user_queue_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._user_queue_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_queue_locks[user_id] = lock
        return lock

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

    def _clear_user_queue(self, user_id: int) -> int:
        tasks = self._prune_user_tasks(user_id)
        cleared = len(tasks)
        for t in list(tasks):
            t.cancel()
        tasks.clear()
        return cleared

    async def _enqueue_user_task(
        self,
        user_id: int,
        run_task: Callable[[], Awaitable[None]],
        on_overflow: Callable[[], Awaitable[None]],
    ) -> bool:
        accepted: Optional[asyncio.Task] = None
        async with self._get_user_queue_lock(user_id):
            tasks = self._prune_user_tasks(user_id)
            if len(tasks) < MAX_INFLIGHT_MESSAGES:
                async def wrapped() -> None:
                    self._active_tasks[user_id] = asyncio.current_task()
                    try:
                        await run_task()
                    finally:
                        self._active_tasks.pop(user_id, None)

                accepted = asyncio.create_task(wrapped())
                self._track_user_task(user_id, accepted)
        if not accepted:
            await on_overflow()
            return False
        return True

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
        if model := session.get("model"):
            return model
        return "sonnet"

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
            session["model"] = name
            session["session_id"] = None
            session["new_session"] = True
            await session_manager.update_session(user_id, session)
            self._runtime_active_sessions.discard(user_id)
            label = _MODEL_LABELS.get(name, name)
            await update.message.reply_text(
                messages.MODEL_SWITCHED.format(label=label)
                + "\n"
                + messages.MODEL_SWITCH_WARNING
            )
            return
        current = self._get_real_model(session)
        models = list(MODELS)
        if current not in dict(models):
            models.append((current, current))
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
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
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

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update):
            return
        user_id = update.effective_user.id
        session = await session_manager.get_session(user_id)
        session_id = session.get("session_id")
        if not session_id:
            await update.message.reply_text(messages.NO_SESSION)
            return
        msgs = self._get_recent_messages(session_id, limit=5)
        if not msgs:
            await update.message.reply_text(messages.NO_HISTORY)
            return
        lines = [messages.HISTORY_HEADER, ""]
        for m in msgs:
            role = "User" if m["role"] == "user" else "Assistant"
            content = m["content"]
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"[{role}] {content}")
            lines.append("")
        reply = "\n".join(lines).strip()
        if len(reply) > 4000:
            reply = reply[:3997] + "..."
        await update.message.reply_text(reply)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update):
            return
        await update.message.reply_text(messages.HELP_TEXT)

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
                    # Folded/literal block: gather following more-indented lines.
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
            rows = []
            for name, desc in entries:
                desc = (desc or "").strip()
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                rows.append(f"/{name}" + (f" - {desc}" if desc else ""))
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
            try:
                await update.message.reply_text(part, parse_mode="HTML")
            except Exception:
                await update.message.reply_text(part)

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
                    parse_mode="Markdown",
                    force_options=response.has_options,
                    streamed=response.streamed,
                    draft_message_ids=response.draft_message_ids,
                )
            except Exception as e:
                logger.error("Skill execution failed: %s", e, exc_info=True)
                await message.reply_text(messages.PROCESSING_FAILED.format(error=e))

        async def on_overflow() -> None:
            await message.reply_text(messages.QUEUE_BUSY)

        await self._enqueue_user_task(user_id, run_task, on_overflow)

    # --- session listing (reads Claude conversation JSONL) ---

    @property
    def _conversations_dir(self) -> Path:
        project_dir_name = str(PROJECT_ROOT).replace("/", "-").replace("_", "-")
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

    def _get_recent_messages(self, session_id: str, limit: int = 5):
        import json

        filepath = self._conversations_dir / f"{session_id}.jsonl"
        if not filepath.exists():
            return []
        out = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") not in ("user", "assistant"):
                        continue
                    msg = d.get("message", {})
                    role = msg.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    content = msg.get("content", "")
                    text = ""
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    break
                    elif isinstance(content, str):
                        text = content.strip()
                    if text:
                        out.append({"role": role, "content": text})
        except Exception as e:
            logger.error("Error reading session messages: %s", e)
            return []
        return out[-limit:]

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

        await self._maybe_capture_outside_approval(user_id, text)

        async def run_task() -> None:
            await self._process_user_message_text(update, user_id, text)

        async def on_overflow() -> None:
            await message.reply_text(messages.QUEUE_BUSY)

        await self._enqueue_user_task(user_id, run_task, on_overflow)

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

        async def run_task() -> None:
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
                    await message.reply_text(messages.VOICE_DOWNLOAD_FAILED)
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
                await self._process_user_message_text(
                    update, user_id, text, voice_input_preview=text
                )
            finally:
                await self._audio_processor.cleanup_audio_files(cleanup_paths)

        async def on_overflow() -> None:
            await message.reply_text(messages.QUEUE_BUSY)

        await self._enqueue_user_task(user_id, run_task, on_overflow)

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
        await self._dispatch_photo_task(
            group["update"], group["user_id"], group["file_ids"], group["caption"]
        )

    async def _dispatch_photo_task(
        self, update: Update, user_id: int, file_ids: List[str], caption: str
    ) -> None:
        # download 1..N photos (single or album) and hand all paths to the
        # multimodal brain in ONE task, so an album is read as a single message.
        message = update.message

        async def run_task() -> None:
            self._image_dir.mkdir(parents=True, exist_ok=True)
            paths: List[Path] = []
            for idx, fid in enumerate(file_ids):
                dest = self._image_dir / f"{user_id}_{int(time.time() * 1000)}_{idx}.jpg"
                try:
                    await self._download_file(fid, dest)
                except Exception as e:
                    logger.error("Photo download failed for user %s: %s", user_id, e)
                    continue
                paths.append(dest)
            if not paths:
                await message.reply_text(messages.PHOTO_DOWNLOAD_FAILED)
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
            await self._process_user_message_text(update, user_id, "\n".join(lines))

        async def on_overflow() -> None:
            await message.reply_text(messages.QUEUE_BUSY)

        await self._enqueue_user_task(user_id, run_task, on_overflow)

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

        async def run_task() -> None:
            self._inbox_dir.mkdir(parents=True, exist_ok=True)
            # Preserve the original filename; prefix user+ts to avoid clashes.
            safe_name = Path(doc.file_name).name if doc.file_name else "file"
            dest = self._inbox_dir / f"{user_id}_{int(time.time() * 1000)}_{safe_name}"
            try:
                await self._download_file(doc.file_id, dest)
            except Exception as e:
                logger.error("Document download failed for user %s: %s", user_id, e)
                await message.reply_text(messages.DOC_DOWNLOAD_FAILED)
                return
            lines = [
                messages.DOC_PROMPT,
                messages.DOC_PROMPT_PATH.format(path=dest),
            ]
            if caption:
                lines.append(messages.USER_CAPTION.format(caption=caption))
            await self._process_user_message_text(update, user_id, "\n".join(lines))

        async def on_overflow() -> None:
            await message.reply_text(messages.QUEUE_BUSY)

        await self._enqueue_user_task(user_id, run_task, on_overflow)

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
        # add retry-with-exponential-backoff for transient errors
        # (Telegram 5xx / connection reset / TLS blip). 3 attempts, 1s/2s backoff.
        # Mirrors _send_guaranteed pattern. Callers' catch/notify logic unchanged.
        _max_attempts = 3
        _backoff_delays = [1.0, 2.0]  # sleep after attempt 1, attempt 2; none after 3
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
            if new_session:
                await session_manager.update_session(user_id, session)
            await session_manager.set_last_user_message_at(user_id, message_ts)

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
            await self._reply_smart(
                message,
                response.content,
                parse_mode="Markdown",
                force_options=response.has_options,
                streamed=response.streamed,
                draft_message_ids=response.draft_message_ids,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Error in chat for user %s: %s", user_id, e, exc_info=True)
            await message.reply_text(messages.GENERIC_ERROR.format(error=e))

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

    # --- send logic ---

    async def _reply_smart(
        self,
        message,
        content: str,
        parse_mode: str = "Markdown",
        force_options: bool = False,
        streamed: bool = False,
        draft_message_ids: Optional[List[int]] = None,
    ) -> None:
        display, _ = strip_options_marker(content)
        display, _ = strip_send_markers(display)
        # DGN-159: last-mile scrub of leaked tool-call markup on the non-streamed
        # / finalized send path (streamed drafts are scrubbed in strip_display_markers).
        display = strip_toolcall_markup(display)
        if not streamed:
            await self._send_text_body(message, display, parse_mode)
        elif "```" in display and draft_message_ids:
            bot = message.get_bot()
            chat_id = message.chat.id
            for mid in draft_message_ids:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception as e:
                    logger.warning("Failed to delete streamed draft %s: %s", mid, e)
            await self._send_text_body(message, display, parse_mode)
        await self._send_content_artifacts(message, content, force_options)

    async def _send_text_body(self, message, content: str, parse_mode: str) -> None:
        for segment, is_code, lang in split_into_segments(content):
            if is_code:
                for part in split_text(segment):
                    if not part.strip():
                        continue
                    try:
                        await message.reply_text(code_segment_html(part, lang), parse_mode="HTML")
                    except Exception:
                        await message.reply_text(part)
            else:
                for part in split_text(segment):
                    if not part.strip():
                        continue
                    try:
                        await message.reply_text(part, parse_mode=parse_mode)
                    except Exception:
                        await message.reply_text(part)

    async def _send_content_artifacts(self, message, content: str, force_options: bool) -> None:
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
                options = extract_options(clean)
                kb = build_option_keyboard(options)
                if kb:
                    await message.reply_text(messages.SELECT_PROMPT, reply_markup=kb)

    async def _proactive_push(
        self, chat_id: int, content: str, has_options: bool
    ) -> None:
        """Deliver main-agent output that arrived with no pending request.

        Invoked by the bridge when the agent emits a turn (e.g. a background-task
        completion report) that the request-response path would otherwise drop.
        Reuses _send_smart so formatting and [[OPTIONS]] buttons behave the same
        as a normal reply.
        """
        try:
            await self._send_smart(chat_id, content, force_options=has_options)
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
    ) -> None:
        bot = self.application.bot
        display, has_marker = strip_options_marker(content)
        display, _ = strip_send_markers(display)
        # DGN-159: last-mile scrub of leaked tool-call markup (proactive / option /
        # resume send path); streamed drafts are scrubbed in strip_display_markers.
        display = strip_toolcall_markup(display)
        if not streamed:
            await self._send_text_body_chat(chat_id, display)
        elif "```" in display and draft_message_ids:
            for mid in draft_message_ids:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception as e:
                    logger.warning("Failed to delete streamed draft %s: %s", mid, e)
            await self._send_text_body_chat(chat_id, display)
        resolved = resolve_send_paths(content, PROJECT_ROOT)
        in_root, _ = split_paths_by_scope(resolved, PROJECT_ROOT)
        await self._send_file_paths(chat_id, in_root)
        if force_options and has_marker:
            options = extract_options(display)
            kb = build_option_keyboard(options)
            if kb:
                await bot.send_message(chat_id, messages.SELECT_PROMPT, reply_markup=kb)

    async def _send_text_body_chat(self, chat_id: int, content: str) -> None:
        bot = self.application.bot
        for segment, is_code, lang in split_into_segments(content):
            if is_code:
                for part in split_text(segment):
                    if not part.strip():
                        continue
                    try:
                        await bot.send_message(chat_id, code_segment_html(part, lang), parse_mode="HTML")
                    except Exception:
                        await bot.send_message(chat_id, part)
            else:
                for part in split_text(segment):
                    if not part.strip():
                        continue
                    try:
                        await bot.send_message(chat_id, part, parse_mode="Markdown")
                    except Exception:
                        await bot.send_message(chat_id, part)

    async def _send_file_paths(self, chat_id: int, paths: List[Path]) -> None:
        bot = self.application.bot
        for p in paths:
            try:
                if p.suffix.lower() in IMAGE_EXTS:
                    with open(p, "rb") as f:
                        await bot.send_photo(chat_id, photo=f)
                else:
                    with open(p, "rb") as f:
                        await bot.send_document(chat_id, document=f)
            except Exception as e:
                logger.warning("Failed to send file %s: %s", p, e)

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
        if not await self._check_access(update):
            return
        query = update.callback_query
        await query.answer()
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
            choice = data.split(":", 1)[1]
            await query.edit_message_text(messages.SELECTED.format(choice=choice))
            await self._maybe_capture_outside_approval(user_id, choice)
            chat_id = chat.id

            async def run_task() -> None:
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
                    )
                except Exception as e:
                    logger.error("Option reply failed: %s", e, exc_info=True)
                    await app.bot.send_message(chat_id, messages.PROCESSING_FAILED.format(error=e))

            async def on_overflow() -> None:
                await app.bot.send_message(chat_id, messages.QUEUE_BUSY)

            await self._enqueue_user_task(user_id, run_task, on_overflow)
            return

        if data.startswith("resume:"):
            await self._handle_resume_callback(update, query, user_id, chat)
            return

        if data.startswith("model:"):
            model_name = data.split(":", 1)[1]
            session = await session_manager.get_session(user_id)
            session["model"] = model_name
            session["session_id"] = None
            session["new_session"] = True
            await session_manager.update_session(user_id, session)
            self._runtime_active_sessions.discard(user_id)
            label = _MODEL_LABELS.get(model_name, model_name)
            await query.edit_message_text(
                messages.MODEL_SWITCHED.format(label=label)
                + "\n"
                + messages.MODEL_SWITCH_WARNING
            )
            return

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
                )
            except Exception as e:
                logger.error("Resume continuation failed: %s", e, exc_info=True)
                await app.bot.send_message(chat_id, messages.RESUME_FAILED.format(error=e))

        async def on_overflow() -> None:
            await app.bot.send_message(chat_id, messages.QUEUE_BUSY)

        await self._enqueue_user_task(user_id, run_task, on_overflow)


bot = TelegramBot()
