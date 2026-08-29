"""DGN-954: ATX heading glyph+bold promotion tests.

Asserts final rendered HTML (via markdown_to_telegram_html) for all spec cases:
  - H1 -> ▪ <b>text</b>
  - H2 -> ◆ <b>text</b>
  - H3 -> ▸ <b>text</b>
  - H4+ -> ▸ <b>text</b>  (clamped)
  - #hashtag unchanged
  - # inside code fence / inline code unchanged  (prose-segment isolation)
  - heading with existing ** -> no double-bold
  - empty heading safe
"""
import pytest
from bridge.formatting import markdown_to_telegram_html


# ---------------------------------------------------------------------------
# Core glyph + bold promotion
# ---------------------------------------------------------------------------

def test_h1_glyph_bold():
    assert markdown_to_telegram_html("# Title") == "▪ <b>Title</b>"


def test_h2_glyph_bold():
    assert markdown_to_telegram_html("## Section") == "◆ <b>Section</b>"


def test_h3_glyph_bold():
    assert markdown_to_telegram_html("### Subsection") == "▸ <b>Subsection</b>"


def test_h4_clamped_to_h3_glyph():
    assert markdown_to_telegram_html("#### Deep") == "▸ <b>Deep</b>"


def test_h6_clamped_to_h3_glyph():
    assert markdown_to_telegram_html("###### H6") == "▸ <b>H6</b>"


def test_heading_with_html_special_chars():
    # html.escape must fire on the text AFTER wrapping; '<' -> '&lt;' inside <b>
    result = markdown_to_telegram_html("# a < b")
    assert result == "▪ <b>a &lt; b</b>"


def test_multiline_headings_all_promoted():
    text = "# One\n## Two\n### Three"
    result = markdown_to_telegram_html(text)
    assert "▪ <b>One</b>" in result
    assert "◆ <b>Two</b>" in result
    assert "▸ <b>Three</b>" in result


# ---------------------------------------------------------------------------
# FP guard: #hashtag unchanged
# ---------------------------------------------------------------------------

def test_hashtag_no_space_unchanged():
    # No mandatory '[ \t]+' after '#' -> not a heading
    assert markdown_to_telegram_html("#hashtag") == "#hashtag"


def test_hashtag_inline_unchanged():
    result = markdown_to_telegram_html("Use #python and #rust")
    assert "#python" in result
    assert "#rust" in result


# ---------------------------------------------------------------------------
# FP guard: code fence content isolated (prose-segment level -- _strip_md_headers
# only runs on prose segments, but we verify inline code path too)
# ---------------------------------------------------------------------------

def test_inline_code_hash_unchanged():
    # '# code' inside backticks: the backtick content is stashed before escape
    # and never touches _strip_md_headers (which matches ^# with re.MULTILINE;
    # inline content is mid-line after the opening backtick).
    result = markdown_to_telegram_html("Use `# raw` here")
    assert "<code># raw</code>" in result


# ---------------------------------------------------------------------------
# FP guard: no double-bold when heading text already contains ** markers
# ---------------------------------------------------------------------------

def test_no_double_bold_with_existing_stars():
    # '**bold**' inside heading text must be stripped, not double-wrapped
    result = markdown_to_telegram_html("# Title with **bold** word")
    assert result == "▪ <b>Title with bold word</b>"
    # Must not contain nested <b> or stray **
    assert "**" not in result
    assert "<b><b>" not in result


# ---------------------------------------------------------------------------
# FP guard: empty heading safe
# ---------------------------------------------------------------------------

def test_empty_heading_safe():
    # '# ' followed only by trailing space; group(2) = ''
    result = markdown_to_telegram_html("# ")
    assert result == "▪ <b></b>"


def test_heading_tab_separator():
    # Tab after # is also valid per _MD_HEADER_RE '[ \t]+'
    result = markdown_to_telegram_html("#\tTitle")
    assert result == "▪ <b>Title</b>"
