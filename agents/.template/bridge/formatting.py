"""Text formatting: code-block splitting, length splitting, send_file:: marker.

These are pure helpers (no Telegram I/O) so they are easy to unit-test. The bot
wires them to actual send calls.
"""

import html
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# [[OPTIONS]] marker constant lives HERE (not in bridge.options) so this module
# stays importable without python-telegram-bot installed. bridge.options pulls
# in `telegram` at import time; the shell sanitize hop (routines/push.sh ->
# sanitize_message_for_telegram) must work outside the bridge venv, and a
# transitive telegram import would silently disable the whole sanitizer
# (DGN-822 P2). bridge.options re-imports this constant, so existing
# `from bridge.options import OPTIONS_MARKER` consumers are unaffected.
OPTIONS_MARKER = "[[OPTIONS]]"

# DGN-924: initial idrill step1 keyboard marker. An agent arming an idrill
# drilldown emits a standalone line `[[IDRILL:<arm_id>]]`; the render layer
# consumes it and sends the step1 message (arm.step_text["1"] + a keyboard
# built from arm.step_buttons["1"]). Homomorphic to [[OPTIONS]] (marker ->
# keyboard); NO file watcher, NO proactive send. arm_id is validated at render
# time ([0-9a-f]{8}); a missing/invalid arm fails soft (marker just dropped).
# Standalone line, arm_id captured. Anchored full-line (^...$) so inline prose
# is never matched.
IDRILL_MARKER_RE = re.compile(r"^\[\[IDRILL:([0-9a-f]{8})\]\]$")


def _i18n(key: str, fallback: str) -> str:
    """Resolve a user-facing copy constant via bridge.i18n, fail-open.

    DGN-851: fold captions/markers are locale data and live in the bridge
    i18n catalogs (ko carries the LOCKED owner copy). This module MUST stay
    importable OUTSIDE the bridge venv (push.sh sanitize hop, DGN-822):
    bridge.i18n pulls bridge.config (pydantic/dotenv, venv-only), so the
    import is guarded and any failure -- or a missing key (t() echoes the
    key) -- degrades to the previous hardcoded copy (zero-delta fallback).
    """
    try:
        from bridge.i18n import t
    except Exception:
        return fallback
    try:
        value = t(key)
    except Exception:
        return fallback
    return fallback if value == key else value

# A reply only sends files when it contains a line whose stripped form starts
# with this prefix. Bare prose paths are never sent.
SEND_FILE_MARKER = "send_file::"

# DGN-376: link previews are disabled by default on every outbound text send.
# A reply opts back in with a standalone line starting with this marker (the
# line is stripped before sending), restoring Telegram's default preview for
# that reply -- meant for intentional deep links that should show a card.
LINK_PREVIEW_MARKER = "link_preview::"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_FILE_BYTES = 10 * 1024 * 1024


# =============================================================================
# DGN-719 phase-1: bridge output CONTRACT (semantic core <-> render bridge seam)
# =============================================================================
#
# The agent SEMANTIC CORE (RULES marker vocabulary) emits channel-neutral intent
# markers; this RENDER BRIDGE transpiles them to Telegram syntax. Phase-1 is a
# backward-compatible DEFENSIVE layer only -- RULES is NOT retrained here. It
# adds a version handshake (a1), a containment net for markers this render layer
# does not recognize (a2), a typed fold intent block (a3), and formalizes the
# link_preview:: control marker as a contract construct (a4).
#
# a1 CONTRACT_VERSION -- handshake between the semantic core (marker vocabulary)
#   and this render layer. DORMANT BY DESIGN (dec-108): this is a phase-2 API
#   surface only. Nothing stamps or checks it at runtime today -- no producer
#   stamps a contract version (the semantic core is an LLM and cannot reliably
#   self-stamp yet) and no consumer calls check_contract_skew(). It exists so a
#   future non-LLM producer / harness can make version-skew (RULES advances a
#   marker while a .dogany-preserve-frozen render layer stays behind -- the
#   DGN-696 drift shape) DETECTABLE. The real runtime defense against skew is
#   a2 (contain_unknown_markers), which needs no handshake. Bump this only when
#   the recognized marker vocabulary changes.
CONTRACT_VERSION = 1

# a2/a4 -- the marker vocabulary THIS render layer recognizes. Every neutral
# `word::` control marker the core may emit is registered here. A marker not in
# this set is treated as UNKNOWN by the containment net (a2) and downgraded to a
# safe literal instead of being raw-relayed. Known markers are stripped upstream
# (strip_*_marker / strip_display_markers) before prose reaches the converter,
# so they never reach the containment net as "unknown".
RECOGNIZED_COLON_MARKERS = frozenset(
    {SEND_FILE_MARKER, LINK_PREVIEW_MARKER, "fold::"}
)
# Bracket-form control markers ([[OPTIONS]] etc.) the render layer recognizes.
RECOGNIZED_BRACKET_MARKERS = frozenset({OPTIONS_MARKER})

# a3 -- fold typed-block markers. compose_fold_block() emits this pair around a
# summary + expandable body; render_fold_block() (or the existing DGN-619 `>! `
# path) transpiles it to <blockquote expandable>. The typed form keeps the fold
# INTENT (summary = required structural field, body = expandable content)
# explicit so a non-Telegram renderer could map it to its own collapsible
# primitive (e.g. Slack). Both markers are single standalone lines.
FOLD_OPEN_MARKER = "fold::"
FOLD_CLOSE_MARKER = "fold::end"

# a2 -- shapes the containment net inspects. A standalone marker-shaped line is:
#   * a neutral colon-marker: `word::` (lowercase word + "::") standing alone or
#     followed by a WHITESPACE-separated payload, OR
#   * a bracket-marker: `[[WORD]]`.
# Anchored to a STANDALONE line (full-line match after strip) so inline prose
# that merely contains "::" (http://, "Note:: x" mid-sentence, code) is never
# touched -- every real Dogany marker is emitted on its own line, matching the
# existing strip_* conventions. The colon-marker also REQUIRES the "::" to be
# followed by end-of-line or whitespace, so ordinary prose like "a::b" (payload
# abutting the colons, never a real marker shape) is left alone. This keeps
# backward-compat parity intact.
_COLON_MARKER_LINE_RE = re.compile(r"^([a-z][a-z0-9_]*::)(|\s.*)$")
_BRACKET_MARKER_LINE_RE = re.compile(r"^(\[\[[A-Z][A-Z0-9_]*\]\])$")

# Zero-width space (U+200B) used to break an unknown marker token so no later
# marker scan re-parses it, while it stays invisible to the reader. Declared as
# an escape so this source file remains pure ASCII.
_ZWSP = "\u200b"


def check_contract_skew(core_version: Optional[int]) -> bool:
    """a1: detect a semantic-core / render-bridge version skew.

    DORMANT phase-2 API surface (dec-108): no runtime caller exists today --
    no producer stamps a contract version (the semantic core is an LLM and
    cannot reliably self-stamp yet), so nothing invokes this check. It is kept
    as the forward observability handshake for a future stamping producer.

    core_version is the CONTRACT_VERSION the emitting semantic core was authored
    against (None = un-stamped / legacy core, treated as compatible). Returns
    True when a skew is detected (core NEWER than this render layer, i.e. the
    render layer is frozen behind an advanced RULES vocabulary -- the DGN-696
    case) and logs one warning; False otherwise. Non-fatal: the containment net
    (a2, contain_unknown_markers) is the real runtime safety; this is only the
    dormant observability handshake.
    """
    if core_version is None:
        return False
    if core_version > CONTRACT_VERSION:
        logger.warning(
            "Contract version skew: core=%d render=%d -- render layer is behind "
            "the semantic core; unknown markers will be contained (a2).",
            core_version,
            CONTRACT_VERSION,
        )
        return True
    return False


