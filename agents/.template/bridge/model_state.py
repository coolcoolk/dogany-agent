"""Last-session model persistence and the session-model resolution chain.

The bridge remembers the model the last session actually used so a NEW session
starts on that model instead of snapping back to a static default. State lives
in a small JSON file owned by the bridge in the runtime data dir (same family
as poll_heartbeat), written atomically (mktemp + os.replace) whenever the
effective session model is resolved to a concrete value or changed by the user.

Resolution priority (highest first):
  1. explicit per-session override (session["model"], e.g. after /model)
  2. persisted last-session model (last_model.json)
  3. workspace .claude/settings.json  "model"
  4. global ~/.claude/settings.json    "model"
  5. hardcoded fallback (caller-supplied, e.g. "sonnet")

Safety: the persisted value is validated before use. JSON parse failure, an
empty value, or a model id outside the known/allowed set makes the bridge
ignore the state file and continue down the chain, logging a single warning.
State-file problems never crash the bridge and never silently pick a wrong
model. Atomic write prevents a torn/partial file from ever being read.
"""

import json
import logging
import os
from pathlib import Path
from typing import Iterable, Optional, Tuple

from bridge.config import PROJECT_ROOT, config

logger = logging.getLogger(__name__)

# Runtime state file (bridge-owned, atomic writes). Sits beside poll_heartbeat.
LAST_MODEL_PATH: Path = config.bot_data_dir / "last_model.json"

# Settings sources, workspace beats global (mirrors Claude settings precedence).
WORKSPACE_SETTINGS_PATH: Path = PROJECT_ROOT / ".claude" / "settings.json"
GLOBAL_SETTINGS_PATH: Path = config.claude_settings_path

# One user-visible notice per bridge start, not per message (grill: no spam).
_start_notice_emitted = False
_pending_start_notice: Optional[str] = None


def is_known_model(name: object, known: Iterable[str]) -> bool:
    """A model id is acceptable if it is a whitelisted short name or a full
    'claude-*' id -- the same rule the /model command enforces."""
    if not isinstance(name, str):
        return False
    name = name.strip()
    if not name:
        return False
    return name in set(known) or name.startswith("claude-")


def persist_model(model: Optional[str], known: Iterable[str]) -> None:
    """Atomically record the model as the last-session model.

    Best-effort: never raises. Skips empty / unknown values so a bad runtime
    value can never poison the state file that future sessions read.
    """
    if not is_known_model(model, known):
        return
    try:
        LAST_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"model": model}, ensure_ascii=True)
        tmp = LAST_MODEL_PATH.with_name(LAST_MODEL_PATH.name + ".tmp")
        tmp.write_text(payload, encoding="ascii")
        os.replace(tmp, LAST_MODEL_PATH)  # atomic within the same dir
    except Exception as e:  # noqa: BLE001 - state write must never crash the bridge
        logger.warning("last_model persist failed: %s", e)


def _read_settings_model(path: Path) -> Optional[str]:
    """Return the 'model' value from a settings.json, or None on any problem."""
    try:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001 - a broken settings file is not fatal
        logger.warning("settings model read failed (%s): %s", path, e)
        return None
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) and model.strip() else None


def load_persisted_model(known: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    """Read + validate the last-session model.

    Returns (model, warning). model is None when the file is missing or
    unusable; warning is a single human-readable line when the file existed but
    was rejected (corrupt / unknown model), else None. Never raises.
    """
    if not LAST_MODEL_PATH.exists():
        return None, None
    try:
        raw = LAST_MODEL_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001 - corrupt file -> ignore + warn, no crash
        warn = f"last_model.json unreadable ({e}); falling back to settings chain"
        logger.warning(warn)
        return None, warn
    model = data.get("model") if isinstance(data, dict) else None
    if not is_known_model(model, known):
        warn = (
            f"last_model.json holds unknown/empty model {model!r}; "
            "falling back to settings chain"
        )
        logger.warning(warn)
        return None, warn
    return model, None


def resolve_session_model(
    override: Optional[str],
    known: Iterable[str],
    fallback: str,
) -> str:
    """Resolve the model for a session and persist the concrete result.

    Priority: override > persisted last-session > workspace settings > global
    settings > fallback. Whatever concrete value wins is written back as the new
    last-session model so the chain is self-healing (a fallback-derived choice
    seeds the persisted value for next time). Sets a one-per-start user notice
    when a persisted value had to be rejected.
    """
    global _pending_start_notice
    known_set = set(known)

    if is_known_model(override, known_set):
        persist_model(override, known_set)
        return override  # type: ignore[return-value]

    persisted, warning = load_persisted_model(known_set)
    if warning and not _start_notice_emitted:
        _pending_start_notice = warning
    if persisted is not None:
        return persisted

    for path in (WORKSPACE_SETTINGS_PATH, GLOBAL_SETTINGS_PATH):
        candidate = _read_settings_model(path)
        if is_known_model(candidate, known_set):
            persist_model(candidate, known_set)
            return candidate  # type: ignore[return-value]

    persist_model(fallback, known_set)
    return fallback


def take_start_notice() -> Optional[str]:
    """Return a pending one-per-start user notice exactly once, then clear it.

    The bridge shows this at most once per process start (not per message) when
    a persisted model had to be rejected. Returns None after it has been taken
    or when there is nothing to report.
    """
    global _start_notice_emitted, _pending_start_notice
    if _start_notice_emitted:
        return None
    notice = _pending_start_notice
    if notice is None:
        return None
    _start_notice_emitted = True
    _pending_start_notice = None
    return notice
