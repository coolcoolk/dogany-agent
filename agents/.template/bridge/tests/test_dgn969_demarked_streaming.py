"""DGN-969: de-marked streaming display -- raw markdown must never be visible
while a draft bubble is streaming (single-bubble AND multi-bubble/overflow
cases), and the transform must be display-only (the accumulated text / draft
text bookkeeping, and therefore the eventual finalized message, stays byte-
identical to before this change).

Covers:
  1. demark_markdown_for_stream unit behavior: complete pairs (bold/under/
     strike/inline-code/italic/link) strip to bare content; dangling opens
     (**, __, ~~, lone backtick) are hidden while pending; a lone stray `*`/
     `_` (glob, math, snake_case) is left untouched; fenced code blocks
     (closed or in-progress/unclosed) are never touched.
  2. StreamingMessageHandler.create_draft / update_draft / finalize_draft:
     the TEXT SENT to Telegram never contains a raw marker, while
     draft.text / handler.accumulated_text stay exactly equal to the raw
     input (the invariant the final HTML render depends on).
  3. A bold span straddling two separate update_if_needed calls: no raw '**'
     visible at any intermediate step, and the final accumulated text is
     byte-identical to the naive concatenation (nothing dropped/mangled).
  4. Multi-bubble overflow: every finalize_draft edit (including bubbles that
     get permanently sealed mid-stream, before the eventual delete+resend
     fallback in bot.py) is also de-marked -- no raw marker visible there.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from bridge.formatting import demark_markdown_for_stream as demark
from bridge.streaming import DraftState, StreamingMessageHandler


def run(coro):
    return asyncio.run(coro)


def _mock_bot(message_id: int = 100):
    bot = MagicMock()
    counter = {"n": message_id}

    async def _send(**kwargs):
        counter["n"] += 1
        return SimpleNamespace(message_id=counter["n"])

    bot.send_message = AsyncMock(side_effect=_send)
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _handler(bot=None):
    return StreamingMessageHandler(bot or _mock_bot(), chat_id=1, user_id=1)


# ---------------------------------------------------------------------------
# 1. demark_markdown_for_stream unit behavior
# ---------------------------------------------------------------------------

def test_complete_bold_star_strips_markers():
    assert demark("This is **bold** text") == "This is bold text"


def test_complete_bold_under_strips_markers():
    assert demark("This is __bold__ text") == "This is bold text"


def test_complete_strike_strips_markers():
    assert demark("This is ~~gone~~ text") == "This is gone text"


def test_complete_inline_code_strips_backticks():
    assert demark("run `pytest -q` now") == "run pytest -q now"


def test_complete_italic_word_only_strips_markers():
    assert demark("This is *great*") == "This is great"
    assert demark("This is _great_") == "This is great"


def test_complete_link_shows_bare_label():
    assert demark("see [docs](https://example.com/x)") == "see docs"


def test_dangling_bold_star_hides_marker_not_content():
    # Chunk cut mid-span: "text **bo" -- no closing pair yet.
    out = demark("This is **bo")
    assert "**" not in out
    assert "bo" in out
    assert out == "This is bo"


def test_dangling_bold_under_hides_marker_not_content():
    out = demark("This is __wo")
    assert "__" not in out
    assert out == "This is wo"


def test_dangling_strike_hides_marker():
    out = demark("This is ~~go")
    assert "~~" not in out
    assert out == "This is go"


def test_dangling_backtick_hides_marker():
    out = demark("run `pytest and keep typing")
    assert "`" not in out
    assert out == "run pytest and keep typing"


def test_orphaned_closing_bold_star_from_split_is_hidden():
    # DGN-969 grill finding: the bridge's own overflow split (character-count
    # cut, formatting-agnostic) or the block-join newline can land a marker's
    # CLOSING half at the start of a bubble with the OPENER sealed into a
    # previous, already-finalized bubble -- this bubble's own text has only
    # an orphaned closer, no opener of its own.
    out = demark("ld** is the rest of the sentence")
    assert "**" not in out
    assert out == "ld is the rest of the sentence"


def test_orphaned_closing_bold_under_from_split_is_hidden():
    out = demark("ld__ is the rest")
    assert "__" not in out
    assert out == "ld is the rest"


def test_orphaned_closing_strike_from_split_is_hidden():
    out = demark("ne~~ is the rest")
    assert "~~" not in out
    assert out == "ne is the rest"


def test_stray_single_asterisk_glob_untouched():
    # DGN-969 grill: a literal '*' (file glob) must NOT be corrupted -- no
    # closing pair exists anywhere, so it is left exactly as literal text,
    # matching the final renderer's own stray-marker passthrough behavior.
    text = "match files by *.md pattern"
    assert demark(text) == text


def test_stray_single_asterisk_math_untouched():
    text = "the result is x*y here"
    assert demark(text) == text


def test_x_star_star_2_math_untouched():
    # x**2 -- the bold-star regex's own alnum-boundary guard already refuses
    # to treat this as a candidate opener (char before '**' is alnum), so it
    # is never touched by either the complete-pair pass or the dangling pass.
    text = "the formula is x**2 plus y**2"
    assert demark(text) == text


def test_snake_case_identifier_untouched():
    text = "call the snake_case_name function"
    assert demark(text) == text


def test_dunder_outside_code_matches_existing_bold_semantics():
    # Pre-existing markdown_to_telegram_html behavior (not introduced by
    # DGN-969): a bare __init__ in PROSE (no backticks) is a complete
    # double-underscore pair and converts identically in both the streaming
    # de-mark and the eventual final HTML render -- consistency is the only
    # DGN-969 requirement, not a new escaping rule.
    from bridge.formatting import markdown_to_telegram_html, html_to_plain_text
    text = "the __init__ method"
    final_plain = html_to_plain_text(markdown_to_telegram_html(text))
    assert demark(text) == final_plain


def test_closed_fenced_code_block_untouched():
    text = "before\n```python\nx = '**not bold**'\n```\nafter **bold**"
    out = demark(text)
    assert "x = '**not bold**'" in out  # code content byte-identical
    assert "after bold" in out  # prose outside the fence still de-marked


def test_unclosed_trailing_fence_left_fully_raw():
    # In-progress code block (fence opened, not yet closed): leave the whole
    # tail untouched, matching the pre-existing plain display of in-flight
    # streamed code (no markdown-look-alike corruption of code being typed).
    text = "here is code:\n```python\ndef f():\n    return '**x**'"
    out = demark(text)
    assert out.endswith("```python\ndef f():\n    return '**x**'")


def test_inline_code_content_never_markdown_converted():
    text = "use `a**b**c` literally"
    out = demark(text)
    assert out == "use a**b**c literally"


def test_empty_and_none_safe():
    assert demark("") == ""


# ---------------------------------------------------------------------------
# 2. StreamingMessageHandler: sent text de-marked, stored text untouched
# ---------------------------------------------------------------------------

def test_create_draft_sends_demarked_text_but_stores_raw():
    bot = _mock_bot()
    handler = _handler(bot)
    raw = "Hello **world**, this is __great__ and `code` too"
    run(handler.create_draft(raw))
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "**" not in sent_text
    assert "__" not in sent_text
    assert "`" not in sent_text
    assert "world" in sent_text and "great" in sent_text and "code" in sent_text
    # Invariant: the tracked draft text is exactly the raw input, untouched.
    assert handler.drafts[-1].text == raw


def test_update_draft_sends_demarked_text_but_stores_raw():
    bot = _mock_bot()
    handler = _handler(bot)
    draft = DraftState(message_id=1, text="Hello", last_update_time=0.0)
    handler.drafts.append(draft)
    raw = "Hello **world** now"
    run(handler.update_draft(draft, raw))
    sent_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "**" not in sent_text
    assert sent_text == "Hello world now"
    assert draft.text == raw


def test_finalize_draft_sends_demarked_text_but_keeps_raw_chunking():
    bot = _mock_bot()
    handler = _handler(bot)
    draft = DraftState(
        message_id=1, text="Hello **world** now", last_update_time=0.0
    )
    handler.drafts.append(draft)
    run(handler.finalize_draft(draft))
    sent_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "**" not in sent_text
    assert sent_text == "Hello world now"
    # draft.text is untouched by finalize_draft (it only edits the display).
    assert draft.text == "Hello **world** now"


# ---------------------------------------------------------------------------
# 3. A bold span straddling two update_if_needed calls
# ---------------------------------------------------------------------------

def test_straddling_bold_span_never_shows_raw_marker_and_final_text_intact():
    bot = _mock_bot()
    handler = _handler(bot)

    run(handler.update_if_needed("This is **bo"))
    first_sent = bot.send_message.await_args.kwargs["text"]
    assert "**" not in first_sent
    assert handler.accumulated_text == "This is **bo"

    run(handler.update_if_needed("ld** now"))
    # Either a further send_message (new bubble) or edit_message_text
    # (updated draft) call carries the latest display text -- whichever
    # fired last must be marker-free.
    if bot.edit_message_text.await_count:
        last_sent = bot.edit_message_text.await_args.kwargs["text"]
    else:
        last_sent = bot.send_message.await_args.kwargs["text"]
    assert "**" not in last_sent
    # The raw accumulated text is exactly the naive join (nothing dropped),
    # modulo the handler's own documented block-join newline rule.
    assert handler.accumulated_text.replace("\n", "") == (
        "This is **bo" + "ld** now"
    )


def test_finalize_all_after_straddling_span_matches_demark_of_full_text():
    bot = _mock_bot()
    handler = _handler(bot)
    run(handler.update_if_needed("This is **bo"))
    run(handler.update_if_needed("ld** now"))
    run(handler.finalize_all())
    last_edit = bot.edit_message_text.await_args.kwargs["text"]
    # StreamingMessageHandler joins separate update_if_needed calls (separate
    # TextBlocks) with a newline (see its own docstring); the bold span here
    # never actually straddles a SINGLE line, so both halves are correctly
    # judged independently -- no raw '**' either way, content fully present.
    assert "**" not in last_edit
    assert "bo" in last_edit and "ld" in last_edit
    # Invariant: the accumulated (raw) text handed to the eventual HTML
    # finalize is untouched -- exactly the naive block-joined text.
    assert handler.accumulated_text == "This is **bo\nld** now"


# ---------------------------------------------------------------------------
# 4. Multi-bubble overflow: every finalized bubble is de-marked
# ---------------------------------------------------------------------------

def test_overflow_sealed_bubble_is_demarked():
    bot = _mock_bot()
    handler = _handler(bot)
    long_bold_prefix = "**" + ("x" * 3990) + "** "
    run(handler.update_if_needed(long_bold_prefix + "tail **bold** text"))
    # At least one edit_message_text call sealed an overflowed bubble; none
    # of the sent/edited texts anywhere in the call history may contain a
    # raw '**' marker.
    all_texts = [c.kwargs.get("text", "") for c in bot.send_message.await_args_list]
    all_texts += [c.kwargs.get("text", "") for c in bot.edit_message_text.await_args_list]
    for t in all_texts:
        assert "**" not in t, f"raw markdown leaked into a streamed bubble: {t!r}"


def test_overflow_split_bisecting_a_bold_span_shows_no_raw_marker_either_side():
    # DGN-969 grill finding: the bridge's own character-count overflow split
    # (formatting-agnostic, unlike the SDK's own TextBlock boundaries) can cut
    # directly through a "**bold**" span -- the opener seals into one bubble,
    # the orphaned closer starts the next. Constructed so _find_split_boundary
    # (no nearby newline -> hard cut at the char limit) lands exactly inside
    # "**bold**": the sealed bubble ends "... **bol" (dangling open) and the
    # new bubble begins "d** tail..." (orphaned close).
    padding = ("word " * 900)[:3995]
    text = padding + "**bold** tail more words here to be safe"
    bot = _mock_bot()
    handler = _handler(bot)
    run(handler.update_if_needed(text))
    all_texts = [c.kwargs.get("text", "") for c in bot.send_message.await_args_list]
    all_texts += [c.kwargs.get("text", "") for c in bot.edit_message_text.await_args_list]
    assert len(all_texts) >= 2, "expected both a sealed bubble and a new draft"
    for t in all_texts:
        assert "**" not in t, f"raw markdown leaked across the overflow split: {t!r}"
    # Invariant: the handler's own bookkeeping (what the eventual HTML
    # finalize reads) is exactly the raw, untouched input.
    assert handler.accumulated_text == text[4000:]


def test_send_extra_chunks_is_demarked():
    bot = _mock_bot()
    handler = _handler(bot)
    chunks = ["first **bold** chunk", "second __chunk__ here"]
    run(handler._send_extra_chunks(chunks))
    all_texts = [c.kwargs.get("text", "") for c in bot.send_message.await_args_list]
    assert len(all_texts) == 2
    for t in all_texts:
        assert "**" not in t and "__" not in t
