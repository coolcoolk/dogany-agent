"""DGN-1813: the [[OPTIONS]] keyboard rides the body bubble.

Owner-confirmed shape (2026-10-01 08:09):
  - no separate SELECT_PROMPT bubble: the keyboard is attached to the LAST
    body message after the final stream edit (split body -> last chunk);
  - DGN-665 list-only body (nothing left to carry it) -> the short
    SELECT_PROMPT line carries the keyboard (also the attach-failure path);
  - on tap the SAME bubble is edited: body kept, keyboard removed,
    "Selected: <label>" appended; over Telegram's 4096 limit the keyboard is
    stripped and the line goes out as a short reply to that bubble;
  - no "N. " prefix on a lone button, nor when the body as sent carries no
    matching numbered list.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram.error

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages
from bridge.options import body_lists_options, build_option_keyboard


def _texts(kb):
    return [row[0].text for row in kb.inline_keyboard]


# ---------------------------------------------------------------------------
# 1. Button prefix rule
# ---------------------------------------------------------------------------


class TestPrefixRule:
    def test_lone_button_has_no_number(self):
        assert _texts(build_option_keyboard(["later"])) == ["later"]

    def test_lone_button_unnumbered_even_when_asked(self):
        # body_lists_options never says True for one option.
        assert body_lists_options("1. later", ["later"]) is False

    def test_multi_default_keeps_numbers(self):
        assert _texts(build_option_keyboard(["a", "b"])) == ["1. a", "2. b"]

    def test_unnumbered_bundle(self):
        kb = build_option_keyboard(["proceed", "hold"], numbered=False)
        assert _texts(kb) == ["proceed", "hold"]
        assert [r[0].callback_data for r in kb.inline_keyboard] == [
            "opt:proceed", "opt:hold",
        ]

    def test_unnumbered_never_degrades_to_number_handle(self):
        long = "a fairly long option label that overflows the width"
        kb = build_option_keyboard([long, "hold"], numbered=False)
        assert _texts(kb) == [long, "hold"]

    def test_numbered_overflow_still_degrades(self):
        long = "a fairly long option label that overflows the width"
        kb = build_option_keyboard([long, "hold"], numbered=True)
        assert all(not t.startswith("1. a fairly") for t in _texts(kb))

    def test_unnumbered_digit_label_never_collides_with_index_fallback(self):
        long = "\uac00" * 30  # overflows the 64-byte callback_data limit
        kb = build_option_keyboard(["2", long], numbered=False)
        cbs = [r[0].callback_data for r in kb.inline_keyboard]
        assert len(set(cbs)) == 2, cbs

    def test_body_with_matching_run(self):
        body = "Pick:\n1. proceed - fast\n2. hold - safe"
        assert body_lists_options(body, ["proceed", "hold"]) is True

    def test_body_without_run(self):
        assert body_lists_options("Pick a direction:", ["proceed", "hold"]) is False

    def test_unrelated_run_length_does_not_count(self):
        body = "Steps done:\n1. a\n2. b\n3. c\n\nPick one."
        assert body_lists_options(body, ["proceed", "hold"]) is False

    def test_run_inside_code_fence_does_not_count(self):
        body = "```\n1. proceed\n2. hold\n```"
        assert body_lists_options(body, ["proceed", "hold"]) is False


# ---------------------------------------------------------------------------
# 2. Send seats: keyboard attaches to the last body message
# ---------------------------------------------------------------------------


def _make_message(sent, chat_id=42):
    """Trigger message; every reply_text returns a message with a fresh id."""
    msg = MagicMock()
    msg.chat.id = chat_id
    msg.message_id = 100
    msg.date = datetime.now(timezone.utc)
    counter = iter(range(500, 600))

    async def _reply_text(text, **kwargs):
        mid = next(counter)
        sent.append({"text": text, "message_id": mid, **kwargs})
        return SimpleNamespace(message_id=mid)

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    tg_bot = MagicMock()
    tg_bot.delete_message = AsyncMock()
    tg_bot.edit_message_text = AsyncMock()
    tg_bot.edit_message_reply_markup = AsyncMock()
    msg.get_bot.return_value = tg_bot
    return msg


def _make_bot():
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    bot._last_incoming_mid = {}
    bot._kick_update_offer = MagicMock()
    return bot


def _make_chat_bot(sent):
    bot = _make_bot()
    counter = iter(range(700, 800))

    async def _send_message(chat_id, text, **kwargs):
        mid = next(counter)
        sent.append({"text": text, "message_id": mid, **kwargs})
        return SimpleNamespace(message_id=mid)

    bot.application.bot.send_message = AsyncMock(side_effect=_send_message)
    bot.application.bot.delete_message = AsyncMock()
    bot.application.bot.edit_message_text = AsyncMock()
    bot.application.bot.edit_message_reply_markup = AsyncMock()
    return bot


def _run(coro):
    return asyncio.run(coro)


MARKER_REPLY = "Pick a direction:\n\n1. proceed\n2. hold\n[[OPTIONS]]\n"


def _prompt_sends(sent):
    return [e for e in sent if e["text"] == messages.SELECT_PROMPT]


class TestReplySmartSeat:
    def test_keyboard_attached_to_body_no_prompt_bubble(self):
        sent = []
        msg = _make_message(sent)
        bot = _make_bot()
        with patch.object(bot, "_reply_link_id", return_value=None):
            _run(bot._reply_smart(msg, MARKER_REPLY, force_options=True))

        assert _prompt_sends(sent) == [], f"select bubble still sent: {sent}"
        assert len(sent) == 1 and "Pick a direction:" in sent[0]["text"]
        edit = msg.get_bot.return_value.edit_message_reply_markup
        edit.assert_awaited_once()
        kwargs = edit.await_args.kwargs
        assert kwargs["chat_id"] == 42
        assert kwargs["message_id"] == sent[0]["message_id"]
        # Body list was stripped -> buttons carry no "N. " prefix.
        assert _texts(kwargs["reply_markup"]) == ["proceed", "hold"]

    def test_split_body_keyboard_on_last_chunk(self):
        sent = []
        msg = _make_message(sent)
        bot = _make_bot()
        long_body = ("para " * 300 + "\n\n") * 4
        with patch.object(bot, "_reply_link_id", return_value=None):
            _run(bot._reply_smart(
                msg, long_body + "1. proceed\n2. hold\n[[OPTIONS]]\n",
                force_options=True,
            ))

        assert len(sent) >= 2, "fixture must split"
        assert _prompt_sends(sent) == []
        edit = msg.get_bot.return_value.edit_message_reply_markup
        assert edit.await_args.kwargs["message_id"] == sent[-1]["message_id"]

    def test_list_only_body_prompt_line_carries_keyboard(self):
        sent = []
        msg = _make_message(sent)
        bot = _make_bot()
        _run(bot._reply_smart(msg, "1. approve\n2. reject\n[[OPTIONS]]\n",
                              force_options=True))

        assert len(sent) == 1
        assert sent[0]["text"] == messages.SELECT_PROMPT
        assert _texts(sent[0]["reply_markup"]) == ["approve", "reject"]
        # The prompt line is the turn's only signal -> loud.
        assert sent[0]["disable_notification"] is False
        msg.get_bot.return_value.edit_message_reply_markup.assert_not_awaited()

    def test_attach_failure_falls_back_to_prompt_line(self):
        sent = []
        msg = _make_message(sent)
        msg.get_bot.return_value.edit_message_reply_markup = AsyncMock(
            side_effect=RuntimeError("message to edit not found")
        )
        bot = _make_bot()
        with patch.object(bot, "_reply_link_id", return_value=None):
            _run(bot._reply_smart(msg, MARKER_REPLY, force_options=True))

        prompts = _prompt_sends(sent)
        assert len(prompts) == 1, f"buttons lost: {sent}"
        assert prompts[0]["reply_markup"] is not None

    def test_streamed_in_place_final_attaches_to_draft(self):
        sent = []
        msg = _make_message(sent)
        bot = _make_bot()
        # Classifier-injected marker: the body keeps its list, so the
        # streamed draft is final in place and buttons stay numbered.
        content = "Pick one:\n1. proceed\n2. hold\n\n[[OPTIONS]]"
        with patch.object(bot, "_reply_link_id", return_value=None):
            _run(bot._reply_smart(
                msg, content, force_options=True, streamed=True,
                draft_message_ids=[321], classifier_injected=True,
            ))

        assert sent == [], f"unexpected re-send: {sent}"
        edit = msg.get_bot.return_value.edit_message_reply_markup
        edit.assert_awaited_once()
        assert edit.await_args.kwargs["message_id"] == 321
        assert _texts(edit.await_args.kwargs["reply_markup"]) == [
            "1. proceed", "2. hold",
        ]

    def test_single_option_unnumbered_even_with_body_line(self):
        sent = []
        msg = _make_message(sent)
        bot = _make_bot()
        with patch.object(bot, "_reply_link_id", return_value=None):
            _run(bot._reply_smart(
                msg, "Ready when you are.\n\n1. later\n\n[[OPTIONS]]",
                force_options=True, classifier_injected=True,
            ))

        edit = msg.get_bot.return_value.edit_message_reply_markup
        assert _texts(edit.await_args.kwargs["reply_markup"]) == ["later"]


class TestSendSmartSeat:
    def test_chat_rail_attaches_to_last_body_message(self):
        sent = []
        bot = _make_chat_bot(sent)
        _run(bot._send_smart(42, MARKER_REPLY, force_options=True))

        assert _prompt_sends(sent) == [], f"select bubble still sent: {sent}"
        edit = bot.application.bot.edit_message_reply_markup
        edit.assert_awaited_once()
        assert edit.await_args.kwargs["message_id"] == sent[-1]["message_id"]
        assert _texts(edit.await_args.kwargs["reply_markup"]) == ["proceed", "hold"]

    def test_chat_rail_list_only_prompt_line(self):
        sent = []
        bot = _make_chat_bot(sent)
        _run(bot._send_smart(42, "1. approve\n2. reject\n[[OPTIONS]]\n",
                             force_options=True))

        assert [e["text"] for e in sent] == [messages.SELECT_PROMPT]
        bot.application.bot.edit_message_reply_markup.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Tap: same bubble keeps its body; overflow -> short reply
# ---------------------------------------------------------------------------


OWNER_ID = 4242


def _tapped(text, text_html, label="proceed"):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=OWNER_ID),
        date=datetime.now(timezone.utc),
        message_id=77,
        text=text,
        text_html=text_html,
        caption=None,
        caption_html=None,
        reply_markup=SimpleNamespace(
            inline_keyboard=[[SimpleNamespace(text=label, callback_data=f"opt:{label}")]]
        ),
        reply_text=AsyncMock(),
    )
    return message


async def _tap(message, data="opt:proceed", edit_text=None):
    query = MagicMock(data=data, message=message)
    query.answer = AsyncMock()
    query.edit_message_text = edit_text or AsyncMock()
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
async def test_tap_keeps_body_and_appends_selected_label():
    message = _tapped("Pick a direction:", "Pick a direction:")
    query = await _tap(message)

    query.edit_message_text.assert_awaited_once_with(
        "Pick a direction:\n\n" + messages.SELECTED.format(choice="proceed"),
        parse_mode="HTML",
        reply_markup=None,
    )
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_tap_over_limit_strips_keyboard_and_replies_short():
    message = _tapped("x" * 4090, "x" * 4090)
    query = await _tap(message)

    query.edit_message_text.assert_not_awaited()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    message.reply_text.assert_awaited_once()
    args, kwargs = message.reply_text.await_args
    assert args[0] == messages.SELECTED.format(choice="proceed")
    assert kwargs["reply_parameters"].message_id == 77
    assert kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_tap_edit_rejected_still_confirms_by_reply():
    message = _tapped("Pick a direction:", "Pick a direction:")
    query = await _tap(
        message, edit_text=AsyncMock(side_effect=RuntimeError("edit failed"))
    )

    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_tap_on_fallback_prompt_line_is_replaced():
    message = _tapped(messages.SELECT_PROMPT, messages.SELECT_PROMPT)
    query = await _tap(message)

    query.edit_message_text.assert_awaited_once_with(
        messages.SELECTED.format(choice="proceed")
    )
    message.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. DGN-1876: duplicate tap / "not modified" never reaches the owner
# ---------------------------------------------------------------------------


async def _tap_capture(message, edit_text=None, strip=None):
    query = MagicMock(data="opt:proceed", message=message)
    query.answer = AsyncMock()
    query.edit_message_text = edit_text or AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = strip or AsyncMock()
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=OWNER_ID),
        effective_chat=message.chat,
    )
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    enqueue = AsyncMock(return_value=True)
    with (
        patch.object(bridge, "_check_access", new=AsyncMock(return_value=True)),
        patch.object(
            bridge, "_maybe_capture_outside_approval", new=AsyncMock(return_value=False)
        ),
        patch.object(bridge, "_enqueue_user_task", new=enqueue),
    ):
        await bridge._handle_callback(update, MagicMock())
    return query, enqueue


def _not_modified():
    return telegram.error.BadRequest(
        "Message is not modified: specified new message content and reply "
        "markup are exactly the same as a current content and reply markup"
    )


@pytest.mark.asyncio
async def test_dgn1876_not_modified_is_duplicate_tap_dropped_quietly():
    message = _tapped("Pick a direction:", "Pick a direction:")
    query, enqueue = await _tap_capture(
        message, edit_text=AsyncMock(side_effect=_not_modified())
    )
    # Duplicate: no keyboard strip, no reply, and the choice is NOT re-dispatched.
    query.edit_message_reply_markup.assert_not_awaited()
    message.reply_text.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_dgn1876_first_tap_still_dispatches():
    message = _tapped("Pick a direction:", "Pick a direction:")
    _query, enqueue = await _tap_capture(message)
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_dgn1876_other_badrequest_and_failed_strip_never_raise():
    message = _tapped("Pick a direction:", "Pick a direction:")
    query, enqueue = await _tap_capture(
        message,
        edit_text=AsyncMock(side_effect=telegram.error.BadRequest("Bad entity")),
        strip=AsyncMock(side_effect=_not_modified()),
    )
    message.reply_text.assert_awaited_once()
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_dgn1876_over_limit_strip_failure_still_dispatches():
    message = _tapped("x" * 4090, "x" * 4090)
    _query, enqueue = await _tap_capture(
        message, strip=AsyncMock(side_effect=RuntimeError("boom"))
    )
    message.reply_text.assert_awaited_once()
    enqueue.assert_awaited_once()
