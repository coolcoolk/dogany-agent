"""Shared artifact-render contract: arm-declared keyboard SPEC building.

DGN-966: artifact rendering (marker -> keyboard) used to be reimplemented per
send path, so every new path (fast-path DGN-801, push.sh) silently lost the
keyboard. This module is the SINGLE source for turning an idrill arm file into
a keyboard spec. Consumers:

  * bot.py -- wraps the spec rows into telegram InlineKeyboardMarkup for the
    model-turn, fast-path and proactive send paths;
  * routines/push.sh -- python hop builds the raw Telegram inline_keyboard
    JSON from the same spec (out-of-process rail, same keyboard guaranteed).

INVARIANT (DGN-822 pattern, same as bridge.formatting): this module MUST stay
importable OUTSIDE the bridge venv -- no python-telegram-bot import, no
bridge.config import. Paths are passed in explicitly; a keyboard spec is plain
lists/tuples.

Security invariants preserved from DGN-918/924/939: arm_id shape + path
containment, malformed-declaration drop, declared-value whitelist derivation.
"""

import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# arm_id is an opaque 8-char token used as a filename; constrain to chars safe
# as path components (no traversal, no shell-special chars). Single source --
# bot.py aliases this (was TelegramBot._IDRILL_ARM_ID_RE).
IDRILL_ARM_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# Telegram hard limit for callback_data (bytes, UTF-8). A button whose
# callback_data exceeds this would 400 the WHOLE sendMessage; the spec builder
# drops the offending button (never the message / never the row's siblings).
CALLBACK_DATA_MAX_BYTES = 64

# Relative arm directory under an instance's PROJECT_ROOT.
ARM_SUBDIR = ("files", "program", ".idrill-arm")

# Fallback Back-button label when the caller resolves no localized label
# (push.sh hop; bot.py passes messages.IDRILL_BACK_LABEL instead).
DEFAULT_BACK_LABEL = "« Back"


def arm_dir_for(project_root: Path) -> Path:
    """The idrill arm directory for an instance root."""
    d = Path(project_root)
    for part in ARM_SUBDIR:
        d = d / part
    return d


