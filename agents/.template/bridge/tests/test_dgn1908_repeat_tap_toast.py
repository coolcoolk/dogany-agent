"""DGN-1908 r2 + DGN-1876: repeated taps on an update apply button.

Owner 2026-10-10 (dev.2 -> dev.3): several taps on 적용 gave the
generic processing error, "업데이트를 시작했어요", and the same "지금 ...
진행 중이에요" line twice.  Wanted: the first tap takes the buttons off at
once; any further tap on that card -- or on another card whose tap the
machine finds IS the running run -- is answered with a popup toast on the
button (callback answer), never a chat message, no carry-through ask; and
"Message is not modified" on a callback edit is success, never surfaced.
"""

# ESTATE:BEGIN DGN-1417 /update is an estate-only region of bot.py
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages

OWNER_ID = 4242
STARTED = "🛠️ 업데이트를 시작했어요. 잠깐 재시작한 뒤, 제대로 적용됐는지 확인해서 알려드릴게요."
BUSY_SAME = "지금 라이프킷 업데이트(2.42.0-dev.1 → 2.43.0-dev.1)가 진행 중이에요. 끝나면 결과를 알려드릴게요."
STALE = "업데이트할 내용이 달라졌어요. 새 목록에서 다시 골라주세요."
TOAST = "이미 업데이트가 진행 중이에요."
APPLY = "upd:p:abcd1234-0000:1:a"


def _bridge():
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    bridge.application.bot.send_message = AsyncMock()
    return bridge


