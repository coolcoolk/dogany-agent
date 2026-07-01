"""[[OPTIONS]] marker handling + Haiku auto-classifier.

A reply turns a numbered list into inline buttons only when the [[OPTIONS]]
marker is present. The marker may be appended explicitly by Claude, injected by
the AskUserQuestion degradation path, or injected by the Haiku classifier when a
numbered list looks like a pick-one decision menu. The marker is always stripped
from user-facing text.
"""

import re
import shutil
import subprocess
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

OPTIONS_MARKER = "[[OPTIONS]]"

# Numbered option line: "1. label", "2) label", CJK punctuation variants, etc.
_OPTION_RE = re.compile(r"^\s*(\d+)[.、)）]\s*(.+)", re.MULTILINE)
# Plain numbered list detector (for the classifier gate): "1. ...".
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+.+$", re.MULTILINE)

# Haiku classifier knobs.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_TIMEOUT_SECONDS = 15
_PREV_MAX_CHARS = 1500
_ASST_MAX_CHARS = 2500

_PROMPT_V3 = """A Telegram bot reply contains a numbered list (1. 2. ...). It will be turned into tappable buttons ONLY if that list is a decision menu the user must pick exactly one from.

Answer "yes" ONLY if ALL hold:
- the numbered items are mutually-exclusive choices (e.g. proceed / hold, optionA / optionB, allow / deny), AND
- the reply is clearly waiting for the user to pick one item right now, AND
- the items are short actionable choices, not narration.

Answer "no" if the numbered list is ANY of:
- a status/progress report (what was done, what is running, "you may sleep", "shall I log this?"),
- follow-up todos or remaining work items,
- steps/procedure or things the user must DO in order,
- examples, or clarifying sub-questions ("did you mean A or B?"),
- things the assistant will do next.
Also "no" if the closing ask is a plain yes/no ("shall I proceed?") not tied to picking one numbered item.

When unsure, answer "no" (a wrong button is worse than a missing one).
Output exactly one word: yes or no.

PREVIOUS USER MESSAGE:
{prev}

ASSISTANT REPLY:
{asst}

One word, yes or no:"""


def strip_options_marker(text: str) -> Tuple[str, bool]:
    """Remove every standalone [[OPTIONS]] marker line.

    Returns (clean_text, had_marker). A marker line is one whose stripped form
    equals the marker exactly, regardless of position. Surrounding text is kept
    intact; trailing whitespace is stripped.
    """
    if not text:
        return text, False
    lines = text.split("\n")
    kept = [ln for ln in lines if ln.strip() != OPTIONS_MARKER]
    if len(kept) == len(lines):
        return text, False
    return "\n".join(kept).rstrip(), True


def has_numbered_list(text: str) -> bool:
    """True when the text has >=2 numbered lines (classifier gate)."""
    return len(_NUMBERED_RE.findall(text)) >= 2


def extract_options(text: str) -> List[str]:
    """Extract option labels from a numbered list.

    Requires >=2 items numbered consecutively from 1. Returns [] otherwise.
    """
    matches = _OPTION_RE.findall(text)
    if len(matches) < 2:
        return []
    nums = [int(m[0]) for m in matches]
    if nums != list(range(1, len(nums) + 1)):
        return []
    return [m[1].strip() for m in matches]


def build_option_keyboard(options: List[str]) -> Optional[InlineKeyboardMarkup]:
    """Build inline buttons; callback 'opt:{i}. {label}' with 'opt:{i}' fallback."""
    if not options:
        return None
    buttons = []
    for i, opt in enumerate(options, 1):
        label = f"{i}. {opt}"
        cb_data = f"opt:{label}"
        if len(cb_data.encode("utf-8")) > 64:
            cb_data = f"opt:{i}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb_data)])
    return InlineKeyboardMarkup(buttons)


def _parse_yes(raw: str) -> bool:
    return bool(re.search(r"\byes\b", (raw or "").strip().lower()))


def classify_is_choice(prev: str, asst: str, cli_path: Optional[str] = None) -> bool:
    """Ask Haiku whether the trailing numbered list is a pick-one menu.

    Shells out to `claude -p <prompt> --model <haiku>`, 15s timeout. Fail-silent:
    any error/timeout/unparseable output returns False (no buttons). Blocking;
    call via asyncio.to_thread.
    """
    try:
        prev_clean = (prev or "").strip()[-_PREV_MAX_CHARS:] or "(none)"
        asst_clean = (asst or "").strip()[-_ASST_MAX_CHARS:]
        if not asst_clean:
            return False
        prompt = _PROMPT_V3.format(prev=prev_clean, asst=asst_clean)
        claude_bin = cli_path or shutil.which("claude")
        if not claude_bin:
            return False
        proc = subprocess.run(
            [claude_bin, "-p", prompt, "--model", _HAIKU_MODEL],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            return False
        return _parse_yes(proc.stdout)
    except Exception:
        return False
