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
import unicodedata
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# DGN-822: the marker constant is defined in bridge.formatting (a telegram-free
# module) so the shell sanitize hop can import formatting without
# python-telegram-bot. Re-exported here for existing consumers.
from bridge.formatting import OPTIONS_MARKER

# Numbered option line: "1. label", "2) label", CJK punctuation variants, etc.
_OPTION_RE = re.compile(r"^\s*(\d+)[.、)）]\s*(.+)", re.MULTILINE)
# Plain numbered list detector (for the classifier gate): "1. ...".
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+.+$", re.MULTILINE)
# Fenced code block -- stripped before option/classifier scanning so numbered
# items inside code examples never shadow the actual options list (DGN-085).
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)

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
    """True when the text (outside code blocks) has >=2 numbered lines (classifier gate)."""
    prose = _FENCED_CODE_RE.sub("", text)
    return len(_NUMBERED_RE.findall(prose)) >= 2


def _option_line_entries(text: str) -> List[Tuple[int, str, int]]:
    """Scan for numbered option lines outside fenced code blocks.

    Returns (number, label, line_index) per matching line, in order. The line
    index maps a match back to its exact source line so the consumed run can be
    removed from the display text (DGN-665). Lines fully inside a fenced code
    block are excluded (DGN-085).
    """
    spans = [m.span() for m in _FENCED_CODE_RE.finditer(text)]
    entries: List[Tuple[int, str, int]] = []
    offset = 0
    for idx, line in enumerate(text.split("\n")):
        start, end = offset, offset + len(line)
        offset = end + 1
        if any(s <= start and end <= e for s, e in spans):
            continue
        m = _OPTION_RE.match(line)
        if m:
            entries.append((int(m.group(1)), m.group(2), idx))
    return entries


def _last_option_run(
    entries: List[Tuple[int, str, int]],
) -> List[Tuple[int, str, int]]:
    """Select the LAST contiguous 1..N run of numbered entries (DGN-494).

    A length-1 run broken by a non-consecutive numbered line is discarded,
    which preserves the 'stray 1. then gap -> []' guard so incidental prose
    numbering never yields a spurious button. A single trailing item is
    accepted (DGN-325). Returns [] when no valid run exists.
    """
    runs: List[List[Tuple[int, str, int]]] = []
    cur: List[Tuple[int, str, int]] = []
    for entry in entries:
        n = entry[0]
        if n == 1:
            if cur:
                runs.append(cur)
            cur = [entry]
        elif cur and n == cur[-1][0] + 1:
            cur.append(entry)
        else:
            if cur and len(cur) >= 2:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs[-1] if runs else []


def extract_options(text: str) -> List[str]:
    """Extract option labels from a numbered list.

    Returns the LAST contiguous 1..N run of numbered lines, so buttons keep
    working when the reply also contains an earlier numbered list -- e.g. a
    prose description of the choices before the [[OPTIONS]] block (DGN-494).
    Code blocks are excluded (DGN-085); a single trailing item is accepted
    (DGN-325); the stray '1. then gap -> []' guard is preserved.
    """
    return [label.strip() for _, label, _ in _last_option_run(_option_line_entries(text))]


def _label_has_description(label: str) -> bool:
    """Return True when a raw option label contains a description clause.

    A description clause is text after the first separator (em-dash, double-
    hyphen, or space-hyphen-space) that follows an actual action phrase. This
    is the SAME sep-strip predicate used by _shorten_button_label so the
    strip_consumed_options keep-in-body decision and the button sep-strip never
    diverge (DGN-790 blocker5 / 704c regression prevention).

    A label whose body STARTS with a separator (DGN-704c number-only-button
    guard) is treated as having NO description -- the separator is part of the
    action phrase itself, not a label/description boundary.

    Colon (':') is intentionally NOT a separator here (DGN-790: colon removed
    from _LABEL_SEPARATORS; Korean labels with colons stay whole).
    """
    prefix_match = re.match(r"^(\d+[.)]\s*)", label)
    body = label[len(prefix_match.group(1)):] if prefix_match else label
    sep_match = _LABEL_SEPARATORS.search(body)
    if not sep_match:
        return False
    head = body[: sep_match.start()].strip()
    # Only a non-empty action phrase before the separator makes it a real boundary.
    return bool(head)


