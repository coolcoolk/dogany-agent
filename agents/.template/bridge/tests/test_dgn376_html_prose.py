"""DGN-376 v1: prose parse_mode Markdown -> HTML + link-preview suppression.

Covers:
  1. markdown_to_telegram_html conversion rules (bold / italic / strike /
     inline code / links), escaping of stray & < >, no-mangle of stray
     _ * [ ] and code-ish identifiers, and the intentional-HTML passthrough
     whitelist.
  2. link_preview:: marker stripping helpers.
  3. Send-path wiring: prose goes out with parse_mode="HTML" and link
     previews disabled by default; the link_preview:: marker opts a reply
     back in; streamed drafts also suppress previews.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge.formatting import (
    markdown_to_telegram_html as md2html,
    split_into_segments,
    strip_display_markers,
    strip_link_preview_marker,
)


# ---------------------------------------------------------------------------
# 1. Converter: markdown -> Telegram HTML
# ---------------------------------------------------------------------------

def test_bold_star():
    assert md2html("a **bold** b") == "a <b>bold</b> b"


def test_bold_underscore():
    assert md2html("a __bold__ b") == "a <b>bold</b> b"


def test_italic_star():
    assert md2html("a *ital* b") == "a <i>ital</i> b"


def test_italic_underscore():
    assert md2html("a _ital_ b") == "a <i>ital</i> b"


def test_strike():
    assert md2html("a ~~gone~~ b") == "a <s>gone</s> b"


def test_inline_code_escaped_not_converted():
    # Content of a code span is escaped and never md-converted.
    assert md2html("run `x<y&z`") == "run <code>x&lt;y&amp;z</code>"
    assert md2html("see `**not bold**`") == "see <code>**not bold**</code>"


def test_link_http():
    url = "http://100.93.89.51:8484/decision/dec-094"
    assert md2html(f"[dec-094]({url})") == f'<a href="{url}">dec-094</a>'


def test_link_tg_scheme():
    assert md2html("[open](tg://resolve?domain=x)") == (
        '<a href="tg://resolve?domain=x">open</a>'
    )


def test_link_non_http_stays_literal():
    # Only http(s)/tg targets become anchors; anything else is plain text.
    assert md2html("[foo](bar)") == "[foo](bar)"


def test_link_url_with_underscores_not_italicized():
    out = md2html("[t](https://x.io/_a_/b_c)")
    assert out == '<a href="https://x.io/_a_/b_c">t</a>'
    assert "<i>" not in out


def test_escapes_stray_angle_and_amp():
    assert md2html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_no_mangle_stray_chars():
    # Stray _ * [ ] pass through unchanged (DGN-372 guarantee, now via HTML).
    text = "[LIVE] status * ok _ pending [1]"
    assert md2html(text) == text


def test_no_mangle_korean_bracket_tag():
    assert md2html("[라이브] ok") == "[라이브] ok"


def test_no_mangle_snake_case():
    text = "call my_var_name and other_func_here now"
    assert md2html(text) == text


def test_no_mangle_power_expr():
    text = "x**2 + y**2 = r**2"
    assert md2html(text) == text


def test_no_mangle_multiline_emphasis():
    # Emphasis must open and close on the same line.
    text = "a *b\nc* d"
    assert md2html(text) == text


def test_passthrough_whitelisted_tags():
    for text in (
        "<b>x</b>",
        "<i>x</i>",
        "<u>x</u>",
        "<s>x</s>",
        "<tg-spoiler>secret</tg-spoiler>",
        "<blockquote expandable>long</blockquote>",
        "<blockquote>q</blockquote>",
        '<span class="tg-spoiler">s</span>',
        '<a href="https://example.com">t</a>',
        "<pre>p</pre>",
        "<code>c</code>",
        '<code class="language-python">c</code>',
    ):
        assert md2html(text) == text


def test_non_whitelisted_tags_escaped():
    assert md2html("<div>x</div>") == "&lt;div&gt;x&lt;/div&gt;"
    assert md2html("<script>x</script>") == "&lt;script&gt;x&lt;/script&gt;"
    # Extra attributes fall off the exact-form whitelist -> escaped.
    assert md2html('<b class="x">y</b>') == '&lt;b class="x"&gt;y</b>'


def test_nested_bold_italic():
    assert md2html("**a *b* c**") == "<b>a <i>b</i> c</b>"


def test_code_span_inside_link_text():
    out = md2html("[`cmd`](https://x.io/doc)")
    assert out == '<a href="https://x.io/doc"><code>cmd</code></a>'


def test_mixed_markdown_and_escape():
    out = md2html("**bold** & `a<b` end")
    assert out == "<b>bold</b> &amp; <code>a&lt;b</code> end"


def test_empty_and_plain():
    assert md2html("") == ""
    assert md2html("plain text") == "plain text"


# ---------------------------------------------------------------------------
# 1b. DGN-619 -- lists / blockquotes / italic word-only
# ---------------------------------------------------------------------------

def test_bullet_top_level():
    # -, *, + all render the top-level bullet glyph.
    assert md2html("- item one") == "• item one"
    assert md2html("* item two") == "• item two"
    assert md2html("+ item three") == "• item three"


def test_bullet_nested_depths():
    # 2 spaces of source indent per depth -> deeper glyph + 2-space visual
    # indent per depth in the output.
    out = md2html("- top\n  - child\n    - grand")
    assert out == "• top\n  ◦ child\n    ‣ grand"


def test_bullet_depth_beyond_two_stays_triangle():
    # depth >= 2 all use the triangle glyph; indent keeps growing.
    out = md2html("- a\n  - b\n    - c\n      - d")
    assert out == "• a\n  ◦ b\n    ‣ c\n      ‣ d"


def test_ordered_list_keeps_numbers():
    assert md2html("1. first\n2. second") == "1. first\n2. second"


def test_ordered_list_indent_preserved():
    assert md2html("  3. deep") == "  3. deep"


def test_bullet_inner_markdown_converts():
    # The item text still flows through inline emphasis / links.
    assert md2html("- see [x](https://y.io)") == (
        '• see <a href="https://y.io">x</a>'
    )
    assert md2html("- a *word* here") == "• a <i>word</i> here"


def test_bullet_glyph_distinct_from_card_tree():
    # Card-tree glyphs (U+2514 / middot) are table/subline-only and must not be
    # emitted by prose bullets.
    out = md2html("- item\n  - child")
    assert "└" not in out and "·" not in out


def test_blockquote_single_line():
    assert md2html("> hello") == "<blockquote>hello</blockquote>"


def test_blockquote_contiguous_lines_join():
    assert md2html("> a\n> b\n> c") == "<blockquote>a\nb\nc</blockquote>"


def test_blockquote_expandable_marker():
    # `>!` on the first line of the run opts into the expandable variant.
    assert md2html(">! secret\n> more") == (
        "<blockquote expandable>secret\nmore</blockquote>"
    )


def test_blockquote_no_linecount_autofold():
    # Owner rule: fold is marker-driven ONLY. A long (7-line) plain quote must
    # stay a plain blockquote -- line count NEVER triggers expandable.
    out = md2html("> l1\n> l2\n> l3\n> l4\n> l5\n> l6\n> l7")
    assert out == "<blockquote>l1\nl2\nl3\nl4\nl5\nl6\nl7</blockquote>"
    assert "expandable" not in out


def test_blockquote_marker_only_on_first_line():
    # A `>!` on a NON-first line does not retroactively fold the run.
    out = md2html("> a\n>! b")
    assert out == "<blockquote>a\nb</blockquote>"
    assert "expandable" not in out


def test_blockquote_inner_markdown_and_escape():
    assert md2html("> a & b *word*") == (
        "<blockquote>a &amp; b <i>word</i></blockquote>"
    )


def test_gt_without_space_is_not_blockquote():
    # A ">" not followed by a space (or line end) is stray text, escaped.
    assert md2html(">notquote") == "&gt;notquote"
    assert md2html("a > b") == "a &gt; b"


def test_italic_single_word_converts():
    assert md2html("a *one* b") == "a <i>one</i> b"
    assert md2html("a _one_ b") == "a <i>one</i> b"


def test_italic_multi_word_stays_literal():
    # A multi-word span is NOT italic -- it stays literal text (word-only rule).
    assert md2html("a *two words* b") == "a *two words* b"
    assert md2html("a _two words_ b") == "a _two words_ b"


def test_italic_multi_word_literal_is_escaped():
    # Left literal, its inner HTML specials are still escaped.
    assert md2html("*a & b*") == "*a &amp; b*"


def test_single_word_italic_never_emits_bold():
    # Word-only italic must not accidentally emit <b>.
    assert "<b>" not in md2html("a *one* b")


def test_converter_only_sees_prose_segments():
    # Fenced code is routed to code_segment_html by the bridge; the converter
    # is applied to prose segments only. Verify the segment contract holds.
    text = "prose [foo]\n```\ncode [bar] **raw**\n```\nmore [baz]"
    segments = split_into_segments(text)
    code_segs = [s for s, is_code, _ in segments if is_code]
    prose_segs = [s for s, is_code, _ in segments if not is_code]
    assert any("[bar]" in s for s in code_segs)
    converted = " ".join(md2html(s) for s in prose_segs)
    assert "[foo]" in converted and "[baz]" in converted


# ---------------------------------------------------------------------------
# 2. link_preview:: marker helpers
# ---------------------------------------------------------------------------

def test_strip_link_preview_marker_absent():
    text = "hello world"
    out, present = strip_link_preview_marker(text)
    assert out == text and present is False


def test_strip_link_preview_marker_present():
    out, present = strip_link_preview_marker("body line\nlink_preview::\n")
    assert present is True
    assert "link_preview::" not in out
    assert "body line" in out


def test_strip_display_markers_drops_link_preview_line():
    out = strip_display_markers("body\nlink_preview::")
    assert "link_preview::" not in out
    assert "body" in out


# ---------------------------------------------------------------------------
# 3. Send-path wiring (mirrors test_autosplit_codeblock_options mocking)
# ---------------------------------------------------------------------------

def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


def _make_message(sent: list, chat_id: int = 42):
    msg = MagicMock()
    msg.chat.id = chat_id

    async def _reply_text(text, parse_mode=None, reply_markup=None, link_preview_options=None):
        sent.append({
            "text": text,
            "parse_mode": parse_mode,
            "markup": reply_markup,
            "link_preview_options": link_preview_options,
        })

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    msg.get_bot.return_value = MagicMock()
    msg.get_bot.return_value.delete_message = AsyncMock()
    return msg


def _make_bot(bot_mod):
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    bot.application.bot = MagicMock()
    bot.application.bot.delete_message = AsyncMock()
    return bot


def _run(coro):
    return asyncio.run(coro)


def _is_disabled(lp):
    return lp is not None and getattr(lp, "is_disabled", None) is True


def test_reply_smart_prose_html_and_preview_off():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []
    msg = _make_message(sent)

    _run(bot._reply_smart(msg, "check **this** link https://example.com"))

    assert len(sent) == 1
    s = sent[0]
    assert s["parse_mode"] == "HTML"
    assert "<b>this</b>" in s["text"]
    assert _is_disabled(s["link_preview_options"]), "preview must default OFF"


def test_reply_smart_link_preview_opt_in():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []
    msg = _make_message(sent)

    _run(bot._reply_smart(msg, "deep link: https://example.com\nlink_preview::"))

    assert len(sent) == 1
    s = sent[0]
    assert "link_preview::" not in s["text"], "marker line must be stripped"
    assert s["link_preview_options"] is None, "opt-in restores Telegram default"


def test_reply_smart_code_segment_preview_off():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []
    msg = _make_message(sent)

    _run(bot._reply_smart(msg, "before\n```python\nprint(1)\n```\nafter"))

    assert len(sent) == 3
    for s in sent:
        assert s["parse_mode"] == "HTML"
        assert _is_disabled(s["link_preview_options"])
    assert any("<pre>" in s["text"] for s in sent)


def test_send_smart_chat_prose_html_and_preview_off():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []

    async def _send_message(chat_id, text, parse_mode=None, reply_markup=None,
                            link_preview_options=None):
        sent.append({
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": link_preview_options,
        })

    bot.application.bot.send_message = AsyncMock(side_effect=_send_message)

    _run(bot._send_smart(7, "push with _emphasis_ done"))

    assert len(sent) == 1
    s = sent[0]
    assert s["parse_mode"] == "HTML"
    assert "<i>emphasis</i>" in s["text"]
    assert _is_disabled(s["link_preview_options"])


def test_streaming_draft_preview_off():
    from bridge.streaming import StreamingMessageHandler

    calls = []

    class _FakeBot:
        async def send_message(self, chat_id, text, link_preview_options=None, **kwargs):
            calls.append(("send", link_preview_options))
            m = MagicMock()
            m.message_id = len(calls)
            return m

        async def edit_message_text(self, chat_id, message_id, text,
                                    link_preview_options=None, **kwargs):
            calls.append(("edit", link_preview_options))
            return True

    handler = StreamingMessageHandler(_FakeBot(), chat_id=1, user_id=1)
    _run(handler.create_draft("draft text"))
    _run(handler.finalize_all())

    assert calls, "expected draft send/edit calls"
    for kind, lp in calls:
        assert _is_disabled(lp), f"{kind} must suppress link preview"


# ---------------------------------------------------------------------------
# 4. DGN-376 v1.1 -- streamed prose finalize as HTML (M1) + fallback preview
#    suppression on plain-text fallback sends (m1)
# ---------------------------------------------------------------------------

def _make_stream_message(sent: list, edits: list, deleted: list,
                         chat_id: int = 42, edit_error=None):
    """Message mock whose get_bot() supports edit/delete, for streamed paths."""
    msg = MagicMock()
    msg.chat.id = chat_id

    async def _reply_text(text, parse_mode=None, reply_markup=None,
                          link_preview_options=None):
        sent.append({
            "text": text,
            "parse_mode": parse_mode,
            "markup": reply_markup,
            "link_preview_options": link_preview_options,
        })

    msg.reply_text = AsyncMock(side_effect=_reply_text)

    bot = MagicMock()

    async def _edit(chat_id=None, message_id=None, text=None, parse_mode=None,
                    link_preview_options=None):
        if edit_error is not None:
            raise edit_error
        edits.append({
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": link_preview_options,
        })

    async def _delete(chat_id, mid):
        deleted.append(mid)

    bot.edit_message_text = AsyncMock(side_effect=_edit)
    bot.delete_message = AsyncMock(side_effect=_delete)
    msg.get_bot.return_value = bot
    return msg


def test_streamed_prose_finalize_edits_draft_to_html():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent, edits, deleted = [], [], []
    msg = _make_stream_message(sent, edits, deleted)

    _run(bot._reply_smart(msg, "check **this** now",
                          streamed=True, draft_message_ids=[7]))

    assert len(edits) == 1, "final draft must be edited in place"
    e = edits[0]
    assert e["message_id"] == 7
    assert e["parse_mode"] == "HTML"
    assert "<b>this</b>" in e["text"]
    assert _is_disabled(e["link_preview_options"]), "preview must default OFF"
    assert deleted == [] and sent == [], "no draft churn on successful edit"


def test_streamed_plain_prose_skips_noop_edit():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent, edits, deleted = [], [], []
    msg = _make_stream_message(sent, edits, deleted)

    _run(bot._reply_smart(msg, "plain text only",
                          streamed=True, draft_message_ids=[7]))

    assert edits == [] and deleted == [] and sent == [], (
        "identical plain draft must be left untouched (no no-op edit)"
    )


def test_streamed_prose_edit_failure_falls_back_to_resend():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent, edits, deleted = [], [], []
    msg = _make_stream_message(sent, edits, deleted,
                               edit_error=RuntimeError("boom"))

    _run(bot._reply_smart(msg, "check **this** now",
                          streamed=True, draft_message_ids=[7]))

    assert deleted == [7], "failed edit must clean up the draft"
    assert len(sent) == 1, "message must be re-sent, never dropped"
    s = sent[0]
    assert s["parse_mode"] == "HTML"
    assert "<b>this</b>" in s["text"]
    assert _is_disabled(s["link_preview_options"])


def test_streamed_prose_not_modified_treated_as_success():
    from telegram.error import BadRequest

    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent, edits, deleted = [], [], []
    # "a & b" converts to "a &amp; b" (differs), but parses back to the same
    # visible text -> Telegram raises "message is not modified".
    msg = _make_stream_message(
        sent, edits, deleted,
        edit_error=BadRequest("Message is not modified: nothing changed"),
    )

    _run(bot._reply_smart(msg, "a & b",
                          streamed=True, draft_message_ids=[7]))

    assert deleted == [] and sent == [], (
        "'not modified' means the draft is already correct -- no fallback"
    )


def test_streamed_prose_multi_draft_falls_back():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent, edits, deleted = [], [], []
    msg = _make_stream_message(sent, edits, deleted)

    _run(bot._reply_smart(msg, "short **x**",
                          streamed=True, draft_message_ids=[7, 8]))

    assert edits == [], "multi-bubble replies never single-edit"
    assert deleted == [7, 8]
    assert len(sent) == 1 and "<b>x</b>" in sent[0]["text"]


def test_streamed_prose_multipart_display_falls_back():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent, edits, deleted = [], [], []
    msg = _make_stream_message(sent, edits, deleted)
    long_text = "line **b**\n" * 500  # > 4000 chars -> 2 parts

    _run(bot._reply_smart(msg, long_text,
                          streamed=True, draft_message_ids=[7]))

    assert edits == [], "multi-part content never single-edit"
    assert deleted == [7]
    assert len(sent) >= 2
    for s in sent:
        assert s["parse_mode"] == "HTML"
        assert "<b>b</b>" in s["text"]


def test_streamed_preview_opt_in_edit_restores_default():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent, edits, deleted = [], [], []
    msg = _make_stream_message(sent, edits, deleted)

    _run(bot._reply_smart(msg, "deep **link** https://example.com\nlink_preview::",
                          streamed=True, draft_message_ids=[7]))

    assert len(edits) == 1
    assert "link_preview::" not in edits[0]["text"]
    assert edits[0]["link_preview_options"] is None, (
        "opt-in must restore Telegram default preview on the edited draft"
    )


def test_send_smart_streamed_prose_edits_draft_to_html():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    edits, deleted = [], []

    async def _edit(chat_id=None, message_id=None, text=None, parse_mode=None,
                    link_preview_options=None):
        edits.append({
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": link_preview_options,
        })

    async def _delete(chat_id, mid):
        deleted.append(mid)

    bot.application.bot.edit_message_text = AsyncMock(side_effect=_edit)
    bot.application.bot.delete_message = AsyncMock(side_effect=_delete)
    bot.application.bot.send_message = AsyncMock()

    _run(bot._send_smart(9, "push **done**",
                         streamed=True, draft_message_ids=[3]))

    assert len(edits) == 1
    assert edits[0]["message_id"] == 3
    assert edits[0]["parse_mode"] == "HTML"
    assert "<b>done</b>" in edits[0]["text"]
    assert _is_disabled(edits[0]["link_preview_options"])
    assert deleted == []
    bot.application.bot.send_message.assert_not_called()


def _make_html_rejecting_message(sent: list):
    """reply_text mock that rejects HTML sends, recording fallback sends."""
    msg = MagicMock()
    msg.chat.id = 42

    async def _reply_text(text, parse_mode=None, reply_markup=None,
                          link_preview_options=None):
        if parse_mode == "HTML":
            raise RuntimeError("telegram rejected HTML")
        sent.append({
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": link_preview_options,
        })

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    msg.get_bot.return_value = MagicMock()
    msg.get_bot.return_value.delete_message = AsyncMock()
    return msg


def test_reply_prose_fallback_carries_preview_suppression():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []
    msg = _make_html_rejecting_message(sent)

    _run(bot._reply_smart(msg, "hello **world**"))

    assert len(sent) == 1, "fallback must deliver the message"
    s = sent[0]
    assert s["parse_mode"] is None
    assert s["text"] == "hello **world**"
    assert _is_disabled(s["link_preview_options"]), (
        "m1: fallback send must keep preview suppression"
    )


def test_reply_code_fallback_carries_preview_suppression():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []
    msg = _make_html_rejecting_message(sent)

    _run(bot._reply_smart(msg, "```python\nprint(1)\n```"))

    assert len(sent) == 1
    s = sent[0]
    assert s["parse_mode"] is None
    assert "print(1)" in s["text"]
    assert _is_disabled(s["link_preview_options"])


def test_chat_fallbacks_carry_preview_suppression():
    bot_mod = _load_bot()
    bot = _make_bot(bot_mod)
    sent = []

    async def _send_message(chat_id, text=None, parse_mode=None,
                            reply_markup=None, link_preview_options=None):
        if parse_mode == "HTML":
            raise RuntimeError("telegram rejected HTML")
        sent.append({
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": link_preview_options,
        })

    bot.application.bot.send_message = AsyncMock(side_effect=_send_message)

    _run(bot._send_smart(9, "prose **b**\n```py\nx = 1\n```"))

    assert len(sent) == 2, "both prose and code fallbacks must deliver"
    for s in sent:
        assert s["parse_mode"] is None
        assert _is_disabled(s["link_preview_options"]), (
            "m1: fallback send must keep preview suppression"
        )
