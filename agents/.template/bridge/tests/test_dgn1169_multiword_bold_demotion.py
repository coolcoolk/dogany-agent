"""DGN-1169: multi-word `*...*` is DEMOTED TO BOLD, single-word stays italic.

Background
----------
DGN-619 made italic WORD-ONLY on an explicit owner rule ("italic is one short
word only").  The mechanism was a regex that forbids internal whitespace, so a
multi-word span matched nothing and fell through as LITERAL text -- the raw
asterisks reached the screen.  DGN-376 measured that leak at 480 occurrences in
one month of assistant output, rising.  telegram.md already told authors "use
bold for multi-word emphasis"; DGN-1169 makes the machine do it instead of
asking.

What this file pins
-------------------
1. Single-word `*word*` -> <i> (UNCHANGED -- the DGN-619 rule survives).
2. Multi-word `*two words*` -> <b> (new).
3. Underscore is NOT demoted (deliberate asymmetry; rationale lives with the
   regex in formatting.py -- unmeasured demand + identifier collisions).
4. The plain-text streaming fallback strips the markers too, so a multi-word
   span never shows raw asterisks in the live bubble either.
5. Regression defence, every item from the ticket's "must not break" list:
   `**bold**` (19,321 live uses) still wins because bold runs first; asterisks
   inside backticks are never converted; plain-text glob patterns
   (`*.db`, `pack/*`, `src/*.py`) do NOT newly pair up now that internal
   whitespace is allowed; [[OPTIONS]] markers, fold blocks and passthrough
   HTML are untouched.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

import pytest

from bridge.formatting import (  # noqa: E402
    balance_telegram_html,
    demark_markdown_for_stream as demark,
    markdown_to_telegram_html as md2html,
    render_fold_block,
    sanitize_message_for_telegram,
)


# ---------------------------------------------------------------------------
# 1. Required behaviour: single word -> italic (unchanged), multi -> bold (new)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "src,expected",
    [
        ("a *one* b", "a <i>one</i> b"),
        ("*one*", "<i>one</i>"),
        ("(*one*)", "(<i>one</i>)"),
        ('*"quoted"*', '<i>"quoted"</i>'),
        ("- a *word* here", "• a <i>word</i> here"),
    ],
)
def test_single_word_star_still_italic(src, expected):
    """DGN-619 word-only italic: byte-identical to pre-DGN-1169 output."""
    assert md2html(src) == expected


@pytest.mark.parametrize(
    "src,expected",
    [
        ("a *two words* b", "a <b>two words</b> b"),
        ("*two words*", "<b>two words</b>"),
        ("*a b c d e*", "<b>a b c d e</b>"),
        # The measured shape: a quoted multi-word span (DGN-376, 480 hits).
        ('*"인용 구절"* 이다', '<b>"인용 구절"</b> 이다'),
        # Korean glues particles straight onto the closing marker; the right
        # flank must stay permissive or the corpus this ticket targets misses.
        ("*두 단어*를 봐", "<b>두 단어</b>를 봐"),
        # Structural pre-pass stashes the bullet / blockquote tag right before
        # the opener, so the left flank must accept a stash placeholder.
        ("- *two words* here", "• <b>two words</b> here"),
        ("> *two words* q", "<blockquote><b>two words</b> q</blockquote>"),
        # Inner HTML specials are still escaped inside the demoted span.
        ("*a & b*", "<b>a &amp; b</b>"),
        ("*a <x> b*", "<b>a &lt;x&gt; b</b>"),
    ],
)
def test_multi_word_star_demoted_to_bold(src, expected):
    assert md2html(src) == expected


def test_multi_word_star_never_emits_italic():
    """The owner rule is 'italic is one short word' -- demotion must not widen it."""
    assert "<i>" not in md2html("a *two words* b")
    assert "<i>" not in md2html("*a very long multi word emphasis span*")


def test_single_word_star_never_emits_bold():
    assert "<b>" not in md2html("a *one* b")


# ---------------------------------------------------------------------------
# 2. Underscore is deliberately NOT demoted
# ---------------------------------------------------------------------------

def test_multi_word_underscore_stays_literal():
    """Asymmetry is intentional: unmeasured demand, identifier collisions."""
    assert md2html("a _two words_ b") == "a _two words_ b"
    assert md2html("_two words_") == "_two words_"


def test_single_word_underscore_still_italic():
    assert md2html("a _one_ b") == "a <i>one</i> b"


def test_underscore_identifier_pair_would_have_bolded_if_symmetric():
    """The concrete case that keeps underscore out.

    `_stash` and `count_` are ordinary bare identifiers in this codebase's
    prose.  Under a symmetric rule they are a legal open/close pair and the
    sentence between them would bold.  Pinned so a future 'make it symmetric'
    change has to argue with this line.
    """
    src = "헬퍼는 _stash 이고 접미사는 count_ 다"
    assert md2html(src) == src
    assert "<b>" not in md2html(src)


# ---------------------------------------------------------------------------
# 3. Plain-text streaming fallback: markers must not survive as literals
# ---------------------------------------------------------------------------

def test_demark_strips_multi_word_star_markers():
    assert demark("This is *two words* here") == "This is two words here"
    assert "*" not in demark("보고는 *두 단어 강조* 입니다")


def test_demark_keeps_single_word_behaviour():
    assert demark("This is *great*") == "This is great"
    assert demark("This is _great_") == "This is great"


def test_demark_leaves_underscore_multi_word_literal():
    # Mirrors the render path exactly: underscore is not part of the demotion.
    assert demark("a _two words_ b") == "a _two words_ b"


def test_demark_does_not_eat_glob_patterns():
    assert demark("pack/* 와 kit/* 를") == "pack/* 와 kit/* 를"
    assert demark("*.db 와 pack/*") == "*.db 와 pack/*"


# ---------------------------------------------------------------------------
# 4. Regression defence -- the ticket's "must not break" list
# ---------------------------------------------------------------------------

def test_bold_still_wins_over_demotion():
    """`**bold**` is 19,321 live uses. Bold runs FIRST; the demotion may only
    ever see what the bold pass left behind."""
    assert md2html("a **bold** b") == "a <b>bold</b> b"
    assert md2html("a __bold__ b") == "a <b>bold</b> b"
    assert md2html("**두 단어 볼드**") == "<b>두 단어 볼드</b>"
    # Bold immediately followed by a demoted span: both survive, independently.
    assert md2html("**볼드** 와 *두 단어* 를") == "<b>볼드</b> 와 <b>두 단어</b> 를"
    # Bold wrapping a single-word italic keeps the DGN-376 nesting pin.
    assert md2html("**a *b* c**") == "<b>a <i>b</i> c</b>"
    # Triple asterisk has no bold/italic reading today and must gain none.
    assert md2html("***x***") == "***x***"


@pytest.mark.parametrize(
    "src,expected",
    [
        ("`*.sql` 파일", "<code>*.sql</code> 파일"),
        ("`pack/lifekit/*` 경로", "<code>pack/lifekit/*</code> 경로"),
        ("run `a *two words* b` ok", "run <code>a *two words* b</code> ok"),
        ("차트의 `*` 항목", "차트의 <code>*</code> 항목"),
    ],
)
def test_asterisks_inside_inline_code_are_never_converted(src, expected):
    """Inline code is stashed before the emphasis passes; 38 of the 86 measured
    (a)-cases were exactly this glob/code shape."""
    out = md2html(src)
    assert out == expected
    assert "<b>" not in out and "<i>" not in out


@pytest.mark.parametrize(
    "src",
    [
        # THE headline risk of this ticket: allowing internal whitespace lets
        # two UNRELATED glob asterisks pair up across tokens. Every one of these
        # must come back byte-identical.
        "pack/* 와 kit/* 를",
        "*.db 와 pack/*",
        "*.sql 과 *.db 를 비교",
        "src/*.py 와 tests/*.py 를",
        "**/*.py 전체",
        "rm -rf build/* 하고 나서 dist/* 도",
        # Multiplication / footnote asterisks in prose.
        "x * y * z",
        "2*3 와 4*5",
        "3개*, 총합은 10*",
        "가격은 100원* 이고 배송비는 별도*",
        # Python star-args in prose.
        "*args 와 **kwargs 를",
    ],
)
def test_plain_text_globs_do_not_newly_pair_up(src):
    assert md2html(src) == src
    assert "<b>" not in md2html(src)
    assert "<i>" not in md2html(src)


def test_preexisting_single_word_glob_false_positive_is_unchanged():
    """Honest pin, NOT an endorsement.

    `*.own.*` has no internal whitespace, so the DGN-619 word-only regex
    already accepted it as italic long before this ticket -- the opener guard
    there has no '.' / '/' exclusion. DGN-1169 neither creates nor fixes it:
    the multi-word pass never sees this span because the single-word pass
    consumed it first. Pinned so a future reader can tell a pre-existing
    DGN-619 false positive apart from one this change introduced, and so
    fixing it later is a deliberate, visible edit.
    """
    src = "로더는 hot.* 만 부르고 업데이트는 *.own.* 을 안 덮는다"
    assert md2html(src) == "로더는 hot.* 만 부르고 업데이트는 <i>.own.</i> 을 안 덮는다"
    assert "<b>" not in md2html(src)  # the demotion added nothing here


def test_options_marker_untouched():
    src = "고를까요?\n\n[[OPTIONS]]\n1. *두 단어* 예\n2. 아니오"
    out = md2html(src)
    assert "[[OPTIONS]]" in out
    assert "<b>두 단어</b>" in out


def test_fold_block_untouched():
    src = "fold:: 제목\n본문 *두 단어* 임\nfold::end"
    folded = render_fold_block(src)
    # The fold transpile still produces the >! expandable-blockquote run...
    assert folded == ">! 제목\n> 본문 *두 단어* 임"
    # ...and the demotion then runs on the folded body, inside the blockquote.
    out = md2html(src)
    assert out == (
        "<blockquote expandable>제목\n본문 <b>두 단어</b> 임</blockquote>"
    )


def test_passthrough_html_tags_still_pass():
    assert md2html("<b>x</b> and <i>y</i>") == "<b>x</b> and <i>y</i>"
    assert md2html("<tg-spoiler>s</tg-spoiler>") == "<tg-spoiler>s</tg-spoiler>"


def test_bold_and_strike_tags_are_never_swallowed_into_a_demoted_span():
    """A span whose content would have to cross a real tag emitted by the bold
    or strike pass is left literal -- exactly its pre-DGN-1169 rendering --
    rather than producing <b> nested inside <b>."""
    assert md2html("*a **b** c*") == "*a <b>b</b> c*"
    assert md2html("*a ~~b~~ c*") == "*a <s>b</s> c*"


def test_demoted_output_is_balanced_html():
    for src in ("a *two words* b", "- *two words*", "> *two words*", "*a & b*"):
        out = md2html(src)
        assert balance_telegram_html(out) == out
        assert out.count("<b>") == out.count("</b>")


# ---------------------------------------------------------------------------
# 5. Blast-radius cap
# ---------------------------------------------------------------------------

def test_overlong_span_is_left_literal():
    """A stray asterisk pair must not be able to bold a whole paragraph.
    Over the cap the span keeps its pre-DGN-1169 literal rendering.

    Corpus check (2026-08-31): the longest span this rule actually demotes in
    27,198 real assistant messages is 144 chars, so the cap costs nothing real.
    """
    inner = "word " * 60  # 300 chars
    src = "*{}end*".format(inner)
    assert md2html(src) == src
    short = "*{}end*".format("word " * 30)  # 150 chars + "end"
    assert md2html(short) == "<b>{}end</b>".format("word " * 30)


# ---------------------------------------------------------------------------
# 6. The shell sanitize rail (routines/push.sh) sees the demotion too
# ---------------------------------------------------------------------------

def test_sanitize_message_for_telegram_demotes():
    out = sanitize_message_for_telegram("a *two words* b")
    assert "<b>two words</b>" in out
    assert "*two words*" not in out
