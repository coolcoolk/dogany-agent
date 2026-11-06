"""DGN-2249: slash commands must not answer an armed retro choice.

Replay Telegram JSON through the registered PTB handlers with an offline
transport. Cover both BOT_COMMAND entities and text-only replay updates.
"""
# test-component: bridge command routing
# test-tier: 0
# test-prereq: tmp-files
# test-new-component: Registered PTB JSON ingress is a separate verification boundary; existing typed-menu suites invoke handler methods directly.

import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest

import bridge.tests.conftest  # noqa: F401
from bridge import bot as bot_mod, messages, sdk_bridge as sdk_mod
from bridge.options import match_typed_choice


CHAT = 42
MENU = 300
LABEL = "\uae30\ub85d\ud558\uae30"
BODY = "Evening retro"


class OfflineTelegram(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 1.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 99, "is_bot": True, "first_name": "Bridge",
                      "username": "retro_bot"}
        elif name in {"sendMessage", "editMessageReplyMarkup", "editMessageText"}:
            result = {"message_id": params.get("message_id", 400), "date": 0,
                      "chat": {"id": CHAT, "type": "private"},
                      "text": params.get("text", BODY)}
        else:
            raise AssertionError("Unexpected Telegram method: " + name)
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.fixture
def armed(tmp_path, monkeypatch):
    (tmp_path / ".telegram_bot").mkdir()
    bubbles = tmp_path / ".telegram_bot" / "options-bubbles.json"
    monkeypatch.setattr(sdk_mod, "options_bubbles_path", lambda: bubbles)
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bot_mod, "DECISION_CARRY", True)
    # The real usage handler runs this report seam; no live account is read.
    script = tmp_path / "routines" / "claude-usage.sh"
    script.parent.mkdir()
    script.touch()
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot._check_access = AsyncMock(return_value=True)
    bot._usage_report = lambda _: "Usage: 42% remaining"
    bot._usage_main = {}
    bot._maybe_consume_secret = AsyncMock(return_value=False)
    bot._maybe_consume_login_code = AsyncMock(return_value=False)
    bot._maybe_capture_outside_approval = AsyncMock(return_value=False)
    bot._maybe_buffer_split_part = AsyncMock(return_value=False)
    bot._dispatch_text_task = AsyncMock()
    bot._exec_slash_command = AsyncMock()
    sdk_mod.publish_options_bubble(
        CHAT, MENU, ["1. " + LABEL],
        menu={"labels": [LABEL], "answered": False, "covered": False},
    )
    bot._live_menu_registry().add(CHAT, MENU, [LABEL])
    return bot, bubbles


async def replay(bot, text, *, entity=False, reply=False):
    transport = OfflineTelegram()
    app = (Application.builder().token("test:token").request(transport)
           .get_updates_request(OfflineTelegram()).build())
    bot.application = app
    bot._setup_handlers()
    errors = []

    async def error(update, context):
        errors.append(context.error)

    app.add_error_handler(error)
    msg = {"message_id": 350, "date": int(time.time()), "text": text,
           "chat": {"id": CHAT, "type": "private"},
           "from": {"id": CHAT, "is_bot": False, "first_name": "Owner"}}
    if entity:
        msg["entities"] = [{"type": "bot_command", "offset": 0,
                            "length": len(text.split()[0])}]
    if reply:
        msg["reply_to_message"] = {
            "message_id": MENU, "date": int(time.time()), "text": BODY,
            "chat": msg["chat"],
            "from": {"id": 99, "is_bot": True, "first_name": "Bridge"},
        }
    async with app:
        update = Update.de_json({"update_id": 123, "message": msg}, app.bot)
        with patch.object(bot_mod.session_manager, "get_session",
                          AsyncMock(return_value={})):
            await app.process_update(update)
    assert not errors, errors
    return transport.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", [False, True], ids=["text-only", "entity"])
@pytest.mark.parametrize("reply", [False, True], ids=["plain", "reply-to-menu"])
async def test_usage_answers_without_consuming_retro(armed, entity, reply):
    bot, bubbles = armed
    calls = await replay(bot, "/usage", entity=entity, reply=reply)
    assert json.loads(bubbles.read_text())[-1]["menu"]["answered"] is False
    assert sdk_mod.live_decision_menu(CHAT)["message_id"] == MENU
    cards = [p for method, p in calls if method == "sendMessage"]
    assert len(cards) == 1
    assert cards[0]["text"] == "<pre>Usage: 42% remaining</pre>"
    assert cards[0]["reply_markup"]  # The real /usage card, with its buttons.
    assert not any(method.startswith("editMessage") for method, _ in calls)
    assert len(bot._live_menu_registry().live(CHAT)) == 1
    bot._dispatch_text_task.assert_not_awaited()
    bot._exec_slash_command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
