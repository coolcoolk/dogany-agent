"""Text formatting: code-block splitting, length splitting, send_file:: marker.

These are pure helpers (no Telegram I/O) so they are easy to unit-test. The bot
wires them to actual send calls.
"""

import html
from pathlib import Path
from typing import List, Optional, Tuple

from bridge.options import OPTIONS_MARKER

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


def strip_display_markers(text: str) -> str:
    """Drop both [[OPTIONS]] and send_file:: marker lines (for bubble text)."""
    if not text:
        return text
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
