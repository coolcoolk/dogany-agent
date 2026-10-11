"""DGN-1936: a final that reproduces the fold edits it in place (no delete-resend).

A fold-mode turn whose final answer fully reproduces the live fold narration
used to DELETE the fold bubble and then send the final as a brand-new
message: the text the owner was reading vanished and came back below. When
the final has no streamed draft of its own, the fold bubble is now handed to
the delivery seat as the final's draft, so the seat edits it in place. Only
when that edit is impossible does the seat fall back to the DGN-1890
send-then-retire swap (never delete-then-send).

A final that DID stream its own draft keeps the old shape: the redundant fold
bubble is deleted and the final's draft stands (nothing is re-sent).
"""

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import TextBlock

from bridge import sdk_bridge as _locale_bridge
from bridge.sdk_bridge import SdkBridge, _UserStreamState
from bridge.tests.test_dgn699_growing_fold import (
    _edit_texts,
    _make_assistant_msg,
    _make_pending_req,
    _make_result_msg,
    _mock_handler,
)
from bridge.tests.test_dgn1890_swap_send_first import (
    _assert_send_then_delete,
    _chat_rig,
    _load_bot,
    _reply_rig,
    _run,
)


@pytest.fixture(autouse=True)
def _en_instance(monkeypatch):
    monkeypatch.setattr(_locale_bridge.config, "locale", "en")


FOLD_MID = 777


def _reader(messages_seq, own_draft_mid=None):
    async def _inner():
        handler = _mock_handler()
        if own_draft_mid is not None:
            handler.drafts = [MagicMock(message_id=own_draft_mid)]
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
        with patch("bridge.sdk_bridge.INTERIM_MODE", "fold"), patch(
            "bridge.sdk_bridge.FOLD_UPDATE_INTERVAL", 0.0
        ):
            await bridge_obj._reader_loop(1, state)
        return await asyncio.wait_for(req.future, timeout=1.0), handler, req

    return asyncio.run(_inner())


def _echo_turn():
    whole = "I did step A\n\nI did step B"
    return whole, [
        _make_assistant_msg("tool_use", [TextBlock(text="I did step A")]),
        _make_assistant_msg("tool_use", [TextBlock(text="I did step B")]),
        _make_assistant_msg("end_turn", [TextBlock(text=whole)]),
        _make_result_msg(result=""),
    ]


# ---------------------------------------------------------------------------
# sdk_bridge: the fold bubble becomes the final's draft
# ---------------------------------------------------------------------------

def test_draftless_final_reproducing_fold_takes_the_bubble_over(caplog):
    whole, msgs = _echo_turn()
    with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        response, handler, req = _reader(msgs)
    handler.bot.delete_message.assert_not_called()
    # Exactly one Telegram send: the fold bubble itself; the final is not
    # a new message.
    handler.bot.send_message.assert_awaited_once()
    assert response.content == whole
    assert response.streamed is True
    assert response.draft_message_ids == [FOLD_MID]
    # The bubble holds the fold render, not the final body: the seat must
    # make the real edit (the no-op skip would leave the narration on screen).
    assert response.turn_assembled is True
    # The latch keeps the collapse swap off the bubble that is now the answer.
    assert req.fold_finalized
    assert not any(t.startswith("<blockquote expandable>")
                   for t in _edit_texts(handler.bot))
    assert "edited in place into the final" in caplog.text
    assert "fold deleted" not in caplog.text


def test_final_with_its_own_draft_still_deletes_the_redundant_fold():
    whole, msgs = _echo_turn()
    response, handler, req = _reader(msgs, own_draft_mid=900)
    handler.bot.delete_message.assert_awaited_once()
    assert handler.bot.delete_message.call_args.kwargs["message_id"] == FOLD_MID
    assert response.draft_message_ids == [900]
    assert response.content == whole


def test_fold_with_surviving_progress_is_not_taken_over():
    msgs = [
        _make_assistant_msg("tool_use", [TextBlock(text="extra progress")]),
        _make_assistant_msg("tool_use", [TextBlock(text="the answer")]),
        _make_assistant_msg("end_turn", [TextBlock(text="the answer")]),
        _make_result_msg(result=""),
    ]
    response, handler, req = _reader(msgs)
    assert FOLD_MID not in response.draft_message_ids
    assert req.fold_buf == ["extra progress"]
    assert _edit_texts(handler.bot)[-1].startswith("<blockquote expandable>")


# ---------------------------------------------------------------------------
# delivery seat: the taken-over bubble is edited in place; the fallback is the
# DGN-1890 send-then-retire swap
# ---------------------------------------------------------------------------

def test_reply_seat_edits_the_fold_bubble_in_place():
    bot_mod = _load_bot()
    events = []
    bot, msg = _reply_rig(bot_mod, events, age_seconds=1)
    _run(bot._reply_smart(msg, "I did step A\n\nI did step B", streamed=True,
                          draft_message_ids=[FOLD_MID], assembled=True))
    tg = msg.get_bot.return_value
    tg.edit_message_text.assert_awaited_once()
    assert tg.edit_message_text.call_args.kwargs["message_id"] == FOLD_MID
    assert events == []  # no send, no delete


def test_reply_seat_edit_failure_sends_before_retiring_the_bubble():
    bot_mod = _load_bot()
    events = []
    bot, msg = _reply_rig(bot_mod, events, age_seconds=1,
                          edit_error=RuntimeError("Bad Request: boom"))
    _run(bot._reply_smart(msg, "I did step A\n\nI did step B", streamed=True,
                          draft_message_ids=[FOLD_MID], assembled=True))
    _assert_send_then_delete(events, [FOLD_MID])


def test_chat_seat_edits_the_fold_bubble_in_place():
    bot_mod = _load_bot()
    events = []
    bot = _chat_rig(bot_mod, events)
    _run(bot._send_smart(42, "I did step A\n\nI did step B", streamed=True,
                         draft_message_ids=[FOLD_MID], assembled=True))
    tg = bot.application.bot
    tg.edit_message_text.assert_awaited_once()
    assert tg.edit_message_text.call_args.kwargs["message_id"] == FOLD_MID
    assert events == []
