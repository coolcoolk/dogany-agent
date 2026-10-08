"""DGN-1934 fix 3: one live decision menu per chat.

A new [[OPTIONS]] keyboard retires every older still-unanswered one in the
same chat: the old message keeps its body, loses its buttons, and gains a
one-line pointer to the newer message (DRAFT copy, owner confirmation
pending). An answered menu is never touched again.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages
from bridge.i18n import en, ko
from bridge.options import LiveMenuRegistry, superseded_body_html

MENU = "Pick a direction:\n\n1. ship\n2. wait\n[[OPTIONS]]\n"
CHAT = 42


def _make_bot():
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    tg = bot.application.bot

    async def _edit_markup(chat_id, message_id, reply_markup=None):
        return SimpleNamespace(
            message_id=message_id, caption=None,
            text_html="<b>Old menu</b> %d" % message_id,
        )

    tg.edit_message_reply_markup = AsyncMock(side_effect=_edit_markup)
    tg.edit_message_text = AsyncMock()
    tg.edit_message_caption = AsyncMock()
    return bot


def _send_menu(bot, carrier_id):
    asyncio.run(bot._send_content_artifacts(
        CHAT, MENU, force_options=True, body=MENU, carrier_id=carrier_id,
    ))


def test_registry_keeps_one_live_menu_per_chat():
    reg = LiveMenuRegistry()
    assert reg.register(1, 10) == []
    assert reg.register(1, 11) == [10]
    assert reg.register(2, 50) == []  # chats are independent
    assert reg.live(1) == [11]
    reg.answered(1, 11)
    assert reg.live(1) == []
    assert reg.register(1, 12) == []  # an answered menu is never retired


def test_new_menu_retires_the_older_unanswered_one():
    bot = _make_bot()
    tg = bot.application.bot
    _send_menu(bot, 100)
    # First menu: attached to its body, nothing to retire.
    assert [c.kwargs["message_id"] for c in tg.edit_message_reply_markup.await_args_list] == [100]
    tg.edit_message_text.assert_not_awaited()

    _send_menu(bot, 200)
    calls = tg.edit_message_reply_markup.await_args_list
    assert [c.kwargs["message_id"] for c in calls] == [100, 200, 100]
    assert calls[-1].kwargs["reply_markup"] is None
    tg.edit_message_text.assert_awaited_once()
    kwargs = tg.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 100
    assert kwargs["reply_markup"] is None
    assert kwargs["text"].startswith("<b>Old menu</b> 100")
    assert kwargs["text"].endswith(messages.OPTIONS_SUPERSEDED_POINTER)
    assert bot._live_menu_registry().live(CHAT) == [200]


def test_answered_menu_is_not_retired():
    bot = _make_bot()
    tg = bot.application.bot
    _send_menu(bot, 100)
    bot._live_menu_registry().answered(CHAT, 100)
    _send_menu(bot, 200)
    assert [c.kwargs["message_id"] for c in tg.edit_message_reply_markup.await_args_list] == [100, 200]
    tg.edit_message_text.assert_not_awaited()


def test_retire_failure_never_breaks_the_new_menu():
    bot = _make_bot()
    tg = bot.application.bot
    _send_menu(bot, 100)
    tg.edit_message_reply_markup.side_effect = [None, RuntimeError("gone")]
    _send_menu(bot, 200)  # must not raise
    tg.edit_message_text.assert_not_awaited()
    assert bot._live_menu_registry().live(CHAT) == [200]


def test_pointer_body_respects_the_limit_and_escapes():
    assert superseded_body_html("<b>x</b>", "a < b") == "<b>x</b>\n\na &lt; b"
    assert superseded_body_html("x" * 4095, "-> below") is None
    assert superseded_body_html("", "-> below") is None
    assert superseded_body_html(None, "-> below") is None


def test_pointer_copy_is_owner_confirmed_in_both_catalogs():
    assert en.STRINGS["options_superseded_pointer"] == "→ continue in the message below"
    assert ko.STRINGS["options_superseded_pointer"] == "→ 아래 메시지에서 이어서 골라 주세요"
    for mod in (en, ko):
        src = open(mod.__file__, encoding="utf-8").read()
        i = src.index('"options_superseded_pointer"')
        assert "owner-confirmed" in src[max(0, i - 400):i]
        assert "DRAFT" not in src[max(0, i - 400):i]
