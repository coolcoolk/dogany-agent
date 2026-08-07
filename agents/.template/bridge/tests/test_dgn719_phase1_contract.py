"""DGN-719 phase-1: bridge output contract (semantic core <-> render bridge).

Covers the phase-1 defensive layer:
  a1 CONTRACT_VERSION handshake + check_contract_skew()
  a2 unknown-marker containment (version-skew safety net)
  a3 fold typed-block (compose_fold_block / render_fold_block)
  a4 link_preview:: as a recognized contract construct
  + an explicit backward-compat PARITY guard: the contract layer must not
    change any output the render layer already produces today.

Phase-1 does NOT retrain RULES; it wraps/protects the existing DGN-376/619/682/
699 render layer. Recognized markers pass through untouched; only markers this
render layer does not know are downgraded to safe literals.
"""

import logging

from bridge.formatting import (
    CONTRACT_VERSION,
    FOLD_CLOSE_MARKER,
    FOLD_OPEN_MARKER,
    LINK_PREVIEW_MARKER,
    RECOGNIZED_BRACKET_MARKERS,
    RECOGNIZED_COLON_MARKERS,
    check_contract_skew,
    compose_fold_block,
    contain_unknown_markers,
    markdown_to_telegram_html as md2html,
    render_fold_block,
)
from bridge.options import OPTIONS_MARKER

_ZWSP = "\u200b"


# ---------------------------------------------------------------------------
# a1: CONTRACT_VERSION handshake
# ---------------------------------------------------------------------------

def test_contract_version_is_declared_int():
    assert isinstance(CONTRACT_VERSION, int)
    assert CONTRACT_VERSION >= 1


def test_skew_none_and_equal_and_older_core_are_compatible():
    # Un-stamped / legacy core, exact match, and an OLDER core all pass.
    assert check_contract_skew(None) is False
    assert check_contract_skew(CONTRACT_VERSION) is False
    assert check_contract_skew(CONTRACT_VERSION - 1) is False


