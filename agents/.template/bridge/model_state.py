"""Owner model preference persistence and read-only session model resolution.

Only explicit owner selections write last_model.json. Resolving a model for a
turn (including background or injected turns) never changes that preference.

Resolution priority (highest first):
  1. explicit per-session override (session["model"], e.g. after /model)
  2. settings whose model value changed since the persisted owner preference
  3. persisted owner preference (last_model.json)
  4. workspace .claude/settings.json  "model"
  5. global ~/.claude/settings.json    "model"
  6. hardcoded fallback (caller-supplied, e.g. "sonnet")

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
from typing import Any, Dict, Iterable, Optional, Tuple

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


def persist_model(model: Optional[str], known: Iterable[str], *, source: str) -> None:
    """Atomically record an explicit owner selection, with its source.

    Best-effort: never raises. Skips empty / unknown values so a bad runtime
    value can never poison the state file that future sessions read.
    """
    if source not in {"cmd_model", "model_callback"}:
        logger.warning("last_model persist refused: source=%s model=%r", source, model)
        return
    if not is_known_model(model, known):
        return
    try:
        LAST_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        settings_seen = {
            "workspace": _read_settings_model(WORKSPACE_SETTINGS_PATH),
            "global": _read_settings_model(GLOBAL_SETTINGS_PATH),
        }
        payload = json.dumps(
            {"model": model, "source": source, "settings_seen": settings_seen},
            ensure_ascii=True,
        )
        tmp = LAST_MODEL_PATH.with_name(LAST_MODEL_PATH.name + ".tmp")
        tmp.write_text(payload, encoding="ascii")
        os.replace(tmp, LAST_MODEL_PATH)  # atomic within the same dir
        logger.info("last_model persisted: source=%s model=%s", source, model)
    except Exception as e:  # noqa: BLE001 - state write must never crash the bridge
        logger.warning("last_model persist failed: source=%s model=%s: %s", source, model, e)


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


def _load_persisted_state(known: Iterable[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return validated persisted state plus the public loader's warning."""
    model, warning = load_persisted_model(known)
    if model is None:
        return None, warning
    try:
        data = json.loads(LAST_MODEL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, warning
    return data if isinstance(data, dict) else None, warning


def resolve_session_model(
    override: Optional[str],
    known: Iterable[str],
    fallback: str,
) -> str:
    """Resolve without writing state. An explicit session override wins.

    Workspace settings take precedence over global settings. A valid settings
    model supersedes owner state only when its value changed since persistence.
    The mtime repair remains only for legacy state not written by an owner pick.
    Rejected persisted state sets a one-per-start user notice.
    """
    global _pending_start_notice
    known_set = set(known)

    if is_known_model(override, known_set):
        return override  # type: ignore[return-value]

    persisted_state, warning = _load_persisted_state(known_set)
    persisted = persisted_state.get("model") if persisted_state is not None else None
    if warning and not _start_notice_emitted:
        _pending_start_notice = warning
    for path in (WORKSPACE_SETTINGS_PATH, GLOBAL_SETTINGS_PATH):
        candidate = _read_settings_model(path)
        if is_known_model(candidate, known_set):
            if persisted is not None:
                settings_seen = persisted_state.get("settings_seen")
                if isinstance(settings_seen, dict):
                    key = "workspace" if path == WORKSPACE_SETTINGS_PATH else "global"
                    return candidate if candidate != settings_seen.get(key) else persisted
                if persisted_state.get("source") in {"cmd_model", "model_callback"}:
                    return persisted
                try:
                    if path.stat().st_mtime_ns <= LAST_MODEL_PATH.stat().st_mtime_ns:
                        return persisted
                except OSError as e:
                    logger.warning("model freshness check failed (%s): %s", path, e)
                    return persisted
            return candidate  # type: ignore[return-value]

    return persisted if persisted is not None else fallback


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
