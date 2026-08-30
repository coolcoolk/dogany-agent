"""Configuration and logging setup.

Two-layer .env loading: PROJECT_ROOT/.telegram_bot/.env wins; if its
TELEGRAM_BOT_TOKEN is a placeholder/empty, fall back to the package .env (which
typically only carries shared bits like CLAUDE_CLI_PATH). PROJECT_ROOT must be
set in the environment before importing this module (done by __main__).
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Dict, List, Optional

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent

PROJECT_ROOT = Path(os.environ["PROJECT_ROOT"]).resolve()
BOT_DATA_DIR = PROJECT_ROOT / ".telegram_bot"
PROJECT_ENV_PATH = BOT_DATA_DIR / ".env"  # project config (priority)
PACKAGE_ENV_PATH = PACKAGE_DIR / ".env"  # package default (lowest priority)


def _find_dogany_env(start: Path) -> Optional[Path]:
    """Nearest ancestor .rules/.env -- shared cross-agent global config.

    Agents may nest at different depths, so resolve by walking up from
    PROJECT_ROOT rather than using a fixed relative offset. This shared file is
    OPTIONAL: if absent (standalone repo), the agent's own .telegram_bot/.env is
    sufficient -- the only key it typically carries is CLAUDE_CLI_PATH, which
    otherwise defaults to a PATH lookup.
    """
    for d in (start, *start.parents):
        cand = d / ".rules" / ".env"
        if cand.exists():
            return cand
    return None


DOGANY_ENV_PATH = _find_dogany_env(PROJECT_ROOT)  # shared global fallback (may be None)

LOGS_DIR = BOT_DATA_DIR / "logs"
SESSION_STORE_PATH = BOT_DATA_DIR / "sessions.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Born-locked ownership state files (see bridge/ownership.py). Plain text files
# under BOT_DATA_DIR: a fresh bot with an empty allowed_user_ids is NOT open to
# all -- it stays in claim mode until the first user claims it with a code.
OWNER_LOCK_PATH = BOT_DATA_DIR / "owner.lock"
CLAIM_CODE_PATH = BOT_DATA_DIR / "claim_code"
CLAIMED_FLAG_PATH = BOT_DATA_DIR / ".claimed"

_PLACEHOLDER_TOKENS = {"your_bot_token_here", ""}

# Project .env first (higher priority). override=True so the project file wins
# over inherited environment variables -- otherwise a TELEGRAM_BOT_TOKEN already
# exported in the launching shell (e.g. an agent's own live token) silently
# shadows the project .env and the bridge polls the WRONG bot.
load_dotenv(dotenv_path=PROJECT_ENV_PATH, override=True)
if os.environ.get("TELEGRAM_BOT_TOKEN", "") in _PLACEHOLDER_TOKENS:
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
# Priority: project .env > shared .rules/.env > package .env. load_dotenv with
# override=False fills only keys not already set, so earlier load == higher priority.
if DOGANY_ENV_PATH is not None:
    load_dotenv(dotenv_path=DOGANY_ENV_PATH)  # shared global fallback
load_dotenv(dotenv_path=PACKAGE_ENV_PATH)  # package default; does not override


class Config(BaseSettings):
    """Runtime configuration sourced from env + .env files."""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=[str(PROJECT_ENV_PATH), str(PACKAGE_ENV_PATH)],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = Field(..., description="Telegram Bot API token")
    allowed_user_ids: List[int] = Field(
        default_factory=list,
        description=(
            "Allowed Telegram user IDs. When set, this list is AUTHORITATIVE and "
            "claim mode is off. When empty, the bot is born-locked: it does NOT "
            "allow all -- it stays in claim mode until claimed (see ownership.py)."
        ),
    )
    extra_allowed_roots: Annotated[List[Path], NoDecode] = Field(
        default_factory=list,
        description=(
            "Extra absolute roots the path guard treats as inside PROJECT_ROOT "
            "(os.pathsep-separated absolute paths; empty = strict default)"
        ),
    )

    # Claude CLI / settings
    claude_cli_path: Optional[Path] = Field(
        default=None, description="Optional absolute path to the Claude CLI binary"
    )
    claude_settings_path: Path = Field(default=CLAUDE_SETTINGS_PATH)

    # Runtime data
    bot_data_dir: Path = Field(default=BOT_DATA_DIR)
    logs_dir: Path = Field(default=LOGS_DIR)
    session_store_path: Path = Field(default=SESSION_STORE_PATH)

    auto_new_session_after_hours: Optional[float] = Field(
        default=24.0,
        description="Start a new session when the gap since the last user message "
        "exceeds this many hours. 0/false/off disables.",
    )

    # Localization: user-facing string locale (env LOCALE). Only ko/en are
    # recognized; anything else falls back to en. Read by bridge/i18n.
    locale: str = Field(default="en")

    # DGN-780: countdown progress-bar glyph set (env COUNTDOWN_GLYPH_SET).
    # VALIDATED ALLOWLIST -- "dot" (default) | "block-line" | "square"; the
    # actual glyph pairs live in bridge/countdown.py GLYPH_SETS. Any other
    # value falls back to dot (here and again defensively at render time).
    countdown_glyph_set: str = Field(default="dot")

    # DGN-829: section header glyphs for update-notify labels
    # (env DOGANY_SECTION_GLYPHS). Space-separated 1-N glyphs (emoji or
    # unicode symbols). Positional mapping: 1st=summary, 2nd=detail, 3rd=try.
    # Fewer than 3 -> missing positions fall back to bracket label form.
    # Empty / unset / all-whitespace -> default palette ["✅", "📌", "📋"].
    # Stored as raw env string; _parse_section_glyphs validator splits it.
    section_glyphs: List[str] = Field(default_factory=lambda: ["✅", "📌", "📋"])

    # DGN-932: class-wise notification policy (env NOTIFY_POLICY).
    # Space- or comma-separated "class=silent" / "class=loud" entries, e.g.
    #   NOTIFY_POLICY="fold=loud countdown=silent"
    # Classes wired in this bridge (mechanism A = edited-surface first send,
    # mechanism B = one-shot appendix):
    #   draft          - final-answer streaming draft, first send   (A)
    #   fold           - interim-narration fold bubble, first send  (A)
    #   countdown      - countdown screen-tick bubble, first send   (A)
    #   dashboard      - pinned dashboard recreate send + pin       (A)
    #   options_prompt - [[OPTIONS]] SELECT_PROMPT button message   (B)
    # Unset / empty -> the locked defaults in NOTIFY_CLASS_DEFAULT_SILENT
    # below apply. Unknown class names are kept (forward-compatible, inert);
    # entries with values other than silent/loud are skipped.
    notify_policy: Annotated[Dict[str, bool], NoDecode] = Field(
        default_factory=dict,
        description="Class->silent overrides for bridge in-process sends",
    )

    # Streaming
    draft_update_min_chars: int = Field(default=30)
    draft_update_interval: float = Field(default=1.0)
    # C-strict interim-narration suppression (DGN-426).
    # When False (default), only the terminal AssistantMessage (stop_reason=end_turn)
    # is live-streamed to the user; interim between-tool narration is suppressed and
    # the typing indicator remains the only feedback during those phases.
    # DGN-682: DEPRECATED alias -- superseded by INTERIM_MODE below. When
    # INTERIM_MODE is unset, STREAM_INTERIM=true maps to interim mode "inline"
    # (the pre-DGN-426 behavior: all AssistantMessage TextBlocks display live).
    # Planned for removal one version after DGN-682 ships. User-facing agents
    # should leave this at the default (False).
    stream_interim: bool = Field(default=False)
    # DGN-682: interim narration mode -- "suppress" | "inline" | "fold".
    #   suppress: interim narration dropped; typing indicator only.
    #   inline: every interim TextBlock streams live (pre-DGN-426 behavior).
    #   fold (default): interim narration is captured during the turn and
    #     prepended to the final answer as ONE collapsed expandable blockquote
    #     (rendered via the DGN-619 `>!` fold marker). v1.31.0 baseline lift:
    #     fold is now the unset default for all agents; suppress requires
    #     explicit INTERIM_MODE=suppress.
    # Unset (None) -> resolution falls back to the STREAM_INTERIM alias (see
    # _resolve_interim_mode / INTERIM_MODE below).
    interim_mode: Optional[str] = Field(default=None)
    # DGN-930: dev vs non-dev interim CLASS knob (env INTERIM_AGENT_CLASS).
    # Only used when INTERIM_MODE is unset and STREAM_INTERIM is falsy -- it
    # picks the unset DEFAULT mode by agent kind:
    #   nondev (default): inline -- every interim block streams live, no fold.
    #     Non-dev / domain agents (coaching, life-assistant) want the live
    #     progress visible, never collapsed into a "진행 기록" quote.
    #   dev: fold -- live-then-fold. Interim streams live as a growing progress
    #     bubble during the turn, then collapses to a caption + expandable quote
    #     at turn end while the final answer arrives as a separate message.
    #     Dev agents (Metal/Skull/까리/담션) keep the detailed progress record.
    # An explicit INTERIM_MODE always overrides this class default. Any value
    # other than dev/nondev (or empty/unset) falls back to nondev.
    interim_agent_class: str = Field(default="nondev")

    # Voice (local faster-whisper only)
    transcription_provider: str = Field(default="local")
    local_whisper_model: str = Field(default="small")
    whisper_language: Optional[str] = Field(default="ko")
    voice_reply_enabled: bool = Field(default=False)
    # Dashboard sync (pinned live-dashboard message, activated by file presence)
    dashboard_enabled: bool = Field(default=True)
    max_voice_duration: int = Field(default=300)
    ffmpeg_path: Optional[str] = Field(default=None)

    # Logging
    log_level: str = Field(default="INFO")
    log_format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def _validate_token(cls, v):
        if not v or str(v).strip() in _PLACEHOLDER_TOKENS:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is not configured. Set it in the project .env "
                "or the package .env file."
            )
        return str(v).strip()

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _parse_allowed_ids(cls, v):
        if isinstance(v, str):
            if not v.strip():
                return []
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("extra_allowed_roots", mode="before")
    @classmethod
    def _parse_extra_roots(cls, v):
        if isinstance(v, str):
            if not v.strip():
                return []
            return [
                Path(entry.strip()).expanduser().resolve()
                for entry in v.split(os.pathsep)
                if entry.strip()
            ]
        return v

    @field_validator("auto_new_session_after_hours", mode="before")
    @classmethod
    def _parse_auto_new(cls, v):
        if v is None:
            return 24.0
        if isinstance(v, bool):
            return None if not v else 24.0
        if isinstance(v, str):
            value = v.strip().lower()
            if not value:
                return 24.0
            if value in {"0", "false", "off", "no", "disable", "disabled"}:
                return None
            parsed = float(value)
        else:
            parsed = float(v)
        if parsed <= 0:
            return None
        return parsed

    @field_validator("interim_mode", mode="before")
    @classmethod
    def _normalize_interim_mode(cls, v):
        # DGN-682: only suppress/inline/fold are recognized; anything else
        # (or empty) behaves as unset so the STREAM_INTERIM alias resolution
        # in _resolve_interim_mode applies.
        if v is None:
            return None
        value = str(v).strip().lower()
        return value if value in {"suppress", "inline", "fold"} else None

    @field_validator("interim_agent_class", mode="before")
    @classmethod
    def _normalize_interim_agent_class(cls, v):
        # DGN-930: only dev/nondev are recognized; anything else (or empty)
        # falls back to nondev so a typo keeps the safe live-inline default.
        value = str(v or "").strip().lower()
        return value if value in {"dev", "nondev"} else "nondev"

    @field_validator("locale", mode="before")
    @classmethod
    def _normalize_locale(cls, v):
        # Only ko/en are supported; any other value (or empty) falls back to en.
        value = str(v or "").strip().lower()
        return value if value in {"ko", "en"} else "en"

    @field_validator("countdown_glyph_set", mode="before")
    @classmethod
    def _normalize_countdown_glyph_set(cls, v):
        # DGN-780: allowlist gate; unknown/empty values fall back to the
        # owner-locked default set (dot). Keys mirror countdown.GLYPH_SETS.
        value = str(v or "").strip().lower()
        return value if value in {"dot", "block-line", "square"} else "dot"

    @field_validator("section_glyphs", mode="before")
    @classmethod
    def _parse_section_glyphs(cls, v):
        # DGN-829: env value is a raw space-separated string (or already a
        # list when the default factory fires). String -> whitespace-split;
        # empty / all-whitespace / parse-fail -> default palette.
        _default = ["✅", "📌", "📋"]
        if isinstance(v, list):
            tokens = [str(t).strip() for t in v if str(t).strip()]
            return tokens if tokens else _default
        raw = str(v or "").strip()
        if not raw:
            return _default
        tokens = [t for t in raw.split() if t]
        return tokens if tokens else _default

    @field_validator("notify_policy", mode="before")
    @classmethod
    def _parse_notify_policy(cls, v):
        # DGN-932: env value is a raw "class=silent" / "class=loud" list
        # (space- or comma-separated). silent -> True, loud -> False.
        # Malformed entries (no "=", unknown value) are skipped so a typo
        # falls back to the locked defaults instead of crashing the boot.
        if isinstance(v, dict):
            return {str(k).strip().lower(): bool(val) for k, val in v.items()}
        raw = str(v or "").strip()
        if not raw:
            return {}
        policy: Dict[str, bool] = {}
        for entry in raw.replace(",", " ").split():
            key, sep, value = entry.partition("=")
            key = key.strip().lower()
            value = value.strip().lower()
            if not sep or not key:
                continue
            if value == "silent":
                policy[key] = True
            elif value == "loud":
                policy[key] = False
        return policy

    @field_validator("whisper_language", mode="before")
    @classmethod
    def _normalize_language(cls, v):
        if v is None:
            return "ko"
        value = str(v).strip().lower()
        if not value or value == "auto":
            return None
        return value


config = Config()  # type: ignore[call-arg]

# DGN-932: locked no-config defaults for the class-wise notification policy
# (owner lock 2026-08-19, two mechanisms):
#   Mechanism A -- edited-surface bubbles: notification is decided ONCE at the
#   first send; every later in-place edit is notification-free at the Telegram
#   level, so only the first send carries a class tag.
#     draft     = loud   (final answer arriving is the turn's signal)
#     fold      = silent (interim progress narration is noise)
#     countdown = loud   (first tick = set-start signal; later ticks are edits)
#     dashboard = silent (routine board recreation would be an alert storm)
#   Mechanism B -- one-shot messages: Telegram-default LOUD; a send site opts
#   into silence by tagging a class here (system/error/timeout/file/command
#   replies stay untagged = loud).
#     options_prompt = silent (button appendix must not bury the loud body)
# True = silent (disable_notification), False = loud.
NOTIFY_CLASS_DEFAULT_SILENT: Dict[str, bool] = {
    "draft": False,
    "fold": True,
    "countdown": False,
    "dashboard": True,
    "options_prompt": True,
}


def notify_silent(notify_class: str) -> bool:
    """DGN-932: resolve a send-class to its disable_notification bool.

    Config override (NOTIFY_POLICY) wins; otherwise the locked default for
    the class applies; an unknown class is LOUD (False) -- one-shot sends
    keep the Telegram default unless deliberately tagged silent.
    """
    override = config.notify_policy.get(notify_class)
    if override is not None:
        return override
    return NOTIFY_CLASS_DEFAULT_SILENT.get(notify_class, False)

# Raw env reads for timeout / resume knobs (not pydantic fields by spec).
PROCESS_TIMEOUT = int(os.getenv("CLAUDE_PROCESS_TIMEOUT", "600") or "600")
AUTO_RESUME = os.getenv("CLAUDE_AUTO_RESUME", "0").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
    "",
)
AUTO_RESUME_MAX = max(0, int(os.getenv("CLAUDE_AUTO_RESUME_MAX", "2") or "2"))
# DGN-460: SDK subprocess-transport JSON read buffer. The claude-agent-sdk
# defaults to 1MB (_DEFAULT_MAX_BUFFER_SIZE); a single CLI->SDK message over
# that (e.g. a base64 image in a tool result) crashes the reader loop with
# "JSON message exceeded maximum buffer size". Raise the ceiling; override via
# env if a deployment needs a different bound.
CLAUDE_MAX_BUFFER_SIZE = int(
    os.getenv("CLAUDE_MAX_BUFFER_SIZE", str(16 * 1024 * 1024)) or str(16 * 1024 * 1024)
)
# DGN-285: scaffold-leak guard gate for outgoing user-facing text (see
# sdk_bridge._scaffold_guard). Default ON when unset; set
# BRIDGE_SCAFFOLD_GUARD=0 on channels that legitimately quote the guarded
# signature strings (e.g. a dev channel pasting incident reports).
# See also BRIDGE_REGISTER_GUARD below (DGN-376 T2).
BRIDGE_SCAFFOLD_GUARD = os.getenv("BRIDGE_SCAFFOLD_GUARD", "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
    "",
)
# DGN-376 T2 / DGN-686 v2: design-system register guard for outgoing
# user-facing text (see sdk_bridge._register_guard). v2 strength = DROP-ONLY:
# a pure-English block (zero Hangul, long prose) on a ko-locale instance is
# dropped whole with a WARNING. Default ON when unset. This flag is an
# EMERGENCY BYPASS (default on): setting BRIDGE_REGISTER_GUARD=0 passes all
# outgoing text through unchanged -- use only to unblock a false-positive
# incident, not as a per-instance dev exemption (the guard leaves dev/debug
# stderr logs untouched either way).
BRIDGE_REGISTER_GUARD = os.getenv("BRIDGE_REGISTER_GUARD", "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
    "",
)
# DGN-429: final-output language guard for user-facing agents (hybrid).
# Gates BOTH legs of the language axis: (1) the output-language rule appended
# to the model system prompt (sdk_bridge._compose_system_prompt) and (2) the
# locale-register charset detector inside sdk_bridge._register_findings.
# v1 detector strength = log-warn only (rides the register guard; never blocks
# or rewrites). Default ON when unset; set OUTPUT_LANG_GUARD=0 on channels
# where mixed-language output is legitimate (e.g. a dev agent working in the
# English technical register). Note the detector additionally requires
# BRIDGE_REGISTER_GUARD on, since it is delivered through that guard's seats.
OUTPUT_LANG_GUARD = os.getenv("OUTPUT_LANG_GUARD", "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
    "",
)
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH") or (
    str(config.claude_cli_path) if config.claude_cli_path else None
)
# DGN-426: expose as module-level constant so sdk_bridge.py can import once
# rather than reaching into the Config object on every message.
# DGN-682: kept as a DERIVED back-compat symbol (existing tests patch it and
# it still acts as the deprecated "inline" alias); INTERIM_MODE below is the
# primary knob.
STREAM_INTERIM: bool = config.stream_interim


# DGN-682 D1 / DGN-930: INTERIM_MODE supersedes the boolean STREAM_INTERIM. An
# explicit INTERIM_MODE always wins; unset + STREAM_INTERIM=true maps to
# "inline" (deprecated alias, removal planned one version out); unset + unset ->
# the CLASS default (DGN-930): dev -> "fold" (live-then-fold), nondev (default)
# -> "inline" (live only, no fold). The v1.31.0 blanket "unset -> fold" default
# is superseded -- non-dev/domain agents no longer collapse live coaching
# narration into a fold; dev agents keep the fold via INTERIM_AGENT_CLASS=dev
# (or an explicit INTERIM_MODE=fold).
# (pydantic extra="ignore" keeps stray keys from crashing).
def _resolve_interim_mode(
    explicit: Optional[str],
    stream_interim: bool,
    agent_class: str = "nondev",
) -> str:
    """Resolve the effective interim mode from the explicit field + alias +
    dev/nondev class. Explicit wins; then the STREAM_INTERIM=true alias
    (inline); otherwise the class default -- dev=fold, nondev=inline."""
    if explicit in ("suppress", "inline", "fold"):
        return explicit
    if stream_interim:
        return "inline"
    return "fold" if agent_class == "dev" else "inline"


INTERIM_MODE: str = _resolve_interim_mode(
    config.interim_mode, config.stream_interim, config.interim_agent_class
)
# DGN-699 D3: growing-fold live-edit throttle -- a TIME-DOMINANT minimum
# interval between fold HTML edits. Reuses the DRAFT_UPDATE_INTERVAL knob but
# floors it at 2.0s (spec: 2~3s band): the fold is a background progress
# surface, so it must never approach the per-chat edit rate limit the way the
# 1.0s draft default may.
FOLD_UPDATE_INTERVAL: float = max(float(config.draft_update_interval), 2.0)
# DGN-555: selective reply-linking. The FINAL response of a turn is sent as a
# Telegram reply to its triggering user message ONLY when newer user messages
# interleaved before the send, or the response fires later than
# REPLY_LINK_LATENCY_S after the trigger arrived (policy in bot.py). Default on.
REPLY_LINK_ENABLED = os.getenv("REPLY_LINK_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
    "",
)
REPLY_LINK_LATENCY_S = int(os.getenv("REPLY_LINK_LATENCY_S", "300") or "300")
# DGN-801: deterministic fast-path interceptor -- per-instance flag, DEFAULT
# OFF. FASTPATH_HANDLER names a domain-owned executable (relative paths resolve
# against PROJECT_ROOT; absolute paths allowed). Unset / empty / missing file
# -> the interceptor is fully inert and every message takes the normal SDK
# path, so agents without a handler are untouched. The key lives in the
# instance's .telegram_bot/.env (the only config layer the bridge reads); it is
# NOT read from config/agent.conf. The bridge knows nothing about what the
# handler does -- contract is exit code + stdout only (see bridge/fastpath.py).
FASTPATH_HANDLER = os.getenv("FASTPATH_HANDLER", "").strip()
# Upper bound for one handler invocation (spawn -> rendered stdout), seconds.
# On expiry the handler's whole process group is killed (see fastpath.py).
FASTPATH_TIMEOUT_S = float(os.getenv("FASTPATH_TIMEOUT", "3.0") or "3.0")
# DGN-911: in-flight regular-message default is DEBOUNCE-INTERRUPT (Claude
# Code-style). A regular message arriving while a turn is in flight opens a
# per-user debounce window of this many seconds; further messages inside the
# window append and reset the timer (continuous-typing protection). On expiry
# the in-flight turn is soft-interrupted (DGN-581) and the buffered messages
# run as ONE merged new turn. Explicit /queue keeps the legacy DGN-616
# coalescing behavior (merge into the next turn, no interrupt).
BRIDGE_INFLIGHT_DEBOUNCE_S = float(
    os.getenv("BRIDGE_INFLIGHT_DEBOUNCE_S", "5.0") or "5.0"
)
# DGN-911: user-visible notice on an AUTOMATIC in-flight interrupt. Owner
# decision 2026-08-17: default OFF = full silence (the new turn's reply is the
# only output, closest to a natural conversation). That decision governs the
# "your message paused the turn" notice ONLY -- confirmed background-subagent
# deaths are reported unconditionally by the DGN-1015 kill notice, which this
# flag does NOT gate. When enabled, the bridge sends the dedicated
# auto-interrupt copy (i18n auto_interrupt_notice, DGN-1016 O1 draft --
# owner confirmation pending; do not ship enabled before approval).
BRIDGE_INFLIGHT_INTERRUPT_NOTICE = os.getenv(
    "BRIDGE_INFLIGHT_INTERRUPT_NOTICE", "0"
).strip().lower() in ("1", "true", "on", "yes")
# DGN-1016: auto-interrupt background guard. When the debounce window closes
# while tracked live background tasks exist (sdk_bridge.live_task_count > 0),
# the auto-interrupt is NOT sent -- a remote interrupt aborts the whole
# session's abort tree and kills every in-session background subagent
# (measured, DGN-991 rev3). The buffered messages fall back to the legacy
# DGN-616 coalescing merge (delivered when the in-flight turn ends; every
# turn is bounded by PROCESS_TIMEOUT, so the wait is never infinite). This
# knob caps the CUMULATIVE deferral per wait: once the first deferred expiry
# is older than this many seconds, the next expiry interrupts anyway --
# sustained owner input eventually wins over background work, and a phantom
# live-task entry (missed terminal event) cannot suppress interrupts forever.
# Explicit /stop is never gated. 0 disables the guard (pre-DGN-1016 behavior).
BRIDGE_INFLIGHT_DEFER_CAP_S = float(
    os.getenv("BRIDGE_INFLIGHT_DEFER_CAP_S", "300.0") or "300.0"
)


def setup_logging() -> None:
    """File log at LOG_LEVEL; console at WARNING (full level if BOT_DEBUG).

    The root logger level is the gate that runs BEFORE handler levels, so it must
    be the most permissive level any handler wants (the file's LOG_LEVEL). Setting
    it via basicConfig(level=WARNING) would silently drop the file handler's INFO
    records (e.g. "Bot is running") -> a clean successful boot logs nothing.
    """
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(config.log_format)
    is_debug = bool(os.environ.get("BOT_DEBUG"))
    console_level = log_level if is_debug else logging.WARNING

    root = logging.getLogger()
    root.setLevel(log_level)  # gate at the file level; per-handler levels filter below

    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    logs_dir = config.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)

    fh = logging.FileHandler(logs_dir / "bot.log", encoding="utf-8")
    fh.setLevel(log_level)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    for noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "telegram.ext.ExtBot"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    err_path = logs_dir / f"error_{datetime.now().strftime('%Y-%m-%d')}.log"
    efh = logging.FileHandler(err_path, encoding="utf-8")
    efh.setLevel(logging.ERROR)
    sep = "=" * 60
    efh.setFormatter(
        logging.Formatter(f"\n{sep}\n[%(asctime)s] %(name)s - %(levelname)s\n%(message)s\n{sep}")
    )
    logging.getLogger().addHandler(efh)