def test_skew_newer_core_is_detected_and_logged(caplog):
    # Core authored against a newer vocabulary than this frozen render layer
    # (the DGN-696 drift shape) is detected and logged.
    with caplog.at_level(logging.WARNING):
        assert check_contract_skew(CONTRACT_VERSION + 1) is True
    assert any("skew" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# a2: unknown-marker containment (version-skew safety net)
# ---------------------------------------------------------------------------

def test_recognized_markers_pass_through_untouched():
    # Recognized markers (colon + bracket) are NOT downgraded here -- they are
    # stripped by their own handlers upstream; the net must leave them alone.
    for marker in RECOGNIZED_COLON_MARKERS:
        out, n = contain_unknown_markers(marker + " payload")
        assert (out, n) == (marker + " payload", 0)
    for marker in RECOGNIZED_BRACKET_MARKERS:
        out, n = contain_unknown_markers(marker)
        assert (out, n) == (marker, 0)


def test_unknown_colon_marker_downgraded_to_literal():
    out, n = contain_unknown_markers("wibble:: do a thing")
    assert n == 1
    # Marker word broken by a zero-width space so no later scan re-parses it.
    assert out == "w" + _ZWSP + "ibble:: do a thing"
    assert not out.startswith("wibble::")


def test_unknown_bracket_marker_downgraded_to_literal():
    out, n = contain_unknown_markers("[[MYSTERY]]")
    assert n == 1
    assert out == "[" + _ZWSP + "[MYSTERY]]"


def test_containment_preserves_indentation_and_other_lines():
    src = "before\n  ghost:: x\nafter"
    out, n = contain_unknown_markers(src)
    assert n == 1
    assert out == "before\n  g" + _ZWSP + "host:: x\nafter"


def test_inline_colon_prose_is_never_touched():
    # "::" that is not a standalone marker line stays byte-identical: URLs,
    # mid-sentence double-colons, and code-ish text must survive.
    for src in [
        "see http://example.com now",
        "Note:: this is inline, not a marker line",
        "a::b in the middle of prose",
        "no markers here at all",
    ]:
        assert contain_unknown_markers(src) == (src, 0)


def test_unknown_marker_never_raw_relayed_through_converter():
    # End-to-end: an unknown marker line survives md2html as visible literal
    # text (zero-width-broken), never a live directive. The rest converts
    # normally.
    html = md2html("hello\nzap:: fire the laser\n**bold** end")
    assert "zap::" not in html  # the live token is broken
    assert "z" + _ZWSP + "ap::" in html
    assert "<b>bold</b>" in html


# ---------------------------------------------------------------------------
# a3: fold typed-block
# ---------------------------------------------------------------------------

def test_compose_fold_block_shape():
    block = compose_fold_block("summary line", "body 1\nbody 2")
    assert block == (
        FOLD_OPEN_MARKER + " summary line\nbody 1\nbody 2\n" + FOLD_CLOSE_MARKER
    )


def test_compose_fold_block_empty_summary_is_empty():
    assert compose_fold_block("   ", "body") == ""


def test_render_fold_block_to_expandable_sentinel():
    # The typed block transpiles to the DGN-619 `>! ` expandable run.
    block = compose_fold_block("summary", "line a\nline b")
    rendered = render_fold_block(block)
    assert rendered == ">! summary\n> line a\n> line b"


def test_render_fold_block_blank_body_line_is_bare_quote():
    block = compose_fold_block("s", "a\n\nb")
    assert render_fold_block(block) == ">! s\n> a\n>\n> b"


def test_render_fold_block_end_to_end_matches_handwritten_fold():
    # a3 backward-compat: a fold:: typed block renders IDENTICALLY to a
    # hand-written `>! ` run through markdown_to_telegram_html.
    typed = md2html(compose_fold_block("secret", "more"))
    handwritten = md2html(">! secret\n> more")
    assert typed == handwritten
    assert typed == "<blockquote expandable>secret\nmore</blockquote>"


def test_render_fold_block_unterminated_is_left_literal():
    # A fold:: open with no fold::end must NOT open a dangling blockquote.
    src = "fold:: dangling\nno close here"
    assert render_fold_block(src) == src


def test_text_without_fold_marker_is_unchanged():
    src = "ordinary text\nwith lines"
    assert render_fold_block(src) == src


# ---------------------------------------------------------------------------
# a4: link_preview:: as a contract construct
# ---------------------------------------------------------------------------

def test_link_preview_is_a_recognized_contract_marker():
    assert LINK_PREVIEW_MARKER == "link_preview::"
    assert LINK_PREVIEW_MARKER in RECOGNIZED_COLON_MARKERS


def test_link_preview_not_downgraded_by_containment():
    # a4 x a2: the link_preview:: control marker must be recognized, so the
    # containment net leaves it intact (its own handler strips it upstream).
    out, n = contain_unknown_markers("link_preview::")
    assert (out, n) == ("link_preview::", 0)


def test_options_and_send_file_recognized_too():
    assert OPTIONS_MARKER in RECOGNIZED_BRACKET_MARKERS
    out, n = contain_unknown_markers("send_file:: /tmp/x.png")
    assert (out, n) == ("send_file:: /tmp/x.png", 0)


# ---------------------------------------------------------------------------
# BACKWARD-COMPAT PARITY GUARD
# ---------------------------------------------------------------------------
# Every output the render layer produces today must be byte-identical after the
# contract layer wraps it. These inputs carry NO contract markers, so md2html
# must return exactly what the pre-phase-1 render layer returned (the same
# literals pinned in test_dgn719_render_parity.py). If the a2/a3 pre-passes ever
# touch ordinary prose, this breaks.

_PARITY_PINS = [
    ("a **bold** b", "a <b>bold</b> b"),
    ("a *ital* b", "a <i>ital</i> b"),
    ("a *two words* b", "a *two words* b"),
    ("a ~~gone~~ b", "a <s>gone</s> b"),
    ("run `x<y&z`", "run <code>x&lt;y&amp;z</code>"),
    (
        "[dec-094](http://console.example.com:8484/decision/dec-094)",
        '<a href="http://console.example.com:8484/decision/dec-094">dec-094</a>',
    ),
    ("- top\n  - child\n    - grand", "• top\n  ◦ child\n    ‣ grand"),
    ("1. first\n2. second", "1. first\n2. second"),
    ("> a\n> b\n> c", "<blockquote>a\nb\nc</blockquote>"),
    (">! secret\n> more", "<blockquote expandable>secret\nmore</blockquote>"),
    (
        "mixed\n\n- list *w*\n> quote **b**\n\n`code` and [l](https://x.io) done",
        "mixed\n\n• list <i>w</i>\n<blockquote>quote <b>b</b></blockquote>"
        '\n\n<code>code</code> and <a href="https://x.io">l</a> done',
    ),
]


def test_backward_compat_parity_no_regression():
    for src, expected in _PARITY_PINS:
        assert md2html(src) == expected, f"contract layer regressed input: {src!r}"


def test_url_with_double_colon_survives_parity():
    # A URL contains "://" but is not a standalone marker line -> untouched.
    src = "check https://x.io:8484/path here"
    assert md2html(src) == "check https://x.io:8484/path here"
