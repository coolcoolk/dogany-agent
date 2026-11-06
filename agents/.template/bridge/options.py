"""[[OPTIONS]] marker handling + Haiku auto-classifier.

A reply turns a numbered list into inline buttons only when the [[OPTIONS]]
marker is present. The marker may be appended explicitly by Claude, injected by
the AskUserQuestion degradation path, or injected by the Haiku classifier when a
numbered list looks like a pick-one decision menu. The marker is always stripped
from user-facing text.
"""

import logging
import re
import subprocess
import unicodedata
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# DGN-822: the marker constant is defined in bridge.formatting (a telegram-free
# module) so the shell sanitize hop can import formatting without
# python-telegram-bot. Re-exported here for existing consumers.
from bridge.formatting import (
    LINK_PREVIEW_MARKER,
    OPTIONS_MARKER,
    SEND_FILE_MARKER,
    is_options_marker_line,
    parse_options_marker_labels,
)
# ESTATE:BEGIN idrill marker re-export for the shell sanitize hop (L-SEAM, DGN-822)
from bridge.formatting import IDRILL_MARKER_RE
# ESTATE:END

# DGN-881: the overflow number-handle button label is localized (ko "N번" /
# en "No.N"). t() resolves the active locale at call time (config.locale).
from bridge.i18n import t

# Numbered option line: "1. label", "2) label", CJK punctuation variants, etc.
_OPTION_RE = re.compile(r"^\s*(\d+)[.、)）]\s*(.+)", re.MULTILINE)
# Position-locked list prefix, including spaced punctuation (DGN-1242).
_NUM_PREFIX_RE = re.compile(r"^(\d+)\s*[.\u3001)\uff09]\s*(.+)$")
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


def has_options_marker(text: str, *, outside_fences: bool = False) -> bool:
    """True when any standalone marker line (bare or labeled) is present.

    Used by the classifier gate (sdk_bridge._maybe_mark_options) so a labeled
    marker suppresses auto-injection the same way the bare marker's substring
    check always has (DGN-992: "[[OPTIONS]]" is not a substring of
    "[[OPTIONS: a | b]]", so the substring check alone misses it).
    Delivery uses outside_fences=True so grammar examples never emit menus.
    """
    # Delivery must not turn a literal fenced example into a failed menu.
    prose = _FENCED_CODE_RE.sub("", text or "") if outside_fences else (text or "")
    return any(is_options_marker_line(ln) for ln in prose.split("\n"))


def _is_foreign_marker_line(stripped: str) -> bool:
    """True for non-OPTIONS control-marker lines (send_file:: etc.).

    Used as a stop condition when collecting bare-marker trailing labels --
    another marker line can never be a button label.
    """
    return (
        stripped.startswith(SEND_FILE_MARKER)
        or stripped.startswith(LINK_PREVIEW_MARKER)
        # ESTATE:BEGIN idrill clause of the foreign-marker predicate (L-SEAM _is_foreign_marker_line)
        or IDRILL_MARKER_RE.match(stripped) is not None
        # ESTATE:END
    )


_ENUMERATOR_FAMILIES = (
    tuple("abcdefghijklmnopqrstuvwxyz"),
    tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    tuple(chr(cp) for cp in (0xAC00, 0xB098, 0xB2E4, 0xB77C, 0xB9C8, 0xBC14, 0xC0AC)),
    tuple(chr(0x2460 + i) for i in range(10)),
    ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"),
)
_ENUMERATOR_PUNCT = re.escape(".)" + chr(0x3001) + chr(0xFF09))


def _normalize_option_label(label: str, token: str, *, circled: bool = False) -> Optional[str]:
    """Strip one expected enumerator; None means this menu family does not match.

    Unpunctuated letters, bare digits and quantity/unit prefixes are content.
    Circled numbers may stand alone before whitespace and label text.
    """
    token = re.escape(token)
    prefix = rf"(?:{token}\s*[{_ENUMERATOR_PUNCT}]|\({token}\))\s*"
    if circled:
        prefix = rf"(?:{prefix}|{token}\s+)"
    match = re.fullmatch(prefix + r"(\S.*)", label)
    return match.group(1).strip() if match else None