def _overflows_to_handle(label: str) -> bool:
    """Return True when this option label would degrade to a number handle on the button.

    Mirrors the SAME sep-strip logic as _shorten_button_label so the two never
    diverge (DGN-790 blocker5 predicate-divergence warning). Returns True when:
    - the label has a number prefix (digit present), AND
    - after applying the same sep-strip, the resulting width exceeds
      _BUTTON_LABEL_MAX_WIDTH (31.0).

    Used by strip_consumed_options to guarantee the full label stays visible in
    the message body whenever the button degrades to "N번" (DGN-879).
    """
    prefix_match = re.match(r"^(\d+[.)]\s*)", label)
    if not prefix_match:
        return False
    body = label[len(prefix_match.group(1)):]
    sep_match = _LABEL_SEPARATORS.search(body)
    if sep_match:
        head = body[: sep_match.start()].strip()
        if head:
            body = head
    short = prefix_match.group(1) + body
    return _label_width(short) > _BUTTON_LABEL_MAX_WIDTH


def strip_consumed_options(text: str) -> Tuple[str, List[str]]:
    """Split a reply into (display_text, options) for button-only choices (DGN-665).

    Locates the SAME last contiguous 1..N run that extract_options selects
    (code blocks excluded), removes exactly those lines plus every standalone
    [[OPTIONS]] marker line from the display text, then rstrips. When no run is
    found, options is [] and the display keeps the full body (marker lines
    still removed) so callers can fall back to showing the original list --
    the user must never be left choice-less.

    Safety (DGN-665 rework): the numbered items only feed the strip when their
    SOURCE LINES are physically adjacent (each line index == previous + 1).
    _last_option_run checks numbering continuity, not line adjacency, so a run
    can span wrapped-label sub-bullets or interleaved prose; deleting only the
    numbered lines there would orphan the in-between text. When the run is not
    line-adjacent the options still build (buttons unchanged) but the body keeps
    the list -- the old safe behavior.

    DGN-720 / DGN-790: when any label in the run carries a description clause
    (detected via _label_has_description -- the same sep-strip predicate used
    by the button shortener), the entire run is KEPT in the display body so the
    full descriptions are visible above the buttons. Only runs where every label
    is a bare short phrase (no description) are dropped from the body.

    DGN-879: the body is also kept when any option would degrade to a number
    handle on the button (detected via _overflows_to_handle -- same sep-strip
    logic). A "N번" button gives no context on its own; keeping the full label
    in the body ensures the user can read the option before tapping.
    """
    clean, _ = strip_options_marker(text or "")
    run = _last_option_run(_option_line_entries(clean))
    if not run:
        return clean, []
    options = [label.strip() for _, label, _ in run]
    line_indices = [idx for _, _, idx in run]
    adjacent = all(
        line_indices[i] == line_indices[i - 1] + 1 for i in range(1, len(line_indices))
    )
    if not adjacent:
        return clean, options
    # DGN-720: keep the body lines when any option has a description clause.
    # The button shortener strips the clause from the button text; the full
    # label stays in the body so the user can read the explanation.
    # DGN-879: also keep the body when any option would degrade to a number
    # handle ("N번") on the button -- the handle alone gives no context, so
    # the full label must remain visible above the buttons.
    # NOTE: options here are raw labels WITHOUT the "N. " prefix (stripped by
    # _option_line_entries). build_option_keyboard reconstructs "{i}. {opt}",
    # so we reconstruct the same form for _overflows_to_handle to match.
    has_any_description = any(_label_has_description(opt) for opt in options)
    has_any_overflow_handle = any(
        _overflows_to_handle(f"{num}. {opt}")
        for (num, _, _), opt in zip(run, options)
    )
    if has_any_description or has_any_overflow_handle:
        return clean, options
    drop = set(line_indices)
    lines = clean.split("\n")
    display = "\n".join(ln for i, ln in enumerate(lines) if i not in drop).rstrip()
    return display, options