async def test_free_text_still_resolves_choice(armed, reply):
    bot, bubbles = armed
    calls = await replay(bot, "My evening went well", reply=reply)
    assert json.loads(bubbles.read_text())[-1]["menu"]["answered"] is True
    assert sdk_mod.live_decision_menu(CHAT) is None
    edits = [p for method, p in calls if method == "editMessageText"]
    if reply:
        assert edits[0]["text"] == BODY + "\n\n" + messages.SELECTED.format(
            choice=messages.SELECTED_TYPED)
        assert not bot._live_menu_registry().live(CHAT)
    else:
        assert not edits
    bot._dispatch_text_task.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["1", LABEL])
async def test_typed_number_and_label_still_select(armed, answer):
    bot, bubbles = armed
    calls = await replay(bot, answer)
    assert json.loads(bubbles.read_text())[-1]["menu"]["answered"] is True
    edits = [p for method, p in calls if method == "editMessageText"]
    assert edits[0]["text"].endswith(messages.SELECTED.format(choice="1. " + LABEL))
    bot._dispatch_text_task.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", [False, True])
@pytest.mark.parametrize("text", ["/USAGE", "/usage@retro_bot"])
async def test_command_spelling(armed, text, entity):
    bot, bubbles = armed
    calls = await replay(bot, text, entity=entity, reply=True)
    assert any(p.get("text") == "<pre>Usage: 42% remaining</pre>"
               for method, p in calls if method == "sendMessage")
    assert json.loads(bubbles.read_text())[-1]["menu"]["answered"] is False
    bot._dispatch_text_task.assert_not_awaited()
    bot._exec_slash_command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", [False, True])
async def test_unknown_command_uses_slash_path(armed, entity):
    bot, bubbles = armed
    await replay(bot, "/retrostatus details", entity=entity, reply=True)
    assert bot._exec_slash_command.await_args.args[1] == "/retrostatus details"
    assert json.loads(bubbles.read_text())[-1]["menu"]["answered"] is False
    bot._dispatch_text_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_for_another_bot_leaves_choice_alone(armed):
    bot, bubbles = armed
    calls = await replay(bot, "/usage@other_bot", reply=True)
    assert [name for name, _ in calls] == ["getMe"]
    assert json.loads(bubbles.read_text())[-1]["menu"]["answered"] is False
    bot._dispatch_text_task.assert_not_awaited()
    bot._exec_slash_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_path_is_still_plain_text(armed):
    bot, _ = armed
    await replay(bot, "/tmp/retro.txt")
    bot._dispatch_text_task.assert_awaited_once()
    bot._exec_slash_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_preserves_builtin_arguments(armed):
    bot, bubbles = armed
    bot._cmd_model = AsyncMock()
    await replay(bot, "/model opus")
    assert bot._cmd_model.await_args.args[1].args == ["opus"]
    assert json.loads(bubbles.read_text())[-1]["menu"]["answered"] is False


@pytest.mark.parametrize("text", ["/usage", "/USAGE", "/usage@retro_bot", "/retrostatus details"])
def test_command_cannot_match_a_button_or_recorded_label(text):
    # A command-shaped label must still be picked by number or by a tap.
    assert match_typed_choice(text, ["1. " + text], [text]) is None
    assert match_typed_choice(text, [text]) is None
    assert match_typed_choice("1", ["1. " + text], [text]) == "1. " + text


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_to", [None, MENU], ids=["plain", "reply-to-menu"])
@pytest.mark.parametrize("text", ["/usage", "/USAGE", "/usage@retro_bot", "/retrostatus details"])
async def test_command_never_settles_live_choice(armed, text, reply_to):
    # DGN-2215: 2026-10-21 retro, /usage must not pick "record it".
    # Exercise the settlement boundary independently of PTB routing.
    bot, bubbles = armed
    before = bubbles.read_bytes()
    bot.application = AsyncMock()
    await bot._clear_typed_over_menu(CHAT, text, reply_to=reply_to)
    assert bubbles.read_bytes() == before
    assert sdk_mod.live_decision_menu(CHAT)["message_id"] == MENU
    assert len(bot._live_menu_registry().live(CHAT)) == 1
    assert not bot.application.bot.mock_calls


def test_filesystem_path_can_still_match_a_choice():
    assert match_typed_choice("/tmp/retro.txt", ["/tmp/retro.txt"]) == "/tmp/retro.txt"