def _normalize_option_labels(labels: List[str]) -> List[str]:
    """Shed one prefix only if every label follows the same 1..N family."""
    labels = [label.strip() for label in labels]
    if not labels:
        return labels
    # Preserve numeric spellings already accepted by the numbered-list parser.
    numbered = [_NUM_PREFIX_RE.fullmatch(label) for label in labels]
    if all(
        match and int(match.group(1)) == i and match.group(2).strip()
        for i, match in enumerate(numbered, 1)
    ):
        return [match.group(2).strip() for match in numbered]
    size = len(labels)
    families = (tuple(str(i) for i in range(1, size + 1)),) + _ENUMERATOR_FAMILIES
    for family in families:
        if len(family) < size:
            continue
        normalized = [
            _normalize_option_label(label, family[i], circled=ord(family[0][0]) == 0x2460)
            for i, label in enumerate(labels)
        ]
        if all(label is not None for label in normalized):
            return normalized
    # Localized overflow handles remain a menu family, resolved from i18n.
    normalized = []
    for i, label in enumerate(labels, 1):
        handle = re.escape(t("option_number_handle").format(n=i))
        match = re.fullmatch(handle + r"\s+(\S.*)", label)
        if not match:
            return labels
        normalized.append(match.group(1).strip())
    return normalized


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
         Blank separators before an explicit 1..N list are accepted too;
         that first menu wins over later numbered descriptions.
    Callers fall back to the body numbered-run path (extract_options /
    strip_consumed_options) only when this returns [].
    All sources shed one complete ordered enumerator family before labels reach
    body deduplication, width checks or keyboard rendering (DGN-2171).

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
        return _normalize_option_labels(labeled)
    if bare_idx is None:
        return []
    trailing: List[str] = []
    for j in range(bare_idx + 1, len(lines)):
        if fenced[j]:
            break
        stripped = lines[j].strip()
        if not stripped:
            # Anchor the first explicit 1..N list after blank separators;
            # later numbered descriptions must not become the menu.
            if not trailing:
                next_line = next((ln for ln in lines[j + 1:] if ln.strip()), "")
                first = _OPTION_RE.match(next_line)
                if first and int(first.group(1)) == 1:
                    continue
            break
        if is_options_marker_line(lines[j]) or _is_foreign_marker_line(stripped):
            break
        trailing.append(stripped)
    return _normalize_option_labels(trailing)


def has_numbered_list(text: str) -> bool:
    """True when the text (outside code blocks) has >=2 numbered lines (classifier gate)."""
    prose = _FENCED_CODE_RE.sub("", text)
    return len(_NUMBERED_RE.findall(prose)) >= 2


def has_single_trailing_option(text: str) -> bool:
    """True for the single-option decision-menu shape (DGN-1128).

    The >=2 threshold above (kept conservative since DGN-325 "to avoid false
    positives on incidental single-item lists") means a reply offering exactly
    ONE choice never reaches the classifier, so no buttons attach unless the
    author hand-writes the marker -- and a forgotten marker leaves the user
    with nothing to tap. This predicate widens ONLY the classifier gate
    (sdk_bridge._maybe_mark_options), not the has_options OR-arms, and only
    for the shape a real one-item menu takes: outside code blocks there is
    EXACTLY one numbered line, it is a "1." item, and it is the LAST
    non-blank line of the prose (a menu awaits the pick at the end; an
    incidental "1." inside running prose is followed by more text). The
    Haiku classifier remains the actual judge -- this is a cheap structural
    pre-filter, not a new judgment heuristic.
    """
    prose = _FENCED_CODE_RE.sub("", text or "")
    if len(_NUMBERED_RE.findall(prose)) != 1:
        return False
    lines = [ln for ln in prose.split("\n") if ln.strip()]
    return bool(lines) and re.match(r"\s*1\.\s+\S", lines[-1]) is not None


_BULLET_LINE_RE = re.compile(r"^\s*[-*\u2022]\s+\S")


def is_nested_step_list(text: str) -> bool:
    """True when the LAST numbered run is a plan nested under a bullet (DGN-2137).

    A status reply that says "- **if all passes:**" and then lists its next
    steps as an INDENTED 1..N run is a procedure, not a pick-one menu. Shape:
    every item of the last contiguous run is indented, and the nearest
    non-blank line above the run is a bullet. A real menu's list sits at the
    left margin under a question. Structural only; used to keep such a run away
    from the classifier (fail closed: unsure -> no buttons).
    """
    prose = _FENCED_CODE_RE.sub("", text or "")
    run = _last_option_run(_option_line_entries(prose))
    if not run:
        return False
    lines = prose.split("\n")
    if not all(lines[idx][:1] in (" ", "\t") for _, _, idx in run):
        return False
    for j in range(run[0][2] - 1, -1, -1):
        if lines[j].strip():
            return _BULLET_LINE_RE.match(lines[j]) is not None
    return False


_DECLARATIVE_STEP_END_RE = re.compile(
    r"(?:[.!]\s*|(?:\ub2c8\ub2e4|\ud574\uc694|\uc5b4\uc694|\uc544\uc694)[.!]?\s*)$"
)


def is_declarative_step_list(text: str) -> bool:
    """True when the last numbered run consists of declarative sentences.

    Marker-less option discovery is deliberately fail-closed.  A top-level
    status/plan list can otherwise look structurally identical to a menu and
    a model-classifier false positive gives the owner buttons for work that
    the assistant, rather than the owner, will perform.  Complete sentences
    (including common Korean polite declarative endings) are a strong local
    signal for that shape.  Explicit ``[[OPTIONS]]`` paths do not use this
    predicate and therefore retain their authored semantics.
    """
    prose = _FENCED_CODE_RE.sub("", text or "")
    run = _last_option_run(_option_line_entries(prose))
    return len(run) >= 2 and all(
        _DECLARATIVE_STEP_END_RE.search(label) is not None
        for _, label, _ in run
    )


