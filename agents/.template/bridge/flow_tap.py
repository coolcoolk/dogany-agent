"""Pack-declared flow callbacks; see FLOW-TAP-CONTRACT.md (DGN-2036)."""

import copy
import importlib.util
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)
PREFIX = "flow|"
TIMEOUT_SECONDS = 15
LATE_TIMEOUT_SECONDS = 120
BUTTON_WIDTH_LIMIT = 31
CARD_REGISTRY_LIMIT = 256


def _commit_lock_token():
    path = Path(__file__).resolve().parents[1] / "routines" / "lib" / "design_tokens.py"
    spec = importlib.util.spec_from_file_location("bridge_design_tokens", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.COMPONENTS["button.commit_lock"]


def _pressed_marker_token():
    path = Path(__file__).resolve().parents[1] / "routines" / "lib" / "design_tokens.py"
    spec = importlib.util.spec_from_file_location("bridge_design_tokens", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.COMPONENTS["button.pressed_marker"]


def pressed_marker_enabled(root: Path) -> bool:
    """Return the instance switch, defaulting the pressed marker on."""
    value = dotenv_values(root / "config" / "agent.conf", interpolate=False).get(
        "FLOW_TAP_PRESSED_MARKER", "on"
    )
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _display_width(value: str) -> float:
    """Return bridge button width units; CJK glyphs count as 1.5 units."""
    return sum(1.5 if "\u2e80" <= char <= "\ud7ff" else 1 for char in value)


def commit_lock_label(picks, locale="en"):
    """Compose the commit lock component, falling back when it is too wide."""
    token = _commit_lock_token()
    locale = locale if locale in token["suffix"] else "en"
    if not picks:
        return token["fallback"][locale]
    label = token["prefix"] + token["joiner"].join(picks) + token["suffix"][locale]
    return label if _display_width(label) <= BUTTON_WIDTH_LIMIT else token["fallback"][locale]


def keyboard_signature(inline_keyboard):
    """Describe a pack keyboard so a replacement invalidates remembered picks."""
    marker = _pressed_marker_token()
    return tuple(tuple((getattr(button, "text", "").removeprefix(marker),
                        getattr(button, "callback_data", None)) for button in row)
                 for row in (inline_keyboard or ()))


def tapped_row(inline_keyboard, data):
    """Return the row index and visible label for a callback, if present."""
    for index, row in enumerate(inline_keyboard or ()):
        for button in row:
            if getattr(button, "callback_data", None) == data:
                return index, getattr(button, "text", "").removeprefix(
                    _pressed_marker_token()
                )
    return None


def marked_keyboard(inline_keyboard, data):
    """Copy a keyboard with exactly one pressed marker in the tapped row."""
    marker = _pressed_marker_token()
    marked = []
    for row in inline_keyboard or ():
        target = any(getattr(button, "callback_data", None) == data for button in row)
        new_row = []
        for button in row:
            clone = copy.copy(button)
            text = getattr(button, "text", "")
            if target:
                text = text.removeprefix(marker)
                if getattr(button, "callback_data", None) == data:
                    text = marker + text
                object.__setattr__(clone, "text", text)
            new_row.append(clone)
        marked.append(new_row)
    return marked


@dataclass(frozen=True)
class DispatchResult:
    handled: bool
    refused: bool = False

    def __bool__(self):
        return self.handled


def callback_fields(data: str):
    """Return the pack session id and opt-in commit marker."""
    fields = data.split("|")
    return (fields[1] if len(fields) > 1 else data,
            len(fields) >= 6 and fields[-1] == "c")


def dispatch(root: Path, data: str) -> DispatchResult:
    """Run the configured argv with the opaque tap appended, off the event loop.

    Configuration is instance-owned, never supplied by the callback. No shell,
    interpolation, or model is involved. The pack owns validation and rendering.
    """
    try:
        conf = dotenv_values(root / "config" / "agent.conf", interpolate=False)
        argv = json.loads(conf.get("FLOW_TAP_COMMAND") or "null")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv)
                or not argv[0]):
            raise ValueError("FLOW_TAP_COMMAND must be a nonempty JSON argv")
        # Resolve relative commands from the instance root, never from PATH.
        argv[0] = str(root / argv[0])
        started = time.monotonic()
        process = subprocess.Popen(
            [*argv, data], cwd=root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        late = False
        try:
            stdout, _ = process.communicate(timeout=TIMEOUT_SECONDS)
            rc = process.returncode
        except subprocess.TimeoutExpired:
            late = True
            logger.warning("flow tap command timed out, still running")
            try:
                stdout, _ = process.communicate(timeout=LATE_TIMEOUT_SECONDS)
                rc = process.returncode
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, _ = process.communicate()
                elapsed_ms = round((time.monotonic() - started) * 1000)
                logger.warning(
                    "flow tap command late outcome elapsed_ms=%s rc=timeout",
                    elapsed_ms,
                )
                logger.info(
                    "flow tap command elapsed_ms=%s rc=timeout", elapsed_ms
                )
                return DispatchResult(False)
        first_line = (stdout.splitlines() or [""])[0][:120]
        if first_line:
            logger.info("flow tap outcome %s", first_line)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        logger.info("flow tap command elapsed_ms=%s rc=%s", elapsed_ms, rc)
        if late:
            logger.info(
                "flow tap command late outcome elapsed_ms=%s rc=%s",
                elapsed_ms, rc,
            )
        refused = rc == 0 and first_line.startswith("FLOW_REFUSED")
        if rc == 0 and not refused:
            return DispatchResult(True)
        logger.warning("flow tap command exited %s", rc)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("flow tap command failed: %s", exc)
    return DispatchResult(False, refused=locals().get("refused", False))