def read_arm(project_root: Path, arm_id: str) -> Optional[dict]:
    """Read + parse an arm file; None on missing/malformed/contained-escape.

    arm_id must already match IDRILL_ARM_ID_RE (callers validate); the strict
    containment re-check below keeps any future arm_id format change from
    escaping the arm directory.
    """
    cb_dir = arm_dir_for(project_root)
    arm_path = (cb_dir / arm_id).resolve()
    try:
        arm_path.relative_to(cb_dir.resolve())
    except ValueError:
        logger.warning("idrill: arm_id %r resolved outside arm dir", arm_id)
        return None
    try:
        return json.loads(arm_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_pair(entry) -> bool:
    """A flat button declaration: [label, value] -- both strings."""
    return (
        isinstance(entry, (list, tuple))
        and len(entry) == 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
    )


def _is_row(entry) -> bool:
    """A grid row declaration: non-empty list of [label, value] pairs."""
    return (
        isinstance(entry, (list, tuple))
        and len(entry) > 0
        and all(_is_pair(b) for b in entry)
    )


def step_button_rows(arm: dict, step: str) -> Optional[List[List[List[str]]]]:
    """Normalize an arm's step_buttons[step] declaration to rows of buttons.

    Returns [[[label, value], ...], ...] (list of rows) or None when the step
    declares no usable buttons. Two declared shapes (DGN-966, shape detection
    is unambiguous because a flat entry's elements are strings while a row
    entry's elements are pairs):

      * legacy flat  [[label, value], ...]        -> ONE row (DGN-918/924/939
        byte-identical; every existing arm file unchanged);
      * grid         [[[label, value], ...], ...] -> declared rows
        (BotFather-style multi-row keyboard).

    Mixed lists: each row entry is a row; each stray pair entry becomes its
    own single-button row, order preserved. Malformed entries are dropped so a
    bad arm cannot inject arbitrary structure (existing discipline).
    """
    decl = arm.get("step_buttons")
    if not isinstance(decl, dict):
        return None
    entries = decl.get(step)
    if not isinstance(entries, list):
        return None
    if all(_is_pair(e) for e in entries) and entries:
        # Legacy flat shape: the whole list is one keyboard row.
        return [[[e[0], e[1]] for e in entries]]
    rows: List[List[List[str]]] = []
    for e in entries:
        if _is_row(e):
            rows.append([[b[0], b[1]] for b in e])
        elif _is_pair(e):
            rows.append([[e[0], e[1]]])
        # else: malformed entry -> dropped
    return rows or None


def declared_step_values(arm: dict, step: str) -> frozenset:
    """Set of tap values the arm declares as buttons for a step (whitelist)."""
    rows = step_button_rows(arm, step)
    if not rows:
        return frozenset()
    return frozenset(value for row in rows for _label, value in row)


def first_declared_step(arm: dict) -> Optional[str]:
    """The lowest-numbered declared step key, or None."""
    decl = arm.get("step_buttons")
    if not isinstance(decl, dict):
        return None
    digit_keys = sorted(
        (k for k in decl.keys() if isinstance(k, str) and k.isdigit()), key=int
    )
    return digit_keys[0] if digit_keys else None


def build_step_keyboard_spec(
    arm: dict,
    arm_id: str,
    step: str,
    back_label: Optional[str] = None,
) -> Tuple[Optional[List[List[Tuple[str, str]]]], Optional[str]]:
    """Build the keyboard SPEC for a step: (rows of (label, callback_data), text).

    Returns (None, None) when the step declares no buttons. The spec is plain
    data -- the caller wraps it for its rail (InlineKeyboardMarkup in bot.py,
    inline_keyboard JSON in the push.sh hop) so the keyboard is IDENTICAL on
    every path (DGN-966 success criterion).

    Back row: appended only when the arm opts in (nav_back: true) and `step`
    is not the first declared step (DGN-939, unchanged). Label priority:
    arm step_back_label > caller back_label > DEFAULT_BACK_LABEL.

    64-byte guard: a button whose callback_data exceeds Telegram's hard limit
    is DROPPED (warning logged); its siblings and the message survive. An
    all-buttons-dropped step returns (None, None) (fail-soft, marker already
    stripped upstream).
    """
    rows = step_button_rows(arm, step)
    if not rows:
        return None, None
    spec_rows: List[List[Tuple[str, str]]] = []
    for row in rows:
        spec_row: List[Tuple[str, str]] = []
        for label, value in row:
            cb = f"idrill:{arm_id}:{step}:{value}"
            if len(cb.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
                logger.warning(
                    "idrill: callback_data over %d bytes dropped "
                    "(arm=%s step=%s label=%r)",
                    CALLBACK_DATA_MAX_BYTES, arm_id, step, label,
                )
                continue
            spec_row.append((label, cb))
        if spec_row:
            spec_rows.append(spec_row)
    if not spec_rows:
        return None, None
    if arm.get("nav_back") is True:
        first = first_declared_step(arm)
        if first is not None and step != first:
            declared_label = arm.get("step_back_label")
            if isinstance(declared_label, str) and declared_label:
                label = declared_label
            elif isinstance(back_label, str) and back_label:
                label = back_label
            else:
                label = DEFAULT_BACK_LABEL
            spec_rows.append([(label, f"idrill:{arm_id}:back")])
    step_text = arm.get("step_text")
    text = step_text.get(step) if isinstance(step_text, dict) else None
    text = text if isinstance(text, str) and text else None
    return spec_rows, text


def initial_keyboard_json(project_root: Path, arm_id: str) -> Optional[dict]:
    """push.sh hop helper: the step-1 keyboard as raw Telegram JSON structures.

    Returns {"text": step_text_1, "reply_markup": {"inline_keyboard": [...]}}
    or None when the arm is invalid/missing/button-less/text-less (fail-soft;
    same gates as bot.py's initial-keyboard render, so path behavior matches).
    """
    if not IDRILL_ARM_ID_RE.match(arm_id):
        logger.warning("idrill: initial marker arm_id failed validation: %r", arm_id)
        return None
    arm = read_arm(project_root, arm_id)
    if arm is None:
        logger.warning("idrill: initial marker arm %s missing/expired", arm_id)
        return None
    spec_rows, text = build_step_keyboard_spec(arm, arm_id, "1")
    if not spec_rows:
        logger.warning("idrill: arm %s declares no step_buttons[1]", arm_id)
        return None
    if not text:
        logger.warning("idrill: arm %s declares no step_text[1]", arm_id)
        return None
    return {
        "text": text,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": lbl, "callback_data": cb} for lbl, cb in row]
                for row in spec_rows
            ]
        },
    }
