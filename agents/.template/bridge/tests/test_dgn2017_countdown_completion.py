"""DGN-2017 A1 selftest: control file -> terminal UI -> completion marker.

All Telegram calls are mocked; the real driver and countdown loop run locally.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import TelegramError

from bridge.countdown import (
    CountdownDriver, render_cancelled, render_done, start_countdown,
)
from bridge.i18n.en import STRINGS as EN
from bridge.i18n.ko import STRINGS as KO


def bot_mock():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
        edit_message_text=AsyncMock(),
        delete_message=AsyncMock(),
    )


@pytest.mark.parametrize("mode", ["edit", "delete"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("button", [False, True])
def test_control_file_terminal_contract(tmp_path, mode, cancel, button):
    bot = bot_mock()
    driver = CountdownDriver(bot, control_dir=tmp_path)
    control = tmp_path / "rest.json"
    marker = tmp_path / "rest.done"
    custom = "Rest finished. Start the next set."
    control.write_text(json.dumps({
        "seconds": 1, "label": "rest", "done_mode": mode,
        "done_text": custom, "done_button": button,
    }))

    async def scenario():
        with patch.object(driver, "_owner_chat_id", return_value=42):
            await driver._tick()
            countdown = driver._active["rest"]
            # Yield once so the initial send has landed before cancellation.
            await asyncio.sleep(0)
            if cancel:
                control.unlink()
                await driver._tick()
            await asyncio.wait_for(countdown.task, timeout=3)
            await driver._tick()
            assert countdown.finished
            assert countdown.completed is not cancel
            assert not driver._active

    asyncio.run(scenario())
    bot.send_message.assert_awaited_once()
    assert not control.exists()
    assert marker.exists() is not cancel
    if not cancel:
        assert json.loads(marker.read_text())["ended_at"]
    if mode == "delete" and not cancel:
        bot.delete_message.assert_awaited_once_with(chat_id=42, message_id=123)
        bot.edit_message_text.assert_not_awaited()
    else:
        bot.delete_message.assert_not_awaited()
        bot.edit_message_text.assert_awaited_once()
        final = bot.edit_message_text.await_args.kwargs
        assert final["chat_id"] == 42
        assert final["message_id"] == 123
        assert final["text"] == (render_cancelled("rest") if cancel else custom)
        assert ("reply_markup" in final) is (button and not cancel)


def test_delete_failure_still_emits_marker_without_done_speech(tmp_path, caplog):
    bot = bot_mock()
    bot.delete_message.side_effect = TelegramError("message unavailable")
    driver = CountdownDriver(bot, control_dir=tmp_path)
    control = tmp_path / "rest.json"
    control.write_text(json.dumps({
        "seconds": 1, "label": "rest", "done_mode": "delete",
    }))

    async def scenario():
        with patch.object(driver, "_owner_chat_id", return_value=42):
            await driver._tick()
            countdown = driver._active["rest"]
            await asyncio.wait_for(countdown.task, timeout=3)
            await driver._tick()
            assert countdown.finished and countdown.completed

    asyncio.run(scenario())
    bot.delete_message.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()
    assert (tmp_path / "rest.done").exists()
    assert not control.exists()
    assert "Countdown delete failed" in caplog.text


@pytest.mark.parametrize("bad", [None, "", "   ", 0, False, [], {}, "x" * 4097])
def test_invalid_optional_fields_preserve_default_edit(tmp_path, bad):
    bot = bot_mock()
    driver = CountdownDriver(bot, control_dir=tmp_path)
    control = tmp_path / "rest.json"
    control.write_text(json.dumps({
        "seconds": 1, "label": "rest", "done_text": bad, "done_mode": bad,
    }))
    spec = driver._read_spec(control)
    assert spec is not None
    assert spec[-2:] == (None, "edit")

    async def scenario():
        countdown = start_countdown(
            bot, 42, 0, "rest", done_text=bad, done_mode=bad,
        )
        await countdown.task
        assert countdown.completed and countdown.finished

    asyncio.run(scenario())
    bot.delete_message.assert_not_awaited()
    assert bot.edit_message_text.await_args.kwargs["text"] == render_done("rest")


def test_custom_text_is_verbatim_and_survives_keyboard_fallback():
    bot = bot_mock()
    bot.edit_message_text.side_effect = [TelegramError("keyboard rejected"), None]
    custom = "  Done: {label} <plain> *literal*\nNext set.  "

    async def scenario():
        countdown = start_countdown(bot, 42, 0, "rest", done_text=custom)
        await countdown.task
        assert countdown.completed and countdown.finished

    asyncio.run(scenario())
    assert bot.edit_message_text.await_count == 2
    first, fallback = [call.kwargs for call in bot.edit_message_text.await_args_list]
    assert first["text"] == fallback["text"] == custom
    assert "reply_markup" in first and "reply_markup" not in fallback
    assert "parse_mode" not in first and "parse_mode" not in fallback


@pytest.mark.parametrize("catalog", [EN, KO])
def test_cancellation_is_localized_and_distinct(catalog):
    assert "{label}" in catalog["countdown_cancelled"]
    assert catalog["countdown_cancelled"] != catalog["countdown_done"]
    assert "done_icon" not in catalog["countdown_cancelled"]
