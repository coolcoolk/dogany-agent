"""`opt:` taps retain pushed bodies instead of replacing them (DGN-1695)."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages


OWNER_ID = 4242
CHOICE = "1. <keep & run>"


def _message(*, text=None, text_html=None, caption=None, caption_html=None):
    return SimpleNamespace(
        chat=SimpleNamespace(id=OWNER_ID),
        date=datetime.now(timezone.utc),
        message_id=1,
        text=text,
        text_html=text_html,
        caption=caption,
        caption_html=caption_html,
        reply_markup=SimpleNamespace(
            inline_keyboard=[[SimpleNamespace(text=CHOICE, callback_data="opt:1")]]
        ),
    )


async def _tap(message):
    query = MagicMock(data="opt:1", message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=OWNER_ID),
        effective_chat=message.chat,
    )
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    with (
        patch.object(bridge, "_check_access", new=AsyncMock(return_value=True)),
        patch.object(
            bridge, "_maybe_capture_outside_approval", new=AsyncMock(return_value=False)
        ),
        patch.object(bridge, "_enqueue_user_task", new=AsyncMock(return_value=True)),
    ):
        await bridge._handle_callback(update, MagicMock())
    return query


@pytest.mark.asyncio
async def test_select_prompt_still_replaces_its_disposable_message():
    query = await _tap(_message(text="  " + messages.SELECT_PROMPT + "\n"))

    query.edit_message_text.assert_awaited_once_with(
        messages.SELECTED.format(choice=CHOICE)
    )
    query.edit_message_caption.assert_not_awaited()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_pushed_text_body_is_kept_and_gets_an_escaped_confirmation():
    query = await _tap(_message(text="Daily summary", text_html="<b>Daily</b> summary"))

    query.edit_message_text.assert_awaited_once_with(
        "<b>Daily</b> summary\n\n선택: 1. &lt;keep &amp; run&gt;",
        parse_mode="HTML",
        reply_markup=None,
    )
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlong_pushed_text_only_removes_its_keyboard():
    query = await _tap(_message(text="x" * 4096, text_html="x" * 4096))

    query.edit_message_text.assert_not_awaited()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_failed_body_append_falls_back_to_removing_the_keyboard():
    message = _message(text="Daily summary", text_html="<b>Daily</b> summary")
    query = MagicMock(data="opt:1", message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock(side_effect=RuntimeError("edit failed"))
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=OWNER_ID),
        effective_chat=message.chat,
    )
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    with (
        patch.object(bridge, "_check_access", new=AsyncMock(return_value=True)),
        patch.object(bridge, "_maybe_capture_outside_approval", new=AsyncMock()),
        patch.object(bridge, "_enqueue_user_task", new=AsyncMock(return_value=True)),
    ):
        await bridge._handle_callback(update, MagicMock())

    query.edit_message_text.assert_awaited_once()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_pushed_media_caption_is_kept_within_caption_limit():
    query = await _tap(
        _message(caption="Photo summary", caption_html="<i>Photo</i> summary")
    )

    query.edit_message_caption.assert_awaited_once_with(
        "<i>Photo</i> summary\n\n선택: 1. &lt;keep &amp; run&gt;",
        parse_mode="HTML",
        reply_markup=None,
    )
    query.edit_message_text.assert_not_awaited()
    query.edit_message_reply_markup.assert_not_awaited()