def contain_unknown_markers(text: str) -> Tuple[str, int]:
    """a2: downgrade unrecognized neutral/semantic markers to safe literals.

    A standalone line shaped like a control marker (`word::` or `[[WORD]]`) that
    is NOT in the recognized vocabulary is the version-skew hazard: a marker the
    core emits from an advanced RULES that this frozen render layer never learned
    to transpile. Rather than raw-relay it to Telegram (where it could look like
    a broken directive or, worse, smuggle syntax), we NEUTRALIZE it: the marker
    token gets a zero-width word break inserted after its first char so it can
    never be re-parsed as a live marker downstream, and it survives html.escape
    as visible literal text. Recognized markers pass through untouched (they are
    stripped by their own handlers before this point).

    Returns (contained_text, count_downgraded). Logs one line with the count
    (never the payload) when a downgrade happens, so skew stays observable.
    """
    if not text or ("::" not in text and "[[" not in text):
        return text, 0
    downgraded = 0
    out: List[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        neutralized = _neutralize_if_unknown_marker(stripped)
        if neutralized is None:
            out.append(line)
            continue
        # Preserve the original leading indentation of the line.
        indent = line[: len(line) - len(line.lstrip())]
        out.append(indent + neutralized)
        downgraded += 1
    if downgraded:
        # debug (not warning) level -- dec-108 a2: ordinary prose lines like
        # "todo:: x" match the marker shape and would WARN-spam on every send;
        # containment is routine hygiene, not an incident.
        logger.debug(
            "Contained %d unknown output marker(s) (version skew?); downgraded "
            "to safe literal.",
            downgraded,
        )
    return "\n".join(out), downgraded


def _neutralize_if_unknown_marker(stripped: str) -> Optional[str]:
    """Return the neutralized literal for an UNKNOWN marker line, else None.

    None means the line is not an unrecognized marker (ordinary prose, or a
    recognized marker) and must be left byte-identical.
    """
    bm = _BRACKET_MARKER_LINE_RE.match(stripped)
    if bm is not None:
        if bm.group(1) in RECOGNIZED_BRACKET_MARKERS:
            return None
        # "[[WORD]]" -> "[<zwsp>[WORD]]" : the doubled bracket is broken so no
        # downstream pass reads it as a live [[..]] marker; renders literally.
        return "[" + _ZWSP + stripped[1:]
    cm = _COLON_MARKER_LINE_RE.match(stripped)
    if cm is not None:
        token = cm.group(1)  # e.g. "wibble::"
        if token in RECOGNIZED_COLON_MARKERS:
            return None
        # "wibble:: x" -> "w<zwsp>ibble:: x" : the marker word is broken by a
        # zero-width space so `startswith("word::")` / re marker scans miss it,
        # while the text reads unchanged to the user.
        return token[0] + _ZWSP + stripped[1:]
    return None


def compose_fold_block(summary: str, body: str) -> str:
    """a3: build a fold TYPED INTENT block (summary + expandable body).

    Emits the neutral typed form:
        fold:: <summary>
        <body...>
        fold::end
    render_fold_block() (below) transpiles this to the existing DGN-619 `>! `
    expandable-blockquote path; a non-Telegram renderer can instead map the
    (summary, body) intent to its own collapsible primitive. summary is the
    REQUIRED structural field (the collapsed preview); an empty summary yields
    an empty block (nothing to fold).
    """
    if not summary.strip():
        return ""
    lines = [FOLD_OPEN_MARKER + " " + summary.strip()]
    if body:
        lines.append(body.rstrip("\n"))
    lines.append(FOLD_CLOSE_MARKER)
    return "\n".join(lines)


_FOLD_OPEN_LINE_RE = re.compile(r"^fold::[ \t]*(.*)$")


def render_fold_block(text: str) -> str:
    """a3: transpile fold:: typed blocks to the DGN-619 `>! ` fold render.

    Each `fold:: <summary>` ... `fold::end` block becomes an expandable
    blockquote run: the summary is the first quoted line carrying the `>! `
    marker (the collapsed preview), each body line a `> ` quote line, blank
    body lines a bare `>`. The result flows through markdown_to_telegram_html
    unchanged (backward-compat: same output as a hand-written `>! ` run). Text
    outside fold blocks is left byte-identical. A fold:: with no matching
    fold::end is left literal (never a dangling open blockquote).
    """
    if FOLD_OPEN_MARKER not in text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _FOLD_OPEN_LINE_RE.match(lines[i].strip())
        # Only a bare "fold::" open line (not "fold::end") starts a block.
        if m is not None and lines[i].strip() != FOLD_CLOSE_MARKER:
            # Find the matching close line.
            j = i + 1
            while j < n and lines[j].strip() != FOLD_CLOSE_MARKER:
                j += 1
            if j >= n:
                # Unterminated: leave the open line literal, continue past it.
                out.append(lines[i])
                i += 1
                continue
            summary = m.group(1).strip()
            body = lines[i + 1 : j]
            quoted: List[str] = []
            if summary:
                quoted.append(">! " + summary)
            for k, ln in enumerate(body):
                if not ln.strip():
                    quoted.append(">")
                elif k == 0 and not summary:
                    quoted.append(">! " + ln)
                else:
                    quoted.append("> " + ln)
            out.extend(quoted)
            i = j + 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def extract_send_marker_paths(content: str) -> List[str]:
    """Return raw path strings from each send_file:: marker line, in order."""
    if not content:
        return []
    out: List[str] = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith(SEND_FILE_MARKER):
            raw = stripped[len(SEND_FILE_MARKER):].strip()
            if raw:
                out.append(raw)
    return out


def strip_send_markers(text: str) -> Tuple[str, bool]:
    """Drop every send_file:: marker line from user-facing text."""
    if not text:
        return text, False
    lines = text.split("\n")
    kept = [ln for ln in lines if not ln.strip().startswith(SEND_FILE_MARKER)]
    if len(kept) == len(lines):
        return text, False
    return "\n".join(kept).rstrip(), True


# DGN-159: the model can emit tool-call syntax INSIDE a text block (a text
# block, not a real ToolUseBlock). The tool still runs, but the raw markup
# rides along in the assistant's visible text and the bridge relays it verbatim
# to Telegram. This is a relay-side safety net that strips that markup from any
# outbound text.
#
# We recognize two structurally-anchored shapes:
#   1. A standalone "call" line immediately followed by one or more <invoke ...>
#      ... </invoke> blocks (each may carry <parameter ...> children). The "call"
#      lead-in line is consumed together with the invoke block(s).
#   2. An orphan <invoke ...> ... </invoke> block with no "call" lead-in.
# Both the plain (<invoke>) and antml-namespaced (<invoke>) tag spellings
# are covered.
#
# Conservative-by-design: the regex is anchored on the real tag structure
# (<invoke name="...">...</invoke>), so ordinary prose that merely mentions the
# word "invoke" is never touched.
#
# FENCED-CODE POLICY: text the agent deliberately wrote inside a ``` ... ```
# fenced block is left untouched -- if the user asked to SEE tool-call syntax
# (e.g. documenting it), stripping it would corrupt an intended answer. So we
# strip ONLY in the prose regions OUTSIDE complete fenced blocks. Markup that
# leaks unfenced (the actual bug) is still removed. An unterminated/opening
# fence with no closer is treated as "not a real code block" so a stray ``` can
# never be used to smuggle markup past the filter.

# One <invoke .../> ... </invoke> block, plain or antml-namespaced. Non-greedy
# body so consecutive blocks are matched individually (DOTALL applied at compile).
_INVOKE_BLOCK = r"<(?:antml:)?invoke\b[^>]*>.*?</(?:antml:)?invoke>"
# Optional standalone "call" lead-in line (the literal word on its own line,
# possibly indented), then one or more invoke blocks separated by whitespace;
# OR an orphan invoke block with no lead-in. Flags (I|M|S) set at compile time.
_TOOLCALL_RE = re.compile(
    r"^[ \t]*call[ \t]*\r?\n"                # a lone "call" line
    r"(?:\s*" + _INVOKE_BLOCK + r")+"        # >= 1 invoke block after it
    r"|"                                      # OR
    r"(?:" + _INVOKE_BLOCK + r")",            # an orphan invoke block, no lead-in
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _collapse_blank_runs(text: str) -> str:
    """Collapse 3+ consecutive newlines (left by an excised block) to 2."""
    return re.sub(r"\n{3,}", "\n\n", text)


def _strip_toolcall_in_prose(prose: str) -> Tuple[str, int]:
    """Strip recognizable tool-call markup from a non-fenced prose region.

    Returns (cleaned_prose, bytes_removed). bytes_removed counts the UTF-8
    length of the removed markup only (never logged content), so a leak stays
    observable without echoing what leaked.
    """
    removed = 0

    def _sub(match: "re.Match") -> str:
        nonlocal removed
        removed += len(match.group(0).encode("utf-8"))
        return ""

    cleaned = _TOOLCALL_RE.sub(_sub, prose)
    if removed:
        cleaned = _collapse_blank_runs(cleaned)
    return cleaned, removed


def strip_toolcall_markup(text: str) -> str:
    """Remove leaked tool-call markup from outbound assistant text.

    Only prose OUTSIDE complete ``` fenced blocks is scrubbed (see the policy
    note above). Surrounding prose is preserved; leftover blank runs collapse.
    Logs one line with the total bytes removed (never the content) when a strip
    happens, so leaks remain visible in bot.log.
    """
    if not text or ("invoke" not in text):
        # Fast path: no tag substring at all -> byte-identical passthrough.
        return text
    total_removed = 0
    out_parts: List[str] = []
    # Walk the text splitting on complete fenced blocks; scrub prose, keep code.
    pos = 0
    n = len(text)
    fence = "```"
    while pos < n:
        start = text.find(fence, pos)
        if start == -1:
            prose = text[pos:]
            cleaned, removed = _strip_toolcall_in_prose(prose)
            total_removed += removed
            out_parts.append(cleaned)
            break
        end = text.find(fence, start + len(fence))
        if end == -1:
            # No closing fence: this is not a real code block. Scrub the rest as
            # prose so an opening ``` cannot shield leaked markup.
            prose = text[pos:]
            cleaned, removed = _strip_toolcall_in_prose(prose)
            total_removed += removed
            out_parts.append(cleaned)
            break
        # Prose before the fence: scrub it.
        prose = text[pos:start]
        cleaned, removed = _strip_toolcall_in_prose(prose)
        total_removed += removed
        out_parts.append(cleaned)
        # The fenced block itself (fences included): keep verbatim.
        out_parts.append(text[start : end + len(fence)])
        pos = end + len(fence)
    if not total_removed:
        return text
    logger.warning("Stripped leaked tool-call markup (%d bytes removed)", total_removed)
    return "".join(out_parts)


def strip_link_preview_marker(text: str) -> Tuple[str, bool]:
    """Drop link_preview:: opt-in lines; True when at least one was present."""
    if not text:
        return text, False
    lines = text.split("\n")
    kept = [ln for ln in lines if not ln.strip().startswith(LINK_PREVIEW_MARKER)]
    if len(kept) == len(lines):
        return text, False
    return "\n".join(kept).rstrip(), True


def strip_idrill_marker(text: str) -> Tuple[str, Optional[str]]:
    """DGN-924: remove a standalone [[IDRILL:<arm_id>]] marker line.

    Returns (clean_text, arm_id | None). Only the FIRST well-formed marker is
    consumed (one initial keyboard per turn); a valid arm_id is returned so the
    caller can render the step1 keyboard. Trailing whitespace is rstripped when
    a marker was removed, mirroring strip_options_marker.
    """
    if not text or "[[IDRILL:" not in text:
        return text, None
    lines = text.split("\n")
    arm_id: Optional[str] = None
    kept: List[str] = []
    for ln in lines:
        m = IDRILL_MARKER_RE.match(ln.strip())
        if m is not None and arm_id is None:
            arm_id = m.group(1)
            continue  # drop the marker line
        kept.append(ln)
    if arm_id is None:
        return text, None
    return "\n".join(kept).rstrip(), arm_id


def strip_display_markers(text: str) -> str:
    """Drop [[OPTIONS]], [[IDRILL:..]], send_file:: and link_preview:: markers.

    Also strips any leaked tool-call markup (DGN-159) so streamed drafts and
    finalized bubbles never surface raw <invoke> blocks.
    """
    if not text:
        return text
    text = strip_toolcall_markup(text)
    lines = text.split("\n")
    kept = [
        ln
        for ln in lines
        if ln.strip() != OPTIONS_MARKER
        and IDRILL_MARKER_RE.match(ln.strip()) is None
        and not ln.strip().startswith(SEND_FILE_MARKER)
        and not ln.strip().startswith(LINK_PREVIEW_MARKER)
    ]
    if len(kept) == len(lines):
        return text
    return "\n".join(kept).rstrip()


def resolve_send_paths(content: str, project_root: Path) -> List[Path]:
    """Resolve send_file:: marker paths to existing files under 10MB, deduped.

    Relative paths resolve against project_root. Order preserved, duplicates
    removed. Non-existent or oversized files are dropped.
    """
    paths: List[Path] = []
    seen = set()
    for raw in extract_send_marker_paths(content):
        p = Path(raw)
        if not p.is_absolute():
            p = project_root / p
        p = p.resolve()
        if p in seen:
            continue
        try:
            if p.is_file() and p.stat().st_size < MAX_FILE_BYTES:
                seen.add(p)
                paths.append(p)
        except OSError:
            continue
    return paths


def is_within_root(path: Path, project_root: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(project_root)
    except Exception:
        return False


def split_paths_by_scope(
    paths: List[Path], project_root: Path
) -> Tuple[List[Path], List[Path]]:
    in_root: List[Path] = []
    outside: List[Path] = []
    for p in paths:
        (in_root if is_within_root(p, project_root) else outside).append(p)
    return in_root, outside


def split_text(text: str, limit: int = 4000) -> List[str]:
    """Split into chunks <= limit, breaking at paragraph > line > hard cut."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        else:
            cut += 1
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def split_into_segments(text: str) -> List[Tuple[str, bool, Optional[str]]]:
    """Split text into ordered (segment, is_code, lang) tuples on ``` fences.

    Fenced blocks become code segments holding only the inner code (no fence, no
    language tag); the language tag (or None) is the third element. Text outside
    fences becomes prose. Empty segments are dropped. Text with no complete
    fenced block yields a single prose segment.
    """
    fence = "```"
    if fence not in text:
        return [(text, False, None)] if text else []
    segments: List[Tuple[str, bool, Optional[str]]] = []
    pos = 0
    n = len(text)
    while pos < n:
        start = text.find(fence, pos)
        if start == -1:
            tail = text[pos:]
            if tail.strip():
                segments.append((tail, False, None))
            break
        end = text.find(fence, start + len(fence))
        if end == -1:
            tail = text[pos:]
            if tail.strip():
                segments.append((tail, False, None))
            break
        prose = text[pos:start]
        if prose.strip():
            segments.append((prose, False, None))
        inner = text[start + len(fence): end]
        lang: Optional[str] = None
        newline = inner.find("\n")
        if newline != -1:
            first_line = inner[:newline]
            if first_line.strip() and " " not in first_line.strip():
                lang = first_line.strip()
                inner = inner[newline + 1:]
        code = inner.strip("\n")
        if code.strip():
            segments.append((code, True, lang))
        pos = end + len(fence)
    return segments


def code_segment_html(code: str, lang: Optional[str]) -> str:
    """Wrap a code segment in a Telegram-safe HTML <pre><code> block."""
    escaped = html.escape(code, quote=False)
    if lang:
        return f'<pre><code class="language-{lang}">{escaped}</code></pre>'
    return f"<pre>{escaped}</pre>"


# --- DGN-376: markdown -> Telegram HTML for prose segments -------------------
#
# The bridge sends prose with parse_mode="HTML" (code segments already go out
# via code_segment_html). Telegram HTML supports ONLY these tags: b strong i
# em u ins s strike del tg-spoiler span(class="tg-spoiler") a(href) code
# (+language class) pre blockquote (+expandable).
#
# Conversion contract:
#   1. Inline `code` spans are lifted first: content is HTML-escaped and
#      wrapped in <code>; markdown inside a code span is never converted.
#   2. INTENTIONAL-HTML PASSTHROUGH: a tag passes through verbatim only when
#      it exactly matches the Telegram whitelist below (lowercase tag,
#      double-quoted attribute, no extra attributes). Everything else --
#      unknown tags, malformed tags, stray < > & -- is HTML-escaped, so
#      accidental angle brackets can never break a message.
#   3. Markdown the Dogany agents actually emit converts to tags:
#        **bold** / __bold__ -> <b>      *italic* / _italic_ -> <i>
#        ~~strike~~          -> <s>      `code`              -> <code>
#        [text](http(s)://... or tg://...) -> <a href="...">
#      Emphasis must open and close on the same line, hug non-whitespace,
#      and not butt against word characters -- so stray _ * [ ] and
#      snake_case / x**2 style identifiers pass through unchanged. Links
#      convert before emphasis so URLs containing _ or * are never mangled;
#      non-http(s)/tg link targets stay literal text.
#   4. Markdown headers and tables (no Telegram HTML equivalent) are left as
#      literal text. Lists and blockquotes ARE rendered (DGN-619 below).
#
# DGN-619 line-structural extensions (run as a pre-pass, before the inline
# pipeline above):
#   LISTS: Telegram HTML has no <ul>/<li>. Markdown bullet items (`- `, `* `,
#     `+ `) become TEXT bullets: `- ` -> "* " at top level is wrong; we use
#     the literal glyphs "• " (top), "◦ " (depth 1), "‣ "
#     (depth >= 2). Source indentation is 2 spaces per depth level; the same
#     2-space visual indent per depth is reproduced with leading spaces in the
#     output (Telegram does not indent tags). Ordered items (`1.`, `2.`) keep
#     their number ("1. ") verbatim. These prose bullets are deliberately
#     distinct from the card-tree glyphs (U+2514 / middot), which stay
#     table/subline-only, so the two conventions never collide.
#   BLOCKQUOTE: a run of contiguous `> ` lines collapses into ONE
#     <blockquote>...</blockquote> (inner lines joined with newlines). The
#     EXPANDABLE variant (<blockquote expandable>) is opted in by a documented
#     fold marker: the run's FIRST quote line begins `>! ` instead of `> `
#     (i.e. the "!" sentinel immediately after the leading ">"). The fold
#     trigger is the marker ONLY -- never line count (owner-explicit). Choosing
#     WHICH content is captured as an expandable blockquote (tool-process
#     capture / role classification) is a separate follow-up, not this pass.
#   ITALIC WORD-ONLY: italic emphasis converts only when the wrapped span is a
#     single word (no internal whitespace); a multi-word span stays literal
#     text. This is enforced by the inline italic regexes (see below).

# Whitelisted, exact-form Telegram HTML tags that pass through verbatim.
_TG_HTML_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|tg-spoiler|code|pre|blockquote)>"
    r"|<blockquote expandable>"
    r'|<code class="language-[A-Za-z0-9_+.#-]+">'
    r'|<span class="tg-spoiler">'
    r"|</span>"
    r'|<a href="[^"<>\s]+">'
    r"|</a>"
)


def contains_telegram_html(text: str) -> bool:
    """True when text carries a whitelisted Telegram HTML tag (DGN-846).

    Used by the streamed-prose finalize path: the plain-text draft renders
    passthrough tags (e.g. <blockquote expandable>) as RAW literal text, so
    the HTML finalize edit must NOT be skipped as a no-op even when the
    markdown conversion leaves the string byte-identical -- sending the same
    string with parse_mode=HTML is what makes the tags render.
    """
    if not text:
        return False
    return _TG_HTML_TAG_RE.search(text) is not None

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\(((?:https?|tg)://[^\s()]+)\)")
_MD_BOLD_STAR_RE = re.compile(
    r"(?<![A-Za-z0-9*])\*\*(?![\s*])([^\n]+?)(?<![\s*])\*\*(?![A-Za-z0-9*])"
)
_MD_BOLD_UNDER_RE = re.compile(
    r"(?<![A-Za-z0-9_])__(?![\s_])([^\n]+?)(?<![\s_])__(?![A-Za-z0-9_])"
)
# DGN-619: italic is WORD-ONLY. The wrapped span must contain no whitespace,
# so `*two words*` never becomes italic (it falls through as literal text and
# is escaped like any stray marker). `[^\s*]` (resp. `[^\s_]`) forbids internal
# whitespace and the same marker char, mechanically enforcing the single-word
# rule while preserving every existing single-word italic case.
_MD_ITALIC_STAR_RE = re.compile(
    r"(?<![A-Za-z0-9*])\*(?![\s*])([^\s*\n]+?)(?<!\s)\*(?![A-Za-z0-9*])"
)
_MD_ITALIC_UNDER_RE = re.compile(
    r"(?<![A-Za-z0-9_])_(?![\s_])([^\s_\n]+?)(?<!\s)_(?![A-Za-z0-9_])"
)
_MD_STRIKE_RE = re.compile(r"~~(?=\S)([^\n]+?)(?<=\S)~~")

# DGN-619 list items: leading indent, a bullet marker (- * +) or an ordered
# marker (digits + "."), then a single required space, then the item text.
_MD_BULLET_ITEM_RE = re.compile(r"^([ \t]*)([-*+])[ \t]+(.*)$")
_MD_ORDERED_ITEM_RE = re.compile(r"^([ \t]*)(\d+)\.[ \t]+(.*)$")
# DGN-619 blockquote line: leading ">", an optional "!" fold sentinel, then a
# single space and the quoted text (the space and text may both be empty for a
# bare ">"). Group 1 = "!" when the expandable marker is present.
_MD_QUOTE_LINE_RE = re.compile(r"^>(!)?(?: (.*))?$")

# DGN-775: markdown header line (1-6 '#' marks at line start).
_MD_HEADER_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$", re.MULTILINE)
# DGN-822: markdown thematic break -- a line consisting ONLY of 3+ hyphens,
# asterisks, or underscores (optional surrounding whitespace). Table separator
# rows (|---|---|) can never match: they contain '|'.
_MD_THEMATIC_BREAK_RE = re.compile(r"^[ \t]*(-{3,}|\*{3,}|_{3,})[ \t]*$")
# DGN-775: markdown table detection helpers.
# A table data row has at least one '|' that is not the only character on the line.
_MD_TABLE_ROW_RE = re.compile(r"^\|.*\|[ \t]*$")
# A separator row: cells containing only '-', ':', and spaces between pipes.
_MD_TABLE_SEP_RE = re.compile(r"^\|[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$")

# Prose bullet glyphs by depth (top, depth 1, depth 2+). Two visual spaces of
# indent are added per depth level in the output.
_BULLET_GLYPHS = ("•", "◦", "‣")  # bullet / white-bullet / triangle

# DGN-954: ATX heading level -> glyph (H4+ clamped to index 2).
# U+25AA BLACK SMALL SQUARE / U+25C6 BLACK DIAMOND / U+25B8 BLACK RIGHT-POINTING SMALL TRIANGLE
_HEADER_GLYPHS = ("▪", "◆", "▸")  # H1 / H2 / H3+

# Strip bare ** markers from heading text before bold-wrapping to prevent
# double-bold or broken tags when the heading already contains bold spans.
_HEADER_BOLD_STRIP_RE = re.compile(r"\*\*")


def _bullet_prefix(depth: int) -> str:
    """Leading indent (2 spaces/depth) + the depth-appropriate bullet glyph."""
    glyph = _BULLET_GLYPHS[min(depth, len(_BULLET_GLYPHS) - 1)]
    return "  " * depth + glyph + " "


def _strip_md_headers(text: str) -> str:
    """DGN-954: promote ATX heading lines to glyph + bold HTML.

    Level -> glyph: H1 -> ▪ (U+25AA), H2 -> ◆ (U+25C6), H3+ -> ▸ (U+25B8, clamped).
    Output form per line: '<glyph> <b>heading text</b>'

    The emitted <b>...</b> tags survive html.escape because _TG_HTML_TAG_RE
    stashes whitelisted tags (step 2 in markdown_to_telegram_html) before
    html.escape runs (step 3).  Final rendered output is therefore:
        '<glyph> <b>heading text</b>'

    Any ** markers in the heading text are stripped before bold-wrapping to
    prevent double-bold / broken nesting when the heading already contains bold.
    Empty headings ('# ' trailing-space only) produce '<glyph> <b></b>' -- safe.

    '#hashtag' (no space after '#') never matches _MD_HEADER_RE (requires
    '[ \\t]+'), so it passes through unchanged.

    Runs inside _prepass_structural, which is called only on prose segments
    (code fences are split out upstream in bot.py), so '#' inside fenced or
    inline code is never affected.
    """

    def _promote(m: "re.Match[str]") -> str:
        level = len(m.group(1))  # number of '#' chars -> heading level
        glyph = _HEADER_GLYPHS[min(level - 1, len(_HEADER_GLYPHS) - 1)]
        inner = _HEADER_BOLD_STRIP_RE.sub("", m.group(2))
        return "{} <b>{}</b>".format(glyph, inner)

    return _MD_HEADER_RE.sub(_promote, text)


def _strip_md_thematic_breaks(text: str) -> str:
    """DGN-822: drop markdown thematic-break lines (--- / *** / ___).

    Telegram has no horizontal-rule construct, so a leaked break renders as a
    literal dash/asterisk/underscore run. Only lines made ENTIRELY of the
    break characters (3+ repeats, optional surrounding whitespace) are
    dropped. Table separator rows (|---|) never match (they contain '|'), and
    code segments are unaffected because this runs on PROSE segments only
    (via _prepass_structural, which the bridge applies after code-fence
    splitting).
    """
    lines = text.split("\n")
    return "\n".join(ln for ln in lines if not _MD_THEMATIC_BREAK_RE.match(ln))


def _wrap_md_tables(text: str) -> str:
    """DGN-775: detect markdown table blocks and wrap each one in <pre>...</pre>.

    A table block is a contiguous run of lines that are either data rows
    (contain at least one '|') or separator rows ('|---|' style). The block
    must contain at least one separator row so pure pipe-containing prose is
    never mistakenly wrapped.

    Wrapping happens BEFORE html.escape (called in markdown_to_telegram_html),
    so the <pre>...</pre> tags themselves must be stash-safe -- they are NOT
    stashed here because _prepass_structural does not call this helper; the
    caller (markdown_to_telegram_html) must ensure the pre tags survive escape
    by stashing them. We therefore emit a special sentinel instead of raw tags,
    and the caller substitutes after escaping. However, to keep the design
    surgical and consistent with the existing stash pattern, this function
    returns the text with <pre> / </pre> replaced by NUL-delimited stash tokens
    that the caller has already registered -- so it accepts a stash callable.

    Implementation note: since this runs BEFORE the stash is set up in
    markdown_to_telegram_html, we take stash as a parameter so the caller can
    register the tag pair and retrieve their placeholders.
    """
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        # Detect start of a possible table: must be a data row.
        if _MD_TABLE_ROW_RE.match(lines[i]):
            # Collect the contiguous run.
            run_start = i
            has_sep = False
            while i < n and (_MD_TABLE_ROW_RE.match(lines[i]) or _MD_TABLE_SEP_RE.match(lines[i])):
                if _MD_TABLE_SEP_RE.match(lines[i]):
                    has_sep = True
                i += 1
            run = lines[run_start:i]
            if has_sep:
                # Confirmed table: join lines as a pre-formatted block.
                # Use literal markers here; markdown_to_telegram_html will stash
                # them via the _stash callback passed in by the caller.
                out.append("\x01TABLE_PRE_OPEN\x01" + "\n".join(run) + "\x01TABLE_PRE_CLOSE\x01")
            else:
                # Not a real table (no separator row): emit unchanged.
                out.extend(run)
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _prepass_structural(text: str, stash) -> str:
    """Convert list/blockquote LINE structure to stashed literals + inner text.

    Runs before the inline pipeline. List bullet prefixes and blockquote tags
    are stashed (so html.escape leaves them intact); the item / quote text is
    left in place to flow through escaping and inline emphasis normally. A run
    of contiguous quote lines collapses into one <blockquote> (or, when the
    first line carries the `>!` fold marker, one <blockquote expandable>).

    DGN-775: also strips markdown headers (# / ## / ...) and wraps table
    blocks in <pre> before the inline pipeline runs.
    """
    # DGN-775: strip header marks first (pure text transform, no stash needed).
    text = _strip_md_headers(text)
    # DGN-822: drop thematic-break lines (pure text transform, no stash needed).
    text = _strip_md_thematic_breaks(text)
    # DGN-775: wrap table blocks with sentinel markers before line iteration.
    text = _wrap_md_tables(text)
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # DGN-775: resolve table sentinel markers into stashed <pre> tags so
        # html.escape cannot corrupt them.  A sentinel line may contain both
        # the open and close marker (single-line table block) or just one.
        if "\x01TABLE_PRE_OPEN\x01" in line or "\x01TABLE_PRE_CLOSE\x01" in line:
            line = line.replace("\x01TABLE_PRE_OPEN\x01", stash("<pre>"))
            line = line.replace("\x01TABLE_PRE_CLOSE\x01", stash("</pre>"))
            out.append(line)
            i += 1
            continue
        qm = _MD_QUOTE_LINE_RE.match(line)
        if qm is not None:
            # Collect the contiguous quote run. The first line's "!" sentinel
            # (and ONLY the first line's) selects the expandable variant.
            expandable = qm.group(1) == "!"
            body: List[str] = []
            while i < n:
                m = _MD_QUOTE_LINE_RE.match(lines[i])
                if m is None:
                    break
                body.append(m.group(2) or "")
                i += 1
            open_tag = "<blockquote expandable>" if expandable else "<blockquote>"
            out.append(
                stash(open_tag) + "\n".join(body) + stash("</blockquote>")
            )
            continue
        bm = _MD_BULLET_ITEM_RE.match(line)
        if bm is not None:
            depth = len(bm.group(1).replace("\t", "  ")) // 2
            out.append(stash(_bullet_prefix(depth)) + bm.group(3))
            i += 1
            continue
        om = _MD_ORDERED_ITEM_RE.match(line)
        if om is not None:
            indent = om.group(1).replace("\t", "  ")
            # Ordered markers keep their number verbatim; indent is preserved
            # as-is (leading spaces stash-free would be escaped harmlessly, but
            # stashing keeps the prefix atomic alongside the bullet path).
            out.append(stash("{}{}. ".format(indent, om.group(2))) + om.group(3))
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def markdown_to_telegram_html(text: str) -> str:
    """Convert a PROSE segment to Telegram-safe HTML (see contract above).

    Apply only to non-code segments (code fences go through code_segment_html)
    and exactly once at send time.
    """
    if not text:
        return text
    # NUL is used as the stash placeholder delimiter; Telegram rejects NUL in
    # message text anyway, so dropping any stray ones is lossless.
    text = text.replace("\x00", "")
    stash: List[str] = []

    def _stash(rendered: str) -> str:
        stash.append(rendered)
        return "\x00{}\x00".format(len(stash) - 1)

    # DGN-719 a3: transpile fold:: typed intent blocks to the DGN-619 `>! `
    #    expandable-blockquote form BEFORE the structural pre-pass consumes it.
    text = render_fold_block(text)
    # DGN-719 a2: contain any unrecognized neutral/semantic marker line (version
    #    skew net) -- downgrade to a safe literal so it is never raw-relayed.
    #    Runs before html.escape so the neutralized text renders as visible
    #    literal; recognized markers are already stripped upstream and pass through.
    text, _ = contain_unknown_markers(text)
    # 0. Line-structural pre-pass (DGN-619): lists + blockquotes. Runs first so
    #    stashed bullet prefixes / blockquote tags survive html.escape below.
    text = _prepass_structural(text, _stash)
    # 1. Inline code spans: escape content, never md-convert it.
    text = _INLINE_CODE_RE.sub(
        lambda m: _stash("<code>{}</code>".format(html.escape(m.group(1), quote=False))),
        text,
    )
    # 2. Whitelisted Telegram tags pass through verbatim.
    text = _TG_HTML_TAG_RE.sub(lambda m: _stash(m.group(0)), text)
    # 3. Escape everything else.
    text = html.escape(text, quote=False)
    # 4. Links first, stashed whole, so URL characters never hit the emphasis
    #    regexes below.
    text = _MD_LINK_RE.sub(
        lambda m: _stash(
            '<a href="{}">{}</a>'.format(m.group(2).replace('"', "&quot;"), m.group(1))
        ),
        text,
    )
    # 5. Emphasis: bold before italic so ** is never read as two *.
    text = _MD_BOLD_STAR_RE.sub(r"<b>\1</b>", text)
    text = _MD_BOLD_UNDER_RE.sub(r"<b>\1</b>", text)
    text = _MD_STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _MD_ITALIC_STAR_RE.sub(r"<i>\1</i>", text)
    text = _MD_ITALIC_UNDER_RE.sub(r"<i>\1</i>", text)
    # 6. Restore stashed spans in reverse: later entries (links) may contain
    #    placeholders of earlier ones (a code span inside link text).
    for i in range(len(stash) - 1, -1, -1):
        text = text.replace("\x00{}\x00".format(i), stash[i])
    return text


# --- DGN-969: de-marked streaming display -------------------------------------
#
# The live streaming draft bubble is sent as PLAIN text (no parse_mode), so raw
# markdown syntax (**bold**, __bold__, ~~strike~~, `code`) was fully visible
# for the whole turn until a single final edit converted it to HTML. This
# section renders a DISPLAY-ONLY de-marked form for that live bubble:
#   - a COMPLETE markdown span (both markers present) is stripped to its bare
#     content using the EXACT SAME regexes as markdown_to_telegram_html, so
#     any case with a real closing pair behaves identically to the eventual
#     final render (content kept, marker characters removed).
#   - a marker still open when a stream chunk lands (a span cut mid-way, e.g.
#     "text **bo") is ALSO stripped (marker characters only, content kept) so
#     a raw ** is never shown while pending -- this is the streaming-specific
#     addition the final renderer does not need, since it only ever sees
#     complete text.
# Deliberately NOT covered by the dangling-open strip: single-character
# emphasis (*word*/_word_) and links ([text](url)). A lone unmatched '*' or
# '_' is indistinguishable from literal content (glob `*.md`, math `x*y`,
# `snake_case`) without a closing pair to confirm intent -- stripping it would
# risk corrupting exactly that literal content during the live window. The
# double-character markers (**, __, ~~) and a lone open backtick are far less
# ambiguous (rare as literal prose) and are the ones named in the DGN-969
# success criteria, so those get the extra dangling-open treatment.
#
# INVARIANT: this is a display transform ONLY. Callers must keep feeding the
# ORIGINAL (untouched) accumulated text into chunking/overflow/finalize logic
# and into the eventual bot.py _edit_streamed_prose_html conversion -- only the
# string actually handed to Telegram for one particular send goes through
# demark_markdown_for_stream. The finalized message is therefore byte-
# identical to what it was before this transform existed.
#
# Fenced code blocks (```...```) are left completely untouched, fences
# included -- markdown-looking characters inside code are literal content,
# never syntax. An in-progress (unclosed) fence at the tail is also left fully
# raw, matching the pre-existing plain display of in-flight streamed code.

_MD_BOLD_STAR_OPEN_RE = re.compile(r"(?<![A-Za-z0-9*])\*\*(?![\s*])")
_MD_BOLD_UNDER_OPEN_RE = re.compile(r"(?<![A-Za-z0-9_])__(?![\s_])")
_MD_STRIKE_OPEN_RE = re.compile(r"~~(?=\S)")
# DGN-969 grill finding: the bridge's own overflow-split (character-count cut,
# formatting-agnostic) and the streaming handler's block-join newline can both
# land a marker's CLOSING half at the start of a bubble/line with no opener in
# that same text (the opener was sealed into a PREVIOUS, already-finalized
# bubble). The _OPEN patterns above only catch a dangling OPENER; this mirrors
# the CLOSING-side guard from the full paired regexes (unchanged) so an
# orphaned closer is caught too. Both sides individually reduce to the exact
# same alnum-adjacency guard the full regex already uses for x**2 / snake_case
# safety, so nothing newly ambiguous is stripped -- only the shape the
# complete-pair regex would have accepted as a valid opener/closer, just
# missing its other half.
_MD_BOLD_STAR_CLOSE_RE = re.compile(r"(?<![\s*])\*\*(?![A-Za-z0-9*])")
_MD_BOLD_UNDER_CLOSE_RE = re.compile(r"(?<![\s_])__(?![A-Za-z0-9_])")
_MD_STRIKE_CLOSE_RE = re.compile(r"(?<=\S)~~")


def _demark_prose(text: str) -> str:
    """De-mark ONE prose chunk (guaranteed no ``` fences inside) for display."""
    if not text:
        return text
    text = text.replace("\x00", "")
    stash: List[str] = []

    def _stash(bare: str) -> str:
        stash.append(bare)
        return "\x00{}\x00".format(len(stash) - 1)

    # 1. Inline code first: content is never touched by the passes below (same
    #    ordering as markdown_to_telegram_html), but the backticks are dropped
    #    (bare content only) -- streaming has no <code> styling to offer.
    text = _INLINE_CODE_RE.sub(lambda m: _stash(m.group(1)), text)
    # 2. Links: bare visible text, URL dropped from the display copy.
    text = _MD_LINK_RE.sub(lambda m: m.group(1), text)
    # 3. Complete-pair emphasis -> bare content (marker chars removed).
    text = _MD_BOLD_STAR_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDER_RE.sub(r"\1", text)
    text = _MD_STRIKE_RE.sub(r"\1", text)
    text = _MD_ITALIC_STAR_RE.sub(r"\1", text)
    text = _MD_ITALIC_UNDER_RE.sub(r"\1", text)
    # 4. Dangling opens/closes (chunk or overflow-split cut mid-span): strip
    #    just the marker characters for the low-ambiguity double-markers --
    #    content is never touched. Both sides are needed: an opener can be cut
    #    open (its close never arrived yet) OR a closer can arrive orphaned
    #    (its open was sealed into a previous, already-finalized bubble).
    text = _MD_BOLD_STAR_OPEN_RE.sub("", text)
    text = _MD_BOLD_STAR_CLOSE_RE.sub("", text)
    text = _MD_BOLD_UNDER_OPEN_RE.sub("", text)
    text = _MD_BOLD_UNDER_CLOSE_RE.sub("", text)
    text = _MD_STRIKE_OPEN_RE.sub("", text)
    text = _MD_STRIKE_CLOSE_RE.sub("", text)
    # A single unmatched opening backtick (odd count survives the inline-code
    # extraction above) -- drop it so a dangling `code isn't shown raw.
    if text.count("`") % 2 == 1:
        text = text.replace("`", "", 1)
    # 5. Restore stashed inline-code content, reverse order (same rule as
    #    markdown_to_telegram_html: later stashes may nest inside earlier text).
    for i in range(len(stash) - 1, -1, -1):
        text = text.replace("\x00{}\x00".format(i), stash[i])
    return text


def demark_markdown_for_stream(text: str) -> str:
    """De-mark markdown syntax for LIVE streaming display only (DGN-969).

    Display-only transform: strips markdown syntax characters from a
    streaming draft so raw '**', '__', backtick etc. are never visible while
    the turn is still in progress. See the module-level DGN-969 comment above
    for the full contract (fence handling, dangling-open scope, the byte-
    identical-finalize invariant). Callers must NOT feed the return value back
    into their own accumulated-text bookkeeping -- only into the Telegram
    send/edit call for this one draft update.
    """
    if not text:
        return text
    if "```" not in text:
        return _demark_prose(text)
    fence = "```"
    parts: List[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        start = text.find(fence, pos)
        if start == -1:
            parts.append(_demark_prose(text[pos:]))
            break
        if start > pos:
            parts.append(_demark_prose(text[pos:start]))
        end = text.find(fence, start + len(fence))
        if end == -1:
            # Unclosed trailing fence: in-progress code, leave fully raw.
            parts.append(text[start:])
            break
        end_incl = end + len(fence)
        parts.append(text[start:end_incl])
        pos = end_incl
    return "".join(parts)


# --- DGN-891: tag-safe HTML balancing for split/truncated sends --------------
#
# Rendered Telegram HTML can end up cut at a length boundary (split_text
# chunking of the final answer, fold rolling-window truncation) with a
# <b>/<blockquote>/<code>/<a> span straddling the cut. Telegram rejects the
# unbalanced chunk with a 400 ("can't find end tag corresponding to start
# tag ...") and the send then degrades to a plain fallback -- historically
# showing RAW markdown source to the user. The helpers below make every
# emitted HTML string independently valid:
#   - balance_telegram_html: closes still-open tags at the end (LIFO order)
#     and drops stray closers that have no opener. Balanced input passes
#     through byte-identical, so it is safe as a universal pre-send guard.
#   - rebalance_html_chunks: balance-and-reopen across an ordered chunk list;
#     tags left open at a chunk boundary are closed at that chunk's end and
#     re-opened (original attributes preserved) at the next chunk's start,
#     so a straddling span keeps its formatting on both sides of the split.
#   - html_to_plain_text: tag-strip + entity-unescape, shared by every plain
#     fallback so a rejected HTML send degrades to readable prose, never raw
#     markdown (**) or leaked tags.

# Container tag names Telegram HTML pairs with a close tag. Longest names
# first so the alternation never half-matches a longer name. The optional
# attribute group covers <a href="...">, <blockquote expandable>,
# <code class="language-...">, <span class="tg-spoiler">.
_TG_BALANCE_TAG_RE = re.compile(
    r"<(/?)(blockquote|tg-spoiler|strong|strike|span|code|pre|ins|del|em|b|i|u|s|a)"
    r"((?:\s[^<>]*)?)>"
)


def _scan_html_tags(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Scan whitelisted tags; fix strays; return (text, open-tag stack).

    The returned stack holds (name, full_open_tag) for every tag still open
    at the end of text, in opening order. Stray closers with no matching
    opener are dropped; an improperly nested closer first closes the tags
    opened inside it (Telegram requires proper nesting). Input with no
    stray/misnested closers is returned as the same object.
    """
    stack: List[Tuple[str, str]] = []
    out: List[str] = []
    pos = 0
    changed = False
    for m in _TG_BALANCE_TAG_RE.finditer(text):
        if m.group(1) != "/":
            stack.append((m.group(2), m.group(0)))
            continue
        name = m.group(2)
        if stack and stack[-1][0] == name:
            stack.pop()
            continue
        out.append(text[pos:m.start()])
        pos = m.end()
        changed = True
        if any(n == name for n, _ in stack):
            # Improper nesting: close the inner tags, then keep the closer.
            while stack and stack[-1][0] != name:
                out.append("</{}>".format(stack.pop()[0]))
            stack.pop()
            out.append(m.group(0))
        else:
            # DGN-891 C1: stray closer with no opener -> escape it so the
            # literal text survives (a bare "</b>" typed in prose must not be
            # silently deleted). Escaping keeps the output balanced.
            out.append(m.group(0).replace("<", "&lt;").replace(">", "&gt;"))
    if not changed:
        return text, stack
    out.append(text[pos:])
    return "".join(out), stack


def balance_telegram_html(html_text: str) -> str:
    """Close open tags / drop stray closers so Telegram accepts the HTML.

    Idempotent and a byte-identical no-op on balanced input, so it is safe
    as a final guard immediately before every parse_mode="HTML" send.
    """
    if not html_text or "<" not in html_text:
        return html_text
    fixed, open_stack = _scan_html_tags(html_text)
    if not open_stack:
        return fixed
    return fixed + "".join(
        "</{}>".format(name) for name, _ in reversed(open_stack)
    )


def rebalance_html_chunks(chunks: List[str]) -> List[str]:
    """Balance-and-reopen: make each rendered-HTML chunk independently valid.

    Tags still open at a chunk's end are closed there and re-opened (with
    their original attributes) at the start of the next chunk, so a span
    that straddled the split boundary stays formatted on both sides.
    Already-balanced chunks pass through byte-identical.
    """
    out: List[str] = []
    carry: List[Tuple[str, str]] = []
    for chunk in chunks:
        prefixed = "".join(tag for _, tag in carry) + chunk if carry else chunk
        fixed, open_stack = _scan_html_tags(prefixed)
        if open_stack:
            fixed += "".join(
                "</{}>".format(name) for name, _ in reversed(open_stack)
            )
        out.append(fixed)
        carry = open_stack
    return out


def html_to_plain_text(html_text: str) -> str:
    """Strip HTML tags + unescape entities: readable plain-text fallback.

    Shared by every plain fallback of a parse_mode="HTML" send so a rejected
    HTML message degrades to clean prose -- never raw markdown or leaked tags.
    """
    if not html_text:
        return html_text
    # DGN-891 C2: preserve link URLs ("text (url)") instead of dropping href
    # when the anchor tag is stripped.
    text = re.sub(
        r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', r"\2 (\1)", html_text, flags=re.DOTALL
    )
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def sanitize_message_for_telegram(text: str) -> str:
    """DGN-822: sanitize a WHOLE message to Telegram-safe HTML (single entry).

    Out-of-band senders (routines/push.sh cron / proactive pushes) bypass the
    bridge send path and would otherwise leak raw markdown to Telegram. This
    wraps the exact conversation-rail pipeline behind one call: the message is
    split on ``` fences the same way bot.py _send_text_body does
    (split_into_segments); code segments render via code_segment_html and
    prose segments via markdown_to_telegram_html; the rendered segments are
    joined back with blank lines into ONE Telegram-safe HTML string. No new
    sanitize logic lives here -- formatting.py stays the single owner.
    """
    if not text:
        return text
    rendered: List[str] = []
    for segment, is_code, lang in split_into_segments(text):
        if is_code:
            rendered.append(code_segment_html(segment, lang))
        else:
            prose = markdown_to_telegram_html(segment).strip("\n")
            if prose.strip():
                rendered.append(prose)
    return "\n\n".join(rendered)


# --- DGN-682: interim narration -> expandable-blockquote fold ----------------
#
# Fold-mode (INTERIM_MODE=fold) turns synthesize the interim narration
# captured during a turn into ONE `>!`-marked quote run, prepended above the
# final answer at finalize time; markdown_to_telegram_html then renders it as
# a single collapsed <blockquote expandable> (DGN-619). Composition contract
# (DGN-682 spec v2, D7/D8/D11):
#   - every narration line gets a "> " prefix, the FIRST line ">! " (the fold
#     marker), and blank lines become a bare ">" so the quote run never
#     breaks mid-fold (a break would leak the rest as plain prose);
#   - code fences inside the narration are neutralized (``` -> ''') so the
#     fold can never open a code segment / delete+resend path downstream;
#   - leading whitespace-only lines are dropped so the collapsed preview
#     (the run's first line, no caption) is always meaningful (D8);
#   - the fold caps at INTERIM_FOLD_CAP raw narration chars; over the cap the
#     first line and the most recent tail are preserved and the middle
#     collapses to one omission line (D7 middle-truncate);
#   - single-chunk guarantee: the composed fold + separator + final answer
#     must fit ONE send chunk -- raw <= 4000 (split_text limit) and <= 4096
#     UTF-16 code units after the Telegram HTML conversion (the real
#     Telegram bound). The fold shrinks FIRST (halving down to a floor, then
#     dropping entirely); a split would strip the `>!` marker from the second
#     chunk's quote lines and break the collapsed rendering (grill B2).

INTERIM_FOLD_CAP = 1500
_FOLD_OMISSION_LINE = _i18n("fold_omission_line", "⋯ 중략 ⋯")
_FOLD_RAW_LIMIT = 4000  # split_text default limit (single-chunk bound)
_FOLD_HTML_LIMIT = 4096  # Telegram hard cap, UTF-16 code units
_FOLD_MIN_CAP = 80
# Blank-line separator between the fold and the final answer: an empty line
# terminates the quote run so the answer renders OUTSIDE the blockquote.
# Callers that prepend a fold MUST join with this exact separator (the fit
# check above assumes it).
INTERIM_FOLD_SEPARATOR = "\n\n"


def _utf16_units(text: str) -> int:
    """Length in UTF-16 code units (Telegram's message-length unit)."""
    return len(text.encode("utf-16-le")) // 2


def _fold_middle_truncate(text: str, cap: int) -> str:
    """Middle-truncate text to <= cap chars, keeping the first line + tail.

    The first line is the collapsed preview (D8) so it is always preserved
    (hard-cut to cap//2 if it alone is oversized); the most recent tail fills
    the remaining budget; the omitted middle collapses to one omission line.
    """
    if len(text) <= cap:
        return text
    first = text.split("\n", 1)[0]
    if len(first) > cap // 2:
        first = first[: max(1, cap // 2)]
    overhead = len(first) + len(_FOLD_OMISSION_LINE) + 2  # two joining newlines
    tail_budget = cap - overhead
    if tail_budget <= 0:
        return first + "\n" + _FOLD_OMISSION_LINE
    tail = text[-tail_budget:]
    # Align the tail to a line start when possible so it never opens mid-word
    # on a partial line (cosmetic; budget already holds).
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1:]
    tail = tail.strip("\n")
    if not tail.strip():
        return first + "\n" + _FOLD_OMISSION_LINE
    return first + "\n" + _FOLD_OMISSION_LINE + "\n" + tail


def _fold_quote_lines(text: str) -> str:
    """Apply D11 quoting: '>! ' first line, '> ' others, bare '>' for blanks."""
    out: List[str] = []
    for i, ln in enumerate(text.split("\n")):
        if not ln.strip():
            out.append(">")
        elif i == 0:
            out.append(">! " + ln)
        else:
            out.append("> " + ln)
    return "\n".join(out)


# --- DGN-699: growing fold (2-phase live render) -----------------------------
#
# Spec v2 (worklog/DGN-699-spec-v2-growing-fold.md). Fold-mode turns stream
# interim narration into a DEDICATED progress bubble that grows live:
#   - live phase (D1, owner 2026-08-02): the bubble renders as PLAIN TEXT
#     (no blockquote), edited over Telegram HTML so the stream reads as normal
#     text while growing;
#   - finalize phase (D1): the same bubble is swapped to a caption + the whole
#     narration wrapped in one <blockquote expandable> (collapsed);
#   - length (D6): the fold owns its own 4096 UTF-16-unit budget (2-bubble
#     split); over budget the OLDEST lines roll off the front and the cut is
#     marked with one FOLD_TRUNCATION_LINE. update_if_needed's built-in
#     overflow rollover is deliberately NOT used.
# The helpers below are pure (no Telegram I/O): they return ready-to-send
# HTML built via markdown_to_telegram_html (escaping + DGN-619 blockquote
# rendering reused). The ko catalog values are the LOCKED copy from the spec
# (owner A-case, 2026-08-02 14:27) -- do not edit without an owner gate.
# DGN-851: caption TEXT lives in bridge/i18n (ko/en); the in-code fallbacks
# below are the locked ko copy (zero-delta when i18n is unavailable).

FOLD_CAPTION_NORMAL = _i18n("fold_caption_normal", "진행 기록")
FOLD_CAPTION_STOPPED = _i18n("fold_caption_stopped", "중단됨 · 진행 기록")
FOLD_CAPTION_TIMEOUT = _i18n("fold_caption_timeout", "시간 초과 · 진행 기록")
FOLD_TRUNCATION_LINE = _i18n("fold_truncation_line", "…(생략)")


def _fold_v2_body(fold_texts: List[str]) -> str:
    """Join captured narration blocks into one fold body.

    Same neutralization contract as compose_interim_fold: code fences are
    disarmed (``` -> ''') so the fold can never open a code segment, edge
    newlines are trimmed, empty blocks dropped, leading blank lines removed.
    Display markers ([[OPTIONS]] / send_file:: / link_preview:: lines and
    leaked tool-call markup) are stripped for parity with the normal send
    path, which this direct-HTML path bypasses.
    """
    blocks: List[str] = []
    for t in fold_texts or []:
        t = strip_display_markers((t or "").replace("```", "'''")).strip("\n")
        if t.strip():
            blocks.append(t)
    joined = "\n\n".join(blocks)
    lines = joined.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).rstrip()


def _fold_v2_quote(text: str, expandable: bool) -> str:
    """Quote a fold body: '> ' per line, blanks bare '>'.

    expandable=True marks the FIRST line '>! ' (the DGN-619 fold marker) so
    markdown_to_telegram_html renders <blockquote expandable> (collapsed);
    False renders a plain <blockquote> (visible while growing).
    """
    out: List[str] = []
    for i, ln in enumerate(text.split("\n")):
        if not ln.strip():
            out.append(">")
        elif i == 0 and expandable:
            out.append(">! " + ln)
        else:
            out.append("> " + ln)
    return "\n".join(out)


def _render_fold_html(body: str, caption: str, expandable: bool, quote: bool = True) -> str:
    """Render body (+optional caption line) to fitted Telegram HTML.

    The caption is the FIRST line INSIDE the blockquote (owner 2026-08-02):
    the whole fold reads as one unified quote bubble rather than a floating
    plain line above a separate quote. It is prepended to the body only at
    render time and is NEVER part of the truncation window (the rolling-window
    loop below operates on the caption-free body), so the caption always
    survives a cut. D6 rolling window: while the rendered HTML exceeds the 4096
    UTF-16-unit budget, the oldest body lines are dropped and the cut is
    marked with one FOLD_TRUNCATION_LINE as the fold's first body line. Returns
    "" when the body is empty.

    quote=False (owner 2026-08-02): render the body as PLAIN TEXT with no
    blockquote wrapping -- used for the live phase so the stream reads as
    normal text while growing; the collapse into an expandable quote fold
    happens only at finalize (quote=True).
    """
    if not body.strip():
        return ""

    def _render(b: str) -> str:
        if caption:
            b = caption + "\n" + b
        if quote:
            out = markdown_to_telegram_html(_fold_v2_quote(b, expandable))
        else:
            out = markdown_to_telegram_html(b)
        # DGN-891: the rolling window can cut a verbatim tag pair apart (or
        # the narration may carry a lone whitelisted tag); balance BEFORE the
        # fit check so the emitted HTML is valid AND the 4096 budget measures
        # the real (balanced) length. No-op on balanced output.
        return balance_telegram_html(out)

    html_out = _render(body)
    if _utf16_units(html_out) <= _FOLD_HTML_LIMIT:
        return html_out
    lines = body.split("\n")
    truncated = False
    # Coarse pre-trim on raw length (HTML output is never shorter than its
    # source) so the fine per-line loop below stays bounded.
    while len(lines) > 1 and sum(len(ln) + 1 for ln in lines) > _FOLD_RAW_LIMIT:
        lines.pop(0)
        truncated = True
    while True:
        window = lines[:]
        while window and not window[0].strip():
            window.pop(0)
        candidate = "\n".join([FOLD_TRUNCATION_LINE] + window) if truncated else "\n".join(window)
        html_out = _render(candidate)
        if _utf16_units(html_out) <= _FOLD_HTML_LIMIT:
            return html_out
        if len(lines) > 1:
            lines.pop(0)
            truncated = True
            continue
        # Single oversized line: hard-cut from the front, keep the tail.
        keep = len(lines[0]) // 2
        if keep < 1:
            return ""
        lines[0] = lines[0][-keep:]
        truncated = True


def render_fold_live(fold_texts: List[str]) -> str:
    """Live-phase HTML: growing PLAIN TEXT, no blockquote, no caption.

    Owner 2026-08-02: the live stream shows as plain text so the flow is
    readable in real time; the collapse into a caption + expandable quote fold
    happens only at finalize (render_fold_final). Supersedes the earlier D1
    'non-expandable blockquote' live render.
    """
    return _render_fold_html(_fold_v2_body(fold_texts), "", expandable=False, quote=False)


def render_fold_final(fold_texts: List[str], caption: str) -> str:
    """Finalize-phase HTML: collapsed expandable fold with the caption as its
    first quoted line (owner 2026-08-02: caption inside the blockquote)."""
    return _render_fold_html(_fold_v2_body(fold_texts), caption, expandable=True)


def compose_interim_fold(interim_texts: List[str], final_text: str) -> str:
    """Compose captured interim narration into one `>!` fold quote run.

    Returns the quoted fold block (no trailing separator) or "" when nothing
    survives composition -- empty/whitespace-only capture (D6) or a final
    answer so large that even the minimum fold cannot fit the single-chunk
    budget (D7: the fold shrinks first, the answer is never cut). The caller
    prepends the result with INTERIM_FOLD_SEPARATOR between fold and answer.
    """
    blocks: List[str] = []
    for t in interim_texts or []:
        # Neutralize code fences so the fold never opens a code segment in
        # the send path (D11); trim edge newlines so joins stay tight.
        t = (t or "").replace("```", "'''").strip("\n")
        if t.strip():
            blocks.append(t)
    joined = "\n\n".join(blocks)
    # D8: drop leading whitespace-only lines so the collapsed preview line is
    # the first MEANINGFUL narration line.
    lines = joined.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    joined = "\n".join(lines).rstrip()
    if not joined.strip():
        return ""
    final = final_text or ""
    cap = min(INTERIM_FOLD_CAP, len(joined))
    while cap > 0:
        quoted = _fold_quote_lines(_fold_middle_truncate(joined, cap))
        combined = quoted + INTERIM_FOLD_SEPARATOR + final
        if (
            len(combined) <= _FOLD_RAW_LIMIT
            and _utf16_units(markdown_to_telegram_html(combined)) <= _FOLD_HTML_LIMIT
        ):
            return quoted
        if cap <= _FOLD_MIN_CAP:
            break
        cap = max(cap // 2, _FOLD_MIN_CAP)
    return ""


# DGN-372: Telegram legacy Markdown (parse_mode="Markdown") swallows unmatched
# '[' -- e.g. "[status]" renders as "status" with the brackets lost.  Backslash
# escaping is supported for '_', '*', '`', and '[' in legacy Markdown.
#
# Strategy: escape every '[' that does NOT open a valid legacy Markdown link of
# the form [text](url).  Real links must pass through untouched.  The regex
# looks ahead for an immediately-following '](' sequence (which is the minimal
# signal that this '[' starts a link); anything else gets a backslash prefix.
_LEGACY_LINK_RE = re.compile(
    r"\["                   # literal [
    r"(?:[^\[\]]*)"         # link text -- no nested brackets (legacy spec)
    r"\]"                   # closing ]
    r"\("                   # opening ( -- marks this as a real link
    r"[^)]*"                # URL body
    r"\)"                   # closing )
)


def escape_legacy_markdown_brackets(text: str) -> str:
    """Escape bare '[' characters for Telegram legacy Markdown parse_mode.

    Telegram's legacy Markdown parser silently swallows any '[' that does not
    form a valid [text](url) link pattern, causing bracket loss (e.g. "[status]"
    renders as "status").  This function prepends a backslash to every '[' that
    is NOT the start of a valid link pattern, so it is displayed literally.
    Real [text](url) links are left intact.  ']' needs no escaping.

    Must be applied ONLY on prose segments (not code spans/fences) and ONLY
    when parse_mode="Markdown" is active.  Apply exactly once at send time;
    double-application doubles backslashes.
    """
    if "[" not in text:
        return text

    # Collect the byte-spans of real links so we can leave them untouched.
    protected: List[Tuple[int, int]] = [m.span() for m in _LEGACY_LINK_RE.finditer(text)]

    def _in_protected(pos: int) -> bool:
        for start, end in protected:
            if start <= pos < end:
                return True
        return False

    parts: List[str] = []
    for i, ch in enumerate(text):
        if ch == "[" and not _in_protected(i):
            parts.append("\\[")
        else:
            parts.append(ch)
    return "".join(parts)
