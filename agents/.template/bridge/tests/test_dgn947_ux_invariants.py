"""DGN-947 v1.39.1: UX invariant regression guard (P2 inline seal + P1 bold).

The v1.39.0 interaction-layer refactor regressed three surfaces (buttons /
raw-tag leak / notice) with zero test resistance because the release gate had
no UX-invariant coverage. This suite pins the invariants the refactor broke,
so a future topology flip cannot pass silently again.

P2 (inline glue teardown, StreamingMessageHandler.seal_segment):
  (a) seal edits every interim draft to its permanent form and clears the
      draft list, so the terminal answer opens a FRESH draft (drafts == final
      only) -- the invariant _reply_smart's delete/skip branches assume.
  (b) seal does NOT latch the handler (_finalized stays False): the terminal
      answer's own finalize_all still runs.
  (c) seal is idempotent on an empty draft list and never opens an empty
      bubble.
  (d) sealed interim bubbles are NOT deleted (edited in place = they stand).

P1 (streamed finalize bold/HTML edit, _edit_streamed_prose_html):
  (e) a streamed final carrying <b> / <blockquote> whitelisted tags is NOT
      skipped as a no-op -- it gets the parse_mode=HTML edit so the tags
      render (raw-leak-zero). Covered via formatting.contains_telegram_html,
      the skip-gate predicate.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_agent_sdk import TextBlock

from bridge import sdk_bridge
from bridge.sdk_bridge import SdkBridge, _UserStreamState
from bridge.streaming import DraftState, StreamingMessageHandler
from bridge.formatting import contains_telegram_html
from bridge.tests.test_dgn699_growing_fold import (
    _make_assistant_msg,
    _make_pending_req,
    _make_result_msg,
)


def run(coro):
    return asyncio.run(coro)


def _mock_bot(message_id: int = 555) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=message_id)
    )
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _handler(bot=None):
    return StreamingMessageHandler(bot or _mock_bot(), chat_id=1, user_id=1)


def _draft(mid, text):
    return DraftState(message_id=mid, text=text, last_update_time=0.0)


# ---------------------------------------------------------------------------
# P2 (a): seal edits interim drafts to permanent form + clears the list
# ---------------------------------------------------------------------------

def test_seal_finalizes_and_clears_drafts():
    bot = _mock_bot()
    h = _handler(bot)
    h.drafts = [_draft(10, "step 1 running"), _draft(11, "step 2 running")]
    h.accumulated_text = "step 2 running"

    run(h.seal_segment())

    # each interim draft edited in place to its permanent form
    edited_ids = {c.kwargs["message_id"] for c in bot.edit_message_text.await_args_list}
    assert edited_ids == {10, 11}
    # nothing deleted -- the narration stands
    assert bot.delete_message.await_count == 0
    # draft list cleared -> terminal answer opens a FRESH draft
    assert h.drafts == []
    assert h.accumulated_text == ""
    assert h._need_new_draft is False


# ---------------------------------------------------------------------------
# P2 (b): seal does NOT latch -- terminal finalize_all still runs afterwards
# ---------------------------------------------------------------------------

def test_seal_does_not_latch_handler():
    h = _handler()
    h.drafts = [_draft(10, "interim")]

    run(h.seal_segment())
    assert h._finalized is False, "seal must NOT latch -- terminal turn still finalizes"

    # a fresh terminal draft can be created and finalized normally
    h.drafts = [_draft(20, "final answer")]
    h.accumulated_text = "final answer"
    ok = run(h.finalize_all())
    assert ok is True
    assert h._finalized is True


# ---------------------------------------------------------------------------
# P2 (c): idempotent on empty draft list, no empty bubble
# ---------------------------------------------------------------------------

def test_seal_empty_drafts_is_noop():
    bot = _mock_bot()
    h = _handler(bot)
    # no drafts accumulated (terminal-first turn, no interim)
    run(h.seal_segment())
    assert bot.edit_message_text.await_count == 0
    assert bot.send_message.await_count == 0
    assert h.drafts == []


def test_seal_after_finalize_is_noop():
    bot = _mock_bot()
    h = _handler(bot)
    h._finalized = True
    h.drafts = [_draft(10, "x")]
    run(h.seal_segment())
    # latched handler -> seal is inert, no edit
    assert bot.edit_message_text.await_count == 0


# ---------------------------------------------------------------------------
# P2 (d): the terminal answer streaming after a seal lands in its own bubble
# ---------------------------------------------------------------------------

def test_terminal_stream_after_seal_opens_new_bubble():
    bot = _mock_bot(message_id=99)
    h = _handler(bot)
    h.drafts = [_draft(10, "interim narration")]
    h.accumulated_text = "interim narration"

    run(h.seal_segment())
    assert h.drafts == []

    # terminal answer streams in -> a brand new draft bubble is created,
    # NOT glued onto message 10
    run(h.update_if_needed("the final answer body"))
    assert len(h.drafts) == 1
    assert h.drafts[0].message_id == 99
    assert "interim narration" not in h.drafts[0].text


# ---------------------------------------------------------------------------
# P1 (e): whitelisted-tag streamed final must not hit the no-op skip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "<b>bold answer</b>",
    "plain line\n<blockquote expandable>folded\nnote</blockquote>",
    "<i>x</i>",
])
def test_tagged_final_forces_html_edit_not_skipped(text):
    # contains_telegram_html is the exact predicate the finalize skip-gate
    # (bot.py _edit_streamed_prose_html) checks: True -> the no-op skip is
    # suppressed and the parse_mode=HTML edit fires (tags render, no raw leak).
    assert contains_telegram_html(text) is True


@pytest.mark.parametrize("text", [
    "just plain prose, no tags",
    "a < b and c > d arithmetic",
    "",
])
def test_untagged_final_allows_skip(text):
    # No whitelisted tag -> the skip-gate may still fire (plain draft already
    # renders this exactly) -- byte-identical to pre-DGN-846 behavior.
    assert contains_telegram_html(text) is False


# ---------------------------------------------------------------------------
# P2 integration: the reader loop actually invokes seal at the inline terminal
# boundary (wiring, not just the seal_segment unit)
# ---------------------------------------------------------------------------

def _run_reader_inline(messages_seq, handler):
    async def _inner():
        bridge_obj = SdkBridge()
        req = _make_pending_req(streaming_handler=handler)
        state = _UserStreamState(client=MagicMock(), model=None)
        state.pending.append(req)
        req.sent = True

        async def fake_receive():
            for m in messages_seq:
                yield m

        state.client.receive_messages = fake_receive
        bridge_obj._streams[1] = state
        with patch("bridge.sdk_bridge.INTERIM_MODE", "inline"):
            await bridge_obj._reader_loop(1, state)
        return await asyncio.wait_for(req.future, timeout=1.0)

    return asyncio.run(_inner())


def test_reader_loop_seals_at_inline_terminal_boundary():
    # A non-terminal interim block followed by the terminal answer, in inline
    # mode with an accumulated draft: the reader loop must call seal_segment
    # exactly once so the terminal answer opens a fresh draft.
    handler = MagicMock()
    handler.drafts = [_draft(10, "interim narration")]
    handler.update_if_needed = AsyncMock(return_value=True)
    handler.finalize_all = AsyncMock(return_value=True)
    handler.seal_segment = AsyncMock(return_value=None)

    msgs = [
        _make_assistant_msg("tool_use", [TextBlock(text="working...")]),
        _make_assistant_msg("end_turn", [TextBlock(text="final answer")]),
        _make_result_msg(result="final answer"),
    ]
    _run_reader_inline(msgs, handler)
    assert handler.seal_segment.await_count == 1


def test_reader_loop_no_seal_without_drafts():
    # inline terminal but no accumulated interim drafts -> seal is not called
    # (nothing to seal; guard is `handler.drafts` truthy).
    handler = MagicMock()
    handler.drafts = []
    handler.update_if_needed = AsyncMock(return_value=True)
    handler.finalize_all = AsyncMock(return_value=True)
    handler.seal_segment = AsyncMock(return_value=None)

    msgs = [
        _make_assistant_msg("end_turn", [TextBlock(text="direct answer")]),
        _make_result_msg(result="direct answer"),
    ]
    _run_reader_inline(msgs, handler)
    assert handler.seal_segment.await_count == 0
