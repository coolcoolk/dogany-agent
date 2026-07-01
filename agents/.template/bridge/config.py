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
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    extra_allowed_roots: List[Path] = Field(
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

    # Streaming
    draft_update_min_chars: int = Field(default=30)
    draft_update_interval: float = Field(default=1.0)

    # Voice (local faster-whisper only)
    transcription_provider: str = Field(default="local")
    local_whisper_model: str = Field(default="small")
    whisper_language: Optional[str] = Field(default="ko")
    voice_reply_enabled: bool = Field(default=False)
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
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH") or (
    str(config.claude_cli_path) if config.claude_cli_path else None
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