def _update(data, message_id=5, markup=None):
    message = SimpleNamespace(chat=SimpleNamespace(id=OWNER_ID), message_id=message_id,
                              date=datetime.now(timezone.utc), reply_text=AsyncMock(),
                              reply_markup=markup)
    query = MagicMock(data=data, message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return SimpleNamespace(callback_query=query, message=None,
                           effective_user=SimpleNamespace(id=OWNER_ID), effective_chat=message.chat)


def _patched(bridge, tool):
    return (patch.object(bridge, "_check_access", new=AsyncMock(return_value=True)),
            patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/plan.tsv")),
            patch.object(bridge, "_update_tool", new=tool))


def _card():
    return InlineKeyboardMarkup([[InlineKeyboardButton("적용", callback_data=APPLY),
                                  InlineKeyboardButton("< 뒤로가기", callback_data="upd:b:abcd1234-0000:1")]])


def test_toast_copy_exists_in_both_languages():
    from bridge.i18n import en, ko
    assert ko.STRINGS["update_tap_running"] == TOAST
    assert en.STRINGS["update_tap_running"] == "An update is already in progress."
    assert messages.UPDATE_TAP_RUNNING == TOAST


@pytest.mark.asyncio
async def test_rapid_triple_tap_starts_once_and_toasts_twice():
    bridge = _bridge()
    gate = asyncio.Event()
    calls = []

    async def tool(*args):
        calls.append(args)
        await gate.wait()  # tap 1 is still planning while taps 2 and 3 land
        return 0, {"action": "started", "started": True, "text": STARTED, "parse_mode": "HTML"}
    taps = [_update(APPLY, markup=_card()) for _ in range(3)]
    a, b, c = _patched(bridge, tool)
    with a, b, c:
        first = asyncio.ensure_future(bridge._handle_callback(taps[0], MagicMock()))
        for _ in range(5):
            await asyncio.sleep(0)
        # The first tap took the buttons off at once, before the machine ran.
        taps[0].callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        await bridge._handle_callback(taps[1], MagicMock())
        await bridge._handle_callback(taps[2], MagicMock())
        gate.set()
        await first
    assert len(calls) == 1
    replies = [t.callback_query.message.reply_text for t in taps]
    assert sum(r.await_count for r in replies) == 1
    replies[0].assert_awaited_once_with(STARTED, parse_mode="HTML")
    for tap in taps[1:]:
        tap.callback_query.answer.assert_awaited_once_with(text=TOAST)
        tap.callback_query.edit_message_text.assert_not_awaited()
    taps[0].callback_query.answer.assert_awaited_once_with()
    bridge.application.bot.send_message.assert_not_awaited()  # no processing-error line


@pytest.mark.asyncio
async def test_tap_on_a_finalized_card_is_a_toast():
    bridge = _bridge()
    results = [(0, {"action": "started", "started": True, "text": STARTED, "parse_mode": "HTML"})]

    async def tool(*args):
        return results.pop(0)
    first, again = _update(APPLY), _update(APPLY)
    a, b, c = _patched(bridge, tool)
    with a, b, c:
        await bridge._handle_callback(first, MagicMock())
        await bridge._handle_callback(again, MagicMock())
    again.callback_query.answer.assert_awaited_once_with(text=TOAST)
    again.callback_query.message.reply_text.assert_not_awaited()
    again.callback_query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("data,tools", [
    (APPLY, []),
    ("upd:s:abcd1234-0000:1:0", []),
    ("upd:n:2.43.0-dev.1", []),
])
async def test_another_card_whose_tap_is_the_running_run_gets_the_toast(data, tools):
    # A stale card (another message) while the update runs: the machine
    # finds the tap's closure inside the running run (DGN-1908 duplicate).
    bridge = _bridge()
    results = tools + [(0, {"action": "busy", "busy": True, "duplicate": True, "started": False,
                            "ledger": "72db8c21", "text": BUSY_SAME})]

    async def tool(*args):
        return results.pop(0)
    tap = _update(data, message_id=9)
    a, b, c = _patched(bridge, tool)
    with a, b, c:
        await bridge._handle_callback(tap, MagicMock())
    tap.callback_query.answer.assert_awaited_once_with(text=TOAST)
    tap.callback_query.message.reply_text.assert_not_awaited()  # no BUSY_SAME line, no ask
    bridge.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("out", [
    {"started": False, "state": "running"},                       # cmd_apply: ledger already active
    {"started": False, "busy": True, "duplicate": True, "text": BUSY_SAME},
])
async def test_detail_card_repeat_is_a_toast(out):
    bridge = _bridge()
    results = [(0, {"raw": "framework:a:2"}), (0, out)]

    async def tool(*args):
        return results.pop(0)
    tap = _update("upd:a:abcd1234:1:0")
    a, b, c = _patched(bridge, tool)
    with a, b, c:
        await bridge._handle_callback(tap, MagicMock())
    tap.callback_query.answer.assert_awaited_once_with(text=TOAST)
    tap.callback_query.message.reply_text.assert_not_awaited()
    tap.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_genuinely_different_pending_item_still_asks():
    bridge = _bridge()
    ask = "지금 라이프킷 업데이트가 진행 중이에요. 끝나면 이어서 적용할까요?"
    buttons = [[{"text": "이어서 적용", "callback_data": "upd:q:beef:y"},
                {"text": "그만두기", "callback_data": "upd:q:beef:n"}]]

    async def tool(*args):
        return 0, {"action": "busy", "busy": True, "started": False, "text": ask, "buttons": buttons}
    tap = _update(APPLY)
    a, b, c = _patched(bridge, tool)
    with a, b, c:
        await bridge._handle_callback(tap, MagicMock())
    assert tap.callback_query.message.reply_text.await_args.args == (ask,)
    tap.callback_query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stale_releases_the_card_and_gives_its_buttons_back():
    bridge = _bridge()
    results = [(0, {"action": "stale", "text": STALE}),
               (0, {"action": "started", "started": True, "text": STARTED})]

    async def tool(*args):
        return results.pop(0)
    first, again = _update(APPLY, markup=_card()), _update(APPLY, markup=_card())
    a, b, c = _patched(bridge, tool)
    with a, b, c:
        await bridge._handle_callback(first, MagicMock())
        await bridge._handle_callback(again, MagicMock())
    calls = first.callback_query.edit_message_reply_markup.await_args_list
    assert calls[0].kwargs["reply_markup"] is None
    assert calls[-1].kwargs["reply_markup"] is first.callback_query.message.reply_markup
    first.callback_query.message.reply_text.assert_awaited_once_with(STALE)
    again.callback_query.message.reply_text.assert_awaited_once_with(STARTED)
    first.callback_query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_list_taps_still_answer_at_once():
    bridge = _bridge()

    async def tool(*args):
        return 0, {"action": "confirm", "html": "<b>x</b>",
                   "buttons": [{"text": "적용", "data": APPLY}]}
    tap = _update("upd:o:abcd1234-0000:1:0")
    a, b, c = _patched(bridge, tool)
    with a, b, c:
        await bridge._handle_callback(tap, MagicMock())
    tap.callback_query.answer.assert_awaited_once_with()
    assert bot_mod._update_apply_tap(APPLY) and not bot_mod._update_apply_tap("upd:o:x:1:0")
    assert not bot_mod._update_apply_tap("upd:q:x:y") and not bot_mod._update_apply_tap("upd:r:x:0")


@pytest.mark.asyncio
async def test_not_modified_on_a_callback_edit_is_never_surfaced():
    bridge = _bridge()
    update = MagicMock(spec=bot_mod.Update)
    update.callback_query = MagicMock(data="upd:o:abcd1234-0000:1:0")
    update.effective_chat = SimpleNamespace(id=OWNER_ID)
    context = SimpleNamespace(error=telegram.error.BadRequest(
        "Message is not modified: specified new message content and reply markup are exactly "
        "the same as a current content and reply markup of the message"), bot=MagicMock())
    context.bot.send_message = AsyncMock()
    await bridge._error_handler(update, context)
    context.bot.send_message.assert_not_awaited()
    # Any other failure still yields the bounded notice.
    context.error = RuntimeError("boom")
    await bridge._error_handler(update, context)
    context.bot.send_message.assert_awaited_once()
# ESTATE:END
