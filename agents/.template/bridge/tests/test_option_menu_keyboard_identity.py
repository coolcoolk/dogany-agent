"""An in-place replacement keyboard is a new owner question."""
# test-component: bridge edited option keyboard identity
# test-tier: 0
# test-prereq: tmp-files
# test-new-component: the existing owner option suite is Tier 1, while this regression requires portable Tier-0 coverage
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge import bot as bot_mod, messages
from bridge.tests.test_dgn1813_keyboard_rides_body import _make_chat_bot, _tapped


@pytest.fixture(autouse=True)
def isolate_menu_state(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod.config, "bot_data_dir", tmp_path)
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", tmp_path)


def callback(mid, label):
    message = _tapped("Reminder", "Reminder", label)
    message.chat.id = 42
    message.message_id = mid
    query = MagicMock(data="opt:" + label, message=message)
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return SimpleNamespace(callback_query=query,
                           effective_user=SimpleNamespace(id=42),
                           effective_chat=message.chat)


async def tap(bridge, update):
    with (
        patch.object(bridge, "_check_access", AsyncMock(return_value=True)),
        patch.object(bridge, "_maybe_capture_outside_approval",
                     AsyncMock(return_value=False)),
        patch.object(bridge, "_enqueue_user_task",
                     AsyncMock(return_value=True)) as enqueue,
    ):
        await bridge._handle_callback(update, MagicMock())
    return enqueue


@pytest.mark.asyncio
async def test_edited_keyboard_rearms_message_but_original_stays_answered():
    bridge = _make_chat_bot([])
    original = callback(20169, "snooze")
    first = await tap(bridge, original)
    first.assert_awaited_once()

    # The kit edits message 20169 in place with the second-step keyboard.
    second_step = callback(20169, "2 hours later - 10:00")
    second = await tap(bridge, second_step)
    second.assert_awaited_once()

    replay = callback(20169, "snooze")
    refused = await tap(bridge, replay)
    refused.assert_not_awaited()
    replay.callback_query.answer.assert_awaited_once_with()

    rows = json.loads((bot_mod.config.bot_data_dir /
                       "option-menu-answers.json").read_text())
    assert len(rows["42:20169"]["keyboard_ids"]) == 2


@pytest.mark.asyncio
async def test_legacy_answered_row_without_keyboard_identity_stays_answered():
    path = bot_mod.config.bot_data_dir / "option-menu-answers.json"
    path.write_text(json.dumps({"42:777": {"chat_id": 42, "message_id": 777,
                                           "answered": True, "choice": "snooze"}}))
    bridge = _make_chat_bot([])
    replay = callback(777, "snooze")
    refused = await tap(bridge, replay)
    refused.assert_not_awaited()
