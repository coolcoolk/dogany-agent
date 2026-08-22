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
from bridge.formatting import (
    IDRILL_MARKER_RE,
    LINK_PREVIEW_MARKER,
    OPTIONS_MARKER,
    SEND_FILE_MARKER,
    is_options_marker_line,
    parse_options_marker_labels,
)

# DGN-881: the overflow number-handle button label is localized (ko "N번" /
# en "No.N"). t() resolves the active locale at call time (config.locale).
from bridge.i18n import t

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
    """Remove every standalone [[OPTIONS]] marker line (bare or labeled).

    Returns (clean_text, had_marker). A marker line is one whose stripped form
    is the bare marker exactly OR a labeled marker "[[OPTIONS: a | b]]"
    (DGN-992), regardless of position. Surrounding text is kept intact;
    trailing whitespace is stripped.
    """
    if not text:
        return text, False
    lines = text.split("\n")
    kept = [ln for ln in lines if not is_options_marker_line(ln)]
    if len(kept) == len(lines):
        return text, False
    return "\n".join(kept).rstrip(), True


def has_options_marker(text: str) -> bool:
    """True when any standalone marker line (bare or labeled) is present.

    Used by the classifier gate (sdk_bridge._maybe_mark_options) so a labeled
    marker suppresses auto-injection the same way the bare marker's substring
    check always has (DGN-992: "[[OPTIONS]]" is not a substring of
    "[[OPTIONS: a | b]]", so the substring check alone misses it).
    """
    return any(is_options_marker_line(ln) for ln in (text or "").split("\n"))


def _is_foreign_marker_line(stripped: str) -> bool:
    """True for non-OPTIONS control-marker lines (send_file:: etc.).

    Used as a stop condition when collecting bare-marker trailing labels --
    another marker line can never be a button label.
    """
    return (
        stripped.startswith(SEND_FILE_MARKER)
        or stripped.startswith(LINK_PREVIEW_MARKER)
        or IDRILL_MARKER_RE.match(stripped) is not None
    )


def extract_marker_labels(text: str) -> List[str]:
    """Extract button labels declared next to an [[OPTIONS]] marker (DGN-992).

    Priority (first source that yields labels wins; later sources unseen):
      1. LABELED marker line "[[OPTIONS: a | b]]" -- labels inside the marker.
      2. BARE marker trailing block -- the contiguous non-blank lines DIRECTLY
         after a standalone "[[OPTIONS]]" line, one line = one label, stopping
         at the first blank line, another marker line, or a code fence. This
         is the natural authoring shape observed in the real incident (labels
         written under the marker, no numbered run anywhere) -- the machine
         accepts it instead of requiring authors to memorize a format.
    Callers fall back to the body numbered-run path (extract_options /
    strip_consumed_options) only when this returns [].

    Fence guard (DGN-085 class): marker lines inside ``` never arm, and label
    collection never crosses into a fence. When several markers exist the
    LAST one wins (mirrors the last-run semantics of extract_options). A
    sentence-looking trailing line is still accepted as a label -- an
    over-wide label rides the existing DGN-881 number-handle degradation on
    the button (choices must not evaporate over authoring shape; no new
    judgment heuristics invented).
    """
    src = text or ""
    spans = [m.span() for m in _FENCED_CODE_RE.finditer(src)]
    lines = src.split("\n")
    fenced: List[bool] = []
    labeled: List[str] = []
    bare_idx: Optional[int] = None
    offset = 0
    for i, ln in enumerate(lines):
        start, end = offset, offset + len(ln)
        offset = end + 1
        in_fence = any(s <= start and end <= e for s, e in spans)
        fenced.append(in_fence)
        if in_fence:
            continue
        parsed = parse_options_marker_labels(ln)
        if parsed:
            labeled = parsed
        elif ln.strip() == OPTIONS_MARKER:
            bare_idx = i
    if labeled:
        return labeled
    if bare_idx is None:
        return []
    trailing: List[str] = []
    for j in range(bare_idx + 1, len(lines)):
        if fenced[j]:
            break
        stripped = lines[j].strip()
        if not stripped:
            break
        if is_options_marker_line(lines[j]) or _is_foreign_marker_line(stripped):
            break
        # A numbered trailing line ("1. label") sheds its number prefix via
        # the EXISTING option-line regex (no new heuristic) --
        # build_option_keyboard prepends "{i}. " itself, so keeping the
        # prefix would render a double-numbered button ("1. 1. label").
        m = _OPTION_RE.match(lines[j])
        trailing.append(m.group(2).strip() if m else stripped)
    return trailing


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


