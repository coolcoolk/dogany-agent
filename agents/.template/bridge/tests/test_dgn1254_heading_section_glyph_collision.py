"""DGN-1254: heading glyph x section glyph collision guard.

'## ✅ 결론' was rendering as '▪ ✅ 결론' -- the ATX heading promotion (DGN-954)
stacked its own glyph in front of a SECTION_GLYPHS label that already carried
one. Per ticket 5.2 (owner-confirmed): the machine absorbs the collision --
when heading text already opens with a SECTION_GLYPHS glyph, _strip_md_headers
skips the heading glyph and keeps the section glyph in its place. Bold
promotion still applies. No rule-doc prohibition on '##' + glyph is added.
"""
import pytest
from bridge.formatting import SECTION_GLYPHS, markdown_to_telegram_html


# ---------------------------------------------------------------------------
# Palette x heading level: 3 glyphs x 3 levels -- no heading glyph, bold kept
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("glyph", SECTION_GLYPHS)
@pytest.mark.parametrize("hashes", ["#", "##", "###"])
def test_section_glyph_heading_absorbs_collision(glyph, hashes):
    result = markdown_to_telegram_html(f"{hashes} {glyph} 결론")
    assert result == f"{glyph} <b>결론</b>"
    # No heading glyph from the DGN-954 palette should appear.
    for header_glyph in ("■", "▪", "▫"):
        assert header_glyph not in result


# ---------------------------------------------------------------------------
# Non-regression baseline: glyph-less headings keep the DGN-954 behavior
# ---------------------------------------------------------------------------

def test_h1_no_section_glyph_unchanged():
    assert markdown_to_telegram_html("# 결론") == "■ <b>결론</b>"


def test_h2_no_section_glyph_unchanged():
    assert markdown_to_telegram_html("## 결론") == "▪ <b>결론</b>"


def test_h3_no_section_glyph_unchanged():
    assert markdown_to_telegram_html("### 결론") == "▫ <b>결론</b>"


# ---------------------------------------------------------------------------
# FP guard: palette-external emoji does NOT trigger the absorption
# ---------------------------------------------------------------------------

def test_non_palette_emoji_heading_keeps_heading_glyph():
    # 😀 is not in SECTION_GLYPHS -- the H2 heading glyph must still be added.
    result = markdown_to_telegram_html("## 😀 결론")
    assert result == "▪ <b>😀 결론</b>"


# ---------------------------------------------------------------------------
# Section glyph heading with existing bold markers -- no double-bold / broken tags
# ---------------------------------------------------------------------------

def test_section_glyph_heading_with_existing_bold_stars():
    result = markdown_to_telegram_html("## ✅ **결론**")
    assert result == "✅ <b>결론</b>"
    assert "**" not in result
    assert "<b><b>" not in result


# ---------------------------------------------------------------------------
# Existing DGN-954 contracts must survive unchanged
# ---------------------------------------------------------------------------

def test_hashtag_no_space_unchanged():
    assert markdown_to_telegram_html("#hashtag") == "#hashtag"


def test_inline_code_hash_unchanged():
    result = markdown_to_telegram_html("Use `# raw` here")
    assert "<code># raw</code>" in result


def test_code_fence_content_unaffected():
    from bridge.formatting import code_segment_html

    result = code_segment_html("## ✅ 결론", lang=None)
    assert result == "<pre>## ✅ 결론</pre>"