# DGN-2185: step/progress prompt whose expected reply is ONE fixed short
# quoted token ('reply "next" when done', or the Korean quoted-word form). Owner must
# not type it: the token becomes a (single) button. Deterministic, no model.
_TOKEN_Q = "\"'\u2018\u2019\u201c\u201d\u300c\u300d`"
_FT_KO = re.compile(
    "[" + _TOKEN_Q + r"]\s*([^\s" + _TOKEN_Q + r"]{1,8})\s*[" + _TOKEN_Q + r"]"
    r"\s*(?:\uc774?\ub77c\uace0|\ub77c\uace0|\uc774?\ub77c\uba70)?\s*"
    r"(?:\ub9d0|\ud574|\ud558\uc2dc|\uc785\ub825|\uc368|\ub2f5|\ub20c\ub7ec|\ud0c0\uc774\ud551|\ub0a8\uacaf)"
)
_FT_EN = re.compile(
    r"\b(?:type|reply|say|send|answer|respond with|tell me)\s+"
    r"[" + _TOKEN_Q + r"]([^\s" + _TOKEN_Q + r"]{1,12})[" + _TOKEN_Q + r"]",
    re.IGNORECASE,
)


def extract_fixed_token_prompt(text: str) -> List[str]:
    """The single fixed token the reply asks the owner to type, else [] (DGN-2185).

    Looks only at the LAST non-blank prose line (fenced code removed); that is
    where a step prompt ends (a quoted "next"-style word). Returns
    [token] for a short QUOTED token behind a type/say/reply instruction.
    Unquoted words are never taken: a plain "please confirm" is a request, not a token. Numbered/bulleted/quote lines
    and lines carrying a marker return [].
    """
    prose = _FENCED_CODE_RE.sub("", text or "")
    lines = [ln.strip() for ln in prose.split("\n") if ln.strip()]
    if not lines:
        return []
    last = lines[-1]
    if _OPTION_RE.match(last) or last.startswith(("-", "*", ">", "#", "[[")):
        return []
    for rx in (_FT_KO, _FT_EN):
        m = rx.search(last)
        if m:
            token = m.group(1).strip()
            if token and "|" not in token and "]" not in token:
                return [token]
    return []


# DGN-1927: alternative-question shape ("A, B?" / "A or B?") on the LAST
# sentence of a reply. Deterministic parser, no model call: a closing
# either/or question gets buttons without an authored marker.
_ALT_KO_END = re.compile(
    r"(?:세요|어요|아요|해요|나요|까요|예요|이에요|인가요)\s*$"
)
_ALT_KO_WH = re.compile(r"뭐|무엇|어디|언제|왜|어떻게|누가|누구|얼마|몇")
_ALT_EN_WH = re.compile(
    r"\b(?:what|where|when|why|how|who|whom|which|whose)\b", re.IGNORECASE
)
_ALT_SPLIT_KO = re.compile(r"\s*,\s+|\s+아니면\s+|\s+or\s+", re.IGNORECASE)
_ALT_SPLIT_EN = re.compile(r"\s*,\s+(?:or\s+)?|\s+or\s+", re.IGNORECASE)
_ALT_SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+")
_ALT_HANGUL = re.compile(r"[\uac00-\ud7a3]")


def extract_alternative_question(text: str) -> List[str]:
    """Labels of a closing either/or question, else [] (DGN-1927).

    Looks only at the LAST sentence of the prose (fenced code removed). It
    must end with "?" and split into 2 (max 3) clauses on ", " / " 아니면 " /
    " or ". Korean clauses must each end in a question/polite ending; English
    needs an explicit " or ". Open questions (wh-words) and single-clause
    yes/no questions return []. Korean labels drop the polite/question
    ending so buttons read as neutral action phrases (vendor contract), see
    _neutral_ko_label.
    """
    prose = _FENCED_CODE_RE.sub("", text or "")
    lines = [ln.strip() for ln in prose.split("\n") if ln.strip()]
    if not lines:
        return []
    last = lines[-1]
    if _OPTION_RE.match(last) or last.startswith(("-", "*", ">", "#", "[[")):
        return []
    sentence = _ALT_SENT_SPLIT.split(last)[-1].strip()
    if not sentence.endswith(("?", "？")):
        return []
    body = sentence.rstrip("?？ ").strip()
    if _ALT_HANGUL.search(body):
        if _ALT_KO_WH.search(body):
            return []
        clauses = [c.strip() for c in _ALT_SPLIT_KO.split(body)]
        if not all(_ALT_KO_END.search(c) for c in clauses if c):
            return []
    else:
        if _ALT_EN_WH.search(body) or not re.search(r"\bor\b", body, re.IGNORECASE):
            return []
        clauses = [c.strip() for c in _ALT_SPLIT_EN.split(body)]
    if not 2 <= len(clauses) <= 3 or not all(clauses):
        return []
    if any(("|" in c or "]" in c or len(c) > 60) for c in clauses):
        return []
    if _ALT_HANGUL.search(body):
        clauses = [_neutral_ko_label(c) for c in clauses]
    return clauses