def resolve_choice(data: str, inline_keyboard: Optional[list]) -> str:
    """Resolve the full option label from the tapped button's own text (DGN-665).

    build_option_keyboard falls back to the number-only callback_data
    "opt:{i}" when "opt:{i}. {label}" exceeds Telegram's 64-byte callback_data
    limit; Korean labels (3 bytes/char) almost always trip this, dropping the
    label. The button TEXT always carries the full "N. label" regardless of
    truncation, so we recover the label from the keyboard.

    Given the raw callback_data ("opt:...") and query.message.reply_markup
    .inline_keyboard, find the button whose callback_data matches and return its
    text. Falls back to the post-"opt:" payload when the keyboard is absent or
    no button matches (guards a truncated keyboard and non-opt callers).
    """
    fallback = data.split(":", 1)[1] if ":" in data else data
    if not inline_keyboard:
        return fallback
    for row in inline_keyboard:
        for button in row:
            if getattr(button, "callback_data", None) == data:
                return button.text
    return fallback


# Separators that mark the boundary between the action phrase and the
# description clause in an option label, in priority order.
# "--" (ASCII double-hyphen) and the Unicode em-dash are treated equally.
# " - " (space-hyphen-space) is a secondary separator.
# Colon (":") is intentionally excluded: Korean labels frequently use ":" as
# part of the action phrase itself (e.g. "실행: 빠르게"), and colon-splitting
# caused widespread mis-truncation (DGN-790 Q4).
# The leading "N." of the number prefix must NOT be treated as a separator,
# so these patterns are only applied to the body after the prefix is stripped.
_LABEL_SEPARATORS = re.compile(r"\s*(?:--|—)\s*|\s+-\s+")

# Generation contract width: the expected maximum weighted display width for a
# one-line Telegram inline button label. Derived from on-device measurements
# (DGN-779): a label stays on one line at <= 31 weighted
# units. Counts CJK/full-width (east_asian_width W/F) as 1.5,
# whitespace as 0.4, and everything else (latin/digit/symbol) as 1.0.
# This constant is also the overflow threshold (DGN-879): labels exceeding
# this width after sep-strip degrade to a number handle "N번" when a number
# prefix is present, or are returned unchanged when no prefix exists.
# Manual "..." truncation is removed (DGN-790 part2); raw glyph-cut is also
# removed (DGN-879) -- Telegram client handles display-side ellipsis for
# modest overflows.
_BUTTON_LABEL_MAX_WIDTH = 31.0


def _label_width(text: str) -> float:
    """Weighted display width of a button label.

    Priority order:
    - CJK / full-width glyphs (east_asian_width W or F): 1.5
    - Whitespace (ch.isspace()): 0.4  -- real on-device width ~0.25; 1.0 was 2.7x overcount
    - Everything else (latin, digit, symbol, ambiguous): 1.0

    Calibrated against on-device measurements (DGN-779).
    """
    width = 0.0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 1.5
        elif ch.isspace():
            width += 0.4
        else:
            width += 1.0
    return width


