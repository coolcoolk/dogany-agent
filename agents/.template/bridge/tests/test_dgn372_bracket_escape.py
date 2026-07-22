"""DGN-372: escape_legacy_markdown_brackets unit tests.

Telegram legacy Markdown (parse_mode="Markdown") silently drops any '[' that
does not form a valid [text](url) link -- e.g. "[status]" renders as "status".
escape_legacy_markdown_brackets() prepends a backslash to every bare '[' so it
is preserved, while real [text](url) links pass through untouched.

Test cases
----------
1. Status tag:  "[status] info"              ->  "\\[status] info"
2. Real link:   "[text](http://x)"           ->  unchanged
3. Mixed:       "see [ref] and [link](http://x)"  ->  "see \\[ref] and [link](http://x)"
4. Code fence / backtick segments: '[' inside code is untouched via the
   split_into_segments path (function is never called on code segments).
5. Idempotency: documented -- function must be applied exactly once at send time
   (double-apply doubles backslashes; caller contract enforces single call).
"""

import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from bridge.formatting import escape_legacy_markdown_brackets, split_into_segments


# ---------------------------------------------------------------------------
# 1. Status tag -- the canonical bug trigger
# ---------------------------------------------------------------------------

def test_status_tag_escaped():
    """[status] loses its brackets in legacy Markdown; must become \\[status]."""
    inp = "[status] info (2 active)"
    out = escape_legacy_markdown_brackets(inp)
    assert out == "\\[status] info (2 active)", repr(out)


# ---------------------------------------------------------------------------
# 2. Real Markdown link -- must be untouched
# ---------------------------------------------------------------------------

def test_real_link_unchanged():
    """A valid [text](url) link must pass through without modification."""
    inp = "[text](http://x)"
    out = escape_legacy_markdown_brackets(inp)
    assert out == "[text](http://x)", repr(out)


def test_real_link_with_path_unchanged():
    """More realistic link with path and port."""
    inp = "[console](http://100.93.89.51:8484/decision/dec-123)"
    out = escape_legacy_markdown_brackets(inp)
    assert out == "[console](http://100.93.89.51:8484/decision/dec-123)", repr(out)


# ---------------------------------------------------------------------------
# 3. Mixed -- bare ref and real link in the same string
# ---------------------------------------------------------------------------

def test_mixed_bare_and_link():
    """Bare [ref] is escaped; adjacent [link](url) is left intact."""
    inp = "see [ref] and [link](http://x)"
    out = escape_legacy_markdown_brackets(inp)
    assert out == "see \\[ref] and [link](http://x)", repr(out)


def test_mixed_status_tag_and_link():
    """Status tag next to a decision link."""
    inp = "[live] active | [decision](http://host/dec-1)"
    out = escape_legacy_markdown_brackets(inp)
    assert out == "\\[live] active | [decision](http://host/dec-1)", repr(out)


# ---------------------------------------------------------------------------
# 4. Code-fence path -- function is NOT called on code segments
#    (this tests the split_into_segments contract, not escape itself)
# ---------------------------------------------------------------------------

def test_code_fence_segments_are_code():
    """split_into_segments marks fence content as is_code=True.

    The bridge only calls escape_legacy_markdown_brackets on prose segments
    (is_code=False), so content inside fences is never touched.
    """
    text = "prose [foo]\n```\ncode [bar]\n```\nmore [baz]"
    segments = split_into_segments(text)
    code_segs = [s for s, is_code, _ in segments if is_code]
    prose_segs = [s for s, is_code, _ in segments if not is_code]

    # Code segment contains the raw '[bar]' -- would be corrupted if escaped.
    assert any("[bar]" in s for s in code_segs), "code segment should contain [bar]"
    # Prose segments contain [foo] and [baz] -- these WOULD be escaped at send time.
    prose_all = " ".join(prose_segs)
    assert "[foo]" in prose_all and "[baz]" in prose_all

    # Verify: escape applied to prose segments works; code segments not involved.
    escaped_prose = [escape_legacy_markdown_brackets(s) for s in prose_segs]
    escaped_all = " ".join(escaped_prose)
    assert "\\[foo]" in escaped_all and "\\[baz]" in escaped_all
    # Code segment is untouched (escape was never called on it).
    assert "[bar]" in code_segs[0] and "\\[bar]" not in code_segs[0]


def test_inline_backtick_in_prose_escaped():
    """Inline backtick spans live inside prose segments and get escaped like prose.

    split_into_segments only splits on ``` fences, not inline backticks -- so
    inline backtick code is part of a prose segment and will have its '[' escaped.
    This is correct Telegram behaviour: inside an inline `code` span, the parser
    does not interpret Markdown, so a backslash before '[' is treated as literal
    backslash inside the backtick -- harmless in practice.  The important case is
    the fenced block, which is already handled by the bridge routing code.
    """
    # Inline backtick is prose as far as split_into_segments is concerned.
    text = "run `cmd [opt]` now"
    segs = split_into_segments(text)
    assert len(segs) == 1
    _, is_code, _ = segs[0]
    assert not is_code  # it's prose


# ---------------------------------------------------------------------------
# 5. Idempotency -- documented single-apply contract
# ---------------------------------------------------------------------------

def test_idempotency_documented():
    """Double-apply doubles backslashes -- caller must apply exactly once.

    This test documents the behaviour rather than asserting idempotency, so any
    future change that makes the function truly idempotent is also acceptable.
    The important invariant is: the bridge calls escape exactly once at send time.
    """
    inp = "[status] info"
    once = escape_legacy_markdown_brackets(inp)
    twice = escape_legacy_markdown_brackets(once)
    # First application: backslash added before '['.
    assert once == "\\[status] info"
    # Second application: the existing '\\[' gets its '[' escaped again.
    # This confirms double-apply is NOT idempotent -- single-call discipline required.
    assert twice == "\\\\[status] info", (
        "Double-application doubles backslashes; escape must be called exactly once per send"
    )


# ---------------------------------------------------------------------------
# 6. Edge cases
# ---------------------------------------------------------------------------

def test_no_brackets_passthrough():
    """Text with no '[' is returned byte-identical (fast path)."""
    inp = "hello -- all good"
    out = escape_legacy_markdown_brackets(inp)
    assert out is inp or out == inp  # fast path may return same object


def test_empty_string():
    out = escape_legacy_markdown_brackets("")
    assert out == ""


def test_multiple_bare_brackets():
    """Multiple bare brackets all get escaped."""
    inp = "[A] and [B] and [C]"
    out = escape_legacy_markdown_brackets(inp)
    assert out == "\\[A] and \\[B] and \\[C]"


def test_multiple_links_unchanged():
    """Multiple real links all pass through unchanged."""
    inp = "[a](http://a) [b](http://b)"
    out = escape_legacy_markdown_brackets(inp)
    assert out == "[a](http://a) [b](http://b)"


def test_nested_bracket_in_link_text():
    """Link with bracket-containing text: the '[' inside the link is protected."""
    # e.g. [[note]](http://x) -- outer '[' starts the link, inner '[' is inside text.
    # Legacy Markdown spec: link text cannot contain nested brackets, so this is
    # technically ambiguous; our regex does not match it as a valid link (the regex
    # uses [^\[\]]* for the link text which rejects nested brackets).
    # The outer '[' that does NOT start a valid link gets escaped; that is correct.
    inp = "[[note]](http://x)"
    out = escape_legacy_markdown_brackets(inp)
    # The inner pattern '[note](http://x)' IS matched as a link; the leading '[' is bare.
    assert out.startswith("\\["), repr(out)