def _neutral_ko_label(clause: str) -> str:
    """Drop the trailing polite/question ending from a Korean clause.

    Multi-word clause: drop the last word (the conjugated verb), e.g.
    "<place> <adverb> <verb+ending>" -> "<place> <adverb>". One-word clause:
    strip just the ending (a one-syllable noun such as a drink name is a
    valid label). Falls back to the verbatim clause only when nothing remains.
    """
    words = clause.split()
    if len(words) > 1:
        out = " ".join(words[:-1])
    else:
        out = _ALT_KO_END.sub("", clause).strip()
    return out or clause


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
            number = int(m.group(1))
            entries.append((number, m.group(2).strip(), idx))
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
    if not runs:
        return []
    run = runs[-1]
    labels = _normalize_option_labels([label for _, label, _ in run])
    return [(number, label, idx) for (number, _, idx), label in zip(run, labels)]


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


def _block_equals_labels(block: List[str], labels: List[str]) -> bool:
    """True when a whole body block restates the labels, bare or enumerated.

    DGN-1242: the duplicate list the author writes above a labeled marker
    carries "N. " prefixes while marker labels do not, so exact equality
    alone never fires. A numbered line matches ONLY when its number equals
    the label's 1-based `position` -- lines 2..3 of a LARGER unrelated list
    can then never satisfy a full-block match (the DGN-984 hijack shape:
    deleting a mid-list slice would orphan the rest of that list).
    """
    block = [line.strip() for line in block]
    if block == labels or _normalize_option_labels(block) == labels:
        return True
    # A body list can add its own numeric wrapper around the authored labels.
    inner = []
    for position, line in enumerate(block, 1):
        match = _NUM_PREFIX_RE.match(line)
        if not match or int(match.group(1)) != position:
            return False
        inner.append(match.group(2).strip())
    return _normalize_option_labels(inner) == labels