def _overflows_to_handle(label: str) -> bool:
    """Return True when this option label would degrade to a number handle on the button.

    The caller passes the reconstructed button form "{n}. {label}" -- the same
    text _shorten_button_label width-checks (DGN-881: no separator parsing, the
    two predicates are the same one-line width comparison and cannot diverge).

    Used by strip_consumed_options to guarantee the full label stays visible in
    the message body whenever the button degrades to a number handle (DGN-879).
    """
    return _label_width(label) > _BUTTON_LABEL_MAX_WIDTH


def _strip_verbatim_label_block(clean: str, labels: List[str]) -> str:
    """Remove the LAST contiguous line block whose stripped lines equal
    `labels` exactly (DGN-992 rev2: bare-marker trailing labels consumed by
    the buttons must not stay in the display as a duplicate list).

    Blocks inside fenced code are never touched (DGN-085 class). When no
    exact match exists the text is returned unchanged -- removal is an
    optimization, never a requirement, so a miss is always safe.
    """
    if not labels:
        return clean
    lines = clean.split("\n")
    spans = [m.span() for m in _FENCED_CODE_RE.finditer(clean)]
    fenced: List[bool] = []
    offset = 0
    for ln in lines:
        start, end = offset, offset + len(ln)
        offset = end + 1
        fenced.append(any(s <= start and end <= e for s, e in spans))
    n = len(labels)
    for i in range(len(lines) - n, -1, -1):
        if any(fenced[i + k] for k in range(n)):
            continue
        if all(lines[i + k].strip() == labels[k] for k in range(n)):
            del lines[i:i + n]
            return "\n".join(lines).rstrip()
    return clean


