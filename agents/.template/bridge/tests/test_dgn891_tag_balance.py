"""DGN-891: tag-safe HTML split/balance + tag-stripped plain fallbacks.

Failure class reproduced here: rendered Telegram HTML cut at a length
boundary (split_text chunking, fold rolling-window) with a <b>/<blockquote>/
<code>/<a> span straddling the cut -> unbalanced chunk -> Telegram 400
("can't find end tag corresponding to start tag ...") -> plain fallback that
historically showed RAW markdown (literal **) or leaked tags.

Contract under test:
1. balance_telegram_html closes still-open tags (LIFO), drops stray closers,
   and is a byte-identical no-op on balanced input (safe universal guard).
2. rebalance_html_chunks makes every chunk independently valid: open tags are
   closed at the chunk end and re-opened (attributes preserved) at the next
   chunk start.
3. html_to_plain_text strips tags + unescapes entities so plain fallbacks are
   readable -- never literal ** and never leaked tags.
4. Fold renders (render_fold_live / render_fold_final) emit balanced HTML
   even when the narration carries unbalanced verbatim tags or the rolling
   window cuts a tag pair apart.
5. The streaming fold send helpers apply the balance guard before the HTML
   send (no plain degrade for a merely-unbalanced input).
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import os
os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge.formatting import (  # noqa: E402
    _TG_BALANCE_TAG_RE,
    balance_telegram_html,
    html_to_plain_text,
    markdown_to_telegram_html,
    rebalance_html_chunks,
    render_fold_final,
    render_fold_live,
    split_text,
)


def _assert_balanced(html_text: str) -> None:
    """Mini Telegram-HTML validator: every opener has a matching closer."""
    stack = []
    for m in _TG_BALANCE_TAG_RE.finditer(html_text):
        if m.group(1) == "/":
            assert stack, f"stray closer </{m.group(2)}> in: {html_text[:200]!r}"
            assert stack[-1] == m.group(2), (
                f"misnested </{m.group(2)}> (open: {stack}) in: {html_text[:200]!r}"
            )
            stack.pop()
        else:
            stack.append(m.group(2))
    assert not stack, f"unclosed tags {stack} in: {html_text[:200]!r}"


# ---------------------------------------------------------------------------
# 1. balance_telegram_html core contract
# ---------------------------------------------------------------------------


class TestBalanceTelegramHtml:
    def test_balanced_input_is_byte_identical_noop(self):
        samples = [
            "plain text, no tags at all",
            "<b>bold</b> and <i>it</i>",
            '<a href="https://example.com/a_b">link</a>',
            "<blockquote expandable>fold body\nline two</blockquote>",
            '<pre><code class="language-python">x = 1</code></pre>',
            '<span class="tg-spoiler">spoiler</span>',
            "escaped &lt;b&gt; stays text",
        ]
        for s in samples:
            assert balance_telegram_html(s) == s

    def test_idempotent(self):
        broken = "<b>unclosed bold and <code>unclosed code"
        once = balance_telegram_html(broken)
        assert balance_telegram_html(once) == once
        _assert_balanced(once)

    def test_unclosed_tags_closed_in_lifo_order(self):
        out = balance_telegram_html("<blockquote>quote <b>bold")
        assert out == "<blockquote>quote <b>bold</b></blockquote>"
        _assert_balanced(out)

    def test_stray_closer_escaped_not_dropped(self):
        # DGN-891 C1: a stray closer with no opener is escaped (content
        # preserved), never silently deleted.
        out = balance_telegram_html("tail of a span</b> more text")
        assert out == "tail of a span&lt;/b&gt; more text"
        assert "</b>" not in out  # not a real tag anymore
        _assert_balanced(out)

    def test_open_tag_attributes_preserved(self):
        out = balance_telegram_html('<a href="https://x.test/p">cut link')
        assert out == '<a href="https://x.test/p">cut link</a>'
        _assert_balanced(out)

    def test_blockquote_expandable_closed(self):
        out = balance_telegram_html("<blockquote expandable>fold start")
        assert out == "<blockquote expandable>fold start</blockquote>"
        _assert_balanced(out)

    def test_misnested_closer_repaired(self):
        out = balance_telegram_html("<blockquote><b>bold</blockquote>")
        _assert_balanced(out)

    def test_empty_and_tagfree_passthrough(self):
        assert balance_telegram_html("") == ""
        assert balance_telegram_html("no angle brackets") == "no angle brackets"


# ---------------------------------------------------------------------------
# 2. rebalance_html_chunks: balance-and-reopen across the split boundary
# ---------------------------------------------------------------------------


class TestRebalanceHtmlChunks:
    def test_bold_straddles_boundary(self):
        chunks = rebalance_html_chunks(["<b>start of bold", "end of bold</b> tail"])
        assert chunks[0] == "<b>start of bold</b>"
        assert chunks[1] == "<b>end of bold</b> tail"
        for c in chunks:
            _assert_balanced(c)

    def test_blockquote_straddles_boundary(self):
        chunks = rebalance_html_chunks(
            ["<blockquote>first lines", "last lines</blockquote> after"]
        )
        assert chunks[0] == "<blockquote>first lines</blockquote>"
        assert chunks[1] == "<blockquote>last lines</blockquote> after"
        for c in chunks:
            _assert_balanced(c)

    def test_pre_code_straddles_boundary(self):
        chunks = rebalance_html_chunks(
            ['<pre><code class="language-py">a = 1', "b = 2</code></pre>"]
        )
        assert chunks[0] == '<pre><code class="language-py">a = 1</code></pre>'
        assert chunks[1] == '<pre><code class="language-py">b = 2</code></pre>'
        for c in chunks:
            _assert_balanced(c)

    def test_a_href_straddles_boundary_attributes_reopened(self):
        chunks = rebalance_html_chunks(
            ['<a href="https://x.test/long">click', "here</a> done"]
        )
        assert chunks[0] == '<a href="https://x.test/long">click</a>'
        assert chunks[1] == '<a href="https://x.test/long">here</a> done'
        for c in chunks:
            _assert_balanced(c)

    def test_balanced_chunks_pass_through_byte_identical(self):
        chunks = ["<b>one</b>", "plain", "<i>two</i>"]
        assert rebalance_html_chunks(chunks) == chunks

    def test_span_across_three_chunks(self):
        chunks = rebalance_html_chunks(["<b>a", "b", "c</b>"])
        assert chunks == ["<b>a</b>", "<b>b</b>", "<b>c</b>"]

    def test_rendered_split_pipeline_every_chunk_valid(self):
        """End-to-end shape of the final-answer path: source with a verbatim
        <b> span straddling the split boundary -> render per part -> rebalance
        -> every chunk independently parseable."""
        span_lines = ["inner bold line {} lorem ipsum dolor".format(i) for i in range(200)]
        src = "<b>bold opens here\n" + "\n".join(span_lines) + "\nand closes here</b>\n\ntail"
        parts = [p for p in split_text(src) if p.strip()]
        assert len(parts) > 1, "precondition: source must split"
        rendered = [markdown_to_telegram_html(p) for p in parts]
        # Precondition: without the fix at least one chunk is unbalanced.
        unbalanced = []
        for r in rendered:
            try:
                _assert_balanced(r)
            except AssertionError:
                unbalanced.append(r)
        assert unbalanced, "precondition: split must produce an unbalanced chunk"
        fixed = rebalance_html_chunks(rendered)
        for c in fixed:
            _assert_balanced(c)


# ---------------------------------------------------------------------------
# 3. html_to_plain_text: readable fallback, no ** and no leaked tags
# ---------------------------------------------------------------------------


class TestPlainFallback:
    def test_bold_message_fallback_has_no_literal_stars_or_tags(self):
        rendered = markdown_to_telegram_html("**중요** 결과가 나왔습니다 & 확인 필요")
        assert "<b>" in rendered  # precondition: bold actually rendered
        plain = html_to_plain_text(rendered)
        assert "**" not in plain
        assert "<" not in plain and ">" not in plain
        assert "중요" in plain and "& 확인 필요" in plain  # unescaped, readable

    def test_code_segment_fallback_unescapes(self):
        plain = html_to_plain_text("<pre>if a &lt; b:\n    pass</pre>")
        assert plain == "if a < b:\n    pass"

    def test_rebalanced_chunk_fallback_readable(self):
        chunks = rebalance_html_chunks(["<b>start", "end</b>"])
        for c in chunks:
            plain = html_to_plain_text(c)
            assert "<" not in plain and "**" not in plain


# ---------------------------------------------------------------------------
# 4. Fold renders always emit balanced HTML
# ---------------------------------------------------------------------------


class TestFoldRenderBalanced:
    def test_live_render_with_lone_open_tag_in_narration(self):
        out = render_fold_live(["checking <b>something -- narration leaked a tag"])
        _assert_balanced(out)

    def test_live_render_with_stray_closer_in_narration(self):
        out = render_fold_live(["tail of narration</blockquote> more text"])
        _assert_balanced(out)

    def test_final_render_with_lone_open_tag(self):
        out = render_fold_final(["step one <code>unclosed"], "진행 기록")
        _assert_balanced(out)

    def test_rolling_window_cut_tag_pair_stays_balanced(self):
        # Opener lives on an early line, closer on a late line; the huge body
        # forces the rolling window to drop the opener line -> stray closer.
        lines = ["<b>opening line of a long bold span"]
        lines += ["narration line {} with some padding text".format(i) for i in range(400)]
        lines += ["closing line</b>"]
        out = render_fold_live(["\n".join(lines)])
        _assert_balanced(out)

    def test_balanced_narration_unaffected(self):
        out = render_fold_live(["plain narration line\nsecond line"])
        _assert_balanced(out)
        assert "plain narration line" in out


# ---------------------------------------------------------------------------
# 5. Streaming fold send helpers: guard fires before the HTML send
# ---------------------------------------------------------------------------


class _FakeBot:
    """Records sends/edits; rejects unbalanced HTML like Telegram does."""

    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, **kwargs):
        self._check(kwargs)
        self.sent.append(kwargs)
        msg = MagicMock()
        msg.message_id = 111
        return msg

    async def edit_message_text(self, **kwargs):
        self._check(kwargs)
        self.edited.append(kwargs)
        return MagicMock()

    @staticmethod
    def _check(kwargs):
        if kwargs.get("parse_mode") == "HTML":
            _assert_balanced(kwargs["text"])


class TestFoldSendGuard:
    def test_send_fold_html_balances_before_send(self):
        from bridge.streaming import send_fold_html

        bot = _FakeBot()
        mid = asyncio.run(send_fold_html(bot, 1, "<blockquote>cut fold <b>bold"))
        assert mid == 111
        assert len(bot.sent) == 1
        assert bot.sent[0]["parse_mode"] == "HTML"  # no plain degrade
        _assert_balanced(bot.sent[0]["text"])

    def test_edit_fold_html_balances_before_edit(self):
        from bridge.streaming import edit_fold_html

        bot = _FakeBot()
        ok, retry = asyncio.run(edit_fold_html(bot, 1, 111, "<b>still growing"))
        assert ok is True and retry == 0.0
        assert bot.edited[0]["parse_mode"] == "HTML"
        _assert_balanced(bot.edited[0]["text"])

    def test_finalize_fold_html_balances_before_edit(self):
        from bridge.streaming import finalize_fold_html

        bot = _FakeBot()
        ok = asyncio.run(
            finalize_fold_html(bot, 1, 111, "<blockquote expandable>caption\n<b>body")
        )
        assert ok is True
        assert bot.edited[0]["parse_mode"] == "HTML"
        _assert_balanced(bot.edited[0]["text"])

    def test_fold_plain_fallback_strips_tags(self):
        from bridge.streaming import _fold_html_to_plain

        plain = _fold_html_to_plain("<blockquote><b>진행 기록</b> &amp; 세부</blockquote>")
        assert plain == "진행 기록 & 세부"