def _shorten_button_label(label: str) -> str:
    """Extract the action phrase from an option label for Telegram button display.

    Strategy (DGN-790 part2, updated DGN-879):
    1. Parse the number prefix (e.g. "1. ", "2) ") from the label.
    2. Strip the description clause that follows the first separator
       (em-dash or double-hyphen, or " - ") from the action phrase.
       Colon is NOT a separator (DGN-790: removed to prevent Korean mis-splits).
    3. After sep-strip, if the weighted width is at or under
       _BUTTON_LABEL_MAX_WIDTH (31.0), return as-is -- no trimming, no ellipsis.
       Telegram client handles display-side overflow with its own ellipsis.
    4. If the width still exceeds 31.0: return a pure number handle "N번" where
       N is the parsed number prefix digit (e.g. "1. ..."->"1번", "2) ..."->"2번").
       If there is NO number prefix, return the short label unchanged -- no
       number to key on, never raw-cut.

    Raw glyph-by-glyph force-trim is removed (DGN-879): it split words
    mid-syllable and distorted meaning. The number handle is the overflow
    fallback; the full label remains visible in the message body when any
    option degrades to a handle (strip_consumed_options keep-condition).

    The number prefix dot "." is NOT treated as a separator -- it is only
    consumed as part of the "N. " / "N) " prefix pattern (DGN-704c guard).
    """
    # Match the number prefix: "1. ", "2) ", "3. ", etc.
    prefix_match = re.match(r"^(\d+[.)]\s*)", label)
    if prefix_match:
        prefix = prefix_match.group(1)
        body = label[len(prefix):]
        # Extract the digit(s) for use as a number handle on overflow.
        number = re.match(r"^(\d+)", prefix_match.group(1)).group(1)
    else:
        prefix = ""
        body = label
        number = None

    # Strip description clause after the first separator in the action body.
    # Only split when an action phrase actually precedes the separator: a label
    # whose body STARTS with a separator (e.g. "--html opt-in ...") would
    # otherwise collapse to an empty body, leaving a number-only button.
    # In that case the separator is part of the label body itself -- keep it.
    sep_match = _LABEL_SEPARATORS.search(body)
    if sep_match:
        head = body[: sep_match.start()].strip()
        if head:
            body = head

    short = prefix + body
    if _label_width(short) <= _BUTTON_LABEL_MAX_WIDTH:
        # Within generation contract: return as-is, no trim, no ellipsis.
        return short

    # Exceeds 31.0 after sep-strip.
    # Overflow -> number handle when a prefix digit is available (DGN-879).
    # No prefix -> return unchanged; never raw-cut mid-syllable.
    if number is not None:
        return f"{number}번"
    return short


# DGN-775: regexes for stripping inline markdown from button label text.
# Applied before display-width trimming so markdown syntax never counts toward
# the width budget or leaks into the Telegram button surface.
_LABEL_MD_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_LABEL_MD_BOLD_UNDER = re.compile(r"__(.+?)__", re.DOTALL)
_LABEL_MD_ITALIC_STAR = re.compile(r"\*(.+?)\*", re.DOTALL)
_LABEL_MD_ITALIC_UNDER = re.compile(r"_(.+?)_", re.DOTALL)
_LABEL_MD_CODE = re.compile(r"`(.+?)`", re.DOTALL)
_LABEL_MD_HEADER = re.compile(r"^#{1,6}\s+")


def strip_markdown_label(text: str) -> str:
    """DGN-775: strip inline markdown syntax from a button label string.

    Removes: **bold** / __bold__ -> text, *italic* / _italic_ -> text,
    `code` -> text, ## header prefix -> text (line-start only).
    The inner content is always preserved; only the markup wrappers are removed.
    Applied to button labels before display-width trimming so syntax chars
    never inflate the width measurement or appear on the Telegram button surface.
    """
    # Header prefix: only meaningful at the very start of the label.
    text = _LABEL_MD_HEADER.sub("", text.lstrip())
    # Bold before italic so ** is not partially consumed as two *.
    text = _LABEL_MD_BOLD_STAR.sub(r"\1", text)
    text = _LABEL_MD_BOLD_UNDER.sub(r"\1", text)
    text = _LABEL_MD_ITALIC_STAR.sub(r"\1", text)
    text = _LABEL_MD_ITALIC_UNDER.sub(r"\1", text)
    text = _LABEL_MD_CODE.sub(r"\1", text)
    return text


def build_option_keyboard(options: List[str]) -> Optional[InlineKeyboardMarkup]:
    """Build inline buttons; callback 'opt:{i}. {label}' with 'opt:{i}' fallback.

    The button TEXT is shortened via _shorten_button_label so it fits within
    the safe Telegram inline button width (DGN-704). The callback_data and the
    resolve_choice path use the index-based "opt:{i}" fallback for Korean/CJK
    labels, so shortening the display text has no effect on choice resolution.

    DGN-775: markdown syntax is stripped from the label before width trimming
    so button text is always plain text (never leaks **bold** etc. to Telegram).
    """
    if not options:
        return None
    buttons = []
    for i, opt in enumerate(options, 1):
        clean_opt = strip_markdown_label(opt)
        label = f"{i}. {clean_opt}"
        cb_data = f"opt:{label}"
        if len(cb_data.encode("utf-8")) > 64:
            cb_data = f"opt:{i}"
        button_text = _shorten_button_label(label)
        buttons.append([InlineKeyboardButton(button_text, callback_data=cb_data)])
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
