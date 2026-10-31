"""DGN-1884 follow-up (dec-263 (a)): the [버전 맞추기] button under a /update
crew split row.

The machine (scripts/pack/update_surface.py `sync`, covered end to end by
tests/dgn1884b_crew_sync_selftest.py) re-checks the split, plans that crew
sync and starts the same ledger run as any tap.  Here only the bridge
adapter: the tap bypasses the 20-min gate like every update-screen tap,
enters the `sync` verb with the callback receipt time and the tapping user,
relays the machine's copy verbatim, and takes only the tapped button off
the card (the other rows stay appliable).  /update sends a screen that
holds only split rows (no items) like any offer.
"""

# ESTATE:BEGIN DGN-1417 /update is an estate-only region of bot.py
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod

OWNER_ID = 4242
STALE = "업데이트할 내용이 달라졌어요. 새 목록에서 다시 골라주세요."
STARTED = "🛠️ 업데이트를 시작했어요. 잠깐 재시작한 뒤, 제대로 적용됐는지 확인해서 알려드릴게요."
LOOKUP_FAILED = "업데이트 목록을 불러오지 못했어요. 몇 분 뒤 한 번 더 확인해서 알려드릴게요."
SYNC = "upd:s:abcd1234-0000:1:0"
FW = "upd:o:abcd1234-0000:1:0"
SPLIT_ROW = ("<blockquote>[킷] 라이프킷: 함께 쓰는 에이전트들의 버전이 서로 달라요(에이 v2.41.0, 비 v2.40.0). "
             "[버전 맞추기]를 누르면 모두 최신 정식판으로 맞춘 뒤 업데이트해요. "
             "나머지 업데이트는 그대로 적용할 수 있어요.</blockquote>")


def _card():
    return InlineKeyboardMarkup([[InlineKeyboardButton("[프레임워크] Dogany v2.6.1 업데이트", callback_data=FW)],
                                 [InlineKeyboardButton("버전 맞추기", callback_data=SYNC)]])


