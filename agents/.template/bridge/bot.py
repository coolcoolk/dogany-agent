"""Telegram bot: handlers, commands, callbacks, queue, send logic.

PTB v20 Application with a manual lifecycle that restarts polling on network
blips (launchd owns crash-restart). Per-user serialized queue (max 3 in-flight,
/stop priority), allowlist + stale-message drop, marker-aware sending.
"""

import asyncio
import html
import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

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

from bridge import btw as btw_module
from bridge import ext
# ESTATE:BEGIN /health surface import -- healthcmd reads estate-only paths (the estate SoT registry, launchctl labels); the module is manifest-excluded, so the public build must not import it (DGN-1276 gate-3 closure)
from bridge import healthcmd
# ESTATE:END
from bridge import fastpath, heartbeat, live_model, messages, model_picker, model_state, owner_belt
from bridge.i18n import skill_display_name
from bridge.config import (
    AUTO_RESUME,
    AUTO_RESUME_MAX,
    BRIDGE_INFLIGHT_DEBOUNCE_S,
    BRIDGE_INFLIGHT_DEFER_CAP_S,
    BRIDGE_INFLIGHT_INTERRUPT_NOTICE,
    PACKAGE_DIR,
    PROCESS_TIMEOUT,
    REPLY_LINK_ENABLED,
    REPLY_LINK_LATENCY_S,
    config,
    log_claude_cli_resolution,
    notify_silent,
)
from bridge import ownership
from bridge.formatting import (
    FOLD_OPEN_MARKER,
    IMAGE_EXTS,
    balance_telegram_html,
    code_segment_html,
    compose_fold_block,
    contains_telegram_html,
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
# ESTATE:BEGIN idrill marker stripper import -- the estate render seam (DGN-924)
from bridge.formatting import strip_idrill_marker
# ESTATE:END
# ESTATE:BEGIN idrill arm-file renderer import -- bridge.artifacts is the idrill engine's file layer (IDRILL-ARM-CONTRACT.md), manifest-excluded; every artifacts.* use site already sits inside the idrill regions (DGN-1276 gate-3 closure)
from bridge import artifacts
# ESTATE:END
from bridge.machine_gate import (
    RAIL_CONSUMER,
    RAIL_MODEL,
    RAIL_UNKNOWN,
    MachineLineAlertLedger,
    apply_machine_line_gate,
    set_machine_line_alert_sink,
    strip_no_push_sentinel,
    today_key,
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
    body_lists_options,
    build_option_keyboard,
    extract_marker_labels,
    extract_options,
    has_options_marker,
    is_number_handle,
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
from bridge import notice_spool
from bridge.sdk_bridge import ChatResponse, PROJECT_ROOT, TYPING_INTERVAL, sdk_bridge
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
#   /authsync  -- RETIRED (DGN-1050): its file->keychain overwrite re-injected
#                 superseded refresh tokens after CLI runtime rotations and
#                 killed the whole estate's auth. A hidden CommandHandler stub
#                 remains registered (off-menu) solely so a typed /authsync is
#                 answered with the retirement notice instead of being
#                 forwarded to the SDK session by the catch-all. It can never
#                 write credentials. Must NOT reappear in this menu.
#   /health    -- DGN-986 D1 owner amendment (2026-08-21): "authsync 다음,
#                 help 앞" (after authsync, before help). authsync was
#                 retired from this menu by DGN-1050 AFTER D1 was decided,
#                 so the literal anchor ("after authsync") no longer exists
#                 in this list -- this merge (DGN-986 integration) places
#                 health immediately before help, preserving the half of D1
#                 that is still satisfiable ("help 앞"/before help) and the
#                 DGN-997/DGN-1050 ordering as landed on main. NOT re-run
#                 past the owner -- flagged for confirmation.
# ESTATE:BEGIN DGN-1417 update machinery lives in the canonical repo
# (.instance.conf DOGANY_REPO_ROOT), not in the instance: an instance has no
# scripts/pack and no releases/.  PROJECT_ROOT is the fallback for a
# canonical checkout run as its own instance.
def _update_machinery_root() -> Path:
    try:
        for line in (PROJECT_ROOT / ".instance.conf").read_text(encoding="utf-8").splitlines():
            if line.startswith("DOGANY_REPO_ROOT="):
                root = Path(line.split("=", 1)[1].strip())
                if (root / "scripts" / "pack" / "update_surface.py").is_file():
                    return root
    except OSError:
        pass
    return PROJECT_ROOT


def _update_plan_usable(rc: int, plan: Path) -> bool:
    """DGN-1843: is this planner run a plan?  rc 0 = yes.  rc 2 means BOTH
    "an item is blocked, the rest was planned" and "aborted, no plan"; only
    the former ends its emit with PLAN-END (scripts/pack/update_plan.sh
    header, same rule as update_surface.plan_usable).  One member's blocked
    pack must not read as a lookup failure in every agent's notice."""
    if not plan.is_file():
        return False
    if rc == 0:
        return True
    if rc != 2:
        return False
    try:
        lines = plan.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    return bool(lines) and lines[0] == "PLAN-EMIT-V1" and lines[-1].startswith("PLAN-END\t")


# The session-start notice's single re-check after a lookup failure ("몇 분
# 뒤 한 번 더 확인해서 알려드릴게요", rehearsal 2026-09-26 items 21/23).
UPDATE_OFFER_RETRY_S = int(os.environ.get("DOGANY_UPDATE_OFFER_RETRY_S", "300") or 300)
# DGN-1749: a crew member's session-inbox drop handing an update offer to
# this leader (update_surface.offer_handoff).  Machine-only: the inbox loop
# sends the notice itself through the session-start seat and never injects
# the drop as a model turn.
UPDATE_OFFER_HANDOFF_PREFIX = "[update-offer-handoff]"
# ESTATE:END
# First contact after /claim (rehearsal 2026-10-02): the claim no longer
# answers with a fixed ownership line; it opens the agent's own first turn so
# the very first bubble is the agent introducing itself. Model-facing turn
# text, not owner copy: it states what happened and defers WHAT to say to the
# agent's first-contact rules (persona onboarding block / onboarding-check.py)
# -- no example sentences here by rule.
# DGN-1842: the turn also restates the opener constraints the Stop gate
# (routines/onboarding-gate-stop.py, DGN-1837) enforces, so the first draft
# passes and no blocked draft is generated at all. Rules only, no wording.
# DGN-1848: the identity is a personal agent with no owner possessive -- a
# possessive identity plus the pronoun ban made the model invent a
# substitute possessive (rehearsal 2026-10-04).
FIRST_CONTACT_TURN = (
    "[bridge:first-contact] The owner has just claimed this bot (/claim "
    "succeeded) and is in the chat now. Nothing has been said yet: open the "
    "conversation with your first-contact message, following your "
    "first-contact rules. The opener is at most two short sentences; it "
    "addresses the user with no second-person pronoun and no name, title or "
    "address guessed for the user, and it asks no question. With no kit yet "
    "(Primary focus still the placeholder) it says only that you are a "
    "personal agent, newly born, plus a light invitation to say hello -- no "
    "task, example, domain or capability. It names no owner at all: no "
    "second-person possessive, and no substitute possessive or place word "
    "in front of personal agent. Compose the wording "
    "yourself in the instance language. Do not mention claiming, ownership, "
    "codes or this note. Send it once; end with a plain reply (never "
    "NO_PUSH)."
)


def _load_instance_module(name: str, path: Path):
    """An instance-root routines module (the first-contact helpers below),
    or None when it is absent or fails to load -- fail-open, the callers
    then take their bare/model-turn path. Deliberately not
    artifacts._load_path_module: bridge.artifacts is the idrill file layer,
    manifest-excluded from the public bridge with its import regioned
    (DGN-1276), so a core path through it is a NameError there."""
    try:
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        logger.warning("cannot load %s", path, exc_info=True)
        return None


def first_contact_turn() -> str:
    """FIRST_CONTACT_TURN plus the second-person forms (and, DGN-1848, the
    substitute possessives) the opener gate blocks, read from the single
    term list (routines/lib/first_contact_terms.py) so the turn and the gate
    never fork. The bare turn when the list cannot be loaded."""
    terms = _load_instance_module(
        "first_contact_terms",
        PROJECT_ROOT / "routines" / "lib" / "first_contact_terms.py",
    )
    forms = getattr(terms, "KO_SECOND_PERSON", None) if terms else None
    if not forms:
        return FIRST_CONTACT_TURN
    text = (
        FIRST_CONTACT_TURN
        + " Second-person forms the gate blocks (use none): "
        + ", ".join(forms)
        + "."
    )
    possessives = getattr(terms, "KO_SUBSTITUTE_POSSESSIVE", None)
    if possessives:
        text += (
            " Substitute possessives the gate blocks (use none): "
            + ", ".join(possessives)
            + "."
        )
    return text


# DGN-1849 (dec-243): on a kit-pending main the opener's wording is fixed
# (i18n onboarding.opener), so the bridge sends it itself right after /claim
# -- no model turn at all -- and records it on the first-contact state
# (routines/lib/first_contact_state.py) so the post-opener logic holds.
# routines/onboarding-screen.py owns the decision (opener_due) and the copy
# read (opener_text); a domain-role instance keeps the model-composed opener
# (FIRST_CONTACT_TURN above).
ONBOARDING_SCREEN_PATH = Path("routines") / "onboarding-screen.py"
FIRST_CONTACT_STATE_PATH = Path("routines") / "lib" / "first_contact_state.py"


def machine_opener_text() -> str:
    """The fixed opener when this instance's opener is machine-sent (kit-
    pending main, onboarding pending), else '' (model-composed opener)."""
    verb = _load_instance_module(
        "onboarding_screen", PROJECT_ROOT / ONBOARDING_SCREEN_PATH
    )
    if verb is None:
        return ""
    try:
        if not verb.opener_due(str(PROJECT_ROOT)):
            return ""
        return verb.opener_text(str(PROJECT_ROOT)) or ""
    except Exception as e:
        logger.warning("machine opener check failed: %s", e)
        return ""


def record_machine_opener(text: str) -> bool:
    state = _load_instance_module(
        "first_contact_state", PROJECT_ROOT / FIRST_CONTACT_STATE_PATH
    )
    if state is None:
        return False
    try:
        return bool(state.record_opener(
            state.resolve_log_dir(str(PROJECT_ROOT)), text,
            boundary=state.SOURCE_BRIDGE, q1_due=True,
            source=state.SOURCE_BRIDGE,
        ))
    except Exception as e:
        logger.warning("machine opener record failed: %s", e)
        return False


COMMAND_MENU_SPEC = [
    ("new",      lambda: messages.CMD_DESC_NEW),
    ("stop",     lambda: messages.CMD_DESC_STOP),
    # DGN-1362: /usage ships. routines/claude-usage.sh sits at the instance
    # root on BOTH sides of the fork (OSS 2.0.1), so the row is public.
    ("usage",    lambda: messages.CMD_DESC_USAGE),
    # ESTATE:BEGIN DGN-1417 update surface is Dogany-only
    ("update",   lambda: messages.CMD_DESC_UPDATE),
    # ESTATE:END
    ("model",    lambda: messages.CMD_DESC_MODEL),
    ("btw",      lambda: messages.CMD_DESC_BTW),
    ("queue",    lambda: messages.CMD_DESC_QUEUE),
    ("skills",   lambda: messages.CMD_DESC_SKILLS),
    ("resume",   lambda: messages.CMD_DESC_RESUME),
    # DGN-997: owner-only explicit restart command.
    ("restart",  lambda: messages.CMD_DESC_RESTART),
    # ESTATE:BEGIN /health menu row -- DGN-1435: pulled off the command surface
    # (owner call 2026-09-12: partial repair rejected, full rewrite before it
    # returns). Inactive until the rewrite re-adds the row.
    # ESTATE:END
    ("help",     lambda: messages.CMD_DESC_HELP),
]

STALE_MESSAGE_SECONDS = 20 * 60
# ^ NO recorded rationale. A 2026-09-10 census (bot.py history, bridge/*.md,
# CHANGELOG, releases/, worklog tickets, the OSS mirror) found the constant
# with zero explanatory comment in every tree it exists in, and no ticket that
# introduced or justified it -- only downstream code that ASSUMES it
# (DGN-841 / DGN-922 / DGN-966 / DGN-1050 comments below). What it actually
# protects today is narrow: process first boot already drops every queued
# update Telegram-side (start_polling drop_pending_updates=first_boot), so
# this gate only covers updates replayed on an IN-PROCESS polling
# re-establish (network loss / laptop sleep-wake).
# It is a poor fit for CALLBACK updates: a notification button is normally
# tapped hours later (measured 2026-09-10: 5 h 57 m), and the age it measures
# is the NOTIFICATION's, not the tap's. Whether callbacks should keep this
# number, get their own, or be exempt entirely is an open product decision
# (DGN-841's third checkbox). What did NOT wait for that decision: a drop is
# no longer SILENT -- see _announce_stale_drop below.
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
# DGN-330: after this many seconds of unresolved conflict, switch the retry
# cadence from linear (CONFLICT_BACKOFF_BASE -> CONFLICT_BACKOFF_MAX) to pure
# exponential starting from CONFLICT_BACKOFF_MAX and doubling up to
# CONFLICT_BACKOFF_EXPO_CAP. One ERROR line is emitted on the first entry into
# this mode; subsequent retries are silent until the conflict clears.
CONFLICT_BACKOFF_EXPONENTIAL_AFTER = 600  # 10 minutes
CONFLICT_BACKOFF_EXPO_CAP = 300  # 5 minutes, cap for exponential backoff
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
# DGN-351: Telegram splits a single long message at the 4096-char limit into N
# separate text updates, delivered back-to-back. A message at/above this length
# is a split-boundary candidate: buffer it and wait SPLIT_MERGE_WINDOW seconds
# for the continuation in the same chat, then dispatch all parts as ONE turn.
# Short messages never enter this path (zero-delay preserved). If a follow-up
# part is itself boundary-length, the window is extended so 3+ part splits merge
# too. Accepted false-positive: an unrelated message arriving inside the window
# after a boundary-length message is merged -- negligible, not defended against.
SPLIT_MERGE_THRESHOLD = 4000
SPLIT_MERGE_WINDOW = 3.0
# An out-of-root / protected-path one-time approval expires this many seconds
# after the deny prompt was shown, so a stale grant can never authorize a much
# later call (F7 hardening).
OUTSIDE_APPROVAL_TTL = 600  # 10 minutes
# Restart re-trigger latch window (formerly AUTHSYNC_RESTART_LATCH_S, DGN-994;
# the /authsync CTA that introduced it is retired per DGN-1050 -- the latch
# itself is retained for /restart): duplicate /restart triggers inside this
# window are dropped (restart is already in flight). Window-based (not a
# sticky flag) so an aborted restart (e.g. smoke-gate exit 4 in the detached
# worker) cannot leave the command dead until the next process restart.
RESTART_LATCH_S = 120
# DGN-1010 layer-2: terminal-state backstop for self_restart.sh. The restart
# completion push is owned by the detached worker -- a single point of
# failure (2026-08-22: the worker was reaped with the old bridge's process
# group and the owner's CTA tap ended in silence). Initial grace covers the
# worker's normal runway (delay ~6s + poll wait <=60s + push); the poll
# keeps waiting while the worker is still alive (e.g. a long --verify).
RESTART_BACKSTOP_INITIAL_S = 90
RESTART_BACKSTOP_POLL_S = 60
# DGN-1588/DGN-1591: session-inbox drops whose CONTENT starts with one of
# these prefixes are self-record injections -- the turn they trigger is
# quiet by default (output suppressed unless the model ends with the bare
# PUSH sentinel; see sdk_bridge.inject_background_turn / _flush_proactive).
# push.sh _record_outbound puts its prefix first. The legacy operator-alert
# prefix remains quiet for compatibility; R2 removes its writer entirely.
QUIET_INJECT_PREFIXES = ("[outbound-record]", "[operator-alert]")
# DGN-376: auto link previews are OFF on every outbound text send by default.
# A reply opts back in with a standalone "link_preview::" line (stripped before
# sending), which restores Telegram's default preview for that reply's text.
LINK_PREVIEW_OFF = LinkPreviewOptions(is_disabled=True)

# DGN-192: performance ordering for the /model picker -- strongest first.
# Unknown names sort last (rank 99 in the sort key) alphabetically. Its keys
# are also the built-in short names; display names and versions come from the
# model table (bridge/model_picker.py, DGN-1814 r3), never from the bridge.
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


# Hardcoded last-rung fallback when nothing else in the chain yields a model.
DEFAULT_MODEL = "sonnet"


def _known_models() -> List[str]:
    """Short names accepted for persistence/resolution: the env whitelist plus
    the built-in short names (full 'claude-*' ids are accepted by the validator)."""
    return list(dict.fromkeys([*_model_whitelist(), *_MODEL_PERF_RANK.keys()]))


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
        # DGN-1385: per-user typing-indicator refresh while a message sits
        # buffered (debounce or /queue) waiting for the in-flight turn to end.
        # Telegram's typing status expires after ~5s; this task re-sends it
        # every TYPING_INTERVAL (sdk_bridge) so the sender sees "received" for
        # the whole wait, not just an instant. One task per user -- cancelled
        # the moment the buffer drains or is discarded.
        self._typing_refresh_tasks: Dict[int, asyncio.Task] = {}
        # DGN-1016: monotonic timestamp of the FIRST auto-interrupt deferral
        # of the current in-flight wait (set when a debounce expiry skips the
        # interrupt because live background tasks exist). Cleared whenever the
        # wait ends: drain (turn finished), actual interrupt, or /stop. Bounds
        # the cumulative deferral to BRIDGE_INFLIGHT_DEFER_CAP_S.
        self._interrupt_deferred_since: Dict[int, float] = {}
        self._audio_dir = config.bot_data_dir / "audio"
        # Inbound photos land here: ephemeral runtime buffer, pruned after 7 days
        # by the cleanup cron. photo input restored.
        self._image_dir = config.bot_data_dir / "images"
        # media_group_id -> buffered album state, flushed as one task.
        self._media_groups: Dict[str, dict] = {}
        # DGN-1209: the machine-line gate's READER. Unregistered machine-shape
        # lines that PASS on a consumer/unknown rail become an owner-chat
        # notice (loud), dedup'd per (rail, token, day) by a durable marker
        # written only AFTER the notice send succeeded.
        self._machine_alert_ledger = MachineLineAlertLedger(
            config.bot_data_dir / "machine-line-alerts"
        )
        self._machine_alert_inflight: set[tuple] = set()
        set_machine_line_alert_sink(self._machine_line_alert_sink)
        self._media_group_lock = asyncio.Lock()
        # chat_id -> buffered split-message state (DGN-351). A boundary-length
        # (>= SPLIT_MERGE_THRESHOLD) text opens a merge window; continuation
        # parts in the same chat are concatenated and flushed as one turn.
        self._split_buffers: Dict[int, dict] = {}
        self._split_buffer_lock = asyncio.Lock()
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
        # block the main conversation for up to BTW_TURN_TIMEOUT seconds
        # (DGN-1177: now the full PROCESS_TIMEOUT budget).
        self._btw_fork_tasks: Dict[int, set] = {}

    # --- lifecycle ---

    def build(self) -> None:
        # Explicit HTTP timeouts. PTB defaults (read_timeout=5s) are SHORTER than
        # the long-poll timeout, so every getUpdates would raise TimedOut and the
        # bot never finishes starting. The get_updates request needs a read_timeout
        # comfortably longer than the long-poll interval.
        # DGN-1736 slice 0: every owner-bound send rides this transport, so it
        # carries the observe-only owner belt (logs OWNER_BELT_HIT on a leaked
        # directive line, never mutates the request).
        request = owner_belt.OwnerGuardRequest(
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
        # DGN-1814: name the Claude CLI every launch site will use, once.
        log_claude_cli_resolution()
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
        # DGN-330: set to True the first time we cross
        # CONFLICT_BACKOFF_EXPONENTIAL_AFTER seconds of sustained conflict; used
        # to gate the one-time ERROR log entry that announces the switch to the
        # slow exponential cadence.
        conflict_in_exponential: bool = False
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
            restart_backstop_task = None
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
                if first_boot:
                    # DGN-1173: extension boot hooks, run once per process
                    # right before the "Bot is running" marker (the DGN-986
                    # boot-snapshot slot, generalized).  run_boot_hooks()
                    # absorbs ALL failures (one WARNING line per hook) -- a
                    # boot hook can never block the boot.
                    ext.run_boot_hooks()
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
                # DGN-950 seam 2: countdown starts share the dashboard's
                # turn_active deferral so a rest-timer send can never race
                # ahead of the in-flight model reply.
                countdown_task = asyncio.create_task(
                    CountdownDriver(
                        bot=self.application.bot,
                        turn_active=self._user_turn_active,
                    ).run()
                )
                # DGN-1010 layer-2: a restart always brings up a NEW bridge,
                # so this process is the natural second leg for the restart
                # completion push. Same lifecycle as the tasks above.
                restart_backstop_task = asyncio.create_task(
                    self._restart_backstop_loop()
                )
                # ESTATE:BEGIN DGN-1417 update ledger backstop (one-shot per boot)
                asyncio.create_task(self._update_ledger_sweep())
                # ESTATE:END
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
                    conflict_in_exponential = False
                if conflict_since is None:
                    conflict_since = time.time()
                elapsed = time.time() - conflict_since
                if elapsed >= CONFLICT_BACKOFF_EXPONENTIAL_AFTER:
                    # DGN-330: sustained past the slow-backoff threshold: switch
                    # to exponential cadence starting from the linear cap. One
                    # ERROR line on first entry; subsequent retries are silent.
                    if not conflict_in_exponential:
                        conflict_in_exponential = True
                        conflict_backoff = CONFLICT_BACKOFF_MAX
                        logger.error(
                            "getUpdates Conflict sustained for %ds; another instance "
                            "still holds this bot token -- switching to slow "
                            "exponential backoff (cap %ds)",
                            int(elapsed), CONFLICT_BACKOFF_EXPO_CAP,
                        )
                    # No further log lines while in exponential mode.
                elif elapsed >= CONFLICT_SUSTAINED_SECONDS:
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
                if conflict_in_exponential:
                    conflict_backoff = min(CONFLICT_BACKOFF_EXPO_CAP, conflict_backoff * 2)
                else:
                    conflict_backoff = min(CONFLICT_BACKOFF_MAX, conflict_backoff * 2)
                continue
            except PollingRestart:
                conflict_since = None
                conflict_backoff = CONFLICT_BACKOFF_BASE
                conflict_in_exponential = False
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
                conflict_in_exponential = False
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
                if restart_backstop_task and not restart_backstop_task.done():
                    restart_backstop_task.cancel()
                    try:
                        await restart_backstop_task
                    except asyncio.CancelledError:
                        pass
                if stop_event.is_set():
                    # DGN-946: flush in-flight fold bubbles while the HTTP
                    # client is still alive. The late CancelledError cleanup
                    # (asyncio.run teardown, after "Bot stopped") otherwise
                    # hits the torn-down client and freezes bubbles in their
                    # last live form. Real stop only: transient restarts
                    # (Conflict/NetworkError) keep their turns alive and must
                    # not collapse live folds.
                    await sdk_bridge.flush_folds_for_shutdown()
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
                owner_id = (
                    owner_ids[0] if owner_ids
                    else ownership.read_owner_lock(config.bot_data_dir)
                )
                if owner_id is None:
                    continue  # Unclaimed or locked out: no recipient.
                if self._user_turn_active(owner_id):
                    continue  # defer: never race a live turn
                path = files[0]
                try:
                    text = path.read_text(encoding="utf-8").strip()
                except UnicodeDecodeError as e:
                    # DGN-inbox-utf8: a file that fails to decode is a poison
                    # pill -- files[0] would pick the SAME undecodable file
                    # again next tick forever (retried every poll_secs, and
                    # blocking any later files behind it in sort order). Move
                    # it aside once and log a single error instead of one
                    # error per 20s poll. ".corrupt" no longer matches the
                    # "*.md" glob above, so it is never picked up again.
                    quarantine_path = path.with_name(path.name + ".corrupt")
                    try:
                        path.rename(quarantine_path)
                        logger.error(
                            "session-inbox undecodable, quarantined %s -> %s: %s",
                            path, quarantine_path.name, e,
                        )
                    except OSError as rename_err:
                        logger.error(
                            "session-inbox undecodable AND quarantine failed "
                            "for %s: %s (rename error: %s)",
                            path, e, rename_err,
                        )
                    continue
                except Exception as e:
                    logger.error("session-inbox read failed for %s: %s", path, e)
                    continue
                if not text:
                    path.unlink(missing_ok=True)
                    continue
                # ESTATE:BEGIN DGN-1749 a member's update-offer handover is sent, not injected
                if text.startswith(UPDATE_OFFER_HANDOFF_PREFIX):
                    # The member left this leader's update-offer-request;
                    # the session-start sender takes it and posts the
                    # notice with no model turn and no other owner text.
                    path.unlink(missing_ok=True)
                    self._kick_update_offer(owner_id)
                    logger.info("session-inbox update-offer handover -> notice sender: %s", path.name)
                    continue
                # ESTATE:END
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
                # DGN-1588/DGN-1591: self-record drops (outbound-record /
                # operator-alert) inject QUIET -- the turn's output is
                # suppressed unless the model ends it with the bare PUSH
                # sentinel (see sdk_bridge._flush_proactive). Keyed on the
                # content prefix (push.sh _record_outbound; the legacy
                # operator-alert writer was removed in DGN-1591 R2).
                quiet = text.startswith(QUIET_INJECT_PREFIXES)
                ok = await sdk_bridge.inject_background_turn(
                    owner_id, text, quiet=quiet
                )
                if ok:
                    path.unlink(missing_ok=True)
                    logger.info(
                        "session-inbox injected%s: %s",
                        " (quiet)" if quiet else "", path.name,
                    )
                # not ok -> stream missing or busy; keep the file, retry next tick
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # The watcher must never die silently on a transient error.
                logger.error("session-inbox loop error: %s", e)

    async def _restart_backstop_loop(self) -> None:
        """DGN-1010 layer-2: terminal-state backstop for self_restart.sh.

        Contract: one restart tap = exactly ONE terminal notification to the
        owner (complete or failed), never silence. The completion push is
        normally owned by the detached self_restart.sh worker; that worker is
        a single point of failure (2026-08-22 08:00: it was reaped with the
        old bridge's process group and the owner's CTA tap ended in silence).

        A restart, by construction, always brings up a NEW bridge process --
        so this process is the second leg. The worker arms
        <bot_data_dir>/state/restart-pending.marker just before severing the
        old bridge and CLAIMS it (atomic rename) before its own terminal
        push. Here: if the marker is present and the worker pid recorded in
        it is dead, the worker can never push -- claim the marker ourselves
        and terminal-close the restart. The rename is the mutex: exactly one
        of {worker, backstop} wins, so no duplicate notice is possible.

        Lifecycle: one task per polling start, cancelled with the sibling
        tasks. Normal boots (no marker) exit after a single check. A live
        worker (e.g. long --verify) keeps ownership -- we just keep waiting.
        """
        marker = config.bot_data_dir / "state" / "restart-pending.marker"
        # Layer-absence probe (2026-08-22 incident, second lesson): layer 1
        # (the marker writer in self_restart.sh) was silently reverted by a
        # framework update while this backstop stayed in place -- neither
        # layer could see the other was gone, so the CTA silence recurred for
        # two days. One static check per boot: if the sibling script no
        # longer references the marker, this backstop is structurally dead --
        # say so in the log instead of waiting forever in silence.
        try:
            writer = PACKAGE_DIR / "self_restart.sh"
            if marker.name not in writer.read_text(encoding="utf-8"):
                logger.warning(
                    "restart backstop: self_restart.sh no longer arms %s "
                    "(layer 1 missing -- update regression?); the backstop "
                    "can never fire until layer 1 is restored",
                    marker.name,
                )
        except OSError:
            pass  # probe is best-effort; never block the backstop itself
        await asyncio.sleep(RESTART_BACKSTOP_INITIAL_S)
        while True:
            try:
                if not marker.is_file():
                    return
                worker_pid = None
                try:
                    for line in marker.read_text(encoding="utf-8").splitlines():
                        if line.startswith("worker_pid="):
                            worker_pid = int(line.split("=", 1)[1].strip())
                            break
                except (OSError, ValueError):
                    worker_pid = None  # unreadable -> treat the worker as gone
                if worker_pid is not None:
                    try:
                        os.kill(worker_pid, 0)
                        # Worker alive: it still owns the terminal push.
                        await asyncio.sleep(RESTART_BACKSTOP_POLL_S)
                        continue
                    except ProcessLookupError:
                        pass  # dead -> orphaned restart, take over
                    except PermissionError:
                        # pid exists under another uid (not our worker, but
                        # err on the safe side and treat it as alive).
                        await asyncio.sleep(RESTART_BACKSTOP_POLL_S)
                        continue
                claimed = marker.with_name(f"{marker.name}.claimed.{os.getpid()}")
                try:
                    marker.rename(claimed)
                except FileNotFoundError:
                    return  # worker claimed in the same instant: it pushed
                except OSError as e:
                    logger.error("restart backstop claim failed: %s", e)
                    return
                try:
                    owner_ids = config.allowed_user_ids
                    owner_id = (
                        owner_ids[0] if owner_ids
                        else ownership.read_owner_lock(config.bot_data_dir)
                    )
                    if owner_id is not None and self.application:
                        await self.application.bot.send_message(
                            chat_id=owner_id,
                            text=messages.RESTART_BACKSTOP_NOTICE,
                        )
                    logger.warning(
                        "restart backstop fired: worker pid %s dead with "
                        "pending marker; terminal-closed the restart",
                        worker_pid,
                    )
                    # DGN-1012 registration call only: the backstop just
                    # delivered the terminal notice, so release the ledger
                    # obligation (third leg) too -- otherwise the hourly
                    # sweep would re-notify an already-closed restart.
                    try:
                        subprocess.run(
                            [
                                "/usr/bin/python3",
                                str(
                                    config.bot_data_dir.parent
                                    / "routines"
                                    / "terminal-state-ledger.py"
                                ),
                                "close", "--id", "restart-pending",
                                "--state", "done",
                                "--note",
                                "bridge backstop terminal-closed "
                                f"(worker pid {worker_pid} dead)",
                            ],
                            capture_output=True,
                            timeout=10,
                            check=False,
                        )
                    except Exception as tsl_err:
                        logger.error(
                            "terminal-state ledger close failed: %s", tsl_err
                        )
                finally:
                    claimed.unlink(missing_ok=True)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never die silently -- but also never spin: one error, one log,
                # done. The worker path is unaffected either way.
                logger.error("restart backstop loop error: %s", e)
                return

    async def _graceful_shutdown(self, force: bool = False) -> None:
        if not self.application:
            return
        try:
            if not force:
                await asyncio.wait_for(self._do_graceful_stop(), timeout=5.0)
            else:
                # DGN-330 force path: skip updater/application stop (the
                # application may not be in a started state), but ALWAYS call
                # shutdown() so the underlying HTTPXRequest/httpx.AsyncClient
                # instances are closed. Without this, each conflict retry leaks
                # two AsyncClient objects (one for request, one for
                # get_updates_request), causing fd exhaustion (OSError [Errno
                # 24]) under sustained token contention.
                await self.application.shutdown()
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
        # ESTATE:BEGIN /usageretry registration -- its handler body (_cmd_usage_retry) is a regioned usage-defer method; registering it in the public build would AttributeError at build()
        # DGN-841 A: STALE-gate-free slash route for the usage-defer manual
        # retry (buttons on aged messages die at the 20-min gate). Group 0
        # registration also keeps the catch-all skill forwarder below from
        # swallowing it (it skips names owned by group-0 CommandHandlers).
        app.add_handler(CommandHandler("usageretry", self._cmd_usage_retry))
        # ESTATE:END
        # DGN-1050: /authsync is RETIRED (see _cmd_authsync). The handler
        # stays registered (off-menu) so a typed /authsync gets the
        # retirement notice instead of falling through to the catch-all
        # skill forwarder and landing in the SDK session.
        app.add_handler(CommandHandler("authsync", self._cmd_authsync))
        # DGN-997: owner-only explicit restart command (the missing surface
        # that made an agent announce a non-existent /restart in the first place).
        app.add_handler(CommandHandler("restart", self._cmd_restart))
        # ESTATE:BEGIN DGN-1417 update surface
        app.add_handler(CommandHandler("update", self._cmd_update))
        # ESTATE:END
        # ESTATE:BEGIN /health registration -- DGN-1435: pulled off the command
        # surface (owner call 2026-09-12: partial repair rejected, full rewrite
        # before it returns). Inactive until the rewrite re-registers the handler;
        # _cmd_health stays defined below as the rewrite's starting point.
        # ESTATE:END
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
                    # The drop used to be TOTALLY silent: no log line, and for a
                    # callback no query.answer() either, so the owner's tap did
                    # nothing at all on screen and left no server-side trace
                    # (measured 2026-09-10, health-observer opt: button tapped
                    # ~6 h after the notification). Announce before returning.
                    await self._announce_stale_drop(update, age)
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

    def _is_owner_user(self, user) -> bool:
        """True when `user` is the established owner under the current mode.

        Read-only twin of the ownership branch in _check_access, extracted so a
        pre-gate path can ask "may I say anything to this sender at all?"
        without duplicating (or weakening) the decision. Both born-locked modes
        answer False on purpose:
          MODE_CLAIM      -- an unclaimed bot must never reveal that it exists.
          MODE_LOCKED_OUT -- anomalous recovery state, deny all, stay silent.
        """
        if user is None:
            return False
        mode, owner_id = ownership.resolve_owner(
            config.allowed_user_ids, config.bot_data_dir
        )
        if mode == ownership.MODE_AUTHORITATIVE:
            return user.id in config.allowed_user_ids
        if mode == ownership.MODE_OWNER_LOCK:
            return user.id == owner_id
        return False

    # Cap for a button label quoted back inside the expiry alert. Telegram's
    # answerCallbackQuery text limit is 200 chars; the wording around the label
    # already spends ~60, so the quote gets a conservative slice of the rest.
    _STALE_ALERT_LABEL_MAX = 90
    # Numbered-option button text is "N. label" (build_option_keyboard prepends
    # the number); the number is keyboard bookkeeping, not part of what the
    # owner would type, so it is stripped before quoting.
    _OPT_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\s*[.)]\s*")

    def _stale_callback_alert_text(self, query) -> str:
        """Wording for the expired-tap alert; quotes the button label if usable.

        The label IS the sentence the tap would have sent (options.resolve_choice
        -> process_message(user_message=...)), so quoting it back gives the owner
        a re-issue path that needs no knowledge of the bridge: type that.
        Degrades to the label-free wording whenever the label cannot be
        recovered (keyboard gone / no match) or is a number handle (DGN-881).
        """
        data = getattr(query, "data", None)
        message = getattr(query, "message", None)
        markup = getattr(message, "reply_markup", None)
        keyboard = getattr(markup, "inline_keyboard", None)
        if not data or not keyboard:
            return messages.STALE_CALLBACK_EXPIRED_NOLABEL.format(
                minutes=STALE_MESSAGE_SECONDS // 60)
        label = resolve_choice(data, keyboard)
        label = self._OPT_NUMBER_PREFIX_RE.sub("", label or "").strip()
        if not label or is_number_handle(label):
            return messages.STALE_CALLBACK_EXPIRED_NOLABEL.format(
                minutes=STALE_MESSAGE_SECONDS // 60)
        if len(label) > self._STALE_ALERT_LABEL_MAX:
            label = label[: self._STALE_ALERT_LABEL_MAX - 1].rstrip() + "…"
        return messages.STALE_CALLBACK_EXPIRED.format(
            choice=label, minutes=STALE_MESSAGE_SECONDS // 60)

    async def _announce_stale_drop(self, update: Update, age: float) -> None:
        """Make a STALE-gate drop observable instead of silent.

        Two effects, deliberately asymmetric:

        LOG (always, every dropped update). The drop was previously invisible
        on the server too -- DGN-841 confirmed the cause in 2026-08 and the
        2026-09-10 recurrence still had to be reconstructed from ABSENT
        evidence because not one line was written. Callbacks log at WARNING (a
        tap is a deliberate owner act that just died); plain messages log at
        INFO (a polling re-establish can replay a burst of them, and that burst
        is normal, not a defect).

        ALERT (callbacks only, owner only). Answering the query is the only way
        the tap stops being a no-op on the owner's screen. It is also an API
        call made BEFORE the ownership branch of _check_access, so it is gated
        on _is_owner_user: a stranger's stale tap still gets total silence and
        cannot probe for the bot's existence. Fail-soft throughout -- an alert
        that cannot be delivered must never turn a drop into an exception.
        """
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        if query is None:
            logger.info(
                "STALE drop: message from user %s in chat %s, age %.0fs > %ds",
                getattr(user, "id", None), getattr(chat, "id", None),
                age, STALE_MESSAGE_SECONDS,
            )
            return
        logger.warning(
            "STALE drop: callback %r from user %s in chat %s, age %.0fs > %ds "
            "-- button tap not executed",
            getattr(query, "data", None), getattr(user, "id", None),
            getattr(chat, "id", None), age, STALE_MESSAGE_SECONDS,
        )
        if not self._is_owner_user(user):
            return
        try:
            await query.answer(
                self._stale_callback_alert_text(query), show_alert=True
            )
        except Exception as e:
            logger.warning("STALE drop alert failed (ignored): %s", e)

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
        opens the agent's first turn (_open_first_contact). Every other
        message (wrong code, /start, random text, callbacks) is silently
        dropped with no reply.
        """
        text = update.message.text if update.message else None
        if text and ownership.verify_and_claim(text, user_id, config.bot_data_dir):
            # The claim code is consumed by verify_and_claim, so this branch
            # runs at most once per instance -- the opener cannot repeat.
            # Off the handler path: starting the stream spawns the CLI.
            chat = update.effective_chat
            chat_id = chat.id if chat is not None else user_id
            # Strong reference: a bare create_task may be collected mid-run.
            tasks = self.__dict__.setdefault("_first_contact_tasks", set())
            task = asyncio.get_running_loop().create_task(
                self._open_first_contact(user_id, chat_id, update.message)
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return False

    async def _open_first_contact(self, user_id: int, chat_id: int, message) -> None:
        """After a successful /claim, let the agent speak first.

        Bootstraps the owner's stream (DGN-399 seam, same as the session
        inbox) and injects first_contact_turn() once; the agent's reply rides
        the proactive push, so the owner's first bubble is the agent's own
        introduction instead of a system line. inject_background_turn
        returning False on a live stream means the owner already sent a
        message: that turn opens the conversation, so nothing else is sent.
        Only a failure to start the turn at all falls back to the fixed
        CLAIM_SUCCESS line (never both).

        DGN-1849: a kit-pending main gets the fixed opener sent here
        directly (machine_opener_text) and recorded on the first-contact
        state -- no stream, no model turn. A failed send falls through to
        the model-turn path above.
        """
        opener = machine_opener_text()
        if opener:
            try:
                await message.reply_text(opener)
                record_machine_opener(opener)
                logger.info("first contact after claim: fixed opener sent")
                return
            except Exception as e:
                logger.warning(
                    "fixed opener send failed for user %s: %s", user_id, e
                )
        try:
            session = await session_manager.get_session(user_id)
            if not await sdk_bridge.ensure_owner_stream(
                user_id, session.get("model"), chat_id, self._proactive_push
            ):
                raise RuntimeError("owner stream not started")
            injected = await sdk_bridge.inject_background_turn(
                user_id, first_contact_turn()
            )
            logger.info(
                "first contact after claim: %s",
                "opener injected" if injected else "owner spoke first, opener skipped",
            )
        except Exception as e:
            logger.warning("first-contact opener failed for user %s: %s", user_id, e)
            await self._claim_success_fallback(message, user_id)

    async def _claim_success_fallback(self, message, user_id: int) -> None:
        try:
            await message.reply_text(messages.CLAIM_SUCCESS)
        except Exception as e:
            logger.warning("claim success reply failed for user %s: %s", user_id, e)

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
        be parsed as a fast-path set-result (one instance kept bare-numeric a valid
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
        def normalize(value: str) -> str:
            return re.sub(r"[_\s-]+", "", value.strip().lower())

        reply = text.strip()
        # Only a bare option or a dotted label is numeric approval; ordinary
        # prose such as "1 more thing" must leave the prompt pending.
        option = re.match(r"^([12])(?:\.(?:\s+|$)|$)", reply)
        number = option.group(1) if option else None
        label = normalize(reply[option.end():] if option else reply)
        normalized = normalize(reply)
        allow = (
            normalize(ALLOW_OUTSIDE_ONCE_TOKEN) in normalized
            or number == "1"
            or label in {
                normalize(word)
                for word in messages.OUTSIDE_APPROVAL_ALLOW_WORDS.split("|")
            }
        )
        deny = (
            normalize(DENY_OUTSIDE_TOKEN) in normalized
            or number == "2"
            or label in {
                normalize(word)
                for word in messages.OUTSIDE_APPROVAL_DENY_WORDS.split("|")
            }
        )
        # A contradictory label/token must never override a denial.
        if allow and not deny:
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
        BTW_TURN_TIMEOUT seconds (violates the DGN-911 5s-reaction contract).

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
        # DGN-1016: a stop also ends any deferred-interrupt wait.
        self._interrupt_deferred_since.pop(user_id, None)
        # DGN-1385: a discarded buffer no longer needs its typing indicator.
        self._cancel_typing_refresh(user_id)

    async def _start_typing_refresh(self, user_id: int, update) -> None:
        """DGN-1385: signal receipt of a message buffered behind an in-flight turn.

        The buffer paths in _enqueue_text_task otherwise send no feedback at
        all while a message waits (max BRIDGE_INFLIGHT_DEBOUNCE_S, or far
        longer if the in-flight turn itself is still running) -- indistinguishable
        from the message being silently dropped (owner-observed, DGN-1385).
        Sends one immediate typing action, then reuses an already-running
        refresh loop for this user (a burst of messages shares one loop) or
        starts exactly one new task to keep the indicator alive past its ~5s
        Telegram expiry until the buffer drains.
        """
        chat = update.effective_chat
        if chat is None:
            return
        try:
            await chat.send_action(action="typing")
        except Exception as e:
            logger.debug("typing send failed for user %s: %s", user_id, e)
        existing = self._typing_refresh_tasks.get(user_id)
        if existing and not existing.done():
            return
        self._typing_refresh_tasks[user_id] = asyncio.create_task(
            self._typing_refresh_loop(user_id, chat)
        )

    async def _typing_refresh_loop(self, user_id: int, chat) -> None:
        """DGN-1385: keep re-sending typing while a message sits buffered.

        Telegram's typing status expires after ~5s; owner decision
        2026-09-10 21:30 was to stay honest about a long wait rather than let
        the indicator go dark and imply the message was dropped. Runs until
        cancelled by _cancel_typing_refresh (drain, /stop, or coalesce-cap
        discard) -- never self-terminates.
        """
        try:
            while True:
                await asyncio.sleep(TYPING_INTERVAL)
                try:
                    await chat.send_action(action="typing")
                except Exception as e:
                    # A flaky Telegram call (429/network) must never kill the
                    # refresh loop or the turn it is decorating.
                    logger.debug(
                        "typing refresh failed for user %s: %s", user_id, e
                    )
        except asyncio.CancelledError:
            raise

    def _cancel_typing_refresh(self, user_id: int) -> None:
        """DGN-1385: stop the typing-refresh loop -- its buffer wait is over."""
        task = self._typing_refresh_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()

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
                    # DGN-1385: /queue lands here silently otherwise -- signal receipt.
                    await self._start_typing_refresh(user_id, update)
                    return
                # DGN-911 default: buffer + (re)arm the debounce window.
                buf = self._debounce_texts.setdefault(user_id, [])
                if len(buf) >= COALESCE_MAX:
                    # Memory-safety valve: only a runaway flood reaches here.
                    await self._notify_coalesce_cap(user_id, update)
                    return
                buf.append((text, ts, update))
                self._reset_inflight_debounce(user_id)
                # DGN-1385: buffered messages get no other feedback until the
                # debounce window (or the in-flight turn itself) ends -- signal
                # receipt now instead of leaving the sender guessing.
                await self._start_typing_refresh(user_id, update)
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

        Notice policy (owner decision 2026-08-17): SILENT by default -- the
        rationale was conversational naturalness (an interjection should
        feel like normal dialogue), not death-invisibility; the decision
        predates the live-task registry, and the DGN-1015 kill notice below
        is deliberately NOT gated by it. The
        BRIDGE_INFLIGHT_INTERRUPT_NOTICE flag stays a future opt-in; it now
        carries a dedicated auto-interrupt copy (AUTO_INTERRUPT_NOTICE,
        DGN-1016 O1 draft -- owner confirmation pending) instead of reusing
        the /stop copy.
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
        killed_descs: List[str] = []
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
                    # DGN-1016 background guard: a remote interrupt aborts the
                    # SESSION-wide abort tree and kills every in-session
                    # background subagent (measured, DGN-991 rev3). If tracked
                    # live background tasks exist, skip the interrupt and let
                    # the already-completed fail-safe hand-off above deliver
                    # via the legacy DGN-616 coalescing drain (interrupted
                    # stays False -> done-path drains; every turn is bounded
                    # by PROCESS_TIMEOUT, so the wait is finite). Deferral is
                    # capped: once the first deferred expiry is older than
                    # BRIDGE_INFLIGHT_DEFER_CAP_S, interrupt anyway --
                    # sustained owner input eventually wins, and a phantom
                    # registry entry cannot gate interrupts forever. Explicit
                    # /stop never runs through here and stays ungated.
                    # live_task_count is a pure dict lookup (no I/O) -- safe
                    # under the queue lock held here.
                    live_tasks = 0
                    if BRIDGE_INFLIGHT_DEFER_CAP_S > 0:
                        try:
                            live_tasks = sdk_bridge.live_task_count(user_id)
                        except Exception as e:
                            # Guard failure must never block the interrupt
                            # path -- fall through as "no live tasks".
                            logger.error(
                                "DGN-1016 live-task probe failed for user %s: %s",
                                user_id,
                                e,
                            )
                    now_mono = time.monotonic()
                    deferred_since = self._interrupt_deferred_since.get(user_id)
                    if live_tasks > 0 and (
                        deferred_since is None
                        or now_mono - deferred_since < BRIDGE_INFLIGHT_DEFER_CAP_S
                    ):
                        if deferred_since is None:
                            self._interrupt_deferred_since[user_id] = now_mono
                        logger.info(
                            "DGN-1016 auto-interrupt deferred for user %s: "
                            "%d live background task(s); falling back to "
                            "coalescing merge on turn end (deferred %.0fs)",
                            user_id,
                            live_tasks,
                            0.0
                            if deferred_since is None
                            else now_mono - deferred_since,
                        )
                        return
                    if live_tasks > 0:
                        logger.warning(
                            "DGN-1016 defer cap exceeded for user %s "
                            "(%.0fs >= %.0fs): auto-interrupting despite "
                            "%d live background task(s)",
                            user_id,
                            now_mono - deferred_since,
                            BRIDGE_INFLIGHT_DEFER_CAP_S,
                            live_tasks,
                        )
                    self._interrupt_deferred_since.pop(user_id, None)
                    try:
                        interrupted = await sdk_bridge.interrupt(
                            user_id, trigger="auto"
                        )
                        if interrupted:
                            # DGN-1015: the defer cap above only gated the
                            # COMMON case; sustained typing can still exceed
                            # it and interrupt with live tasks. Whatever died
                            # is fetched now so it is never silently lost.
                            killed_descs = sdk_bridge.pop_interrupt_killed(
                                user_id
                            )
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
            # DGN-1016 O1: dedicated auto-interrupt copy (draft, owner
            # confirmation pending) -- the /stop copy this used to reuse
            # described an action the user never took.
            try:
                chat = newest_update.effective_chat
                chat_id = chat.id if chat else user_id
                await self.application.bot.send_message(
                    chat_id, messages.AUTO_INTERRUPT_NOTICE
                )
            except Exception as e:
                logger.error(
                    "DGN-911 interrupt notice send failed for user %s: %s",
                    user_id,
                    e,
                )
        if killed_descs and newest_update:
            # DGN-1015: unconditional (NOT gated by BRIDGE_INFLIGHT_INTERRUPT_
            # NOTICE) -- that flag silences "your message caused an
            # interrupt" noise (owner decision 2026-08-17, predates the
            # live-task registry this reports on). This is a different,
            # narrower fact: background work is confirmed dead, which is
            # exactly the silent-death gap DGN-1015 exists to close.
            chat = getattr(newest_update, "effective_chat", None)
            await self._send_bg_killed_notice(
                chat.id if chat else user_id, user_id, killed_descs
            )

    async def _send_bg_killed_notice(self, chat_id, user_id, killed) -> None:
        """DGN-1015 bg-kill notice, shared by every AUTOMATIC kill: the
        defer-cap auto-interrupt and (DGN-1593 r3) the DGN-1499 timeout stop
        signal. Never /stop (DGN-1593 B). No-op on an empty kill list.

        DGN-1593: the count line always; one bullet per job with an
        owner-facing name (r2 -- "" entries are counted, unnamed).
        """
        if not killed:
            return
        try:
            lines = [messages.BG_TASK_KILLED_NOTICE.format(count=len(killed))]
            lines += [
                messages.BG_TASK_KILLED_ITEM.format(name=name)
                for name in killed
                if name
            ]
            await self.application.bot.send_message(chat_id, "\n".join(lines))
        except Exception as e:
            logger.error(
                "DGN-1015 bg-kill notice send failed for user %s: %s",
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
            # DGN-1016: the in-flight wait this drain ends is over -- reset
            # the deferral clock so the next wait gets a fresh cap budget.
            self._interrupt_deferred_since.pop(user_id, None)
        if timer and not timer.done():
            timer.cancel()
        # DGN-1385: the wait these buffers signaled is over -- stop refreshing.
        self._cancel_typing_refresh(user_id)
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

        DGN-1021 4th path: this call site was the one delivery path never
        wired to the canonical has_options_marker recognizer (the other three
        -- model-turn finalize, proactive push, classifier -- were fixed and
        gate-tested under DGN-1021; this one still hand-defaulted
        force_options=False). A fast-path body that contains BOTH a fenced
        table (DGN-085 has_code split) AND an [[OPTIONS]] marker landed with
        the marker silently stripped and zero buttons built -- the exact
        "code block + [[OPTIONS]] in one message" contract failure (now
        taught by the injected grammar fragment) the
        has_code branch in _send_smart exists to prevent, just never
        triggered because force_options never turned on for this path.
        Computing it here from the same canonical recognizer the other three
        paths use closes the gap without adding a new detector.
        """
        has_options = has_options_marker(content)
        last_error: Optional[Exception] = None
        for attempt, delay in enumerate((0.0, *FASTPATH_PUSH_RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                # DGN-1209 R2: handler stdout is consumer text (no model turn).
                await self._send_smart(
                    chat_id, content, force_options=has_options, rail=RAIL_CONSUMER
                )
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

    # --- DGN-1209: machine-line gate alert reader -------------------------

    def _machine_line_alert_sink(self, rail: str, tokens: tuple, sample: str) -> None:
        """Sync sink installed into bridge.machine_gate at construction.

        Called from inside a render (sync context) when an UNREGISTERED
        machine-shape line PASSED on a consumer/unknown rail. Dedup against
        the durable (rail, token, day) ledger plus an in-flight set, then
        schedule ledger-only recording on the running loop. Never raises
        (the send this alert rides on must not fail because of the alert).
        """
        try:
            day = today_key()
            fresh = [
                t for t in self._machine_alert_ledger.unseen(rail, tokens, day)
                if (rail, t, day) not in self._machine_alert_inflight
            ]
            if not fresh:
                return
            for t in fresh:
                self._machine_alert_inflight.add((rail, t, day))
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._send_machine_line_alert(rail, fresh, day)
            )
        except Exception:  # noqa: BLE001 -- never let the alert break a send
            logger.exception("machine-line gate: alert scheduling failed")

    # DGN-1779: no longer regioned. Since DGN-1756 this is log + ledger mark
    # only (no steward routing), and the sink above calls it on every build --
    # stripping it left the public bridge raising AttributeError per alert.
    async def _send_machine_line_alert(
        self, rail: str, tokens: List[str], day: str
    ) -> None:
        """Persist diagnostics in the alert ledger; never push to an owner chat."""
        try:
            logger.warning(
                "machine-line gate: ledger only rail=%s tokens=%s",
                rail, ",".join(tokens),
            )
            self._machine_alert_ledger.mark(rail, tokens, day)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "machine-line gate: alert routing failed for rail=%s tokens=%s: %s",
                rail, ",".join(tokens), e,
            )
        finally:
            for t in tokens:
                self._machine_alert_inflight.discard((rail, t, day))

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
        await self._reply_guaranteed(update, messages.NEW_SESSION)

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
                label = model_picker.full_name(name, model_picker.chat_table())
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
            label = model_picker.full_name(name, model_picker.chat_table())
            await self._reply_guaranteed(
                update, messages.MODEL_SWITCHED.format(label=label)
            )
            return
        # DGN-1814 r3: versioned family buttons, current one marked (i18n),
        # "Now:" = the model that really answered; two-step (vendor first)
        # once the table offers 2+ chat vendors. DGN-192 order kept.
        text, buttons = model_picker.render(
            self._get_real_model(session), _model_whitelist(),
            self._live_model_id(), _MODEL_PERF_RANK,
        )
        await update.message.reply_text(
            text, reply_markup=self._picker_markup(buttons)
        )

    @staticmethod
    def _picker_markup(buttons) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=cb)] for label, cb in buttons
        ])

    @staticmethod
    def _live_model_id() -> str:
        return live_model.this_process_model(live_model.state_path(config.bot_data_dir))

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
            if await sdk_bridge.interrupt(user_id, trigger="stop"):
                # DGN-991 (2026-09-03 owner approval): the /stop reply is
                # this one sentence, full stop -- no standing background
                # warning appended (DGN-991 measured that in-session
                # subagents die on the soft interrupt while detached
                # dispatches survive it, so a blanket claim either way would
                # be false). The same STOP_INTERRUPTED copy is also reused by
                # the DGN-911 auto-interrupt notice.
                # DGN-1593 B: no kill notice here even when the registry
                # confirms a kill -- the owner just ordered the stop, so it
                # is not a silent death (DGN-1015's notice is for the
                # automatic interrupt only). Drained so it cannot linger.
                sdk_bridge.pop_interrupt_killed(user_id)
                await self._reply_guaranteed(update, messages.STOP_INTERRUPTED)
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

    # DGN-941: leading "/queue" token in a photo/document CAPTION. Telegram
    # only parses commands from message.text, so a caption "/queue ..." never
    # reaches the CommandHandler -- the photo/doc handlers must detect and strip
    # it themselves, then route through the coalescing path (same effect as a
    # text /queue). Matched anchored at the caption start, optional bot-mention
    # suffix ("/queue@bot"), followed by whitespace or end-of-string; the token
    # is removed and the remaining caption is returned trimmed. Case-insensitive
    # (Telegram lowercases command entities, but a hand-typed caption may not).
    _CAPTION_QUEUE_RE = re.compile(r"^/queue(?:@\w+)?(?:\s+|$)", re.IGNORECASE)

    @classmethod
    def _split_caption_queue(cls, caption: str) -> Tuple[str, bool]:
        """Return (cleaned_caption, coalesce). coalesce=True when the caption
        opened with a "/queue" token; the token is stripped from the caption.
        A caption with no leading /queue is returned unchanged with False."""
        cap = (caption or "").strip()
        m = cls._CAPTION_QUEUE_RE.match(cap)
        if not m:
            return cap, False
        return cap[m.end():].strip(), True

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
        # DGN-991 stopgap B: honest copy split. The old STOP_PAUSED claim
        # ("session and conversation intact") is FALSE when the teardown
        # actually killed the stream/CLI subprocess -- background work in that
        # process dies too. task_cancelled or killed = the process side was
        # torn down -> forced copy. cleared-only = queued messages dropped,
        # no live process touched -> the old copy is still true.
        if task_cancelled or killed:
            reply = messages.STOP_FORCED
        elif cleared:
            reply = messages.STOP_PAUSED
        else:
            reply = messages.STOP_NOTHING
        await self._reply_guaranteed(update, reply)

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

    # ESTATE:BEGIN DGN-1417 /update owner adapter.  The ledger owns state;
    # this bridge only renders its fixed facts and forwards authenticated taps.
    async def _update_tool(self, *args: str) -> Tuple[int, Dict[str, Any]]:
        """Run scripts/pack/update_surface.py off the event loop -> (rc, json)."""
        machinery = _update_machinery_root()
        tool = machinery / "scripts" / "pack" / "update_surface.py"
        # DGN-1778: declare that this bridge renders the machine's HTML
        # start text (parse_mode HTML); without it the script sends plain.
        env = dict(os.environ, DOGANY_UPDATE_RENDER="html",
                   DOGANY_DISCOVER_SELF_ROOT=str(PROJECT_ROOT))
        def run():
            proc = subprocess.run([sys.executable, str(tool)] + list(args),
                                  capture_output=True, text=True, env=env)
            try:
                payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
            except ValueError:
                payload = {"raw": proc.stdout.strip()}
            return proc.returncode, payload
        return await asyncio.to_thread(run)

    async def _update_plan(self) -> Path:
        """Fresh planner product for one ledger (the planner's emit IS the plan)."""
        machinery = _update_machinery_root()
        state = PROJECT_ROOT / ".telegram_bot" / "state" / "update-plans"
        state.mkdir(parents=True, exist_ok=True)
        plan = state / ("%s.tsv" % uuid.uuid4().hex)
        def run():
            # The asking instance is always a discovery source (rehearsal
            # 2026-10-02: the estate scan missed ~/.dogany/main and the
            # planner aborted on 0 instances right after /claim).
            env = dict(os.environ, DOGANY_PLAN_EMIT=str(plan),
                       DOGANY_DISCOVER_SELF_ROOT=str(PROJECT_ROOT))
            made = subprocess.run([str(machinery / "scripts" / "pack" / "update_plan.sh")],
                                  env=env, capture_output=True, text=True)
            if not _update_plan_usable(made.returncode, plan):
                raise RuntimeError("lookup failed")
            if made.returncode != 0:
                # DGN-1843: a complete plan with blocked items is still a
                # plan (the blocked ones are simply not in it); the reason
                # stays in the log, not in every agent's notice.
                logger.warning("DGN-1843 update plan has blocked items: %s",
                               " | ".join(l for l in (made.stderr or "").splitlines()
                                          if l.startswith("[plan] FAIL:"))[:2000])
        await asyncio.to_thread(run)
        return plan

    def _update_origin(self, plan: Path, user_id: int, chat_id: int) -> List[str]:
        return ["--plan", str(plan), "--repo", str(_update_machinery_root()),
                "--instance-root", str(PROJECT_ROOT), "--requester", str(user_id),
                "--chat", str(chat_id)]

    @staticmethod
    def _update_row(item: Dict[str, Any]) -> str:
        # The machine's caption when present (crew framework row, item 26:
        # one line, agents grouped by installed version, DGN-1749);
        # full semver with v, always (rehearsal 2026-09-26 item 16).
        if item.get("caption"):
            return item["caption"]
        def v(ver: str) -> str:
            ver = (ver or "").strip().lstrip("v")
            return "v" + ver if ver else ""
        return "%s %s → %s" % (item["name"], v(item["current"]), v(item["target"]))

    @staticmethod
    def _update_consent_prefix(item: Dict[str, Any]) -> str:
        # Owner-facing names (DGN-1744): the machine's labels, never the slugs.
        return messages.UPDATE_CONSENT_PREFIX.format(labels=", ".join(item.get("labels") or item["members"]))

    async def _cmd_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_access(update):
            return
        try:
            plan = await self._update_plan()
            rc, payload = await self._update_tool(
                "list", *self._update_origin(plan, update.effective_user.id, update.effective_chat.id))
            if rc != 0:
                raise RuntimeError("list failed")
        except Exception:
            await update.message.reply_text(messages.UPDATE_CHECK_FAILED)
            return
        if not payload.get("items"):
            # DGN-1790: the machine's "nothing to apply" text carries the
            # installed versions (HTML quote); absent -> the plain line.
            if payload.get("text"):
                await self._update_send_reply(update.message.reply_text, payload, payload["text"])
            else:
                await update.message.reply_text(messages.UPDATE_NONE)
            return
        # DGN-1775: the machine renders /update exactly like the session-start
        # notice (render_offer + offer_view); the taps are the notice's
        # (upd:o/b/p -> _handle_update_offer_tap).  No bridge-side copy.
        await self._update_send_offer(update.message.reply_text, payload)

    # Round-2 notice consent card only (upd:n); /update no longer has a detail
    # screen of its own (DGN-1775).
    async def _update_detail_card(self, ledger_id: str, revision: Any, index: Any, item: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
        body = "\n\n".join("▸ %s\n%s" % (part["title"], part["body"]) for part in item["sections"])
        # Dependency lines are machine copy (rehearsal item 21): "<pack> vB로
        # 업데이트하려면 <kit>도 vB로 함께 업데이트해야 해요."
        head = list(item.get("bundle") or [])
        if item["needs_consent"]:
            head.append(self._update_consent_prefix(item))
        text = "\n\n".join(head + [body or self._update_row(item)])
        if item["needs_consent"]:
            button = InlineKeyboardButton(messages.UPDATE_CONSENT_BUTTON, callback_data="upd:c:%s:%s:%s" % (ledger_id, revision, index))
        else:
            button = InlineKeyboardButton(messages.UPDATE_APPLY_BUTTON, callback_data="upd:a:%s:%s:%s" % (ledger_id, revision, index))
        return text, InlineKeyboardMarkup([[button]])

    async def _update_start_reply(self, outcome: Dict[str, Any]) -> Optional[str]:
        # The machine owns the copy (start + item list, busy): rehearsal items 19/21.
        if outcome.get("started") or outcome.get("busy"):
            return outcome.get("text") or None
        return None  # duplicate tap on a running/finished ledger: its result is (or will be) sent

    async def _update_send_reply(self, send, outcome: Dict[str, Any], text: str,
                                 markup: Optional[InlineKeyboardMarkup] = None) -> None:
        """DGN-1776: the start text is machine HTML (version lines in one
        quote) -> parse_mode HTML, plain fallback.  Busy/stale stay plain.
        markup: the busy ask's yes / no keyboard (DGN-1794 r2)."""
        extra = {"reply_markup": markup} if markup is not None else {}
        if outcome.get("parse_mode") != "HTML":
            await send(text, **extra)
            return
        try:
            await send(text, parse_mode="HTML", **extra)
        except Exception:
            logger.exception("DGN-1776 update start HTML send failed; plain fallback")
            await send(html_to_plain_text(text), **extra)

    @staticmethod
    def _update_markup(outcome: Dict[str, Any]) -> Optional[InlineKeyboardMarkup]:
        """Machine button rows ({text, callback_data}) -> a keyboard, or None."""
        rows = [[InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
                for row in outcome.get("buttons") or []]
        return InlineKeyboardMarkup(rows) if rows else None

    async def _handle_update_callback(self, query, data: str, user_id: int, chat_id: int, tapped_at: int) -> None:
        stale = messages.UPDATE_STALE
        if data.startswith(("upd:o:", "upd:p:", "upd:b:")):
            await self._handle_update_offer_tap(query, data, user_id, chat_id, tapped_at)
            return
        if data.startswith("upd:r:"):
            await self._handle_update_restart_tap(query, data, user_id)
            return
        if data.startswith("upd:q:"):
            await self._handle_update_follow_tap(query, data, user_id, chat_id)
            return
        if data.startswith("upd:n:"):
            # Round-2 notice button (no longer attached): a tap on an old,
            # already-delivered notice still resolves through the ledger.
            version = data[len("upd:n:"):]
            try:
                plan = await self._update_plan()
                rc, out = await self._update_tool(
                    "notice", *self._update_origin(plan, user_id, chat_id),
                    "--version", version, "--tapped-at", str(tapped_at))
                if rc != 0:
                    raise RuntimeError("notice failed")
            except Exception:
                await query.message.reply_text(messages.UPDATE_CHECK_FAILED)
                return
            action = out.get("action")
            if action == "consent":
                text, markup = await self._update_detail_card(out["ledger"], out["revision"], out["index"], out["item"])
                await query.message.reply_text(text, reply_markup=markup)
            elif action in ("started", "busy"):
                reply = await self._update_start_reply(out)
                if reply:
                    await self._update_send_reply(query.message.reply_text, out, reply,
                                                  self._update_markup(out) if action == "busy" else None)
            else:
                await query.message.reply_text(stale)
            # DGN-1794: busy strips it too; the busy reply carries its own
            # ask (r2: carry the apply through after the running run?).
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        try:
            _, verb, ledger_id, revision, index = data.split(":")
        except ValueError:
            await query.edit_message_text(stale)
            return
        rc, key = await self._update_tool("key", "--ledger", ledger_id, "--revision", revision, "--index", index)
        key = key.get("raw") if isinstance(key, dict) else None
        if rc != 0 or not key:
            await query.edit_message_text(stale)
            return
        common = ["--ledger", ledger_id, "--revision", revision, "--key", key,
                  "--user", str(user_id), "--tapped-at", str(tapped_at)]
        if verb == "c":
            rc, _ = await self._update_tool("consent", *common)
            if rc != 0:
                await query.edit_message_text(stale)
                return
        elif verb != "a":
            await query.edit_message_text(stale)
            return
        rc, out = await self._update_tool("apply", *common)
        if rc != 0:
            await query.edit_message_text(stale)
            return
        reply = await self._update_start_reply(out)
        if reply and out.get("busy"):
            # DGN-1794 r2: the ask goes out as its own reply (like upd:p /
            # upd:n) and the detail card loses its dead apply button.
            await self._update_send_reply(query.message.reply_text, out, reply, self._update_markup(out))
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        elif reply:
            await self._update_send_reply(query.edit_message_text, out, reply)

    # --- session-start update notice (owner rehearsal 2026-09-26 section D) ---
    # routines/version-check.py (session start) and routines/pack-lag.sh (the
    # daily measurement) only leave a REQUEST; the notice itself is built here
    # from the same planner + ledger /update uses and sent as its own message
    # under the agent's reply.  Asked once per version set (the machine keeps
    # that record, written only after a real send).

    def _update_offer_request_path(self) -> Path:
        return PROJECT_ROOT / ".telegram_bot" / "state" / "update-offer-request"

    def _kick_update_offer(self, chat_id: int) -> None:
        """After a reply: take a pending request (once -- the rename is the
        mutex) and build/send the notice off the reply path."""
        try:
            request = self._update_offer_request_path()
            if not request.is_file():
                return
            os.replace(request, request.with_name(request.name + ".taken"))
        except OSError:
            return
        try:
            self._update_background(self._update_offer_run(chat_id))
        except RuntimeError:
            pass

    def _update_background(self, coro) -> None:
        """Keep a strong reference: a bare create_task may be collected
        before the promised re-check runs."""
        tasks = self.__dict__.setdefault("_update_bg_tasks", set())
        task = asyncio.get_running_loop().create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _update_send(self, chat_id: int, text: str) -> bool:
        try:
            await self.application.bot.send_message(chat_id, text)
            return True
        except Exception:
            logger.exception("DGN-1417 update notice line not sent")
            return False

    async def _update_offer_run(self, chat_id: int, retry: bool = False) -> None:
        """Send a valid offer; unsolicited lookup failures stay in the log.

        retry=True belongs to an explicit update tap whose failure response
        promised a re-check. Only that requested retry reports failure/none.
        """
        try:
            plan = await self._update_plan()
            args = ["offer", *self._update_origin(plan, chat_id, chat_id)]
            if retry:
                args.append("--force")  # promised: report even an already-asked set
            rc, out = await self._update_tool(*args)
            if rc != 0 or out.get("action") not in ("offer", "quiet", "none"):
                raise RuntimeError("offer failed")
            if out.get("blocked"):
                logger.warning("DGN-1843 update blocked for this agent: %s", out["blocked"])
        except Exception:
            logger.exception("DGN-1417 update notice lookup failed (retry=%s)", retry)
            if retry:
                await self._update_send(chat_id, messages.UPDATE_LOOKUP_FAILED_AGAIN)
                return
            return
        action = out["action"]
        if action == "none" and retry:
            await self._update_send(chat_id, messages.UPDATE_NONE)
        if action != "offer":
            return
        sent = await self._update_send_offer(
            lambda *a, **kw: self.application.bot.send_message(chat_id, *a, **kw), out)
        if sent is None:
            return
        await self._update_tool("offered", "--instance-root", str(PROJECT_ROOT),
                                "--signature", out["signature"], "--ledger", out["ledger"],
                                "--message-id", str(getattr(sent, "message_id", 0) or 0))

    async def _update_send_offer(self, send, out: Dict[str, Any]) -> Any:
        """One sender for the notice and /update (DGN-1775): the machine's
        HTML + one button per row, plain-text fallback.  None = not sent."""
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], callback_data=b["data"])]
                                       for b in out["buttons"]])
        try:
            return await send(out["html"], parse_mode="HTML", reply_markup=markup)
        except Exception:
            logger.exception("DGN-1417 update notice HTML send failed; plain fallback")
            try:
                return await send(html_to_plain_text(out["html"]), reply_markup=markup)
            except Exception:
                logger.exception("DGN-1417 update notice not sent (not recorded as asked)")
                return None

    async def _update_offer_retry(self, chat_id: int) -> None:
        await asyncio.sleep(UPDATE_OFFER_RETRY_S)
        await self._update_offer_run(chat_id, retry=True)

    async def _handle_update_offer_tap(self, query, data: str, user_id: int, chat_id: int, tapped_at: int) -> None:
        """Offer opens a caution; apply uses pick; back rebuilds the offer."""
        try:
            parts = data.split(":")
            _, verb, ledger_id, revision = parts[:4]
            pick = "a" if verb == "b" else parts[4]
            plan = await self._update_plan()
            extra = ["--tapped-at", str(tapped_at)] if verb == "p" else (["--back"] if verb == "b" else [])
            rc, out = await self._update_tool(
                "pick" if verb == "p" else "offer-view", *self._update_origin(plan, user_id, chat_id),
                "--ledger", ledger_id, "--revision", revision, "--pick", pick,
                "--user", str(user_id), *extra)
            if rc != 0 or not out.get("action"):
                raise RuntimeError("pick failed")
        except Exception:
            logger.exception("DGN-1417 update notice tap: lookup failed")
            await query.message.reply_text(messages.UPDATE_LOOKUP_FAILED)
            self._update_background(self._update_offer_retry(chat_id))
            return
        if out["action"] in ("confirm", "offer"):
            buttons = [InlineKeyboardButton(b["text"], callback_data=b["data"]) for b in out["buttons"]]
            rows = [buttons] if out["action"] == "confirm" else [[b] for b in buttons]
            await query.edit_message_text(out["html"], parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
            return
        action = out["action"]
        await self._update_send_reply(query.message.reply_text, out,
                                      out.get("text") or messages.UPDATE_STALE,
                                      self._update_markup(out) if action == "busy" else None)
        # The confirm card replaced the list in place: after a start nothing is
        # left to tap; after busy (DGN-1794) its apply button stays dead for the
        # running run -- the busy reply asks instead whether to carry it
        # through (r2, upd:q).
        if action in ("started", "busy"):
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

    async def _handle_update_follow_tap(self, query, data: str, user_id: int, chat_id: int) -> None:
        """upd:q:<ledger>:y|n = the yes / no buttons under a busy ask
        (DGN-1794 r2).  The machine queues or declines (or, when the running
        run is already over, picks it up at once); its reply replaces the
        ask -- a line, or a fresh /update card when the plan moved."""
        try:
            _, _, ledger_id, choice = data.split(":")
        except ValueError:
            return
        rc, out = await self._update_tool("follow", "--ledger", ledger_id, "--choice", choice,
                                          "--user", str(user_id), "--chat", str(chat_id))
        if rc != 0 or not out.get("text"):
            logger.warning("DGN-1794 follow tap: machine refused (rc=%s)", rc)
            await query.message.reply_text(messages.UPDATE_STALE)
            return
        markup = self._update_markup(out)
        try:
            await query.edit_message_text(out["text"], parse_mode="HTML", reply_markup=markup)
        except Exception:
            logger.exception("DGN-1794 follow reply HTML edit failed; plain fallback")
            await query.edit_message_text(html_to_plain_text(out["text"]), reply_markup=markup)

    async def _handle_update_restart_tap(self, query, data: str, user_id: int) -> None:
        """upd:r:<ledger>:<index> = the machine's restart_button under the partial result
        (item 24).  Owner-initiated: the machine restarts that one agent
        through the existing restart path and answers only after the
        liveness verdict (success, still stopped, or the completed update)."""
        try:
            _, _, ledger_id, index = data.split(":")
        except ValueError:
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        rc, out = await self._update_tool("restart", "--ledger", ledger_id, "--index", index,
                                          "--user", str(user_id))
        if rc != 0 or not out.get("text"):
            logger.warning("DGN-1417 restart tap: machine refused (rc=%s)", rc)
            await query.message.reply_text(messages.UPDATE_STALE)
            return
        rows = [[InlineKeyboardButton(b["text"], callback_data=b["callback_data"])] for row in out.get("buttons") or [] for b in row]
        await query.message.reply_text(out["text"], reply_markup=InlineKeyboardMarkup(rows) if rows else None)

    async def _update_ledger_sweep(self) -> None:
        """Boot backstop: close a ledger whose supervisor died mid-run."""
        for _ in range(180):
            try:
                rc, out = await self._update_tool("sweep")
            except Exception:
                logger.exception("DGN-1417 update ledger sweep failed")
                return
            if rc != 0 or not out.get("pending"):
                return
            await asyncio.sleep(60)
    # ESTATE:END

    # ESTATE:BEGIN /health command body -- delegates to healthcmd.build_health_report (estate-only paths: the estate SoT registry, launchctl); healthcmd.py is manifest-excluded (DGN-986 [c])
    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """DGN-986 [c]: /health -- estate health surface (bridge/healthcmd.py).

        No model call / no session (same contract as /usage). The collection
        (launchctl + ps + file reads + a 2s-budget network lookup, worst case
        ~3s) runs via asyncio.to_thread -- R6: a synchronous call here would
        stall the whole bridge event loop for that long.

        CTA rail: the recommended action rides ONE opt: callback button --
        the only button rail that reaches the session as a user message
        (bot.py opt: handler -> resolve_choice -> run_task). IDRILL buttons
        fire an argv subprocess and cannot reach the session (spec section 5
        correction), so they are not used here. Sentence CTAs in the body
        remain valid after the button expires.
        """
        if not await self._check_access(update):
            return
        try:
            text, ctas = await asyncio.to_thread(healthcmd.build_health_report)
        except Exception as e:
            await update.message.reply_text(
                messages.HEALTH_FAILED.format(error=str(e))
            )
            return
        keyboard = build_option_keyboard(ctas[:1]) if ctas else None
        parts = split_text(text)
        for i, part in enumerate(parts):
            body = balance_telegram_html(markdown_to_telegram_html(part))
            markup = keyboard if i == len(parts) - 1 else None
            try:
                await update.message.reply_text(
                    body, parse_mode="HTML", reply_markup=markup
                )
            except Exception:
                await update.message.reply_text(
                    html_to_plain_text(body), reply_markup=markup
                )
    # ESTATE:END

    async def _cmd_authsync(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """DGN-1050: /authsync is RETIRED. This stub only explains why.

        The original command (DGN-759) ran token-sync.sh, whose `sync`
        subcommand unconditionally overwrote the macOS Keychain entry from
        ~/.claude/.credentials.json. The Claude CLI keeps the keychain copy
        authoritative and does NOT rewrite the file after runtime token
        rotations, so the file is stale BY DESIGN -- the overwrite re-injected
        superseded refresh tokens, the server revoked the token family on
        reuse, and every instance sharing the account died (~daily).

        The only safe credential writer is the CLI itself
        (`claude auth login` in a plain terminal). This handler therefore:
          - spawns NO subprocess and touches NO credential store, ever;
          - replies with the retirement notice pointing to the correct
            procedure;
          - stays registered off-menu ONLY so the catch-all skill forwarder
            cannot ship a typed /authsync into the SDK session (where a model
            could otherwise be talked into running the retired skill script).

        The DGN-994 sync-ok restart CTA died with the sync path; /restart
        (DGN-997) is the surviving restart surface.
        """
        if not await self._check_access(update):
            return
        await update.message.reply_text(messages.AUTHSYNC_RETIRED)

    async def _cmd_restart(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """DGN-997: /restart -- owner-only explicit bridge restart command.

        Fills a missing command-line surface: DGN-994 wired a restart CTA
        only inside the /authsync sync-ok reply (a button, not a command),
        so the general-purpose "just restart" path had no slash entry at
        all -- an agent ended up announcing a non-existent /restart to the
        owner (DGN-997 incident).

        Owner gate: same _check_access() every other command uses -- no new
        permission logic. An explicit /restart typed by the owner IS the
        explicit restart command (AGENT.md Self-restart: an owner trigger
        skips the idle guard entirely, immediate, no confirmation menu),
        the same convention DGN-994's CTA tap already uses, so this reuses
        that exact call shape: --trigger user, no --resume-intent.

        --resume-intent intentionally omitted: --resume-intent exists for a
        caller that KNOWS the specific in-flight task it is cutting off
        (e.g. an agent restarting itself mid-fix, session-side). This handler
        is a bridge-process command entry point -- it has no visibility
        into what the live SDK session is doing at the moment the owner
        types /restart, and guessing would violate the "no hunting
        chronically-open wip tickets" contract self_restart.sh already
        documents for the omitted case. Omitting the flag is the correct
        "no specific in-flight task asserted" signal (self_restart.sh's own
        contract), not a gap -- the resumed session's DGN-226 post-restart
        self-verification step covers anything actually cut off.

        Progress/completion notice: self_restart.sh owns the completion push
        on its own (default fallback copy when no --notice is passed, see
        self_restart.sh DGN-687/834). dec-094 adds a synchronous immediate
        ack (RESTART_ACK) right after the launch subprocess returns 0, so
        /restart is not silent for the ~6s SIGTERM window before that
        completion push lands -- distinct message, no duplicate notice.
        """
        if not await self._check_access(update):
            return

        # Duplicate-launch latch (_restart_launch_started): a rapid double
        # /restart must not double-fire the script. Window-based, not sticky,
        # so an aborted restart self-heals (see RESTART_LATCH_S). DGN-1050:
        # formerly shared with the /authsync sync-ok CTA callback (DGN-994);
        # that path is retired, /restart is now the latch's only trigger --
        # keep the shared-attribute shape in case another restart entry
        # point ever returns.
        now = time.monotonic()
        started = getattr(self, "_restart_launch_started", None)
        if started is not None and now - started < RESTART_LATCH_S:
            return
        self._restart_launch_started = now

        script = PACKAGE_DIR / "self_restart.sh"
        if not script.is_file() or not os.access(script, os.X_OK):
            self._restart_launch_started = None
            await update.message.reply_text(
                messages.RESTART_ERROR.format(
                    error=f"restart script missing/not executable: {script}"
                )
            )
            return

        def _run_restart() -> subprocess.CompletedProcess:
            # Launcher detaches a worker and exits quickly; the worker
            # (nohup'd, session-detached) survives the bridge SIGTERM and
            # owns the restart-complete owner push -- no new copy needed
            # here.
            return subprocess.run(
                [
                    str(script),
                    "--trigger", "user",
                    "--reason", "owner /restart command",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

        try:
            result = await asyncio.to_thread(_run_restart)
        except Exception as e:
            self._restart_launch_started = None
            await update.message.reply_text(
                messages.RESTART_ERROR.format(error=str(e))
            )
            return

        if result.returncode != 0:
            self._restart_launch_started = None
            detail = (result.stderr or result.stdout or "").strip()
            await update.message.reply_text(
                messages.RESTART_ERROR.format(
                    error=detail or f"self_restart.sh exit {result.returncode}"
                )
            )
            return
        # Success: the detached worker takes over (SIGTERM in ~6s); the
        # restart-complete push is owned by self_restart.sh. Latch stays
        # armed for the window so a second /restart cannot double-fire
        # mid-restart.
        #
        # dec-094 (형님 2026-08-21): immediate ack here so /restart is not
        # silent for the ~6s until the process dies. This is sent
        # synchronously, before this handler returns, so it lands well
        # inside the SIGTERM window. Distinct from the completion push
        # self_restart.sh sends after the worker finishes -- no duplicate.
        await self._reply_guaranteed(update, messages.RESTART_ACK)

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
                # DGN-953: this fallback used to be fully silent -- the user
                # got BTW_FORK_FAILED with zero log trace. Cause signals are
                # in the bridge.btw empty-turn diagnostic line logged by the
                # fork turn itself.
                logger.warning(
                    "DGN-953: btw fork turn produced empty response for "
                    "user %s; replying with BTW_FORK_FAILED",
                    user_id,
                )
                response = messages.BTW_FORK_FAILED

            # Prepend the 💭 marker.
            marked = f"{messages.BTW_MARKER}\n\n{response}"

            # DGN-920: format through the shared prose->HTML helper so markdown
            # in the fork response renders (** -> <b>, code blocks, etc.) the
            # same way the normal send path does (DGN-891 tag balancing included).
            # Fail-soft: if formatting raises, fall back to the raw marked text
            # so the answer is delivered even without formatting.
            try:
                formatted = balance_telegram_html(
                    sanitize_message_for_telegram(marked, rail=RAIL_MODEL)
                )
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
                # DGN-953: same observability gap as the first-turn fallback
                # above -- log before silently swapping in BTW_FORK_FAILED.
                logger.warning(
                    "DGN-953: btw fork continuation produced empty response "
                    "for user %s; replying with BTW_FORK_FAILED",
                    user_id,
                )
                response = messages.BTW_FORK_FAILED

            marked = f"{messages.BTW_MARKER}\n\n{response}"
            # DGN-920: same shared formatting as the initial fork send path.
            # Fail-soft: if formatting raises, send the raw marked text.
            try:
                formatted = balance_telegram_html(
                    sanitize_message_for_telegram(marked, rail=RAIL_MODEL)
                )
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

    @staticmethod
    def _skill_roots() -> List[Path]:
        """Skill install roots, workspace-first with HOME fallback.

        DGN-929: single source of truth for skill/script lookup order.
        Project skills live under PROJECT_ROOT/.claude/skills (standard
        Dogany instance layout); ~/.claude/skills is the legacy/global
        fallback. Shared by /skills listing and any skill-script lookup
        (formerly also the retired /authsync, DGN-1050) -- do not
        re-hardcode either root elsewhere.
        """
        return [PROJECT_ROOT / ".claude" / "skills", Path.home() / ".claude" / "skills"]

    def _resolve_skill_script(self, skill_name: str, script_name: str) -> Optional[Path]:
        """Resolve <skill_name>/<script_name> against _skill_roots() in order,
        returning the first existing file. None if not found in either root."""
        for root in self._skill_roots():
            candidate = root / skill_name / script_name
            if candidate.is_file():
                return candidate
        return None

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
        # stream teardown (RELIABILITY #4). Lookup order per _skill_roots():
        # PROJECT_ROOT/.claude/skills first (workspace), ~/.claude/skills (global) fallback.
        project_root, global_root = self._skill_roots()
        project_skills = self._collect_skills(project_root)
        global_skills = self._collect_skills(global_root)
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

                response = await self._finish_turn_with_auto_resume(
                    user_id=user_id, chat_id=chat.id, response=response, resume_caller=resume_caller
                )
                if response is None:
                    return
                await self._save_session_id(user_id, response)
                await self._reply_smart(
                    message,
                    response.content,
                    force_options=response.has_options,
                    streamed=response.streamed,
                    draft_message_ids=response.draft_message_ids,
                    classifier_injected=getattr(response, "options_classifier_injected", False),
                    assembled=getattr(response, "turn_assembled", False),
                    notice_meta=self._notice_meta(response),
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

        # DGN-351: Telegram splits a long pasted message at 4096 chars into
        # back-to-back updates. A boundary-length part opens a merge window in
        # the same chat; continuation parts are concatenated and dispatched as
        # ONE turn. Short messages skip this entirely (zero-delay unchanged).
        # Placed BEFORE the btw/fast-path checks on purpose: the tail part of a
        # split is often short and would otherwise be eaten by the fast-path
        # prefilter, which would strand the buffered head. Both checks still run
        # on the merged text in _dispatch_text_task below.
        if await self._maybe_buffer_split_part(
            message.chat_id, update, user_id, text
        ):
            return

        await self._dispatch_text_task(
            update, user_id, text, approval_pending=approval_pending
        )

    async def _dispatch_text_task(
        self,
        update: Update,
        user_id: int,
        text: str,
        *,
        approval_pending: bool = False,
    ) -> None:
        """Route one (possibly split-merged) regular text message.

        DGN-351 split-merge dispatches through here too, so the fork-reply and
        fast-path checks see the MERGED text, never a half of it.
        """
        message = update.message

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

    async def _maybe_buffer_split_part(
        self, chat_id: int, update: Update, user_id: int, text: str
    ) -> bool:
        """Conditional split-message merge (DGN-351).

        Returns True when the part was buffered (caller must NOT dispatch);
        False when the message takes the normal zero-delay path.

        A boundary-length (>= SPLIT_MERGE_THRESHOLD) message either opens a new
        merge window or continues an open one. A short message that arrives while
        a window is open is treated as the final continuation part: it is appended
        and the buffer flushes immediately. A short message with no open window
        takes the normal path (returns False) -- latency untouched.
        """
        is_boundary = len(text) >= SPLIT_MERGE_THRESHOLD
        async with self._split_buffer_lock:
            buf = self._split_buffers.get(chat_id)
            if buf is None:
                if not is_boundary:
                    # Common case: short message, no open window -> zero delay.
                    return False
                # Open a new window on the first boundary-length part.
                buf = {
                    "user_id": user_id,
                    "update": update,
                    "parts": [text],
                    "timer": None,
                }
                self._split_buffers[chat_id] = buf
                buf["timer"] = asyncio.create_task(
                    self._flush_split_buffer_after(chat_id)
                )
                logger.info(
                    "split-merge: opened window chat=%s part1_len=%d",
                    chat_id,
                    len(text),
                )
                return True
            # A window is already open: append this part.
            buf["parts"].append(text)
            if buf["timer"] is not None:
                buf["timer"].cancel()
            if is_boundary:
                # Another boundary-length part: more may follow. Extend the window.
                buf["timer"] = asyncio.create_task(
                    self._flush_split_buffer_after(chat_id)
                )
                logger.info(
                    "split-merge: extended window chat=%s parts=%d last_len=%d",
                    chat_id,
                    len(buf["parts"]),
                    len(text),
                )
                return True
            # Short continuation: this is the tail. Flush now.
            self._split_buffers.pop(chat_id, None)
            merged = "".join(buf["parts"])
            dispatch_update = buf["update"]
            dispatch_user_id = buf["user_id"]
            n_parts = len(buf["parts"])
        logger.info(
            "split-merge: flushed chat=%s parts=%d merged_len=%d (tail)",
            chat_id,
            n_parts,
            len(merged),
        )
        await self._dispatch_text_task(dispatch_update, dispatch_user_id, merged)
        return True

    async def _flush_split_buffer_after(self, chat_id: int) -> None:
        """Wait out the merge window; a newer part cancels us and restarts a
        timer. When we win, pop the buffer and dispatch the concatenated parts
        as one turn."""
        try:
            await asyncio.sleep(SPLIT_MERGE_WINDOW)
        except asyncio.CancelledError:
            return
        async with self._split_buffer_lock:
            buf = self._split_buffers.pop(chat_id, None)
            if not buf or not buf["parts"]:
                return
            merged = "".join(buf["parts"])
            dispatch_update = buf["update"]
            dispatch_user_id = buf["user_id"]
            n_parts = len(buf["parts"])
        logger.info(
            "split-merge: flushed chat=%s parts=%d merged_len=%d (timeout)",
            chat_id,
            n_parts,
            len(merged),
        )
        await self._dispatch_text_task(dispatch_update, dispatch_user_id, merged)

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
        # DGN-941: a leading "/queue" caption token routes the photo turn
        # through the coalescing path instead of the immediate (interrupting)
        # dispatch. The token is stripped so it never reaches the prompt.
        caption, coalesce = self._split_caption_queue(message.caption or "")
        mgid = message.media_group_id

        if not mgid:
            # Single photo: dispatch immediately (legacy behavior) unless the
            # caption carried /queue, which coalesces instead of interrupting.
            await self._dispatch_photo_task(
                update, user_id, [photo.file_id], caption, coalesce=coalesce
            )
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
                    "coalesce": False,
                    "timer": None,
                }
                self._media_groups[mgid] = group
            group["file_ids"].append(photo.file_id)
            # Telegram puts the caption only on the first album item; keep first
            # seen. DGN-941: the /queue token rides that same first-item caption,
            # so latch the coalesce flag alongside the caption.
            if caption and not group["caption"]:
                group["caption"] = caption
            if coalesce:
                group["coalesce"] = True
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
                group["update"],
                group["user_id"],
                group["file_ids"],
                group["caption"],
                coalesce=group.get("coalesce", False),
            )
        except Exception as e:
            logger.error("Media-group dispatch failed for user %s: %s", group["user_id"], e)
            upd = group["update"]
            chat_id = upd.effective_chat.id if upd.effective_chat else group["user_id"]
            await self._turn_death_notice(
                group["user_id"], chat_id, messages.TURN_FAILED_PHOTO
            )

    async def _dispatch_photo_task(
        self,
        update: Update,
        user_id: int,
        file_ids: List[str],
        caption: str,
        *,
        coalesce: bool = False,
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
            coalesce=coalesce,
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
        # DGN-941: leading "/queue" caption token -> coalescing path (token
        # stripped from the caption before it enters the prompt).
        caption, coalesce = self._split_caption_queue(message.caption or "")

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
            coalesce=coalesce,
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
                inbound={
                    "chat_id": chat.id,
                    "thread_id": getattr(message, "message_thread_id", None),
                    "message_id": getattr(message, "message_id", None),
                    "source": "message",
                },
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

            response = await self._finish_turn_with_auto_resume(
                user_id=user_id, chat_id=chat.id, response=response, resume_caller=resume_caller
            )
            if response is None:
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
                assembled=getattr(response, "turn_assembled", False),
                notice_meta=self._notice_meta(response),
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

    async def _retry_send(
        self, send, *, chat_id, text: str, retries: int, raise_on_failure: bool = False
    ) -> bool:
        """DGN-1557 shared retry-with-backoff core: attempt `send()` up to
        `retries` times (1s -> 2s backoff), log loudly (never swallow silently)
        on final failure. `send` closes over whichever transport a caller has
        on hand (app.bot.send_message for a bare chat_id, update.message.
        reply_text for a command ack) -- one retry loop, two send shapes.

        raise_on_failure=True re-raises the last attempt's exception instead
        of returning False: used by _reply_guaranteed so a genuinely dead
        channel still escapes to PTB's own dispatcher and the existing
        _error_handler turn-death notice -- the same fallback a bare
        reply_text already had, not a new one. _send_guaranteed's callers
        (mid-stream notices) keep the swallow-and-return-False default.
        """
        delay = 1.0
        last_exc: Optional[Exception] = None
        for i in range(retries):
            try:
                await send()
                return True
            except Exception as e:
                last_exc = e
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
        if raise_on_failure and last_exc is not None:
            raise last_exc
        return False

    async def _send_guaranteed(
        self, chat_id: int, text: str, *, reply_markup=None, retries: int = 3
    ) -> bool:
        """B3: at timeout the Telegram HTTP path can be transiently down, and a single
        unguarded send gets swallowed -> total silence. Retry with backoff so at least
        one user-facing message lands; log loudly (never swallow) on final failure."""
        app = self.application
        return await self._retry_send(
            lambda: app.bot.send_message(chat_id, text, reply_markup=reply_markup),
            chat_id=chat_id, text=text, retries=retries,
        )

    async def _reply_guaranteed(
        self, update: Update, text: str, *, reply_markup=None, retries: int = 3
    ) -> None:
        """DGN-1557: command acks that follow a REAL committed side effect
        (session reset, model switch, interrupt/teardown, restart launch,
        usage-retry job launch) travel the same flaky channel a plain
        reply_text can silently lose -- and unlike a fresh notice, the state
        change already happened, so losing the ack is a correctness gap, not
        just a UX one. Retries a transient failure silently. If the channel
        is genuinely dead past all retries, raises the last exception so
        PTB's normal dispatch takes over: the existing _error_handler sends
        its one TURN_FAILED notice exactly as it always has -- no new
        "committed but unacked" copy, no witness bookkeeping (owner
        explicitly rejected both). If that notice also fails, the SAME
        channel just failed twice in a row, which is the expected shape of a
        truly dead transport, not a new failure mode to design around."""
        message = update.message
        chat = getattr(update, "effective_chat", None)
        chat_id = chat.id if chat else getattr(message, "chat_id", None)
        kwargs = {"reply_markup": reply_markup} if reply_markup is not None else {}
        await self._retry_send(
            lambda: message.reply_text(text, **kwargs),
            chat_id=chat_id, text=text, retries=retries, raise_on_failure=True,
        )

    async def _auto_resume_loop(self, *, user_id, chat_id, response, resume_caller):
        app = self.application
        attempt = 0
        notified = False
        # DGN-1593 r3: a timeout stop signal kills background jobs like an
        # auto-interrupt; tell the owner which, before STILL_WORKING.
        await self._send_bg_killed_notice(
            chat_id, user_id, getattr(response, "killed_jobs", None)
        )
        while (
            getattr(response, "timed_out", False)
            and AUTO_RESUME
            and attempt < AUTO_RESUME_MAX
        ):
            resume_sid = await self._resolve_resume_sid(user_id, response)
            if not resume_sid:
                logger.warning(
                    "Auto-resume for user %s: no resume sid available on "
                    "attempt %d -- ending resume loop without dispatching",
                    user_id, attempt,
                )
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
            await self._send_bg_killed_notice(
                chat_id, user_id, getattr(response, "killed_jobs", None)
            )
        return response

    async def _finish_turn_with_auto_resume(
        self, *, user_id: int, chat_id: int, response: ChatResponse, resume_caller
    ) -> Optional[ChatResponse]:
        """DGN-1523: the ONE gate every process_message caller must pass
        through before touching response.content.

        Runs _auto_resume_loop; if the settled response still carries
        timed_out=True (auto-resume off, exhausted, or no resume sid), sends
        the tap-to-continue notice (the ONLY place that both instructs a tap
        AND creates the button) and returns None -- the caller must stop.
        Otherwise returns the settled response for the caller's normal reply
        path. Skipping this call (as three call sites did before DGN-1523)
        means a turn that soft-stops on timeout never gets a resume attempt
        AND leaks response.content -- messages.TIMEOUT_PAUSED, a fact-only
        string with no button behind it -- straight to the user.
        """
        response = await self._auto_resume_loop(
            user_id=user_id, chat_id=chat_id, response=response, resume_caller=resume_caller
        )
        if getattr(response, "timed_out", False):
            await self._send_resume_notice(chat_id=chat_id, user_id=user_id, response=response)
            return None
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

    @staticmethod
    def _notice_meta(response) -> Optional[Dict[str, Any]]:
        """DGN-1586: typed notice-carrier metadata off a ChatResponse. The
        promotion decision keys off THIS, never off string matching against
        the body (an accidental identical model string is not a receipt)."""
        nid = getattr(response, "notice_id", None)
        if not nid:
            return None
        return {
            "id": nid,
            "attempt": getattr(response, "notice_attempt", None),
            "kind": getattr(response, "notice_kind", None),
            "version": getattr(response, "notice_version", None),
            "text": getattr(response, "notice_text", None),
        }

    async def _promote_notice_delivered(
        self,
        meta: Dict[str, Any],
        chat_id: int,
        message_id: int,
        method: str,
        body_text: str,
        related_ids: Optional[List[int]] = None,
    ) -> None:
        """DGN-1586 receipt processor (spec 3.6): record the REAL carrier
        receipt on the spool AND the outbound ledger. Both writes are
        fail-soft in opposite directions: a promotion failure after a real
        send still leaves the ledger row as success evidence (duplicate risk
        stays bounded by the 3-attempt cap), and a ledger failure after a
        promoted receipt keeps delivered (record repair is operator-owned,
        spec 5)."""
        try:
            await asyncio.to_thread(
                notice_spool.promote_delivered,
                PROJECT_ROOT,
                meta["id"],
                chat_id,
                message_id,
                method,
                meta.get("attempt"),
                related_ids,
            )
        except Exception:
            logger.exception(
                "DGN-1586 notice %s promotion failed AFTER successful send "
                "(chat %s msg %s) -- ledger row below is the evidence",
                meta["id"], chat_id, message_id,
            )
        try:
            await asyncio.to_thread(
                notice_spool.append_ledger,
                PROJECT_ROOT,
                {
                    "ts": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                    "chat_id": str(chat_id),
                    "body": body_text,
                    "audience": "owner",
                    "notice_id": meta["id"],
                    "kind": meta.get("kind"),
                    "version": meta.get("version"),
                    "message_id": message_id,
                    "method": method,
                    "attempt": meta.get("attempt"),
                },
            )
        except Exception:
            logger.exception("DGN-1586 notice ledger append failed")

    async def _settle_notice_receipt(
        self,
        meta: Dict[str, Any],
        chat_id: int,
        body_receipts: List[Tuple[Any, str]],
        edited_message_id: Optional[int],
        display: str,
    ) -> None:
        """DGN-1586: pick the carrier receipt for this turn's notice.

        The notice rides the TAIL of the body, so the carrier is the LAST
        successfully sent body part (all earlier parts succeeded too or the
        send raised before reaching here -- a first-segment success alone
        can never promote). The streamed in-place edit path passes the
        edited draft id: force_edit guarantees the edit request actually
        carried the full notice payload (a no-call `True` cannot reach
        here), and Telegram's `message is not modified` on that exact
        payload is accepted as same-content confirmation (spec 3.6). No
        carrier at all -> pending is preserved for the next owner turn."""
        if edited_message_id is not None:
            converted = balance_telegram_html(markdown_to_telegram_html(display))
            await self._promote_notice_delivered(
                meta, chat_id, edited_message_id, "edit", converted
            )
            return
        if body_receipts:
            carrier, sent_text = body_receipts[-1]
            mid = getattr(carrier, "message_id", None)
            if mid is None:
                logger.error(
                    "DGN-1586 notice %s carrier has no message_id -- not "
                    "promoted (pending preserved)", meta["id"],
                )
                return
            related = [
                m for m, _ in body_receipts
                if getattr(m, "message_id", None) is not None
            ]
            await self._promote_notice_delivered(
                meta, chat_id, mid, "send", sent_text,
                related_ids=[m.message_id for m in related],
            )
            return
        logger.warning(
            "DGN-1586 notice %s: no body carrier went out this turn -- "
            "attempt consumed, pending preserved", meta["id"],
        )

    async def _reply_smart(
        self,
        message,
        content: str,
        force_options: bool = False,
        streamed: bool = False,
        draft_message_ids: Optional[List[int]] = None,
        classifier_injected: bool = False,
        assembled: bool = False,
        notice_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        # DGN-1732: shared delivery seat -- the NO_PUSH sentinel never reaches
        # the owner (the streamed-edit path skips the render-time gate).
        content, _ = strip_no_push_sentinel(content)
        display, has_marker = strip_options_marker(content)
        display, _ = strip_send_markers(display)
        # ESTATE:BEGIN idrill marker strip on the reply path (L-SEAM _reply_smart, DGN-924)
        # DGN-924: drop the [[IDRILL:..]] marker from the body; the step1
        # keyboard is rendered from the marker in _send_content_artifacts.
        display, idrill_arm_id = strip_idrill_marker(display)
        # DGN-983: a model turn that arms a keyboard must not also ask for the
        # same value in prose (output_gate IDRILL_PROSE_Q; normalize + count).
        display, prose_normalized = self._idrill_prose_gate(
            display, idrill_arm_id, RAIL_MODEL)
        # DGN-1500: layered term dictionary (output_gate TERM_LEAK): every
        # listed mistranslation is counted, only safe entries are rewritten.
        display, terms_rewritten = self._term_dict_gate(display, RAIL_MODEL)
        prose_normalized = prose_normalized or terms_rewritten
        # ESTATE:ELSE
        # prose_normalized = False
        # ESTATE:END
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
        # DGN-992: a LABELED marker carries the button labels itself; the
        # labels must be read from the ORIGINAL content because `display`
        # already had the marker line stripped above.
        authored_marker = has_marker and not classifier_injected
        consumed_options: Optional[List[str]] = None
        if force_options and authored_marker:
            stripped_display, consumed_options = strip_consumed_options(
                display, marker_labels=extract_marker_labels(content)
            )
            if consumed_options:
                display = stripped_display
        # DGN-555: selective reply-linking of the final response body.
        reply_to = self._reply_link_id(message)
        has_code = "```" in display
        # DGN-932: track whether at least one loud send occurred this turn so
        # the button appendix can fall back to loud when it is the turn's only
        # notification (turn-level 1-loud guarantee, M1 fix).
        body_was_loud = False
        # DGN-1586: carrier receipts for a notice-bearing turn (spec 3.6).
        body_receipts: List[Tuple[Any, str]] = []
        edited_message_id: Optional[int] = None
        if not streamed:
            # DGN-665: a list-only body strips to whitespace -- skip the empty
            # bubble; the SELECT_PROMPT + buttons message carries the turn.
            if display.strip():
                body_receipts = await self._send_text_body(
                    message, display, preview, reply_to=reply_to)
                body_was_loud = True
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
            body_receipts = await self._send_text_body(
                message, display, preview, reply_to=reply_to)
            body_was_loud = True
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
                body_receipts = await self._send_text_body(
                    message, display, preview, reply_to=reply_to)
                body_was_loud = True
            else:
                # Streamed draft was the loud carrier before the turn finalized;
                # the draft notification already fired at create_draft() time.
                body_was_loud = True
        elif draft_message_ids and display.strip():
            # DGN-376 v1.1 (M1): streamed prose-only reply. The final draft
            # bubble streamed as plain text; re-render it in place as HTML via
            # the same converter the non-streamed path uses. Fallback (multi-
            # bubble reply or edit failure): delete the drafts and re-send
            # through the converting body path so the message is never lost.
            # DGN-555: a reply link cannot ride an in-place edit, so when the
            # link policy fires on a RICH final (DGN-1720) the edit is skipped
            # and the same delete + re-send fallback delivers the body as a
            # fresh, linked message.
            # DGN-1586: a notice-bearing turn forces the REAL edit -- the
            # live draft streamed WITHOUT the finalize-appended notice, so
            # the no-op skip's "draft already correct" assumption is false
            # and its no-call True is not delivery evidence (spec 3.6).
            # DGN-1720 (owner 2026-09-26): the reply-link swap is reserved for
            # a RICH final. A plain final (nothing the plain draft cannot
            # already show -- see _streamed_final_rich_reasons) is edited in
            # place even when the link policy fires: no fade-out + re-send,
            # the reply link is forgone. An edit failure still falls through
            # to the linked delete + re-send below, so no text is lost.
            bot = message.get_bot()
            chat_id = message.chat.id
            edited = False
            rich = self._streamed_final_rich_reasons(
                content, display, preview, force_options, notice_meta,
                draft_message_ids,
            )
            if reply_to is None or not rich:
                edited = await self._edit_streamed_prose_html(
                    bot, chat_id, display, preview, draft_message_ids,
                    force_edit=(assembled or notice_meta is not None
                                or prose_normalized),
                )
            if edited:
                edited_message_id = draft_message_ids[0]
                if reply_to is not None:
                    logger.info(
                        "DGN-1720 plain final edited in place (draft %s); "
                        "reply link to %s skipped",
                        draft_message_ids[0], reply_to,
                    )
            else:
                if reply_to is not None and rich:
                    logger.info(
                        "DGN-1720 rich final (%s): draft swap with reply link",
                        ",".join(rich),
                    )
                for mid in draft_message_ids:
                    try:
                        await bot.delete_message(chat_id, mid)
                    except Exception as e:
                        logger.warning("Failed to delete streamed draft %s: %s", mid, e)
                body_receipts = await self._send_text_body(
                    message, display, preview, reply_to=reply_to)
            # In-place edit or re-send: draft already notified at creation.
            body_was_loud = True
        # ESTATE:BEGIN DGN-983 a streamed body the idrill prose gate emptied
        # DGN-983: the count-question WAS the whole streamed body. No branch
        # above re-renders an empty body, so the stale draft would keep the
        # question on screen; delete it -- the keyboard message carries the turn.
        if (prose_normalized and draft_message_ids and not display.strip()
                and not consumed_options):
            for mid in draft_message_ids:
                try:
                    await message.get_bot().delete_message(message.chat.id, mid)
                except Exception as e:
                    logger.warning("Failed to delete streamed draft %s: %s", mid, e)
            body_was_loud = True
        # ESTATE:END
        if notice_meta is not None:
            # Promote BEFORE the artifact tail: buttons/files are not the
            # notice carrier and their failures must not lose the receipt.
            await self._settle_notice_receipt(
                notice_meta, message.chat.id, body_receipts,
                edited_message_id, display,
            )
        await self._send_content_artifacts(
            message, content, force_options, options=consumed_options,
            body_was_loud=body_was_loud, body=display,
            carrier_id=self._body_carrier_id(body_receipts, edited_message_id),
        )
        # ESTATE:BEGIN DGN-1417 the session-start update notice goes out under this reply
        self._kick_update_offer(message.chat.id)
        # ESTATE:END

    async def _send_text_body(
        self,
        message,
        content: str,
        preview: bool = False,
        reply_to: Optional[int] = None,
    ) -> List[Tuple[Any, str]]:
        # DGN-1586: returns the per-part send receipts [(telegram message,
        # actually-transmitted text)] so a notice-carrying turn can promote
        # delivered from the REAL carrier (the last body part). A mid-body
        # send failure raises out of here, so a partial send never returns
        # a receipt list that looks complete.
        receipts: List[Tuple[Any, str]] = []
        # DGN-376: preview=True (link_preview:: opt-in) restores Telegram's
        # default preview; otherwise previews are suppressed.
        lp = None if preview else LINK_PREVIEW_OFF
        # DGN-555: only the FIRST sent part of the final body carries the reply
        # link; every subsequent part goes out plain.
        link_pending = reply_to is not None
        # DGN-1209: the reply body renders through the SAME segment-aware
        # pipeline as every other rail (_render_prose_html_segments: fence
        # split -> code_segment_html / markdown_to_telegram_html -> DGN-891
        # rebalance), so the machine-line gate has ONE bot-side choke point.
        # This is the model-turn reply -> rail=model (drop/strip registered
        # machine lines; unregistered shapes only log, never alert).
        for rendered in self._render_prose_html_segments(content, rail=RAIL_MODEL):
            if link_pending:
                link_pending = False
                linked_ok, linked = await self._try_send_linked(
                    message, rendered, lp, reply_to)
                if linked_ok:
                    receipts.append((linked, rendered))
                    continue
                # DGN-555: linked send rejected -> degrade to the plain
                # (unlinked) sends below; a reply link never fails a turn.
            try:
                sent = await message.reply_text(
                    rendered,
                    parse_mode="HTML",
                    link_preview_options=lp,
                )
                receipts.append((sent, rendered))
            except Exception:
                # DGN-376 v1.1 (m1) + DGN-891: tag-STRIPPED readable
                # fallback (never raw markdown source, never leaked
                # tags); keeps the preview suppression of the send it
                # replaces. DGN-1586: the receipt records the fallback
                # text -- the ledger body is what actually went out.
                plain = html_to_plain_text(rendered)
                sent = await message.reply_text(plain, link_preview_options=lp)
                receipts.append((sent, plain))
        return receipts

    async def _try_send_linked(self, message, rendered: str, lp, reply_to: int):
        """DGN-555: attempt the reply-linked send of the first body part.

        allow_sending_without_reply lets Telegram itself degrade to a plain
        send when the trigger message is gone; any other rejection returns
        (False, None) so the caller re-sends unlinked. Never raises.
        Returns (True, sent message) on success -- the success FLAG is
        separate from the message object so the send-succeeded judgment
        never depends on what the transport returned (DGN-1586 receipt
        source; a receipt-less success just skips promotion).
        """
        try:
            sent = await message.reply_text(
                rendered,
                parse_mode="HTML",
                link_preview_options=lp,
                reply_parameters=ReplyParameters(
                    message_id=reply_to, allow_sending_without_reply=True
                ),
            )
            return True, sent
        except Exception as e:
            logger.warning("Reply-linked send failed (mid=%s): %s", reply_to, e)
            return False, None

    _RICH_TAG_REASONS = {
        "blockquote": "blockquote",
        "code": "code",
        "pre": "code",
    }

    @classmethod
    def _streamed_final_rich_reasons(
        cls,
        content: str,
        display: str,
        preview: bool,
        force_options: bool,
        notice_meta: Optional[Dict[str, Any]],
        draft_message_ids: Optional[List[int]],
    ) -> List[str]:
        """DGN-1720: why a streamed final is RICH (empty list == PLAIN).

        PLAIN means the plain-text draft already shows everything the final
        carries, so finalizing is an in-place edit of that draft (no fade-out
        + re-send). Anything listed here is something the live draft cannot
        show; a rich final keeps today's finalize (swap where it swapped).
        Reasons:
          buttons      -- [[OPTIONS]] keyboard (authored or classifier)
          send_file    -- send_file:: marker (file follows the body)
          code_block   -- fenced ``` code (DGN-085 HTML segment re-send)
          blockquote   -- blockquote / expandable fold in the HTML render
          code         -- inline <code>/<pre> in the HTML render
          formatting   -- any other HTML tag (bold, italic, link, heading..)
          link_preview -- link_preview:: opt-in (draft streams previews off)
          notice       -- DGN-1586 notice-bearing turn (receipt contract)
          multi_bubble -- more than one draft or the final splits (overflow)
        Pure; never raises into the finalize path.
        """
        reasons: List[str] = []
        try:
            if force_options:
                reasons.append("buttons")
            if strip_send_markers(content)[1]:
                reasons.append("send_file")
            if "```" in display:
                reasons.append("code_block")
            converted = markdown_to_telegram_html(display)
            for tag in sorted(set(re.findall(r"<([a-zA-Z][a-zA-Z0-9-]*)", converted))):
                reason = cls._RICH_TAG_REASONS.get(tag.lower(), "formatting")
                if reason not in reasons:
                    reasons.append(reason)
            if preview:
                reasons.append("link_preview")
            if notice_meta is not None:
                reasons.append("notice")
            if len(draft_message_ids or []) != 1 or len(split_text(display)) != 1:
                reasons.append("multi_bubble")
        except Exception as e:
            logger.warning("DGN-1720 rich classification failed: %s", e)
            reasons.append("unclassified")
        return reasons

    async def _edit_streamed_prose_html(
        self,
        bot,
        chat_id: int,
        display: str,
        preview: bool,
        draft_message_ids: List[int],
        force_edit: bool = False,
    ) -> bool:
        """DGN-376 v1.1 (M1): re-render a finalized streamed prose draft as HTML.

        The draft bubble streamed as plain text (unchanged behavior); once the
        turn is final, edit that single bubble in place with the converted HTML
        (markdown_to_telegram_html -- the exact converter the non-streamed path
        uses). Returns True when the draft is already correct or the edit
        succeeded; False tells the caller to fall back to the existing
        delete-drafts + convert-resend path (multi-bubble replies, edit
        failures), which never drops the message.

        force_edit (DGN-1253): on a turn-assembled body the live draft glued
        every terminal segment verbatim, so the "plain draft already renders
        exactly this text" assumption behind the no-op skip does not hold --
        inter-segment dedup may have removed a restated paragraph, and the
        segment boundary differs. The edit is then always attempted; an
        actually-identical draft resolves via the "not modified" branch.
        """
        if len(draft_message_ids) != 1:
            return False
        if len(split_text(display)) != 1:
            return False
        # DGN-891: balance guard -- no-op on balanced (or tag-free) output,
        # so the converted == display no-op check below is unaffected.
        converted = balance_telegram_html(markdown_to_telegram_html(display))
        if (
            not force_edit
            and converted == display
            and not preview
            and not contains_telegram_html(display)
        ):
            # No markdown and nothing to escape: the plain draft already
            # renders exactly this text, so skip the no-op edit (Telegram
            # rejects it with "message is not modified").
            # DGN-846: the skip must NOT fire when the text carries
            # whitelisted Telegram HTML tags (converted == display because
            # the tags pass through verbatim) -- the plain draft shows them
            # as raw literal text, and only the parse_mode=HTML edit below
            # makes them render (e.g. the version-check update notice's
            # <blockquote expandable> fold).
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

    @staticmethod
    def _artifact_chat_id(target) -> int:
        """Resolve a send target (telegram message OR bare chat_id) to chat_id."""
        chat = getattr(target, "chat", None)
        chat_id = getattr(chat, "id", None)
        return chat_id if chat_id is not None else target

    async def _artifact_send(self, target, text: str, **kwargs) -> None:
        """DGN-966: single send primitive for artifact messages.

        target is either a telegram message (model-turn reply path ->
        reply_text) or a bare chat_id int (fast-path / proactive ->
        bot.send_message). Keeps the ONE shared artifact renderer below
        target-polymorphic without duplicating render logic per path.
        """
        if hasattr(target, "reply_text"):
            await target.reply_text(text, **kwargs)
        else:
            await self.application.bot.send_message(target, text, **kwargs)

    @staticmethod
    async def _reply_selected(message, text: str) -> None:
        """DGN-1813: "Selected: <label>" as a silent reply to the tapped bubble.

        Used when the confirmation cannot be appended to the bubble itself
        (Telegram length limit, or the edit was rejected). The owner just
        tapped, so it never alerts. Never raises: the tap still dispatches.
        """
        try:
            await message.reply_text(
                text,
                disable_notification=True,
                reply_parameters=ReplyParameters(
                    message_id=message.message_id,
                    allow_sending_without_reply=True,
                ),
            )
        except Exception as e:
            logger.warning("DGN-1813 selected-reply send failed: %s", e)

    async def _strip_keyboard_and_confirm(
        self, query, message, visible_confirmation: str
    ) -> None:
        """DGN-1876: drop the keyboard, then confirm in a silent reply.

        Never raises: a keyboard-strip rejection ("not modified" when the
        keyboard is already gone, or any other API error) must not abort the
        tap -- the choice still dispatches and the owner never sees a generic
        processing-error message for it.
        """
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.info("DGN-1876 keyboard strip skipped: %s", e)
        await self._reply_selected(message, visible_confirmation)

    async def _attach_keyboard(self, target, message_id: int, kb) -> bool:
        """DGN-1813: put the [[OPTIONS]] keyboard on an already-sent body message.

        A markup-only edit: the body text and its formatting are untouched
        and no second notification fires (the body already alerted). Returns
        False on any failure so the caller falls back to the SELECT_PROMPT
        line -- the buttons are never lost.
        """
        chat_id = self._artifact_chat_id(target)
        try:
            bot = (
                target.get_bot() if hasattr(target, "get_bot")
                else self.application.bot
            )
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=kb
            )
            return True
        except Exception as e:
            logger.warning(
                "DGN-1813 keyboard attach to body message %s failed (%s); "
                "falling back to the prompt line", message_id, e,
            )
            return False

    @staticmethod
    def _body_carrier_id(
        receipts: List[Tuple[Any, str]], edited_message_id: Optional[int]
    ) -> Optional[int]:
        """DGN-1813: id of the LAST body message of a turn, or None.

        A re-sent body's last receipt wins (split body -> last chunk); else
        the draft that was finalized in place.
        """
        if receipts:
            mid = getattr(receipts[-1][0], "message_id", None)
            if isinstance(mid, int):
                return mid
            return None
        return edited_message_id

    async def _send_content_artifacts(
        self,
        target,
        content: str,
        force_options: bool,
        options: Optional[List[str]] = None,
        body_was_loud: bool = True,
        body: Optional[str] = None,
        carrier_id: Optional[int] = None,
    ) -> None:
        """DGN-966: the SHARED artifact-render tail (marker -> file / keyboard).

        Every send path terminates here -- model turn (_reply_smart), fast-path
        and proactive (_send_smart) -- so a content string produces the SAME
        artifacts regardless of which path carried it. `target` is a telegram
        message or a bare chat_id (see _artifact_send).

        DGN-1813: `carrier_id` is the LAST body message of this turn (the
        last split chunk, or the draft edited in place) and `body` the text
        it was rendered from. The [[OPTIONS]] keyboard rides that message
        (attached after the final edit); only when there is no carrier (a
        DGN-665 list-only body stripped to nothing) or the attach fails does
        a short SELECT_PROMPT line carry it. `body` also decides the "N. "
        button prefix (options.body_lists_options).
        """
        chat_id = self._artifact_chat_id(target)
        resolved = resolve_send_paths(content, PROJECT_ROOT)
        in_root, outside = split_paths_by_scope(resolved, PROJECT_ROOT)
        await self._send_file_paths(chat_id, in_root)
        if outside:
            # DGN-966 verification round: the Allow/Deny confirm keyboard
            # (_prompt_outside_file_confirmation) assumes a live turn to
            # answer it. `target` is a real telegram message (reply_text)
            # ONLY on the model-turn path, where the owner is mid-conversation
            # right now. The bare-chat_id rail (_send_smart -- fast-path AND
            # proactive/cron push) has no such live turn: a confirm button
            # sent there either fires unattended or dies at the 20-min STALE
            # gate (_check_access) with nobody around to tap it (measured:
            # a cron push at an odd hour would leave a dead-end prompt AND
            # never actually deliver the file). Never silently drop (that IS
            # the DGN-966 defect class) -- log loud + a plain no-button
            # notice on that rail instead.
            if hasattr(target, "reply_text"):
                await self._prompt_outside_file_confirmation(
                    chat_id, chat_id, outside
                )
            else:
                await self._notify_outside_file_omitted(chat_id, outside)
        if force_options:
            clean, has_marker = strip_options_marker(content)
            if has_marker:
                # DGN-665: prefer the caller's consumed-run extraction so the
                # body strip and the buttons always come from the same run;
                # fall back to a local extraction when it was not computed
                # (strip_consumed_options first -- DGN-992: labeled-marker
                # labels win there -- then the plain extractor so a
                # non-adjacent/failed run still renders its buttons).
                button_options = options
                if not button_options:
                    _, button_options = strip_consumed_options(
                        clean, marker_labels=extract_marker_labels(content)
                    )
                if not button_options:
                    button_options = extract_options(clean)
                # DGN-1813: number the buttons only when the body as sent
                # still shows the matching numbered list; body=None (caller
                # without a body) keeps the lone-button rule only.
                numbered = (
                    body_lists_options(body, button_options or [])
                    if body is not None else None
                )
                kb = build_option_keyboard(button_options, numbered=numbered)
                if not kb:
                    # DGN-992 (fail-loud): a marker with ZERO buildable buttons
                    # must never pass silently. The body is guaranteed intact
                    # here -- the display strip only fires when options
                    # extracted (which always builds a keyboard), so whatever
                    # list/bullet text the author wrote is still readable and
                    # the user can answer in text. Log the one-line WARNING so
                    # the miss is visible in ops instead of evaporating.
                    logger.warning(
                        "[[OPTIONS]] marker present but no buttons could be "
                        "built (no marker labels, no numbered run) -- body "
                        "kept as text fallback (DGN-992)"
                    )
                if kb:
                    # DGN-932 mechanism B: the button appendix is silent by
                    # default (class "options_prompt") -- the loud body
                    # already carried the arrival signal, and a second alert
                    # buries it under the buttons message.
                    # Turn-level 1-loud guarantee: when no loud send preceded
                    # this turn (list-only body, non-streamed, no drafts), the
                    # buttons ARE the turn's only signal -- promote to loud.
                    if carrier_id is not None and await self._attach_keyboard(
                        target, carrier_id, kb
                    ):
                        kb = None
                if kb:
                    silent = (
                        notify_silent("options_prompt")
                        if body_was_loud
                        else False
                    )
                    await self._artifact_send(
                        target,
                        messages.SELECT_PROMPT,
                        reply_markup=kb,
                        disable_notification=silent,
                    )
        elif has_options_marker(content):
            # DGN-1021 (fail-loud HOIST): the DGN-992 fail-loud above sits
            # inside `if force_options:` -- when an upstream gate misses the
            # marker (the recognizer-drift defect class that killed labeled
            # markers silently), the defense was skipped along with the
            # buttons. This branch is the gate's own tripwire: an ARMABLE
            # marker line reached the render seat with the gate off. Body is
            # delivered intact either way; log-only, zero user-visible side
            # effect, and marker-less messages never enter this branch.
            logger.warning(
                "[[OPTIONS]] marker present but options gate is OFF "
                "(force_options=False) -- buttons skipped; upstream "
                "recognizer drift? (DGN-1021)"
            )
        # ESTATE:BEGIN idrill initial-keyboard render -- marker consume + the estate renderer (DGN-924/966)
        # DGN-924: [[IDRILL:<arm_id>]] initial step1 keyboard (homomorphic to
        # [[OPTIONS]] -- marker -> keyboard, no file watcher). Independent of
        # force_options: an arming turn may carry only the IDRILL marker.
        _, idrill_arm_id = strip_idrill_marker(content)
        if idrill_arm_id is not None:
            await self._idrill_send_initial_keyboard(target, idrill_arm_id)

    def _idrill_prose_gate(
        self, display: str, arm_id: Optional[str], rail: str
    ) -> Tuple[str, bool]:
        """DGN-983: drop a prose count-question beside an armed keyboard.

        Only when the marker's arm will really render a step-1 keyboard (the
        same gates as _idrill_send_initial_keyboard); the rule itself is
        output_gate IDRILL_PROSE_Q, reached through bridge.artifacts. Every
        failure leaves `display` untouched. Returns (display, changed).
        """
        if arm_id is None or not display:
            return display, False
        try:
            ready = (
                self._IDRILL_ARM_ID_RE.match(arm_id) is not None
                and artifacts.step1_renders(self._idrill_read_arm(arm_id), arm_id)
            )
            return artifacts.normalize_idrill_prose(
                display, PROJECT_ROOT, ready,
                log_dir=Path(config.logs_dir), arm_id=arm_id, rail=rail,
            )
        except Exception:
            logger.warning("idrill prose gate failed; body untouched", exc_info=True)
            return display, False

    def _term_dict_gate(self, display: str, rail: str) -> Tuple[str, bool]:
        """DGN-1500: check a model body against the layered term dictionary.

        The dictionary is this instance's framework / kit / module layer
        files (routines/lib/term_dict.py), read here and never injected into
        model context; the matcher is output_gate TERM_LEAK, reached through
        bridge.artifacts. Every hit is counted; only safe-to-rewrite entries
        change the text. Every failure leaves `display` untouched.
        """
        if not display:
            return display, False
        try:
            return artifacts.normalize_term_leaks(
                display, PROJECT_ROOT, log_dir=Path(config.logs_dir), rail=rail,
            )
        except Exception:
            logger.warning("term dictionary gate failed; body untouched",
                           exc_info=True)
            return display, False

    async def _idrill_send_initial_keyboard(self, target, arm_id: str) -> None:
        """DGN-924: render the initial step1 keyboard for an [[IDRILL:..]] marker.

        Reads the arm file and builds the keyboard from the SHARED spec builder
        (bridge.artifacts, DGN-966) -- the same spec the push.sh hop renders, so
        the keyboard is identical on every path. The arm file is the single
        source of the buttons and the copy (contract v3). Fail-soft: an invalid
        arm_id or a missing / button-less / text-less arm drops the keyboard
        silently (the marker was already stripped from the body).
        """
        if not self._IDRILL_ARM_ID_RE.match(arm_id):
            logger.warning("idrill: initial marker arm_id failed validation: %r", arm_id)
            return
        arm = self._idrill_read_arm(arm_id)
        if arm is None:
            logger.warning("idrill: initial marker arm %s missing/expired", arm_id)
            return
        spec_rows, text = artifacts.build_step_keyboard_spec(arm, arm_id, "1")
        if not spec_rows:
            logger.warning("idrill: arm %s declares no step_buttons[1]", arm_id)
            return
        if not text:
            logger.warning("idrill: arm %s declares no step_text[1]", arm_id)
            return
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(lbl, callback_data=cb) for lbl, cb in row]
            for row in spec_rows
        ])
        try:
            await self._artifact_send(target, text, reply_markup=markup)
        except Exception as e:
            logger.warning(
                "idrill: initial keyboard send failed for arm_id=%s: %s",
                arm_id, e,
            )
        # ESTATE:END

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

        DGN-1736 F1: a delivery failure is logged and RE-RAISED. Swallowing it
        made the result-first caller (sdk_bridge._send_dispatch_return_first_text)
        read success, open the latch and drop the text (popped from the
        proactive buffer / subtracted from the final), so a failed first
        text reached the owner nowhere. Every proactive_push caller already
        wraps the await in try/except.
        """
        try:
            await self._send_smart(
                chat_id,
                content,
                force_options=has_options,
                classifier_injected=classifier_injected,
                rail=RAIL_MODEL,
            )
        except Exception as e:
            logger.error("Proactive push delivery failed for chat %s: %s", chat_id, e)
            raise

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
        rail: str = RAIL_UNKNOWN,
        assembled: bool = False,
        notice_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        # DGN-1209: `rail` is the caller's declaration for the machine-line gate
        # (never inferred). Threaded down to _render_prose_html_segments.
        # D2 (DGN-515): guard shutdown race -- application is set to None in
        # _graceful_shutdown; queued run_task closures may call this after that.
        if self.application is None:
            raise RuntimeError("Bot application already stopped")
        bot = self.application.bot
        # DGN-1732: shared delivery seat for every proactive / option / resume
        # send. NO_PUSH is a bridge directive, never owner text: bare -> no
        # body goes out; trailing line -> stripped, the rest is sent.
        content, had_sentinel = strip_no_push_sentinel(content)
        if (
            had_sentinel and not content.strip()
            and not draft_message_ids and notice_meta is None
        ):
            logger.info("NO_PUSH-only send to chat %s suppressed at the seat", chat_id)
            return
        display, has_marker = strip_options_marker(content)
        display, _ = strip_send_markers(display)
        # ESTATE:BEGIN idrill marker strip on the proactive/option send path (L-SEAM _send_smart)
        # DGN-924: drop the [[IDRILL:..]] marker from the body; the step1
        # keyboard is rendered from the marker in _send_content_artifacts.
        display, idrill_arm_id = strip_idrill_marker(display)
        # DGN-983: model prose only -- consumer stdout (fastpath / followup)
        # is pack-authored copy and is never rewritten here.
        prose_normalized = False
        if rail == RAIL_MODEL:
            display, prose_normalized = self._idrill_prose_gate(
                display, idrill_arm_id, rail)
            # DGN-1500: layered term dictionary, model prose only (same
            # reason: consumer stdout is pack-authored copy).
            display, terms_rewritten = self._term_dict_gate(display, rail)
            prose_normalized = prose_normalized or terms_rewritten
        # ESTATE:ELSE
        # prose_normalized = False
        # ESTATE:END
        # DGN-376: link previews default OFF; a link_preview:: line opts in.
        display, preview = strip_link_preview_marker(display)
        # DGN-159: last-mile scrub of leaked tool-call markup (proactive / option /
        # resume send path); streamed drafts are scrubbed in strip_display_markers.
        display = strip_toolcall_markup(display)
        # DGN-665: same as _reply_smart -- an AGENT-AUTHORED marker renders the
        # choices as buttons only, so drop the consumed numbered run from the
        # body when it extracts; on extraction failure the list stays. A
        # classifier-injected marker keeps the body list.
        # DGN-992: labeled-marker labels come from the ORIGINAL content (the
        # marker line is already stripped from `display`).
        authored_marker = has_marker and not classifier_injected
        options: List[str] = []
        if force_options and authored_marker:
            stripped_display, options = strip_consumed_options(
                display, marker_labels=extract_marker_labels(content)
            )
            if options:
                display = stripped_display
        has_code = "```" in display
        # DGN-932: track whether at least one loud send occurred this turn so
        # the button appendix can fall back to loud when it is the turn's only
        # notification (turn-level 1-loud guarantee, M1 fix).
        body_was_loud = False
        # DGN-1586: same carrier-receipt contract as _reply_smart -- both
        # rails share it (spec 3.6).
        body_receipts: List[Tuple[Any, str]] = []
        edited_message_id: Optional[int] = None
        if not streamed:
            # DGN-665: a list-only body strips to whitespace -- skip the empty
            # bubble; the SELECT_PROMPT + buttons message carries the turn.
            if display.strip():
                body_receipts = await self._send_text_body_chat(
                    chat_id, display, preview, rail=rail)
                body_was_loud = True
        elif has_code and (force_options or draft_message_ids):
            # DGN-085: same backstop as _reply_smart -- code+options coexistence
            # forces clean re-send via HTML segments so code/tables render correctly.
            for mid in (draft_message_ids or []):
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception as e:
                    logger.warning("Failed to delete streamed draft %s: %s", mid, e)
            body_receipts = await self._send_text_body_chat(
                chat_id, display, preview, rail=rail)
            body_was_loud = True
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
                body_receipts = await self._send_text_body_chat(
                    chat_id, display, preview, rail=rail)
            # Streamed draft was the loud carrier; notification fired at
            # create_draft() time.
            body_was_loud = True
        elif draft_message_ids and display.strip():
            # DGN-376 v1.1 (M1): same streamed prose finalize as _reply_smart --
            # edit the single draft bubble to HTML in place; fall back to
            # delete + convert-resend so the message is never lost.
            # DGN-1586: notice-bearing turn -> force the real edit (the
            # draft never carried the finalize-appended notice; a no-call
            # skip is not delivery evidence).
            if await self._edit_streamed_prose_html(
                bot, chat_id, display, preview, draft_message_ids,
                force_edit=(assembled or notice_meta is not None
                            or prose_normalized),
            ):
                edited_message_id = draft_message_ids[0]
            else:
                for mid in draft_message_ids:
                    try:
                        await bot.delete_message(chat_id, mid)
                    except Exception as e:
                        logger.warning("Failed to delete streamed draft %s: %s", mid, e)
                body_receipts = await self._send_text_body_chat(
                    chat_id, display, preview, rail=rail)
            # Draft was loud at creation; in-place edit inherits that.
            body_was_loud = True
        # ESTATE:BEGIN DGN-983 a streamed body the idrill prose gate emptied
        # DGN-983: same as _reply_smart -- an emptied streamed body leaves no
        # re-render, so the stale draft with the question is deleted.
        if (prose_normalized and draft_message_ids and not display.strip()
                and not options):
            for mid in draft_message_ids:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception as e:
                    logger.warning("Failed to delete streamed draft %s: %s", mid, e)
            body_was_loud = True
        # ESTATE:END
        if notice_meta is not None:
            await self._settle_notice_receipt(
                notice_meta, chat_id, body_receipts, edited_message_id,
                display,
            )
        # DGN-966: the artifact tail (send_file paths, [[OPTIONS]] keyboard,
        # [[IDRILL]] keyboard) is the SHARED renderer -- the same one the
        # model-turn path uses -- so the fast-path (DGN-801) and proactive
        # pushes render identical artifacts. Before this, _send_smart carried
        # its own partial copy and silently swallowed the IDRILL keyboard.
        await self._send_content_artifacts(
            chat_id,
            content,
            force_options,
            options=options or None,
            body_was_loud=body_was_loud,
            body=display,
            carrier_id=self._body_carrier_id(body_receipts, edited_message_id),
        )
        # ESTATE:BEGIN DGN-1417 the session-start update notice goes out under this reply
        self._kick_update_offer(chat_id)
        # ESTATE:END

    @staticmethod
    def _render_prose_html_segments(content: str, rail: str = RAIL_UNKNOWN) -> List[str]:
        """Render content into Telegram-HTML chunks via the segment-aware
        pipeline: fenced code -> code_segment_html, prose (outside fences)
        -> markdown_to_telegram_html. markdown_to_telegram_html's own
        contract (formatting.py:950-954) forbids feeding it a raw code
        fence, so the split MUST happen before either renderer runs.

        Shared by _send_text_body / _send_text_body_chat (model-turn sends,
        fast-path body) and _idrill_post_followup (idrill followup_cmd
        STDOUT, DGN-1076) so every path honors the same contract and
        renders identically.

        DGN-1209: this is the bot-side CHOKE POINT for the machine-line gate
        (the other is formatting.sanitize_message_for_telegram). The gate runs
        on the raw pre-render text, before the fence split. `rail` is the
        caller's declaration (RAIL_CONSUMER / RAIL_MODEL); an undeclared
        caller gets RAIL_UNKNOWN and its alerts say so.
        """
        content = apply_machine_line_gate(content, rail)
        rendered: List[str] = []
        for segment, is_code, lang in split_into_segments(content):
            parts = [p for p in split_text(segment) if p.strip()]
            if is_code:
                seg_parts = [code_segment_html(p, lang) for p in parts]
            else:
                # DGN-376: prose goes out as Telegram HTML; markdown the
                # agents emit is converted, everything else is escaped so
                # stray _ * [ ] < > never mangle the message.
                seg_parts = [markdown_to_telegram_html(p) for p in parts]
            # DGN-891: a tag span can straddle a split_text boundary; close
            # open tags at each chunk end and re-open them on the next chunk
            # so every send is independently valid HTML (no-op when balanced).
            rendered.extend(rebalance_html_chunks(seg_parts))
        return rendered

    async def _send_text_body_chat(
        self, chat_id: int, content: str, preview: bool = False, rail: str = RAIL_UNKNOWN
    ) -> List[Tuple[Any, str]]:
        # DGN-1586: same per-part receipt contract as _send_text_body (the
        # bare-chat rail shares the notice receipt contract, spec 3.6).
        receipts: List[Tuple[Any, str]] = []
        bot = self.application.bot
        # DGN-376: preview=True (link_preview:: opt-in) restores Telegram's
        # default preview; otherwise previews are suppressed.
        lp = None if preview else LINK_PREVIEW_OFF
        for rendered in self._render_prose_html_segments(content, rail=rail):
            try:
                sent = await bot.send_message(
                    chat_id,
                    rendered,
                    parse_mode="HTML",
                    link_preview_options=lp,
                )
                receipts.append((sent, rendered))
            except Exception:
                # DGN-376 v1.1 (m1) + DGN-891: tag-STRIPPED readable
                # fallback (never raw markdown source, never leaked
                # tags); keeps the preview suppression of the send it
                # replaces.
                plain = html_to_plain_text(rendered)
                sent = await bot.send_message(
                    chat_id, plain, link_preview_options=lp
                )
                receipts.append((sent, plain))
        return receipts

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

    async def _notify_outside_file_omitted(
        self, chat_id: int, paths: List[Path]
    ) -> None:
        """DGN-966 verification round: non-interactive-rail counterpart to
        _prompt_outside_file_confirmation. No live turn exists to answer an
        Allow/Deny tap here (see call site comment), so the file is withheld
        with NO button and no session-state write (nothing pending to expire
        or leak into a later unrelated turn) -- but never silent: a loud log
        line plus a plain owner-visible chat notice, so the omission is
        findable rather than swallowed.
        """
        logger.warning(
            "outside-root send_file omitted on non-interactive rail "
            "(chat %s, no live turn to confirm): %s",
            chat_id, [str(p) for p in paths],
        )
        listing = "\n".join(f"- {p}" for p in paths)
        try:
            await self.application.bot.send_message(
                chat_id,
                f"{messages.EXTERNAL_FILE_OMITTED_NONINTERACTIVE}\n{listing}",
            )
        except Exception as e:
            logger.warning(
                "outside-root omission notice send failed (chat %s): %s",
                chat_id, e,
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
        # ESTATE:BEGIN DGN-1417 tap time is recorded at callback receipt (grill
        # fix 4), and the update notice / restart buttons are exempt from the
        # 20-min gate: their staleness is decided by the ledger.
        update_tap_at = int(time.time())
        if query and query.data and query.data.startswith(("upd:n:", "upd:o:", "upd:p:", "upd:b:", "upd:r:", "upd:q:")):
            cdn_done_tap = True
        # ESTATE:END
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

        # ESTATE:BEGIN DGN-1417 ledger callbacks.  Callback data holds only an
        # index; the durable ledger prevents forged/stale target substitution.
        if data.startswith("upd:"):
            await self._handle_update_callback(query, data, user_id, chat.id, update_tap_at)
            return
        # ESTATE:END

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
            # DGN-1813: the keyboard rides the body bubble (model replies and
            # push notifications alike). On tap that SAME bubble is edited:
            # body kept verbatim, keyboard removed, "Selected: <label>"
            # appended. Only the DGN-665 fallback prompt line (a list-only
            # body) is replaced outright -- it has no body to keep.
            message = query.message
            text = getattr(message, "text", None)
            caption = getattr(message, "caption", None)
            if isinstance(text, str) and text.strip() == messages.SELECT_PROMPT.strip():
                await query.edit_message_text(messages.SELECTED.format(choice=choice))
            # Non-text callback messages have no body to preserve. Keep the
            # legacy confirmation behavior for them (also covers old clients).
            elif not isinstance(text, str) and not isinstance(caption, str):
                await query.edit_message_text(messages.SELECTED.format(choice=choice))
            else:
                is_caption = isinstance(caption, str)
                body_html = getattr(
                    message, "caption_html" if is_caption else "text_html", None
                )
                confirmation = messages.SELECTED.format(choice=html.escape(choice))
                visible_confirmation = messages.SELECTED.format(choice=choice)
                limit = 1024 if is_caption else 4096
                combined = (
                    body_html + "\n\n" + confirmation
                    if isinstance(body_html, str) else None
                )
                visible_body = caption if is_caption else text
                if (
                    combined is not None
                    and len(visible_body + "\n\n" + visible_confirmation) <= limit
                ):
                    try:
                        if is_caption:
                            await query.edit_message_caption(
                                combined, parse_mode="HTML", reply_markup=None
                            )
                        else:
                            await query.edit_message_text(
                                combined, parse_mode="HTML", reply_markup=None
                            )
                    except telegram.error.BadRequest as e:
                        # DGN-1876: "not modified" means this bubble already
                        # carries this exact selection with no keyboard -- an
                        # earlier callback for the same tap (double tap /
                        # redelivery) already confirmed AND dispatched it.
                        # Re-dispatching would send the choice to the agent
                        # twice; surfacing it would show the owner a generic
                        # processing error. Drop the duplicate quietly.
                        if "not modified" in str(e).lower():
                            logger.info(
                                "opt callback %r: bubble already shows this "
                                "selection (duplicate tap) -- ignored", data
                            )
                            return
                        await self._strip_keyboard_and_confirm(
                            query, message, visible_confirmation
                        )
                    except Exception:
                        await self._strip_keyboard_and_confirm(
                            query, message, visible_confirmation
                        )
                else:
                    # DGN-1813: appending would break the message limit --
                    # strip the keyboard only and confirm in a short reply
                    # to that bubble.
                    await self._strip_keyboard_and_confirm(
                        query, message, visible_confirmation
                    )
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
                        inbound={
                            "chat_id": chat_id,
                            "thread_id": getattr(query.message, "message_thread_id", None),
                            "message_id": getattr(query.message, "message_id", None),
                            "source": "callback",
                        },
                    )

                    async def resume_caller(cont: str) -> ChatResponse:
                        sess = await session_manager.get_session(user_id)
                        return await sdk_bridge.process_message(
                            user_message=cont,
                            user_id=user_id,
                            chat_id=chat_id,
                            session_id=self._effective_session_id(user_id, sess),
                            model=sess.get("model"),
                            permission_callback=self._permission_callback,
                            typing_callback=lambda: app.bot.send_chat_action(chat_id, action="typing"),
                            bot=app.bot,
                            proactive_push=self._proactive_push,
                        )

                    response = await self._finish_turn_with_auto_resume(
                        user_id=user_id, chat_id=chat_id, response=response, resume_caller=resume_caller
                    )
                    if response is None:
                        return
                    await self._save_session_id(user_id, response)
                    await self._send_smart(
                        chat_id,
                        response.content,
                        force_options=response.has_options,
                        streamed=response.streamed,
                        draft_message_ids=response.draft_message_ids,
                        classifier_injected=getattr(response, "options_classifier_injected", False),
                        rail=RAIL_MODEL,
                        assembled=getattr(response, "turn_assembled", False),
                        notice_meta=self._notice_meta(response),
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

        # ESTATE:BEGIN estate callback dispatch branches -- usage-defer retry + idrill (L-SEAM _handle_callback)
        if data.startswith("usageretry:"):
            await self._handle_usage_retry_callback(query, data)
            return

        if data.startswith("idrill:"):
            await self._handle_idrill_callback(query, data)
            return
        # ESTATE:END

        if data.startswith(CDN_DONE_PREFIX):
            # DGN-915: countdown completion-affordance tap. Remove the inline
            # keyboard from the done message so the affordance clears cleanly.
            # query.answer() already called above (spinner dismiss).
            # Stale tap (message gone / session ended): Telegram returns an
            # error; swallowed fail-soft -- the button is already unreachable.
            # TODO(DGN-915): no next-step trigger exists in the bridge yet.
            # Removing dead-air (dead button) is the sole win here. A future
            # agent-coordinated step can wire a proactive model-turn from this
            # branch when the domain sequence machinery is in place.
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as e:
                logger.warning("cdn:done keyboard clear failed (ignored): %s", e)
            return

        # DGN-1050: the DGN-994 "authsync:restart" CTA branch is retired with
        # /authsync itself. A tap on a leftover CTA button in old chat history
        # falls through all branches below and no-ops (query already answered
        # above; the 20-min stale gate drops most of them before that).

        # DGN-1814 r3 two-step picker: a vendor tap opens its family step; a
        # family of a vendor the bridge cannot run says so (no switch).
        if data.startswith(model_picker.CB_VENDOR):
            vendor = data[len(model_picker.CB_VENDOR):]
            session = await session_manager.get_session(user_id)
            text, buttons = model_picker.family_step(
                vendor, self._get_real_model(session), _model_whitelist(),
                self._live_model_id(), _MODEL_PERF_RANK,
            )
            await query.edit_message_text(
                text, reply_markup=self._picker_markup(buttons)
            )
            return

        if data.startswith(model_picker.CB_FOREIGN):
            vendor = data[len(model_picker.CB_FOREIGN):].split(":", 1)[0]
            await query.edit_message_text(
                messages.MODEL_VENDOR_NOT_WIRED.format(vendor=vendor)
            )
            return

        if data.startswith("model:"):
            model_name = data.split(":", 1)[1]
            session = await session_manager.get_session(user_id)
            label = model_picker.full_name(model_name, model_picker.chat_table())
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

    # DGN-1050: _handle_authsync_restart_callback (DGN-994 /authsync sync-ok
    # restart CTA) removed with the retired /authsync sync path. The restart
    # launch machinery it shared with /restart (self_restart.sh --trigger
    # user + the _restart_launch_started latch) lives on in _cmd_restart.

    # ESTATE:BEGIN idrill in-place drilldown engine (DGN-918/939/966) -- the whole declaration cluster
    # --- DGN-918: idrill in-place drilldown capture callback primitive ---

    # arm_id validation: single source lives in bridge.artifacts (DGN-966);
    # aliased here so callback handlers keep their existing reference.
    _IDRILL_ARM_ID_RE = artifacts.IDRILL_ARM_ID_RE

    # Special step-button values with bridge-level meaning (contract v3).
    # Every other value is an opaque domain token declared by the arm file.
    _IDRILL_SKIP_VALUE = "skip"
    _IDRILL_OTHER_VALUE = "other"

    @staticmethod
    def _idrill_declared_values(arm: dict, step: str):
        """Set of tap values the arm declares as buttons for a step (whitelist).
        Empty set if the arm declares no buttons for that step. Delegates to
        the shared row normalizer (bridge.artifacts, DGN-966) so flat and grid
        step_buttons shapes gate identically on every path.
        """
        return artifacts.declared_step_values(arm, step)

    @classmethod
    def _idrill_value_allowed(cls, arm: dict, step: str, value: str) -> bool:
        """Gate a tapped/captured value for a step against the arm declaration.

        Source of truth is the arm file, never a hardcoded set (contract v3):
          1. If the arm declares step_validate[step], the value must match that
             regex.
          2. Otherwise the value must be one of the declared step_buttons[step]
             values (declared-button whitelist).
          3. If the arm declares neither for that step, the value is rejected
             (fail-soft): the bridge never accepts an ungated value.
        """
        validate = arm.get("step_validate")
        pattern = None
        if isinstance(validate, dict):
            pattern = validate.get(step)
        if isinstance(pattern, str) and pattern:
            try:
                return re.match(pattern, value) is not None
            except re.error:
                logger.warning(
                    "idrill: arm step_validate[%s] is not a valid regex", step
                )
                return False
        declared = cls._idrill_declared_values(arm, step)
        if declared:
            return value in declared
        return False

    @staticmethod
    def _idrill_read_arm(arm_id: str) -> Optional[dict]:
        """Read the arm-id state file; returns dict or None (missing/malformed).

        Path + containment: bridge.artifacts is the single path authority;
        the path itself is declared by bridge/IDRILL-ARM-CONTRACT.md
        section 1. arm_id is pre-validated (no traversal); artifacts still
        resolves strictly inside the arm directory so any unexpected '/' in
        a future arm_id format change cannot escape the directory.
        """
        return artifacts.read_arm(PROJECT_ROOT, arm_id)

    @staticmethod
    def _idrill_consume_arm(arm_id: str) -> bool:
        """Consume the arm-id state file on the final tap (double-tap guard).

        Delegates to bridge.artifacts.consume_arm -- the same single path
        authority as _idrill_read_arm (DGN-1123: this helper used to hold
        its own path literal, so a relocation could break consume-only
        while reads kept working -- buttons alive, state never cleared).
        True = removed by this call; False = already absent (converged, do
        NOT fire). Any other unlink failure RAISES OSError; the caller
        turns it into a loud IDRILL_ERROR and aborts the fire.
        """
        return artifacts.consume_arm(PROJECT_ROOT, arm_id)

    @staticmethod
    def _idrill_positionals(val1, val2: str) -> list:
        """DGN-966 rider: normalize confirmation inputs to the ordered
        positional value list backing the {i} tokens.

        val1 is either the FULL ordered capture-value list (N-step fire path;
        already carries the final tap for a non-skip fire) or a legacy scalar
        step-1 value (DGN-918/924 two-step signature). The skip path appends
        the literal skip value so {N_final} renders exactly as it did on
        2-step arms ({2} == "skip").
        """
        if isinstance(val1, list):
            vals = list(val1)
            if val2 == TelegramBot._IDRILL_SKIP_VALUE:
                vals.append(val2)
            return vals
        return [val1, val2]

    @staticmethod
    def _idrill_render_confirmation(arm: dict, val1, val2: str) -> str:
        """Render the captured-input confirmation (Addition B spec).

        Priority: arm-file confirm_fmt / confirm_fmt_skip fields (the arming
        consumer owns the text; bridge renders only). Tokens are positional:
        {i} = i-th captured step value over the FULL ordered capture list
        (DGN-966 rider; two-step arms: {1}/{2} byte-identical to DGN-918/924);
        {sec} stays a named extra for hold-style arms. val1 may be the capture
        value list (N-step) or a legacy scalar step-1 value. Fallback:
        messages constants. Defensive .format: KeyError / IndexError ->
        fallback, never crash.
        """
        base = TelegramBot._idrill_confirm_base(arm, val1, val2)
        fold = TelegramBot._idrill_confirm_fold(arm, val1, val2)
        if fold:
            return base + "\n" + fold
        return base

    @staticmethod
    def _idrill_confirm_base(arm: dict, val1, val2: str) -> str:
        """The confirmation headline (DGN-918/924 confirm_fmt path; DGN-966
        renders {i} from the full capture list -- see _idrill_positionals)."""
        vals = TelegramBot._idrill_positionals(val1, val2)
        try:
            val1_num = int(vals[0])
        except (ValueError, TypeError, IndexError):
            val1_num = vals[0] if vals else "?"
        # First positional keeps the legacy int coercion; the rest render raw
        # (byte-identical to the DGN-918/924 (val1_num, val2) pair form).
        positionals = [val1_num] + vals[1:]

        is_hold = arm.get("is_hold", False)

        if is_hold:
            # hold-style arm: confirm_fmt.format(sec=<hold_sec>)
            fmt = arm.get("confirm_fmt", "")
            hold_sec = arm.get("hold_sec", val1_num)
            if fmt:
                try:
                    return fmt.format("", *positionals, sec=hold_sec)
                except (KeyError, IndexError):
                    pass
            return messages.IDRILL_LOGGED_DEFAULT.format("", hold_sec)
        elif val2 == TelegramBot._IDRILL_SKIP_VALUE:
            fmt = arm.get("confirm_fmt_skip", "")
            if fmt:
                try:
                    return fmt.format("", *positionals, sec=val1_num)
                except (KeyError, IndexError):
                    pass
            return messages.IDRILL_LOGGED_DEFAULT_SKIP.format("", val1_num)
        else:
            fmt = arm.get("confirm_fmt", "")
            if fmt:
                try:
                    return fmt.format("", *positionals, sec=val1_num)
                except (KeyError, IndexError):
                    pass
            return messages.IDRILL_LOGGED_DEFAULT.format("", val1_num)

    @staticmethod
    def _idrill_confirm_fold(arm: dict, val1, val2: str) -> str:
        """DGN-939 item6: build the optional expandable-blockquote fold block
        appended below the confirmation headline.

        Arm declaration (all optional; the CONTENT is the arm consumer's --
        Skull -- the bridge only renders):
            confirm_fold: {"summary": "...", "body": "..."}
        Both summary and body may carry positional {i} tokens (full capture
        list, DGN-966 rider; {1}/{2} byte-identical on 2-step arms). Returns
        the neutral `fold:: ... fold::end` typed block (transpiled to a
        Telegram <blockquote expandable> by markdown_to_telegram_html at send
        time), or "" when the arm declares no fold. Never raises.
        """
        decl = arm.get("confirm_fold")
        if not isinstance(decl, dict):
            return ""
        summary = decl.get("summary", "")
        body = decl.get("body", "")
        if not isinstance(summary, str) or not summary.strip():
            return ""
        if not isinstance(body, str):
            body = ""

        vals = TelegramBot._idrill_positionals(val1, val2)
        try:
            val1_num = int(vals[0])
        except (ValueError, TypeError, IndexError):
            val1_num = vals[0] if vals else "?"
        positionals = [val1_num] + vals[1:]

        def _fmt(s: str) -> str:
            try:
                return s.format("", *positionals)
            except (KeyError, IndexError, ValueError):
                return s

        return compose_fold_block(_fmt(summary), _fmt(body))

    # DGN-939: special step-token for the auto-injected Back button. It is a
    # bridge-level navigation action, never a domain value; the arm file never
    # declares it as a step_buttons value.
    _IDRILL_BACK_ACTION = "back"

    # --- DGN-939: N-step navigation helpers ---
    #
    # The arm file's step_buttons / step_text / step_validate are already
    # step-keyed dicts (contract v3), so an arm may declare arbitrary steps
    # "1".."N" -- not just the fixed pair "1"/"2". The bridge walks them as a
    # linear stack: each non-final tap pushes (step, value) onto arm["_nav"]
    # and advances to the next declared step; the final step fires the cmd.
    #
    # Which step is FINAL is arm-declared (step_final), defaulting to "2" for
    # backward-compat with the DGN-918/924 two-step contract. The bridge holds
    # no hardcoded step count.

    @staticmethod
    def _idrill_next_step(arm: dict, step: str) -> Optional[str]:
        """Return the step key that follows `step` in the arm's declared order.

        Steps are ordered by the numeric sort of the step_buttons keys the arm
        declares. Returns None when `step` is the last declared step.
        """
        decl = arm.get("step_buttons")
        if not isinstance(decl, dict):
            return None
        keys = [k for k in decl.keys() if isinstance(k, str) and k.isdigit()]
        keys.sort(key=int)
        try:
            idx = keys.index(step)
        except ValueError:
            return None
        return keys[idx + 1] if idx + 1 < len(keys) else None

    @classmethod
    def _idrill_final_step(cls, arm: dict) -> str:
        """The step at which the arm fires its declared cmd.

        Arm-declared via `step_final`; defaults to "2" (the DGN-918/924
        two-step contract) so pre-DGN-939 arm files keep firing at step 2.
        A declared step_final that is not a digit string is ignored (fallback).
        """
        declared = arm.get("step_final")
        if isinstance(declared, str) and declared.isdigit():
            return declared
        return "2"

    @classmethod
    def _idrill_nav_get(cls, arm: dict):
        """Return the navigation stack list [[step, value], ...], normalized.

        Malformed entries are dropped so a corrupted arm file cannot inject
        arbitrary structure. Absent/other -> empty list.
        """
        nav = arm.get("_nav")
        if not isinstance(nav, list):
            return []
        clean = []
        for entry in nav:
            if (isinstance(entry, (list, tuple)) and len(entry) == 2
                    and isinstance(entry[0], str) and isinstance(entry[1], str)):
                clean.append([entry[0], entry[1]])
        return clean

    @classmethod
    def _idrill_build_step_keyboard(cls, arm: dict, arm_id: str, step: str):
        """Build the InlineKeyboardMarkup for a step from the arm declaration.

        Returns (markup, text) or (None, None) if the step declares no buttons.
        A Back button is appended as its own row for every step after the
        first declared step (never on the first step -- nothing to go back to).
        The Back label is arm-overridable via step_back_label.
        """
        # DGN-966: the spec (rows, back row, 64-byte guard, step text) comes
        # from the SHARED builder; this wrapper only lifts it into telegram
        # objects. Back button stays OPT-IN (arm nav_back: true) -- DGN-918/924
        # two-step arms that do not declare nav_back keep the exact keyboard.
        spec_rows, text = artifacts.build_step_keyboard_spec(
            arm, arm_id, step, back_label=messages.IDRILL_BACK_LABEL
        )
        if not spec_rows:
            return None, None
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(lbl, callback_data=cb) for lbl, cb in row]
            for row in spec_rows
        ])
        return markup, text

    # Placeholder tokens a declared argv may carry (contract v2).  The
    # bridge substitutes ONLY these exact-match elements; every other argv
    # element is executed verbatim as declared by the arm file.  Tokens are
    # positional -- the bridge knows no domain meaning for either step.
    _IDRILL_TOKEN_1 = "{1}"
    _IDRILL_TOKEN_2 = "{2}"

    # DGN-939: positional token pattern {1}..{N}. The bridge substitutes any
    # exact-match {i} element with the i-th captured step value; every other
    # argv element passes through verbatim. i is 1-based over the ordered nav
    # captures.
    _IDRILL_TOKEN_RE = re.compile(r"^\{([1-9][0-9]*)\}$")

    async def _idrill_fire_cmd(self, arm: dict, val1, val2: str) -> bool:
        """Execute the arm-declared argv once (2-step compat wrapper).

        Preserved DGN-918/924 signature: (val1, val2). Builds the ordered
        capture list [val1, val2] and delegates to _idrill_fire_cmd_n, which
        also serves N-step arms. `val2 == skip` selects the cmd_skip2 argv.
        """
        skipped = val2 == self._IDRILL_SKIP_VALUE
        captures = [("1", str(val1))]
        if not skipped:
            captures.append(("2", val2))
        return await self._idrill_fire_cmd_n(arm, captures, skipped)

    async def _idrill_fire_cmd_n(
        self, arm: dict, captures, skipped: bool
    ) -> bool:
        """Execute the arm-declared argv once from an ordered capture list.

        The bridge is domain-verb agnostic: it never assembles a command.
        The arm file declares the full argv -- `cmd`, or `cmd_skip2` when the
        final step was skipped -- with argv[0] already an absolute path. The
        bridge substitutes only "{i}" placeholder elements with the i-th
        arm-declaration-whitelisted capture value and executes the list
        verbatim: list-form subprocess, no shell, no PATH lookup, no cwd
        change, no re-parsing of declared elements. Returns True on exit 0.

        `captures` is an ordered list of (step_key, value) for every captured
        step (final skip step omitted). Each value is gated against its step's
        own declaration (step_buttons / step_validate) before it can enter the
        argv -- an out-of-declaration value aborts the fire (no subprocess).
        """
        key = "cmd_skip2" if skipped else "cmd"
        declared = arm.get(key)
        if (not isinstance(declared, list) or not declared
                or not all(isinstance(el, str) for el in declared)):
            logger.warning(
                "idrill: arm file %s argv missing/malformed (session=%s)",
                key, arm.get("session_id", "?"),
            )
            return False

        sub_values = self._idrill_whitelisted_values(arm, captures)
        if sub_values is None:
            return False

        argv = self._idrill_substitute_tokens(declared, sub_values, key)
        if argv is None:
            return False

        proc = await self._idrill_exec_argv(argv, arm)
        if proc is None:
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

    def _idrill_whitelisted_values(self, arm: dict, captures):
        """Gate every capture against its step's own declaration.

        Whitelist source (contract v3): a value may enter an argv only if it
        belongs to the step's declared buttons or passes the step's declared
        validate regex. The bridge holds no hardcoded set -- the gate source
        is the arm file. Returns the ordered positional substitution values,
        or None when any capture is out-of-declaration (caller aborts, no
        subprocess).
        """
        sub_values = []  # positional {1}..{N} substitution values
        for step_key, value in captures:
            value_str = str(value)
            if not self._idrill_value_allowed(arm, step_key, value_str):
                logger.warning(
                    "idrill: step%s value rejected by whitelist: %r",
                    step_key, value_str,
                )
                return None
            sub_values.append(value_str)
        return sub_values

    def _idrill_substitute_tokens(self, declared, sub_values, key: str):
        """Substitute exact-match "{i}" argv elements from sub_values.

        Every other element passes through verbatim (substrings like "x{1}y"
        are NOT substituted). Returns the final argv, or None on a malformed
        declaration (token references a capture that does not exist).
        """
        argv = []
        for el in declared:
            m = self._IDRILL_TOKEN_RE.match(el)
            if m is not None:
                idx = int(m.group(1))  # 1-based
                if idx < 1 or idx > len(sub_values):
                    # Token references a capture that does not exist (e.g. a
                    # {2} in the skip variant, or {3} on a 2-step arm): a
                    # malformed declaration -- abort rather than guess.
                    logger.warning(
                        "idrill: %s declares token {%d} but only %d value(s) "
                        "captured", key, idx, len(sub_values),
                    )
                    return None
                argv.append(sub_values[idx - 1])
            else:
                argv.append(el)
        return argv

    @staticmethod
    async def _idrill_exec_argv(argv, arm: dict):
        """Execute one declared argv under the idrill security invariants.

        List-form subprocess, NEVER a shell, no PATH lookup, no cwd change, no
        env injection, 15s timeout. Returns the CompletedProcess, or None on a
        rejected argv / timeout / OS error (fail-soft, logged).

        argv[0] MUST be an absolute path (contract section 4): a relative
        argv[0] would let subprocess.run fall back to a PATH search, defeating
        the "no PATH lookup" invariant. Enforced HERE so cmd and followup_cmd
        share the gate -- an absent or relative argv[0] aborts the exec with no
        subprocess. The check runs on the POST-substitution argv, so a token
        that resolves to a relative path is caught too.
        """
        if not argv or not os.path.isabs(argv[0]):
            logger.warning(
                "idrill: argv[0] not an absolute path, refusing exec "
                "(session=%s): %r",
                arm.get("session_id", "?"),
                (argv[0] if argv else None),
            )
            return None

        def _run() -> subprocess.CompletedProcess:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=15,
            )

        try:
            return await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            logger.warning(
                "idrill: declared cmd timed out (session=%s)",
                arm.get("session_id"),
            )
            return None
        except Exception as e:
            logger.warning("idrill: declared cmd subprocess error: %s", e)
            return None

    async def _idrill_run_followup(self, arm: dict, captures, skipped: bool):
        """Execute the OPTIONAL arm-declared `followup_cmd` argv (DGN-939
        item2, Skull handoff 6EXRS2Z1) and return its STDOUT for posting.

        Runs ONLY after the terminal cmd fired with exit 0 (caller enforces).
        Same security invariants as `cmd` (list-form, no shell, no PATH, 15s
        timeout) and the same exact-match {i} substitution from the same
        capture list. Absent or empty declaration -> None with zero side
        effects (today's behavior, byte-identical).

        Every failure here is fail-soft (logged, None returned): the terminal
        cmd already fired and the arm is consumed, so a followup problem must
        never turn a succeeded fire into a user-facing error.
        """
        declared = arm.get("followup_cmd")
        if not declared:
            return None
        if (not isinstance(declared, list)
                or not all(isinstance(el, str) for el in declared)):
            logger.warning(
                "idrill: followup_cmd malformed (session=%s)",
                arm.get("session_id", "?"),
            )
            return None

        sub_values = self._idrill_whitelisted_values(arm, captures)
        if sub_values is None:
            return None

        argv = self._idrill_substitute_tokens(
            declared, sub_values, "followup_cmd"
        )
        if argv is None:
            return None

        proc = await self._idrill_exec_argv(argv, arm)
        if proc is None:
            return None
        if proc.returncode != 0:
            logger.warning(
                "idrill: followup_cmd exited %d for session=%s: %s",
                proc.returncode,
                arm.get("session_id"),
                (proc.stderr or "").strip()[:200],
            )
            return None
        out = (proc.stdout or "").strip()
        return out or None

    @staticmethod
    async def _idrill_post_followup(query, text: str) -> None:
        """Post the followup STDOUT to the owner chat as a NEW message.

        Never an edit -- the tapped message already carries the confirmation.
        The bridge posts the consumer-authored STDOUT through the SAME
        segment-aware render pipeline as the model-turn send path
        (_render_prose_html_segments): fenced code renders via
        code_segment_html, and only the prose OUTSIDE fences reaches
        markdown_to_telegram_html. That function's own contract
        (formatting.py:950-954) forbids feeding it a raw code fence --
        DGN-1076 found this path doing exactly that, which left pasted ```
        tables showing as literal ``` text with no <pre>. On a render/post
        failure fall back to a plain-text post. Never raises (fail-soft --
        the fire already succeeded).
        """
        try:
            # DGN-1209 R1: followup_cmd stdout is consumer text (no model turn).
            rendered_parts = TelegramBot._render_prose_html_segments(
                text, rail=RAIL_CONSUMER
            )
        except Exception as e:
            # Pure-function render failure: nothing has been posted yet, so
            # fall straight back to a single plain-text post of the source.
            # DGN-1209: the plain fallback must not bypass the gate -- the raw
            # source is exactly where the machine lines live. Gate it again
            # here; if the GATE itself is what raised, post nothing (a lost
            # table refresh is the benign failure; the owner reading machine
            # text is the defect this gate exists for).
            logger.warning("idrill: followup render failed, retry plain: %s", e)
            try:
                plain = apply_machine_line_gate(text, RAIL_CONSUMER)
            except Exception as e_gate:  # noqa: BLE001
                logger.error(
                    "idrill: followup gate failed; plain fallback SKIPPED "
                    "(never post ungated consumer stdout): %s", e_gate,
                )
                return
            if not plain.strip():
                return
            try:
                await query.message.reply_text(plain)
            except Exception as e2:
                logger.warning("idrill: followup post failed (dropped): %s", e2)
            return

        for rendered in rendered_parts:
            try:
                await query.message.reply_text(rendered, parse_mode="HTML")
            except Exception as e:
                # HTML post rejection for THIS part only: fall back to a
                # plain-text post of the same part (never re-post earlier
                # parts that already sent -- that would duplicate them).
                # The fire already succeeded, so this may never escape into
                # _idrill_fire_final and turn a good fire into a callback
                # error.
                logger.warning(
                    "idrill: followup HTML post failed, retry plain: %s", e
                )
                try:
                    await query.message.reply_text(html_to_plain_text(rendered))
                except Exception as e2:
                    logger.warning(
                        "idrill: followup post failed (dropped): %s", e2
                    )

    async def _handle_idrill_callback(self, query, data: str) -> None:
        """Dispatch idrill callbacks (DGN-918 seed, DGN-939 N-step + Back).

        Callback grammar:
          idrill:<arm_id>:<step>:<value>   -- a step tap (step = "1".."N")
          idrill:<arm_id>:back             -- pop one nav level (« Back)

        Behavior model turn 0, outbound 0 -- every path is an in-place edit:
          non-final step tap -> capture value, swap to the NEXT declared
              step keyboard (edit_message_text/reply_markup, no new message).
          final step tap      -> read arm, fire declared cmd, confirm, consume.
          :<step>:other       -> free-text prompt (no fire, arm kept for TTL).
          :back               -> pop the last capture, re-render the prior step.

        Which step is FINAL is arm-declared (step_final, default "2"): a
        two-step arm (no step_final) fires at step 2 exactly as DGN-918/924.
        All paths are fail-soft (mirror D1/D2 patterns in _handle_callback).
        """
        parts = data.split(":")
        # idrill : arm_id : (back | step : value)  -- min 3 parts (back form).
        if len(parts) < 3:
            logger.warning("idrill: malformed callback_data %r", data)
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return

        arm_id = parts[1]
        action = parts[2]
        # value may contain ':' for future-proofing (rejoin from part 3 on)
        value = ":".join(parts[3:])

        # Security: arm_id is used as a filename; validate strictly.
        if not self._IDRILL_ARM_ID_RE.match(arm_id):
            logger.warning("idrill: arm_id failed validation: %r", arm_id)
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return

        # --- « Back navigation: idrill:<arm_id>:back ---
        if action == self._IDRILL_BACK_ACTION:
            await self._idrill_nav_back(query, arm_id)
            return

        # A step tap needs a value part.
        if len(parts) < 4:
            logger.warning("idrill: step tap missing value %r", data)
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return

        step = action

        # "other" special value: free-text fallback, arm untouched (TTL cleans).
        if value == self._IDRILL_OTHER_VALUE:
            await self._idrill_edit_soft(query, messages.IDRILL_OTHER_PROMPT)
            return

        arm = self._idrill_read_arm(arm_id)
        if arm is None:
            await self._idrill_edit_soft(query, messages.IDRILL_ARM_EXPIRED)
            return

        # Gate the tapped value against this step's own declaration (contract
        # v3): only a declared/validated value may enter the flow.
        if not self._idrill_value_allowed(arm, step, value):
            logger.warning(
                "idrill: step%s tap value %r not allowed by arm %s",
                step, value, arm_id,
            )
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return

        final_step = self._idrill_final_step(arm)

        # --- FINAL step tap: fire the declared cmd, confirm, consume ---
        if step == final_step:
            await self._idrill_fire_final(query, arm_id, arm, step, value)
            return

        # --- non-final step tap: capture + advance to the next keyboard ---
        await self._idrill_advance(query, arm_id, arm, step, value)

    @staticmethod
    async def _idrill_edit_soft(query, text: str) -> None:
        """edit_message_text that never raises (stale/gone message tolerated)."""
        try:
            await query.edit_message_text(text)
        except Exception as e:
            logger.warning("idrill: edit_message_text failed (ignored): %s", e)

    def _idrill_write_arm(self, arm_id: str, arm: dict) -> bool:
        """Persist the arm file; return False (fail-soft) on write error.

        Path + containment via bridge.artifacts (single authority, DGN-1123).
        """
        arm_path = artifacts.arm_path_for(PROJECT_ROOT, arm_id)
        if arm_path is None:
            return False
        try:
            arm_path.write_text(json.dumps(arm), encoding="utf-8")
            return True
        except OSError as e:
            logger.warning("idrill: arm file update failed: %s", e)
            return False

    async def _idrill_advance(
        self, query, arm_id: str, arm: dict, step: str, value: str
    ) -> None:
        """Capture `value` for `step`, push it on the nav stack, and swap the
        message to the next declared step's keyboard (in-place edit)."""
        next_step = self._idrill_next_step(arm, step)
        if next_step is None:
            # Non-final step with no successor -> arm declaration inconsistency.
            logger.warning(
                "idrill: step%s is non-final but has no next step (arm %s)",
                step, arm_id,
            )
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return

        markup, text = self._idrill_build_step_keyboard(arm, arm_id, next_step)
        if markup is None:
            logger.warning(
                "idrill: arm %s declares no step_buttons[%s]", arm_id, next_step
            )
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return

        # Push (step, value) onto the nav stack. _pending_step1 is kept in sync
        # for the DGN-918/924 two-step confirmation render (backward-compat).
        nav = self._idrill_nav_get(arm)
        nav.append([step, value])
        arm["_nav"] = nav
        if step == "1":
            arm["_pending_step1"] = value
        if not self._idrill_write_arm(arm_id, arm):
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return

        # Prefer text+markup swap when the next step declares copy; otherwise a
        # markup-only swap (backward-compat with the DGN-918 markup-only path).
        if text is not None:
            try:
                await query.edit_message_text(text, reply_markup=markup)
            except Exception as e:
                logger.warning(
                    "idrill: step%s text swap failed for arm_id=%s: %s",
                    next_step, arm_id, e,
                )
            return
        try:
            await query.edit_message_reply_markup(reply_markup=markup)
        except Exception as e:
            logger.warning(
                "idrill: step%s markup swap failed for arm_id=%s: %s",
                next_step, arm_id, e,
            )

    async def _idrill_nav_back(self, query, arm_id: str) -> None:
        """« Back: pop the last capture and re-render the step it came from."""
        arm = self._idrill_read_arm(arm_id)
        if arm is None:
            await self._idrill_edit_soft(query, messages.IDRILL_ARM_EXPIRED)
            return
        nav = self._idrill_nav_get(arm)
        if not nav:
            # Underflow: already at the first step -- inform, no crash.
            await self._idrill_edit_soft(query, messages.IDRILL_NAV_ROOT)
            return
        prev_step, _prev_value = nav.pop()
        arm["_nav"] = nav
        # Keep _pending_step1 consistent after popping back to / past step 1.
        if prev_step == "1":
            arm.pop("_pending_step1", None)
        elif nav:
            # if a step-1 capture still remains, restore it
            for s, v in nav:
                if s == "1":
                    arm["_pending_step1"] = v
                    break
        if not self._idrill_write_arm(arm_id, arm):
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return

        markup, text = self._idrill_build_step_keyboard(arm, arm_id, prev_step)
        if markup is None:
            logger.warning(
                "idrill: back target step%s has no buttons (arm %s)",
                prev_step, arm_id,
            )
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return
        if text is not None:
            try:
                await query.edit_message_text(text, reply_markup=markup)
            except Exception as e:
                logger.warning("idrill: back text swap failed: %s", e)
            return
        try:
            await query.edit_message_reply_markup(reply_markup=markup)
        except Exception as e:
            logger.warning("idrill: back markup swap failed: %s", e)

    async def _idrill_fire_final(
        self, query, arm_id: str, arm: dict, step: str, value: str
    ) -> None:
        """Final step tap: fire the declared cmd from the full nav stack, then
        consume the arm file (once). Concurrent double-tap: the second tap
        re-reads and finds the file gone -> EXPIRED (handled by re-read)."""
        # Re-read + consume to close the double-tap race: read the freshest arm
        # (the caller's copy may be pre-advance), then delete before firing so a
        # concurrent tap cannot double-fire.
        fresh = self._idrill_read_arm(arm_id)
        if fresh is None:
            await self._idrill_edit_soft(query, messages.IDRILL_ARM_EXPIRED)
            return
        arm = fresh

        # Build the ordered capture list from the nav stack + this final tap.
        # Backward-compat: a two-step arm reaching here with an empty nav but a
        # _pending_step1 rebuilds [("1", pending)] so DGN-918/924 arms fire.
        nav = self._idrill_nav_get(arm)
        captures = list(nav)
        if not captures:
            pending = arm.get("_pending_step1")
            if isinstance(pending, str):
                captures = [["1", pending]]
        skipped = value == self._IDRILL_SKIP_VALUE
        if not skipped:
            captures.append([step, value])

        # Consume the arm file before firing (prevents double-submission).
        # Three outcomes (DGN-1123):
        #   removed now      -> proceed to fire (the normal path);
        #   already absent   -> a concurrent tap consumed it after our
        #                       re-read; that tap owns the fire -> EXPIRED,
        #                       no fire (completes the double-tap guard the
        #                       old blanket `except OSError: pass` left
        #                       open);
        #   unlink FAILED    -> LOUD abort. Firing with the arm still on
        #                       disk would break the no-double-submission
        #                       invariant, and a silent no-op here is the
        #                       invisible failure direction (button works,
        #                       state never clears).
        try:
            consumed = self._idrill_consume_arm(arm_id)
        except OSError:
            logger.exception(
                "idrill: arm consume FAILED for %s -- fire ABORTED "
                "(arm file still on disk; see IDRILL-ARM-CONTRACT.md)",
                arm_id,
            )
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return
        if not consumed:
            await self._idrill_edit_soft(query, messages.IDRILL_ARM_EXPIRED)
            return

        capture_pairs = [(s, v) for s, v in captures]
        ok = await self._idrill_fire_cmd_n(arm, capture_pairs, skipped)
        if not ok:
            await self._idrill_edit_soft(query, messages.IDRILL_ERROR)
            return

        # Confirmation render (DGN-966 rider): pass the FULL ordered capture
        # value list so confirm_fmt {i} tokens render the real i-th step value
        # on N-step arms (the old fixed captures[0][1] made {1} render a gate
        # tap instead of the recorded value). 2-step arms are byte-identical.
        capture_values = [v for _s, v in captures]
        confirmation = self._idrill_render_confirmation(
            arm, capture_values if capture_values else "?", value
        )
        await self._idrill_edit_confirmation(query, confirmation)

        # GUARD (DGN-939 item2, mutual exclusion): the followup post fires
        # ONLY here, on the zero-model-turn callback fire path, and only
        # after the terminal cmd exited 0 (the `ok` gate above). The
        # model-turn record path keeps its existing PostToolUse hook as the
        # sole refresh author for that path -- the two paths never overlap,
        # so a double-fire is structurally impossible.
        # _idrill_fire_cmd_n itself never posts and must stay post-free.
        followup_text = await self._idrill_run_followup(
            arm, capture_pairs, skipped
        )
        if followup_text:
            await self._idrill_post_followup(query, followup_text)

    @staticmethod
    async def _idrill_edit_confirmation(query, text: str) -> None:
        """Edit the final confirmation in place. When the text carries a fold::
        typed block (DGN-939 item6 expandable blockquote), render it to HTML and
        edit with parse_mode=HTML so the collapsible blockquote shows; otherwise
        a plain-text edit (DGN-918/924 path, byte-identical). Never raises.
        """
        if FOLD_OPEN_MARKER in text:
            rendered = balance_telegram_html(markdown_to_telegram_html(text))
            try:
                await query.edit_message_text(rendered, parse_mode="HTML")
                return
            except Exception as e:
                logger.warning(
                    "idrill: HTML confirmation edit failed, retry plain: %s", e
                )
                # Fall through to a plain-text edit so the user still sees the
                # headline even if the fold render is rejected.
        try:
            await query.edit_message_text(text)
        except Exception as e:
            logger.warning("idrill: confirmation edit failed (ignored): %s", e)
    # ESTATE:END

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

                response = await self._finish_turn_with_auto_resume(
                    user_id=user_id, chat_id=chat_id, response=response, resume_caller=resume_caller
                )
                if response is None:
                    return
                await self._save_session_id(user_id, response)
                await self._send_smart(
                    chat_id,
                    response.content,
                    force_options=response.has_options,
                    streamed=response.streamed,
                    draft_message_ids=response.draft_message_ids,
                    classifier_injected=getattr(response, "options_classifier_injected", False),
                    rail=RAIL_MODEL,
                    assembled=getattr(response, "turn_assembled", False),
                    notice_meta=self._notice_meta(response),
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

                response = await self._finish_turn_with_auto_resume(
                    user_id=user_id, chat_id=chat_id, response=response, resume_caller=resume_caller
                )
                if response is None:
                    return
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
                    rail=RAIL_MODEL,
                    assembled=getattr(response, "turn_assembled", False),
                    notice_meta=self._notice_meta(response),
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

    # ESTATE:BEGIN usage-defer manual retry (DGN-835/841) -- estate routine, its callback and the slash fallback
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

        Returns (reply_text, retriable, committed): retriable True = a later
        attempt may succeed (callers keep the button / the user may
        re-issue). committed True = the replay actually launched (DGN-1557:
        the ONLY branch with a real side effect, so it's the only one whose
        confirmation needs a guaranteed-delivery ack -- the other branches
        are read-only lookups/rejections, safe to lose and re-ask).
        """
        if not self._USAGE_RETRY_LABEL_RE.match(label):
            return messages.USAGE_RETRY_BAD_LABEL, False, False
        replay = Path.home() / ".dogany" / "usage-defer" / f"{label}.replay"
        if not replay.is_file():
            return messages.USAGE_RETRY_NO_REPLAY.format(label=label), False, False

        usage = await asyncio.to_thread(self._fetch_usage_json)
        if not isinstance(usage, dict):
            return messages.USAGE_RETRY_LOOKUP_FAILED, True, False
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
                False,
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
            return messages.USAGE_RETRY_LOOKUP_FAILED, True, False
        return messages.USAGE_RETRY_STARTED.format(label=label), False, True

    async def _handle_usage_retry_callback(self, query, data: str) -> None:
        """DGN-835: 'usageretry:<label>' button from a seven_day usage-defer
        notification (cron-guard). NOTE the 20-min STALE gate (_check_access)
        kills taps on aged messages, so this path realistically serves only
        the fresh notification and the reset-time re-notify (DGN-841 B);
        /usageretry (DGN-841 A) covers everything else."""
        label = data.split(":", 1)[1]
        text, retriable, _committed = await self._usage_retry_run(label)
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
        text, _retriable, committed = await self._usage_retry_run(args[0])
        if committed:
            # DGN-1557: the replay actually launched -- this confirmation
            # must survive a transient send failure, unlike the read-only
            # rejection/lookup branches above.
            await self._reply_guaranteed(update, text)
        else:
            await update.message.reply_text(text)
    # ESTATE:END


bot = TelegramBot()