def strip_consumed_options(
    text: str, marker_labels: Optional[List[str]] = None
) -> Tuple[str, List[str]]:
    """Split a reply into (display_text, options) for button-only choices (DGN-665).

    DGN-992: marker-declared labels (labeled marker "[[OPTIONS: a | b]]", or
    the bare marker's TRAILING lines -- see extract_marker_labels priority)
    are the primary label source -- they become the options directly,
    independent of body formatting, so a bullet/prose body still gets
    buttons. The numbered-run parse is demoted to a body-dedup optimization:
    the run is stripped from the display ONLY when it exists, is
    line-adjacent, its labels EXACTLY match the marker labels (a mismatched
    run is unrelated content -- the DGN-984 hijack shape -- and must stay in
    the body), and no label would degrade to a number handle (DGN-879 keep
    rule). Bare-marker trailing labels consumed by the buttons are removed
    from the display via _strip_verbatim_label_block under the same overflow
    keep rule. `marker_labels` lets callers pass labels precomputed from the
    ORIGINAL content (the marker may already be stripped from `text`); None
    means "extract from text here".

    Bare-marker path (marker carries no labels) is unchanged: locates the
    SAME last contiguous 1..N run that extract_options selects (code blocks
    excluded), removes exactly those lines plus every standalone [[OPTIONS]]
    marker line from the display text, then rstrips. When no run is found,
    options is [] and the display keeps the full body (marker lines still
    removed) so callers can fall back to showing the original list -- the
    user must never be left choice-less.

    Safety (DGN-665 rework): the numbered items only feed the strip when their
    SOURCE LINES are physically adjacent (each line index == previous + 1).
    _last_option_run checks numbering continuity, not line adjacency, so a run
    can span wrapped-label sub-bullets or interleaved prose; deleting only the
    numbered lines there would orphan the in-between text. When the run is not
    line-adjacent the options still build (buttons unchanged) but the body keeps
    the list -- the old safe behavior.

    DGN-879 / DGN-881: the body is kept when any option would degrade to a
    number handle on the button (detected via _overflows_to_handle -- the same
    width check the button shortener applies). A number-handle button gives no
    context on its own; keeping the full label in the body ensures the user
    can read the option before tapping. Runs where every button shows its
    label in full are dropped from the body (descriptions live in the message
    body per the thin-label contract, not in the labels -- DGN-881).
    """
    clean, _ = strip_options_marker(text or "")
    if marker_labels is None:
        marker_labels = extract_marker_labels(text or "")
    run = _last_option_run(_option_line_entries(clean))
    if marker_labels:
        # DGN-992: marker labels are the options; body content is only a
        # duplicate to remove -- and only when it provably IS a duplicate.
        # DGN-879 keep rule applies to both removal shapes below: when any
        # label degrades to a number handle, the body keeps the full text.
        overflow = any(
            _overflows_to_handle(f"{i}. {opt}")
            for i, opt in enumerate(marker_labels, 1)
        )
        stripped_any = False
        if run:
            run_labels = [label.strip() for _, label, _ in run]
            line_indices = [idx for _, _, idx in run]
            adjacent = all(
                line_indices[i] == line_indices[i - 1] + 1
                for i in range(1, len(line_indices))
            )
            if adjacent and run_labels == marker_labels and not overflow:
                drop = set(line_indices)
                lines = clean.split("\n")
                clean = "\n".join(
                    ln for i, ln in enumerate(lines) if i not in drop
                ).rstrip()
                stripped_any = True
        if not stripped_any and not overflow:
            # DGN-992 rev2: bare-marker TRAILING labels remain in the body
            # after the marker line is stripped (they came from the lines
            # under the marker) -- the buttons consumed them, so remove the
            # matching block to avoid a duplicate display. Same mechanism
            # also removes a verbatim (unnumbered) restatement of labeled-
            # marker labels. Only an EXACT contiguous match is touched; a
            # non-matching body always survives intact (fail-safe).
            clean = _strip_verbatim_label_block(clean, marker_labels)
        return clean, marker_labels
    if not run:
        return clean, []
    options = [label.strip() for _, label, _ in run]
    line_indices = [idx for _, _, idx in run]
    adjacent = all(
        line_indices[i] == line_indices[i - 1] + 1 for i in range(1, len(line_indices))
    )
    if not adjacent:
        return clean, options
    # DGN-879: keep the body when any option would degrade to a number handle
    # on the button -- the handle alone gives no context, so the full label
    # must remain visible above the buttons.
    # NOTE: options here are raw labels WITHOUT the "N. " prefix (stripped by
    # _option_line_entries). build_option_keyboard reconstructs "{i}. {opt}",
    # so we reconstruct the same form for _overflows_to_handle to match.
    has_any_overflow_handle = any(
        _overflows_to_handle(f"{num}. {opt}")
        for (num, _, _), opt in zip(run, options)
    )
    if has_any_overflow_handle:
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


# Generation contract width: the expected maximum weighted display width for a
# one-line Telegram inline button label. Derived from on-device measurements
# (DGN-779): a label stays on one line at <= 31 weighted
# units. Counts CJK/full-width (east_asian_width W/F) as 1.5,
# whitespace as 0.4, and everything else (latin/digit/symbol) as 1.0.
# This constant is also the overflow threshold (DGN-879/DGN-881): labels
# exceeding this width degrade to a localized number handle (ko "N번" /
# en "No.N"). Manual "..." truncation is removed (DGN-790 part2); raw
# glyph-cut is also removed (DGN-879); separator parsing is removed (DGN-881:
# labels are thin tokens by contract, descriptions live in the message body).
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
    """Return the button text for a numbered option label (DGN-881).

    Labels are thin tokens by contract (bridge.md): descriptions and the
    recommendation marker live in the message body, so no separator parsing
    happens here. Within the width contract the label passes through
    untouched. On overflow the button degrades to a localized number handle
    (ko "N번" / en "No.N", i18n key option_number_handle) -- the full label
    stays readable in the body (strip_consumed_options overflow-keep).
    The number prefix always exists: build_option_keyboard prepends "{i}. ".
    """
    if _label_width(label) <= _BUTTON_LABEL_MAX_WIDTH:
        return label
    number = re.match(r"^(\d+)", label).group(1)
    return t("option_number_handle").format(n=number)


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

    The button TEXT passes through _shorten_button_label: passthrough within
    the width contract, localized number handle on overflow (DGN-881). The
    callback_data and the resolve_choice path use the index-based "opt:{i}"
    fallback for Korean/CJK labels, so the handle degradation has no effect
    on choice resolution.

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