async def _tap(data, tool_results, markup=None):
    message = SimpleNamespace(chat=SimpleNamespace(id=OWNER_ID), message_id=5,
                              date=datetime.now(timezone.utc), reply_text=AsyncMock(),
                              reply_markup=markup)
    query = MagicMock(data=data, message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = SimpleNamespace(callback_query=query, message=None,
                             effective_user=SimpleNamespace(id=OWNER_ID), effective_chat=message.chat)
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    calls = []

    async def tool(*args):
        calls.append(args)
        return tool_results.pop(0)
    access = AsyncMock(return_value=True)
    with (
        patch.object(bridge, "_check_access", new=access),
        patch.object(bridge, "_update_plan", new=AsyncMock(return_value="/tmp/plan.tsv")),
        patch.object(bridge, "_update_tool", new=tool),
    ):
        await bridge._handle_callback(update, MagicMock())
    return query, calls, access


async def _drain():
    for _ in range(20):
        await asyncio.sleep(0)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


@pytest.mark.asyncio
async def test_sync_tap_enters_the_sync_verb_and_relays_the_start_copy():
    query, calls, access = await _tap(SYNC, [(0, {"action": "started", "started": True, "text": STARTED,
                                                  "parse_mode": "HTML", "ledger": "beef"})], _card())
    assert bot_mod._callback_ttl(query.data) is None  # dec-272: the ledger decides staleness
    verb = calls[0]
    assert verb[0] == "sync"
    assert verb[verb.index("--ledger") + 1] == "abcd1234-0000"
    assert verb[verb.index("--revision") + 1] == "1"
    assert verb[verb.index("--pick") + 1] == "0"
    assert verb[verb.index("--user") + 1] == str(OWNER_ID)
    assert verb[verb.index("--requester") + 1] == str(OWNER_ID)
    assert verb[verb.index("--plan") + 1] == "/tmp/plan.tsv"
    assert int(verb[verb.index("--tapped-at") + 1]) > 0
    query.message.reply_text.assert_awaited_once_with(STARTED, parse_mode="HTML")


@pytest.mark.asyncio
async def test_sync_tap_takes_only_its_own_button_off_the_card():
    query, _, _ = await _tap(SYNC, [(0, {"action": "started", "started": True, "text": STARTED,
                                         "parse_mode": "HTML"})], _card())
    markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert _rows(markup) == [[("[프레임워크] Dogany v2.6.1 업데이트", FW)]]


@pytest.mark.asyncio
async def test_sync_tap_on_a_split_only_card_clears_it():
    card = InlineKeyboardMarkup([[InlineKeyboardButton("버전 맞추기", callback_data=SYNC)]])
    query, _, _ = await _tap(SYNC, [(0, {"action": "started", "started": True, "text": STARTED,
                                         "parse_mode": "HTML"})], card)
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_busy_sync_tap_relays_the_ask_with_its_buttons():
    ask = "지금 [킷] 라이프킷 업데이트가 진행 중이에요. 끝나면 이어서 적용할까요?"
    buttons = [[{"text": "이어서 적용", "callback_data": "upd:q:beef:y"},
                {"text": "그만두기", "callback_data": "upd:q:beef:n"}]]
    query, _, _ = await _tap(SYNC, [(0, {"action": "busy", "busy": True, "text": ask, "buttons": buttons})], _card())
    sent = query.message.reply_text.await_args
    assert sent.args == (ask,)
    assert _rows(sent.kwargs["reply_markup"]) == [[("이어서 적용", "upd:q:beef:y"), ("그만두기", "upd:q:beef:n")]]
    query.edit_message_reply_markup.assert_awaited_once()


def _restored(query):
    # DGN-1908 r2: the tapped button left at the tap; the card comes back whole.
    first, last = query.edit_message_reply_markup.await_args_list
    assert _rows(first.kwargs["reply_markup"]) == [[("[프레임워크] Dogany v2.6.1 업데이트", FW)]]
    assert _rows(last.kwargs["reply_markup"]) == _rows(_card())


@pytest.mark.asyncio
async def test_stale_sync_tap_relays_stale_and_keeps_the_card():
    query, _, _ = await _tap(SYNC, [(0, {"action": "stale", "text": STALE})], _card())
    query.message.reply_text.assert_awaited_once_with(STALE)
    _restored(query)
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_tap_failure_says_so_and_schedules_the_recheck():
    retried = []

    async def fake_retry(self, chat_id):
        retried.append(chat_id)
    with patch.object(bot_mod.TelegramBot, "_update_offer_retry", new=fake_retry):
        query, _, _ = await _tap(SYNC, [(2, {})], _card())
        await _drain()
    query.message.reply_text.assert_awaited_once_with(LOOKUP_FAILED)
    _restored(query)
    assert retried == [OWNER_ID]


@pytest.mark.asyncio
async def test_update_command_sends_a_split_only_screen_with_its_button():
    bot = bot_mod.TelegramBot()
    bot.application = MagicMock()
    bot._check_access = AsyncMock(return_value=True)
    bot._update_plan = AsyncMock(return_value="/tmp/plan.tsv")
    bot._update_tool = AsyncMock(return_value=(0, {"ledger": "abcd1234-0000", "revision": 1, "items": [],
                                                   "html": SPLIT_ROW,
                                                   "buttons": [{"text": "버전 맞추기", "data": SYNC}]}))
    update = MagicMock()
    update.effective_user.id = OWNER_ID
    update.effective_chat.id = OWNER_ID
    update.message.reply_text = AsyncMock()
    await bot._cmd_update(update, MagicMock())
    sent = update.message.reply_text.await_args
    assert sent.args == (SPLIT_ROW,)
    assert sent.kwargs["parse_mode"] == "HTML"
    assert _rows(sent.kwargs["reply_markup"]) == [[("버전 맞추기", SYNC)]]
# ESTATE:END
