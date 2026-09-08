"""DGN-846: streamed-prose finalize must NOT skip the HTML edit when the text
carries whitelisted Telegram HTML tags.

The version-check update notice is relayed verbatim by the agent and contains
<blockquote expandable>...</blockquote>. Its markdown conversion is a no-op
(the tags pass the whitelist verbatim, nothing else converts), so the old
`converted == display -> skip edit` fast path left the PLAIN streamed draft as
the final bubble -- raw tags visible (the DGN-788 leak that dec-117 papered
over with plaintext). The fix gates the skip on contains_telegram_html().

Covers:
  1. contains_telegram_html(): tag detection on/off.
  2. Leak precondition documented: notice conversion is byte-identical.
  3. _edit_streamed_prose_html: notice -> HTML edit IS performed (no skip);
     plain tag-free text -> skip preserved (no needless edit).
  4. Raw-leak zero on the fresh-send render: exactly one balanced
     <blockquote expandable> pair, no escaped tag leakage, adversarial note
     content still escaped.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import os  # noqa: E402

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge.formatting import (  # noqa: E402
    contains_telegram_html,
    markdown_to_telegram_html,
)

# Owner-confirmed 2026-08-12 (DGN-846): header + action are PLAIN lines
# OUTSIDE the fold; ONLY the fold label + notes sit inside the
# <blockquote expandable>. No-notes case = 2 plain lines, no blockquote.
NOTICE_KO = (
    "도가니 업데이트 있어요 · v1.31.1\n"
    "지금 업데이트하실래요?\n"
    "<blockquote expandable>▸ 업데이트 노트\n"
    "usage-gate 과차단이 완화되고, 인지 출력 포맷이 정비됐어요.</blockquote>"
)
NOTICE_NO_NOTES = "도가니 업데이트 있어요 · v1.31.1\n지금 업데이트하실래요?"


def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


def _make_bot(bot_mod):
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    return bot


# --- 1. tag detection ---------------------------------------------------------

def test_contains_telegram_html_detects_notice_tags():
    assert contains_telegram_html(NOTICE_KO)
    assert contains_telegram_html("prose with <b>bold</b> tag")
    # No-notes notice is fully PLAIN (fold dropped) -> no tag to detect.
    assert not contains_telegram_html(NOTICE_NO_NOTES)


def test_contains_telegram_html_false_on_plain_text():
    assert not contains_telegram_html("도가니 업데이트 있어요 · v1.31.1\n지금 업데이트하실래요?")
    assert not contains_telegram_html("")
    # Non-whitelisted tag shapes are escaped by the converter, not passthrough.
    assert not contains_telegram_html("stray <div> tag and a < b comparison")


# --- 2. leak precondition: notice conversion is a no-op ------------------------

def test_notice_conversion_is_byte_identical():
    # This is exactly why the old skip leaked: converted == display holds for
    # the notice (its only markup is the whitelisted fold tag), yet the plain
    # draft shows that tag raw. The no-notes form is plain and also no-op.
    assert markdown_to_telegram_html(NOTICE_KO) == NOTICE_KO
    assert markdown_to_telegram_html(NOTICE_NO_NOTES) == NOTICE_NO_NOTES


# --- 3. finalize edit decision --------------------------------------------------

def _run_finalize(display):
    bot_mod = _load_bot()
    b = _make_bot(bot_mod)
    tg_bot = MagicMock()
    tg_bot.edit_message_text = AsyncMock()
    ok = asyncio.run(
        b._edit_streamed_prose_html(tg_bot, 42, display, False, [7])
    )
    return ok, tg_bot.edit_message_text


def test_notice_finalize_performs_html_edit():
    ok, edit = _run_finalize(NOTICE_KO)
    assert ok is True
    edit.assert_awaited_once()
    kwargs = edit.await_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    assert "<blockquote expandable>" in kwargs["text"]


def test_plain_text_finalize_still_skips_noop_edit():
    ok, edit = _run_finalize("한 줄 평문 답변입니다.")
    assert ok is True
    edit.assert_not_awaited()


# --- 4. raw-leak zero on the fresh-send render ----------------------------------

def test_fresh_send_render_no_raw_leak():
    out = markdown_to_telegram_html(NOTICE_KO)
    # Plain header + action survive as-is; exactly one fold pair, no escaped
    # tag leak.
    assert out.startswith("도가니 업데이트 있어요")
    assert out.count("<blockquote expandable>") == 1
    assert out.count("</blockquote>") == 1
    assert "&lt;blockquote" not in out


def test_adversarial_notes_still_escaped_inside_fold():
    # New form: header + action plain, only the notes fold carries the tag.
    hostile = (
        "header\naction\n<blockquote expandable>▸ notes\n"
        "note with <div>bad</div> & <script>x</script> and **bold**</blockquote>"
    )
    out = markdown_to_telegram_html(hostile)
    # Plain lines pass through; whitelist pair survives; non-whitelisted tags
    # are escaped literals.
    assert out.startswith("header\naction\n<blockquote expandable>")
    assert out.endswith("</blockquote>")
    assert "&lt;div&gt;" in out and "&lt;script&gt;" in out
    assert "<div>" not in out and "<script>" not in out
    # Markdown inside the fold still converts.
    assert "<b>bold</b>" in out
