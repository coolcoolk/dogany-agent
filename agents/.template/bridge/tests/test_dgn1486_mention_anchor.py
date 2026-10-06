"""DGN-1486: @mention dies when Hangul (or any non-ASCII alnum) is glued to it.

Background
----------
Telegram's automatic entity parser absorbs a non-ASCII alphanumeric character
glued directly onto a `@username` as part of the username itself, so
"@BotFather<hangul>" is read as one invalid (non-ASCII) username and NO
mention entity is produced -- the tap is dead. Measured via real Bot API
sendMessage + entities in the response (ticket section "The bug"):
    @BotFather로     -> no mention entity   (dead)
    @BotFather 로    -> mention             (alive, space terminates)
    @BotFather,      -> mention             (alive, punctuation terminates)

Locked design (implement verbatim, surgical): promote to an explicit anchor
ONLY in the glued shape where the automatic entity fails. Every currently
working shape must come out byte-identical.
    - Trigger: `@<username>` immediately followed by a non-ASCII alnum.
    - Username grammar: [A-Za-z][A-Za-z0-9_]{4,31} (Telegram 5-32 chars).
    - Left boundary: preceding char in [A-Za-z0-9_@./-] kills the match
      (email false positive `a@b.co`, URL-path false positive `t.me/@x`).
    - Output: <a href="https://t.me/NAME">@NAME</a>
    - Insertion point: immediately after the markdown-link stash (step 4),
      before dash normalization / emphasis, inside markdown_to_telegram_html.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import os  # noqa: E402

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge.formatting import markdown_to_telegram_html as md2html  # noqa: E402


# ---------------------------------------------------------------------------
# 1-2: the glued (dead) shape gets an explicit anchor.
# ---------------------------------------------------------------------------

def test_hangul_glued_directly_gets_anchor():
    out = md2html("@BotFather로")
    assert out == '<a href="https://t.me/BotFather">@BotFather</a>로'


def test_hangul_glued_multi_char_gets_anchor():
    out = md2html("@BotFather에서")
    assert out == '<a href="https://t.me/BotFather">@BotFather</a>에서'


def test_hangul_glued_mid_sentence():
    out = md2html("@BotFather로 가세요")
    assert out == '<a href="https://t.me/BotFather">@BotFather</a>로 가세요'


# ---------------------------------------------------------------------------
# 3-5: already-working shapes are byte-identical (UNCHANGED).
# ---------------------------------------------------------------------------

def test_space_before_hangul_unchanged():
    assert md2html("@BotFather 로") == "@BotFather 로"


def test_comma_terminator_unchanged():
    assert md2html("@BotFather,") == "@BotFather,"


def test_period_terminator_unchanged():
    assert md2html("@BotFather.") == "@BotFather."


def test_hyphen_terminator_unchanged():
    assert md2html("@BotFather-로") == "@BotFather-로"


def test_end_of_string_unchanged():
    assert md2html("@BotFather") == "@BotFather"


def test_ascii_alnum_after_name_unchanged():
    # Part of the real username -- must not be touched.
    assert md2html("@BotFather9") == "@BotFather9"
    assert md2html("@BotFather_x") == "@BotFather_x"


# ---------------------------------------------------------------------------
# 6-7: left-boundary rejection (email / URL-path false positives).
# ---------------------------------------------------------------------------

def test_email_left_boundary_rejected():
    assert md2html("a@bcdef.com로") == "a@bcdef.com로"


def test_url_path_left_boundary_rejected():
    assert md2html("t.me/@BotFather로") == "t.me/@BotFather로"


# ---------------------------------------------------------------------------
# 8-10: structurally unreachable regions (code / fenced / stashed links).
# ---------------------------------------------------------------------------

def test_inline_code_untouched():
    out = md2html("`@BotFather로`")
    assert out == "<code>@BotFather로</code>"


def test_fenced_code_not_this_function():
    # Fenced blocks never reach markdown_to_telegram_html -- they go through
    # code_segment_html via split_into_segments in the caller. Confirm the
    # mention regex plays no role by checking the segment split itself.
    from bridge.formatting import split_into_segments

    segments = split_into_segments("```\n@BotFather로\n```")
    assert segments == [("@BotFather로", True, None)]


def test_markdown_link_text_stashed_whole_no_nested_anchor():
    out = md2html("[@BotFather로](https://example.com)")
    assert out == '<a href="https://example.com">@BotFather로</a>'
    assert out.count("<a ") == 1


# ---------------------------------------------------------------------------
# 11: too-short username never matches Telegram's 5-32 char grammar.
# ---------------------------------------------------------------------------

def test_too_short_username_unchanged():
    assert md2html("@abc로") == "@abc로"


# ---------------------------------------------------------------------------
# 12: bold wrapping and the mention anchor both survive, well-formed tags.
# ---------------------------------------------------------------------------

def test_bold_wrapped_mention_anchor_survives():
    out = md2html("**@BotFather로**")
    assert out == '<b><a href="https://t.me/BotFather">@BotFather</a>로</b>'