def _strip_verbatim_label_block(clean: str, labels: List[str]) -> str:
    """Remove EVERY contiguous line block that restates `labels` in full
    (DGN-992 rev2: bare-marker trailing labels consumed by the buttons must
    not stay in the display as a duplicate list; DGN-1242: a numbered
    restatement "1. label" is the same duplicate and is matched via
    _block_equals_labels -- position-locked, so only a whole 1..N run whose
    text equals the label set exactly, in order, ever matches).

    All-blocks removal (DGN-1242): the incident shape can carry the same
    duplicate twice (numbered list in the body AND bare trailing labels
    under the marker); removing only the last match would leave a triple
    exposure half-fixed. Each removed block is independently a full
    verbatim restatement, so this widens nothing -- a partial or reordered
    match still never strips (DGN-984 no-hijack invariant).

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
    removed = False
    i = len(lines) - n
    while i >= 0:
        if not any(fenced[i:i + n]) and _block_equals_labels(lines[i:i + n], labels):
            del lines[i:i + n]
            del fenced[i:i + n]
            # Seam collapse: the removed block usually sat between blank
            # separator lines; keep exactly one so the display never shows
            # a double blank where the duplicate list used to be.
            if 0 < i < len(lines) and not lines[i - 1].strip() and not lines[i].strip():
                del lines[i]
                del fenced[i]
            removed = True
            i -= n
        else:
            i -= 1
    if removed:
        return "\n".join(lines).rstrip()
    return clean


def _without_rec_marker(label: str) -> str:
    """Label with the locale recommendation marker removed (DGN-1926)."""
    return _norm_ws(label.replace(t("option_rec_marker"), ""))


def _carry_rec_marker(labels: List[str], run_labels: List[str]) -> List[str]:
    """Append the locale rec marker to labels whose body line carries it (DGN-1926, dec-315).

    `run_labels` are the body numbered-run labels. Applies only when the run
    has the same length as `labels` and each body line, marker removed, equals
    its label (so an unrelated run never lends a marker). A label that already
    contains the marker is left alone (no double marker). The marker counts in
    the width budget like any label text: if it tips the keyboard over, the
    existing bundle degrade to number handles applies (no label trimming).
    """
    marker = t("option_rec_marker")
    if len(run_labels) != len(labels):
        return labels
    if any(_without_rec_marker(r) != _norm_ws(o) for r, o in zip(run_labels, labels)
           if marker in r):
        return labels
    out = []
    for opt, rl in zip(labels, run_labels):
        if marker in rl and marker not in opt and _without_rec_marker(rl) == _norm_ws(opt):
            opt = f"{opt} {marker}"
        out.append(opt)
    return out


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
        button_labels = marker_labels
        if run:
            button_labels = _carry_rec_marker(
                marker_labels, [label.strip() for _, label, _ in run]
            )
        overflow = any(
            _overflows_to_handle(f"{i}. {opt}")
            for i, opt in enumerate(button_labels, 1)
        )
        if run:
            run_labels = [label.strip() for _, label, _ in run]
            line_indices = [idx for _, _, idx in run]
            adjacent = all(
                line_indices[i] == line_indices[i - 1] + 1
                for i in range(1, len(line_indices))
            )
            if (
                adjacent
                and [_without_rec_marker(r) for r in run_labels]
                == [_without_rec_marker(m) for m in marker_labels]
                and not overflow
            ):
                drop = set(line_indices)
                lines = clean.split("\n")
                clean = "\n".join(
                    ln for i, ln in enumerate(lines) if i not in drop
                ).rstrip()
        if not overflow:
            # DGN-992 rev2: bare-marker TRAILING labels remain in the body
            # after the marker line is stripped (they came from the lines
            # under the marker) -- the buttons consumed them, so remove the
            # matching block to avoid a duplicate display. Same mechanism
            # also removes any verbatim restatement of the marker labels,
            # bare or "N. "-numbered (DGN-1242), and runs even after the
            # run-strip above so no second duplicate survives. Only an
            # EXACT full-block match is touched (numbered lines position-
            # locked); a non-matching body always survives intact
            # (fail-safe, DGN-984 no-hijack).
            clean = _strip_verbatim_label_block(clean, marker_labels)
        return clean, filter_exit_hatch_options(button_labels)
    if not run:
        return clean, []
    options = [label.strip() for _, label, _ in run]
    line_indices = [idx for _, _, idx in run]
    adjacent = all(
        line_indices[i] == line_indices[i - 1] + 1 for i in range(1, len(line_indices))
    )
    if not adjacent:
        return clean, filter_exit_hatch_options(options)
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
        return clean, filter_exit_hatch_options(options)
    drop = set(line_indices)
    lines = clean.split("\n")
    display = "\n".join(ln for i, ln in enumerate(lines) if i not in drop).rstrip()
    return display, filter_exit_hatch_options(options)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def keyboard_labels(text: str) -> List[str]:
    """Labels of the keyboard an AUTHORED [[OPTIONS]] marker would build.

    Same source priority as the seat (_send_smart -> strip_consumed_options):
    marker-declared labels first, then the last numbered run. [] when the
    text carries no marker or the marker yields no labels.
    """
    if not has_options_marker(text):
        return []
    labels = extract_marker_labels(text) or extract_options(
        strip_options_marker(text)[0]
    )
    return filter_exit_hatch_options(
        [_norm_ws(label) for label in labels if _norm_ws(label)], log=False
    )


def _drop_keyboard(text: str) -> str:
    """The text with its marker lines and verbatim label block removed."""
    clean, _ = strip_options_marker(text)
    labels = extract_marker_labels(text) or extract_options(clean)
    if labels:
        clean = _strip_verbatim_label_block(clean, labels)
    return clean.strip()


def subtract_delivered(content: str, delivered: Optional[str]) -> str:
    """DGN-1732: drop from a turn's final text what the turn already delivered.

    The dispatch-return result-first push (DGN-1687/1715) puts the turn's
    first text on the owner's screen before the turn ends; the finalize then
    must not send it again. Same lossless rule as the DGN-947 fold drop:
    only paragraphs that EXACTLY reproduce a delivered paragraph
    (whitespace-normalized, full-paragraph match, containment forbidden) are
    removed; every new paragraph survives in order.

    Keyboards: a final keyboard whose labels equal the delivered keyboard's
    labels is the same menu the owner can already tap -- its marker and label
    block are removed, so one turn never builds the same keyboard twice. A
    keyboard with different labels is new content and is kept intact.

    Returns "" when nothing new remains. ``delivered`` None/empty -> content
    unchanged (a plain turn). Never raises (content unchanged on error).
    """
    try:
        if not content or not delivered or not delivered.strip():
            return content
        delivered_labels = keyboard_labels(delivered)
        same_keyboard = bool(delivered_labels) and (
            keyboard_labels(content) == delivered_labels
        )
        body = _drop_keyboard(content) if same_keyboard else content
        seen = {
            _norm_ws(p)
            for src in (
                (delivered, _drop_keyboard(delivered))
                if delivered_labels else (delivered,)
            )
            for p in re.split(r"\n\s*\n", src)
            if _norm_ws(p)
        }
        kept = [
            p for p in re.split(r"\n\s*\n", body)
            if _norm_ws(p) and _norm_ws(p) not in seen
        ]
        return "\n\n".join(p.strip("\n") for p in kept).strip()
    except Exception:
        return content


def resolve_choice(data: str, inline_keyboard: Optional[list]) -> str:
    """Resolve the full option label from the tapped button's own text (DGN-665).

    build_option_keyboard falls back to the number-only callback_data
    "opt:{i}" when "opt:{i}. {label}" exceeds Telegram's 64-byte callback_data
    limit; Korean labels (3 bytes/char) almost always trip this, dropping the
    label. The button TEXT always carries the full "N. label" regardless of
    truncation, so we recover the label from the keyboard.

    Lease-repair buttons keep their short confirmed label and carry the
    held kit ID in opt:repair-update:<kit>. Append that ID to the owner's
    text through this same chat-turn path; no recovery is executed here.
    Drift checks likewise carry a validated agent slug in opt:drift-check:<slug>.

    Given the raw callback_data ("opt:...") and query.message.reply_markup
    .inline_keyboard, find the button whose callback_data matches and return its
    text. Falls back to the post-"opt:" payload when the keyboard is absent or
    no button matches (guards a truncated keyboard and non-opt callers).
    """
    drift = re.fullmatch(r"opt:drift-check:([a-z][a-z0-9_-]{0,31})", data)
    repair = re.fullmatch(r"opt:repair-update:([a-z][a-z0-9_-]{0,31})", data)
    target = repair or drift
    fallback = (f"Repair update ({repair[1]})" if repair else
                f"{t('update_drift_check_button')} ({drift[1]})" if drift else
                data.split(":", 1)[1] if ":" in data else data)
    if not inline_keyboard:
        return fallback
    for row in inline_keyboard:
        for button in row:
            if getattr(button, "callback_data", None) == data:
                return f"{button.text} ({target[1]})" if target else button.text
    return fallback


def is_slash_command(text: str) -> bool:
    """Telegram command syntax, including bot mentions and arguments.

    Text-only replays may lack BOT_COMMAND entities. Keep filesystem paths
    such as /tmp/retro.txt on the ordinary text path.
    """
    return re.match(r"^/[A-Za-z0-9_]{1,32}(?:@[A-Za-z0-9_]+)?(?:\s|$)", text or "") is not None


def match_typed_choice(text: str, buttons: List[str],
                       labels: Optional[List[str]] = None) -> Optional[str]:
    """DGN-2062: the button a typed message picks, as a tap would show it.

    `buttons` are the keyboard's button texts in order; `labels` the menu's
    own labels when the bridge recorded them (a degraded number-handle keyboard has
    no label on its buttons). A match is the option number alone ("1",
    "1.") in range, or one option's label / button text. Label comparison is
    deliberately forgiving for owner-typed answers: markdown, recommendation
    markers, whitespace and dots are ignored, then case and Unicode are folded.
    Thus ``dev7shipnow`` selects ``dev.7 ship now (recommended)`` without
    allowing the menu to leak into the next turn. Returns that button's text
    (what a tap's "Selected" line shows), or None. Only the first line is
    matched; later lines remain part of the normal incoming turn."""
    buttons = [b for b in (buttons or []) if isinstance(b, str)]
    raw = (text or "").strip()
    if not buttons or not raw or is_slash_command(raw):
        return None
    # The first line selects; subsequent lines are the user's explanation.
    raw = raw.splitlines()[0].strip()
    m = re.fullmatch(r"(\d+)[.)]?", raw)
    if m:
        n = int(m.group(1))
        return buttons[n - 1] if 1 <= n <= len(buttons) else None

    def norm(v: str) -> str:
        v = unicodedata.normalize("NFC", strip_markdown_label(str(v)))
        # The recommendation suffix is presentation metadata, not part of the
        # choice the owner must reproduce. Recognize both shipped locales as
        # persisted menus can outlive a locale change.
        markers = {t("option_rec_marker"), "(recommended)", "(recommend)"}
        for marker in markers:
            if marker:
                v = v.replace(marker, "")
        return re.sub(r"[.\s]+", "", v).casefold()

    want = norm(raw)
    for i, button in enumerate(buttons, 1):
        forms = {norm(button)}
        prefix = "%d. " % i
        if button.startswith(prefix):
            forms.add(norm(button[len(prefix):]))
        if labels and len(labels) == len(buttons) and isinstance(labels[i - 1], str):
            forms.add(norm(labels[i - 1]))
        if want in forms:
            return button
    return None


# --- DGN-1934: one live decision menu ---------------------------------------
#
# Every [[OPTIONS]] keyboard used to stay tappable until its own tap, so a
# chat could hold several unanswered menus and a late tap on an old one was
# ambiguous. A newer [[OPTIONS]] keyboard now RETIRES every older unanswered
# one in the same chat: its buttons are dropped and a one-line pointer to the
# newer message is appended (bot._retire_live_menus). The registry is
# in-memory: after a bridge restart older keyboards are simply not tracked
# (they stay as before -- the stale-button gate still covers them).
#
# DGN-1997 (2.6.4 stopgap): the retire step is switched off -- a new menu
# never retires an unrelated one, so different questions keep their own
# buttons on their own message.
#
# DGN-1997 (v2.7): the registry tracks every live menu with its labels and
# its question body. Only a menu that is provably the SAME question -- an
# IDENTICAL button set AND an identical normalized body -- is consolidated
# (bot._consolidate_duplicate_menu): one live copy is sent as a reply to the
# original question message and the older duplicates lose their buttons.
# Generic labels ("1. confirm / 2. edit") repeat across different questions,
# so labels alone never match (vendors/telegram.md: never merge options of
# different questions). The DGN-1952 ledger id is NOT an identity: the ledger
# allocates a fresh id for every menu turn and its pending pointer is matched
# on labels only, so a shared id can only be a stale pointer.
_LIVE_MENU_CAP = 32


def menu_labels_key(labels: Optional[List[str]]) -> tuple:
    """DGN-1997: identity of a button set -- the labels in order, markdown
    stripped, whitespace collapsed, case folded."""
    return tuple(
        " ".join(strip_markdown_label(str(label)).split()).casefold()
        for label in (labels or [])
    )


def menu_body_key(body: Optional[str]) -> str:
    """DGN-1997: identity of a question body -- markdown stripped per line,
    whitespace collapsed, case folded. Empty when there is no body (an
    empty key never matches)."""
    lines = (strip_markdown_label(line) for line in (body or "").splitlines())
    return " ".join(" ".join(lines).split()).casefold()


class LiveMenuRegistry:
    """Per-chat list of the [[OPTIONS]] keyboards that are still live.

    Each entry: message id, the labels key, the question body key, and the
    ORIGINAL question message it was consolidated from (its own id when it
    is the original).
    In-memory: after a bridge restart older keyboards are not tracked."""

    def __init__(self) -> None:
        self._live: dict = {}

    def add(self, chat_id: int, message_id: int,
            labels: Optional[List[str]] = None,
            origin_id: Optional[int] = None,
            body_key: str = "") -> None:
        """Record `message_id` as a live menu next to the others.
        `body_key` is menu_body_key() of its question body."""
        entries = [e for e in self._live.get(chat_id, [])
                   if e["message_id"] != message_id]
        entries.append({
            "message_id": message_id,
            "key": menu_labels_key(labels),
            "body_key": body_key or "",
            "origin_id": origin_id if isinstance(origin_id, int) else message_id,
        })
        self._live[chat_id] = entries[-_LIVE_MENU_CAP:]

    def duplicates(self, chat_id: int, labels: Optional[List[str]],
                   body_key: str = "") -> List[dict]:
        """Live menus that are the SAME question: identical button set AND
        identical body key (oldest first). An empty label set or an empty
        body key never matches -- any doubt is no duplicate."""
        key = menu_labels_key(labels)
        if not key or not body_key:
            return []
        return [dict(e) for e in self._live.get(chat_id, [])
                if e["key"] == key and e.get("body_key") == body_key]

    def body_key(self, chat_id: int, message_id: int) -> str:
        """The body key recorded for a live menu ("" when not tracked)."""
        for e in self._live.get(chat_id, []):
            if e["message_id"] == message_id:
                return e.get("body_key") or ""
        return ""

    def register(self, chat_id: int, message_id: int) -> List[int]:
        """DGN-1934 (unused since DGN-1997): record `message_id` as the
        chat's only live menu; return the older ids it supersedes (oldest
        first). They are dropped from the registry."""
        older = [e["message_id"] for e in self._live.get(chat_id, [])
                 if e["message_id"] != message_id]
        self._live[chat_id] = [{"message_id": message_id, "key": (),
                                "body_key": "", "origin_id": message_id}]
        return older[-_LIVE_MENU_CAP:]

    def answered(self, chat_id: int, message_id: int) -> None:
        entries = self._live.get(chat_id)
        if not entries:
            return
        entries[:] = [e for e in entries if e["message_id"] != message_id]
        if not entries:
            self._live.pop(chat_id, None)

    def live(self, chat_id: int) -> List[int]:
        return [e["message_id"] for e in self._live.get(chat_id, [])]


def superseded_body_html(body_html: Optional[str], pointer: str, limit: int = 4096) -> Optional[str]:
    """DGN-1934: the retired menu's body with the pointer line appended.

    `body_html` is the message's own text_html/caption_html (as Telegram
    returns it); the pointer is escaped plain copy. None when there is no
    body or the result would exceed `limit` -- the caller then only drops
    the keyboard."""
    if not isinstance(body_html, str) or not body_html.strip():
        return None
    import html as _html

    tail = _html.escape(pointer or "").strip()
    if not tail:
        return None
    combined = body_html.rstrip() + "\n\n" + tail
    return combined if len(combined) <= limit else None


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


def _number_handle_label(number: int) -> str:
    """Localized number-handle button text (ko "N번" / en "No.N", DGN-881)."""
    return t("option_number_handle").format(n=number)


def is_number_handle(text: str) -> bool:
    """True when `text` is a degraded number-handle label, not a real sentence.

    Same authority as _number_handle_label -- the pattern is derived from the
    live i18n template, so a catalog edit cannot silently desync it. Callers
    that QUOTE a button label back to the user need this: telling the owner to
    retype "3번" / "No.3" is useless (the full label lives in the message body
    on the overflow path, DGN-881), so they degrade to a label-free wording.
    A bare number is treated as a handle too -- resolve_choice's keyboard-absent
    fallback returns exactly that.
    """
    if not text:
        return True
    stripped = text.strip()
    if stripped.isdigit():
        return True
    template = t("option_number_handle")
    pattern = "".join(
        r"\d+" if part == "{n}" else re.escape(part)
        for part in re.split(r"(\{n\})", template)
    )
    return re.fullmatch(pattern, stripped) is not None


def _shorten_button_label(label: str) -> str:
    """Return the button text for a numbered option label (DGN-881).

    Single-label API: kept as-is for callers that need a per-label decision
    in isolation (unit tests). build_option_keyboard does NOT call this for
    its bundle degrade decision (DGN-1092) -- it uses _overflows_to_handle
    across the whole option set first so a keyboard never mixes full labels
    with number handles.

    Labels are thin tokens by contract (vendor contract, vendors/telegram.md
    Options rules): descriptions and the
    recommendation marker live in the message body, so no separator parsing
    happens here. Within the width contract the label passes through
    untouched. On overflow the button degrades to a localized number handle
    (ko "N번" / en "No.N", i18n key option_number_handle) -- the full label
    stays readable in the body (strip_consumed_options overflow-keep).
    The number prefix always exists: build_option_keyboard prepends "{i}. ".
    """
    if _label_width(label) <= _BUTTON_LABEL_MAX_WIDTH:
        return label
    number = int(re.match(r"^(\d+)", label).group(1))
    return _number_handle_label(number)


# DGN-775: regexes for stripping inline markdown from button label text.
# Applied before display-width trimming so markdown syntax never counts toward
# the width budget or leaks into the Telegram button surface.
_LABEL_MD_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# Underscore emphasis only at word boundaries (CommonMark: intraword "_" is
# literal) -- ALLOW_OUTSIDE_ONCE / my_file must keep their underscores.
_LABEL_MD_BOLD_UNDER = re.compile(r"(?<![0-9A-Za-z])__(.+?)__(?![0-9A-Za-z])", re.DOTALL)
_LABEL_MD_ITALIC_STAR = re.compile(r"\*(.+?)\*", re.DOTALL)
_LABEL_MD_ITALIC_UNDER = re.compile(r"(?<![0-9A-Za-z])_(.+?)_(?![0-9A-Za-z])", re.DOTALL)
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


# DGN-2033: closed whole-label policy; mirrored by the portable output gate.
# The bridge ships standalone without routines/. Fixture parity pins the set.
EXIT_HATCH_LABELS = frozenset({
    "\uc77c\ub2e8 \uc5ec\uae30\uae4c\uc9c0", "\ub098\uc911\uc5d0", "\uadf8\ub9cc", "\ub2e4\uc74c\uc5d0",
})


def filter_exit_hatch_options(options: List[str], *, log: bool = True) -> List[str]:
    """Drop exact exit labels; a reduced menu needs at least two choices.

    Match the rendered label, ignoring whitespace and a trailing rec marker.
    Untouched single-option menus retain their existing confirmation behavior.
    """
    kept = []
    dropped = []
    for label in options:
        plain = _norm_ws(strip_markdown_label(label))
        plain = re.sub(r"\s*\((?:rec|\ucd94\ucc9c)\)$", "", plain).strip()
        if plain in EXIT_HATCH_LABELS:
            dropped.append(plain)
        else:
            kept.append(label)
    if not dropped:
        return options
    if log:
        for label in dropped:
            logging.getLogger(__name__).warning(
                "EXIT_HATCH_OPTION action=drop label=%r remaining=%d keyboard=%s",
                label, len(kept), "suppressed" if len(kept) < 2 else "kept",
            )
    return kept if len(kept) >= 2 else []


def build_option_keyboard(options: List[str]) -> Optional[InlineKeyboardMarkup]:
    """Build inline buttons; callback 'opt:{text}' with 'opt:{i}' fallback.

    DGN-1928: two or more buttons ALWAYS carry "N. " (every shape: labeled
    marker, trailing lines, consumed body list); a lone button shows the bare
    label. The "N. " prefix counts toward the width budget.

    DGN-1092: the degrade decision is bundle-level, not per-button. A first
    pass checks _overflows_to_handle across every reconstructed "{i}. {label}"
    in the set; if ANY one would overflow, EVERY button in this keyboard
    renders as a number handle (ko "N번" / en "No.N") -- never a mix of full
    labels and number handles in the same keyboard. If none overflow, every
    button shows its full label untouched. callback_data and resolve_choice
    still use the index-based "opt:{i}" fallback for Korean/CJK labels, so the
    degrade decision has no effect on choice resolution.

    DGN-775: markdown syntax is stripped from the label before width trimming
    so button text is always plain text (never leaks **bold** etc. to Telegram).
    """
    options = filter_exit_hatch_options(options)
    if not options:
        return None
    numbered = len(options) > 1
    plain = [strip_markdown_label(opt) for opt in options]
    if numbered:
        labels = [f"{i}. {label}" for i, label in enumerate(plain, 1)]
        degrade_all = any(_overflows_to_handle(label) for label in labels)
    else:
        labels = plain
        degrade_all = False
    buttons = []
    for i, label in enumerate(labels, 1):
        cb_data = f"opt:{label}"
        # DGN-1813: an unnumbered digit-only label ("2") would collide with
        # another button's index fallback "opt:2" -- give it its own index.
        if len(cb_data.encode("utf-8")) > 64 or label.strip().isdigit():
            cb_data = f"opt:{i}"
        button_text = _number_handle_label(i) if degrade_all else label
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
        if not cli_path:
            # DGN-1814: the shared resolver (lazy: config needs PROJECT_ROOT).
            from bridge.config import resolve_claude_cli

            cli_path = resolve_claude_cli()
        claude_bin = cli_path
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
