"""Text formatting: code-block splitting, length splitting, send_file:: marker.

These are pure helpers (no Telegram I/O) so they are easy to unit-test. The bot
wires them to actual send calls.
"""

import html
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

from bridge.options import OPTIONS_MARKER

logger = logging.getLogger(__name__)

# A reply only sends files when it contains a line whose stripped form starts
# with this prefix. Bare prose paths are never sent.
SEND_FILE_MARKER = "send_file::"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_FILE_BYTES = 10 * 1024 * 1024


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


def strip_display_markers(text: str) -> str:
    """Drop both [[OPTIONS]] and send_file:: marker lines (for bubble text).

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
        if ln.strip() != OPTIONS_MARKER and not ln.strip().startswith(SEND_FILE_MARKER)
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
