"""DGN-351 split-message merge -- the long-paste path, absorbed from the public
bridge line (DGN-1276 D9).

Telegram cuts a message longer than 4096 chars into N back-to-back updates.
Without this path each part becomes its own turn, so a long paste is answered
in halves. The feature buffers a boundary-length part and merges whatever
arrives inside the window into ONE dispatch.

Contract under test:
  (a) a short message with no open window is NOT buffered (zero delay kept)
  (b) a boundary-length part opens a window and is buffered
  (c) a short tail flushes immediately and dispatches the concatenation
  (d) a second boundary-length part EXTENDS the window (3+ part splits merge)
  (e) the window timing out dispatches whatever accumulated
  (f) parts of different chats do not merge into each other
  (g) the merged text is dispatched through _dispatch_text_task, so the
      fork-reply and fast-path checks see the whole message, never a half
"""

import asyncio

import pytest

from bridge import bot as bot_mod


def _bot():
    b = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    b._split_buffers = {}
    b._split_buffer_lock = asyncio.Lock()
    b._dispatched = []

    async def _dispatch(update, user_id, text, **kw):
        b._dispatched.append((update, user_id, text))

    b._dispatch_text_task = _dispatch
    return b


LONG = "x" * bot_mod.SPLIT_MERGE_THRESHOLD
SHORT = "tail"


@pytest.mark.asyncio
async def test_short_message_with_no_window_is_not_buffered():
    b = _bot()
    assert await b._maybe_buffer_split_part(7, "upd", 1, SHORT) is False
    assert b._split_buffers == {}
    assert b._dispatched == []


@pytest.mark.asyncio
async def test_boundary_part_opens_a_window():
    b = _bot()
    try:
        assert await b._maybe_buffer_split_part(7, "upd", 1, LONG) is True
        assert list(b._split_buffers[7]["parts"]) == [LONG]
        assert b._dispatched == []
    finally:
        b._split_buffers[7]["timer"].cancel()


@pytest.mark.asyncio
async def test_short_tail_flushes_the_merge_immediately():
    b = _bot()
    await b._maybe_buffer_split_part(7, "upd", 1, LONG)
    assert await b._maybe_buffer_split_part(7, "upd2", 1, SHORT) is True
    assert b._split_buffers == {}
    assert b._dispatched == [("upd", 1, LONG + SHORT)]


@pytest.mark.asyncio
async def test_second_boundary_part_extends_the_window():
    b = _bot()
    await b._maybe_buffer_split_part(7, "upd", 1, LONG)
    first_timer = b._split_buffers[7]["timer"]
    assert await b._maybe_buffer_split_part(7, "upd2", 1, LONG) is True
    assert b._split_buffers[7]["timer"] is not first_timer
    await asyncio.sleep(0)  # let the cancellation of the old timer land
    assert first_timer.cancelled()
    assert b._dispatched == []
    # third part, short -> the whole 3-part split lands as one turn
    await b._maybe_buffer_split_part(7, "upd3", 1, SHORT)
    assert b._dispatched == [("upd", 1, LONG + LONG + SHORT)]


@pytest.mark.asyncio
async def test_window_timeout_dispatches_what_accumulated(monkeypatch):
    monkeypatch.setattr(bot_mod, "SPLIT_MERGE_WINDOW", 0.01)
    b = _bot()
    await b._maybe_buffer_split_part(7, "upd", 1, LONG)
    await asyncio.sleep(0.1)
    assert b._split_buffers == {}
    assert b._dispatched == [("upd", 1, LONG)]


@pytest.mark.asyncio
async def test_windows_are_per_chat():
    b = _bot()
    await b._maybe_buffer_split_part(7, "a", 1, LONG)
    await b._maybe_buffer_split_part(8, "b", 2, LONG)
    await b._maybe_buffer_split_part(8, "b2", 2, SHORT)
    assert b._dispatched == [("b", 2, LONG + SHORT)]
    assert list(b._split_buffers) == [7]
    b._split_buffers[7]["timer"].cancel()


@pytest.mark.asyncio
async def test_merged_text_goes_through_the_dispatch_tail():
    # (g) the flush must NOT call _enqueue_text_task directly: the btw
    # fork-reply and fast-path checks live in _dispatch_text_task and have to
    # see the merged text. Proven by the spy above being the only sink.
    b = _bot()
    await b._maybe_buffer_split_part(7, "upd", 1, LONG)
    await b._maybe_buffer_split_part(7, "upd2", 1, SHORT)
    assert len(b._dispatched) == 1
    update, user_id, text = b._dispatched[0]
    assert text == LONG + SHORT
    assert update == "upd"          # the FIRST part's update carries the reply target
    assert user_id == 1
