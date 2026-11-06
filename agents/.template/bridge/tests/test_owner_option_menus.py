"""DGN-2311/2312: owner menus deliver, accept multiline text, and latch answers.

Port of release commit 4e794b2, adapted to DGN-1928 numbering and the
DGN-2062/DGN-2084 typed-answer path in the framework source.
"""
# test-component: bridge owner option menus
# test-tier: 1
# test-prereq: tmp-files
# test-new-component: cross-rail owner answer lifecycle and durable replay boundary, including generated bridge parity
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge import bot as bot_mod, messages, sdk_bridge
from bridge.options import extract_marker_labels, strip_consumed_options
from bridge.tests.test_dgn1813_keyboard_rides_body import (
    _make_bot, _make_chat_bot, _make_message, _tapped,
)

LABELS = ['계획표 중복 정리', '마무리 화면 줄이기', '다른 메시지 (말씀해 주세요)']
SKULL = (
    '요리 메시지에서 고칠 곳을 골라 주세요.\n\n[[OPTIONS]]\n'
    '1. 계획표 중복 정리\n2. 마무리 화면 줄이기\n3. 다른 메시지 (말씀해 주세요)\n\n'
    '- **1. 계획표 중복 정리:** 프렙 목록 메시지와 시간표 메시지를 하나로 합칩니다.\n'
    '- **2. 마무리 화면 줄이기:** 하루 요약만 남깁니다.\n'
    '- **3. 다른 메시지:** 제가 보지 못한 메시지를 짚어 주세요.'
)
METAL = (
    '레인 알림 이름을 골라 주세요.\n\n'
    '[[OPTIONS: 작업 이름 + 검토 | 검토: 작업 이름 | PM 검토 · 작업 이름 | 작업 이름 결과 확인]]'
)


@pytest.fixture(autouse=True)
def isolate_menu_state(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod.config, 'bot_data_dir', tmp_path)
    monkeypatch.setattr(bot_mod, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(sdk_bridge, 'options_bubbles_path', lambda: tmp_path / 'options-bubbles.json')


@pytest.mark.asyncio
@pytest.mark.parametrize('rail', ['send', 'reply'])
async def test_skull_observed_shape_has_buttons_and_a_verdict(rail, caplog):
    sent = []
    bridge = _make_chat_bot(sent) if rail == 'send' else _make_bot()
    with caplog.at_level(logging.INFO, logger='bridge.bot'):
        if rail == 'send':
            await bridge._send_smart(42, SKULL, force_options=True)
            edit = bridge.application.bot.edit_message_reply_markup
        else:
            msg = _make_message(sent)
            with patch.object(bridge, '_reply_link_id', return_value=None):
                await bridge._reply_smart(msg, SKULL, force_options=True)
            edit = msg.get_bot.return_value.edit_message_reply_markup
    kb = edit.await_args.kwargs['reply_markup']
    assert len(kb.inline_keyboard) == 3
    assert all('**' not in row[0].text for row in kb.inline_keyboard)
    assert extract_marker_labels(SKULL) == LABELS
    assert any('keyboard verdict' in r.message and 'marker_found' in r.message
               and 'labels' in r.message and 'drop_reason' in r.message for r in caplog.records)


def test_shape_three_marker_anchor_beats_numbered_descriptions():
    text = SKULL.replace('[[OPTIONS]]\n', '[[OPTIONS]]\n\n').replace('- **', '')
    assert strip_consumed_options(text)[1] == LABELS


@pytest.mark.asyncio
async def test_marker_without_buildable_labels_has_visible_fallback(caplog):
    sent = []
    bridge = _make_chat_bot(sent)
    with caplog.at_level(logging.INFO, logger='bridge.bot'):
        await bridge._send_smart(42, 'Pick a direction.\n\n[[OPTIONS: | ]]', force_options=True)
    assert len(sent) == 2
    assert sent[-1]['text'] == messages.OPTIONS_UNAVAILABLE
    assert any('no_labels' in r.message for r in caplog.records)
    notices = list(bot_mod.config.bot_data_dir.glob('session-inbox/keyboard-failure-*.md'))
    assert len(notices) == 1
    assert 'reason=no_labels' in notices[0].read_text()
    assert '[[OPTIONS: first label | second label]]' in notices[0].read_text()


@pytest.mark.asyncio
async def test_gate_miss_recovers_buttons(caplog):
    bridge = _make_chat_bot([])
    with caplog.at_level(logging.INFO, logger='bridge.bot'):
        await bridge._send_smart(42, SKULL, force_options=False)
    bridge.application.bot.edit_message_reply_markup.assert_awaited_once()
    assert any('keyboard verdict' in r.message for r in caplog.records)


async def typed(bridge, text, reply_to=None):
    if text.startswith('/'):
        await bridge._clear_typed_over_menu(42, text, reply_to=reply_to)
        return
    message = SimpleNamespace(text=text, chat_id=42, chat=SimpleNamespace(id=42),
                              reply_to_message=SimpleNamespace(message_id=reply_to))
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=42))
    with (
        patch.object(bridge, '_check_access', AsyncMock(return_value=True)),
        patch.object(bridge, '_maybe_consume_secret', AsyncMock(return_value=False)),
        patch.object(bridge, '_maybe_consume_login_code', AsyncMock(return_value=False)),
        patch.object(bridge, '_maybe_capture_outside_approval', AsyncMock(return_value=False)),
        patch.object(bridge, '_maybe_buffer_split_part', AsyncMock(return_value=False)),
        patch.object(bridge, '_dispatch_text_task', AsyncMock()) as dispatch,
        patch.object(bot_mod.session_manager, 'get_session', AsyncMock(return_value={})),
    ):
        await bridge._handle_text_message(update, MagicMock())
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[2] == text


def tap_update(mid, data='opt:PM 검토 · 작업 이름'):
    message = _tapped('Choose a name.', 'Choose a name.', 'PM 검토 · 작업 이름')
    message.chat.id = 42
    message.message_id = mid
    query = MagicMock(data=data, message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42),
                           effective_chat=message.chat)


async def tap(bridge, mid, data='opt:PM 검토 · 작업 이름'):
    update = tap_update(mid, data)
    with (
        patch.object(bridge, '_check_access', AsyncMock(return_value=True)),
        patch.object(bridge, '_maybe_capture_outside_approval', AsyncMock(return_value=False)),
        patch.object(bridge, '_enqueue_user_task', AsyncMock(return_value=True)) as enqueue,
    ):
        await bridge._handle_callback(update, MagicMock())
    return update.callback_query, enqueue


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', ['3', '3\n주체와 목적어가 명확히\n나와야지 codex는 필요없는게 왼쪽 지피티 모델로 나오고', 'PM 검토 · 작업 이름\n이걸로 하죠.'])
async def test_metal_typed_answer_closes_menu_and_rejects_late_tap(answer):
    sent = []
    bridge = _make_chat_bot(sent)
    await bridge._send_smart(42, METAL, force_options=True)
    mid = sent[-1]['message_id']
    bridge.application.bot.edit_message_reply_markup.reset_mock()
    bridge.application.bot.edit_message_reply_markup.return_value = SimpleNamespace(
        text='Choose a name.', text_html='Choose a name.', caption=None)
    await typed(bridge, answer)
    bridge.application.bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=42, message_id=mid, reply_markup=None)
    # Source DGN-1928 numbers every multi-option keyboard, including confirmations.
    assert messages.SELECTED.format(choice='3. PM 검토 · 작업 이름') in (
        bridge.application.bot.edit_message_text.await_args.kwargs['text'])
    query, enqueue = await tap(_make_chat_bot([]), mid)
    enqueue.assert_not_awaited()
    query.answer.assert_awaited_once_with(messages.OPTIONS_ALREADY_ANSWERED, show_alert=True)


@pytest.mark.asyncio
async def test_failed_keyboard_edit_still_rejects_late_tap():
    sent = []
    bridge = _make_chat_bot(sent)
    await bridge._send_smart(42, METAL, force_options=True)
    mid = sent[-1]['message_id']
    bridge.application.bot.edit_message_reply_markup.side_effect = RuntimeError('offline')
    await typed(bridge, '3\nUse this.')
    assert sent[-1]['text'] == messages.SELECTED.format(choice='3. PM 검토 · 작업 이름')
    _, enqueue = await tap(_make_chat_bot([]), mid)
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_double_tap_and_different_late_choice_dispatch_only_once():
    bridge = _make_chat_bot([])
    _, first = await tap(bridge, 700)
    first.assert_awaited_once()
    _, second = await tap(bridge, 700, 'opt:작업 이름 + 검토')
    second.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_text_and_commands_leave_menu_live():
    bridge = _make_chat_bot([])
    await bridge._send_smart(42, METAL, force_options=True)
    bridge.application.bot.edit_message_reply_markup.reset_mock()
    for text in ['How is the other task?', '/stop', '9\nout of range']:
        await typed(bridge, text)
    bridge.application.bot.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_reply_closes_only_that_question_as_typed_input():
    sent = []
    bridge = _make_chat_bot(sent)
    await bridge._send_smart(42, METAL, force_options=True)
    old = sent[-1]['message_id']
    await bridge._send_smart(42, 'Other question\n[[OPTIONS: yes | no]]', force_options=True)
    new = sent[-1]['message_id']
    bridge.application.bot.edit_message_reply_markup.reset_mock()
    await typed(bridge, 'My custom name', reply_to=old)
    bridge.application.bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=42, message_id=old, reply_markup=None)
    _, enqueued = await tap(bridge, new, 'opt:yes')
    enqueued.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_only_prompt_is_registered_and_closes_on_multiline_answer():
    sent = []
    bridge = _make_chat_bot(sent)
    await bridge._send_smart(42, '1. approve\n2. hold\n[[OPTIONS]]', force_options=True)
    mid = sent[-1]['message_id']
    assert sent[-1]['text'] == messages.SELECT_PROMPT
    bridge.application.bot.edit_message_reply_markup.return_value = SimpleNamespace(
        text=messages.SELECT_PROMPT, caption=None)
    await typed(bridge, '1\nGo ahead.')
    bridge.application.bot.edit_message_text.assert_awaited_once_with(
        chat_id=42, message_id=mid, text=messages.SELECTED.format(choice='1. approve'))
    _, enqueue = await tap(_make_chat_bot([]), mid, 'opt:approve')
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_first_tap_is_persisted_for_restart():
    _, first = await tap(_make_chat_bot([]), 123)
    first.assert_awaited_once()
    _, replay = await tap(_make_chat_bot([]), 123)
    replay.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_message_has_no_keyboard_verdict(caplog):
    with caplog.at_level(logging.INFO, logger='bridge.bot'):
        await _make_chat_bot([])._send_smart(42, 'Plain reply')
    assert any('"marker_found": false' in r.message and 'no_marker' in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_simultaneous_taps_claim_before_network_await():
    bridge = _make_chat_bot([])
    updates = [tap_update(700, data) for data in ['opt:PM 검토 · 작업 이름', 'opt:작업 이름 + 검토']]

    async def yield_answer(*args, **kwargs):
        await asyncio.sleep(0)

    for update in updates:
        update.callback_query.answer.side_effect = yield_answer
    with (
        patch.object(bridge, '_check_access', AsyncMock(return_value=True)),
        patch.object(bridge, '_maybe_capture_outside_approval', AsyncMock(return_value=False)),
        patch.object(bridge, '_enqueue_user_task', AsyncMock(return_value=True)) as enqueue,
    ):
        await asyncio.gather(*(bridge._handle_callback(u, MagicMock()) for u in updates))
    enqueue.assert_awaited_once()
    updates[1].callback_query.answer.assert_awaited_once_with(
        messages.OPTIONS_ALREADY_ANSWERED, show_alert=True)


@pytest.mark.asyncio
async def test_split_body_logs_verdict_for_every_sent_message(caplog):
    sent = []
    with caplog.at_level(logging.INFO, logger='bridge.bot'):
        await _make_chat_bot(sent)._send_smart(42, ('Paragraph words. ' * 1000) + '\n' + METAL,
                                               force_options=True)
    assert len(sent) > 1
    rows = [json.loads(r.message.split('keyboard verdict: ', 1)[1])
            for r in caplog.records if 'keyboard verdict: ' in r.message]
    assert {r['message_id'] for r in rows} == {m['message_id'] for m in sent}
    assert all(r['marker_found'] and len(r['labels']) == 4 for r in rows)
    assert rows[-1]['keyboard_message_id'] == sent[-1]['message_id']
    assert all(r['drop_reason'] == 'keyboard_on_other_message' for r in rows[:-1])


@pytest.mark.asyncio
async def test_rejected_markup_restores_numbered_text_fallback():
    sent = []
    bridge = _make_chat_bot(sent)
    original_send = bridge.application.bot.send_message.side_effect

    async def reject_markup(chat_id, text, **kwargs):
        if kwargs.get('reply_markup') is not None:
            raise RuntimeError('markup rejected')
        return await original_send(chat_id, text, **kwargs)

    bridge.application.bot.send_message.side_effect = reject_markup
    bridge.application.bot.edit_message_reply_markup.side_effect = RuntimeError('attach refused')
    await bridge._send_smart(42, METAL, force_options=True)
    assert '1. 작업 이름 + 검토' in sent[-1]['text']
    assert '3. PM 검토 · 작업 이름' in sent[-1]['text']
    assert messages.OPTIONS_UNAVAILABLE in sent[-1]['text']
    mid = sent[-1]['message_id']
    await typed(bridge, '3\nUse this.')
    _, enqueue = await tap(_make_chat_bot([]), mid)
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_answered_newest_menu_is_a_boundary_for_older_menus():
    sent = []
    bridge = _make_chat_bot(sent)
    await bridge._send_smart(42, METAL, force_options=True)
    old = sent[-1]['message_id']
    await bridge._send_smart(42, 'Other question\n[[OPTIONS: yes | no]]', force_options=True)
    new = sent[-1]['message_id']
    await tap(bridge, new, 'opt:yes')
    bridge.application.bot.edit_message_reply_markup.reset_mock()
    await typed(bridge, '1\nAnother thought.')
    bridge.application.bot.edit_message_reply_markup.assert_not_awaited()
    _, enqueue = await tap(bridge, old)
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_answer_tombstone_survives_bubble_history_pruning():
    bridge = _make_chat_bot([])
    _, first = await tap(bridge, 123)
    first.assert_awaited_once()
    for mid in range(200, 200 + sdk_bridge.OPTIONS_BUBBLES_MAX + 1):
        sdk_bridge.publish_options_bubble(42, mid, ['yes', 'no'])
    assert all(row['message_id'] != 123 for row in sdk_bridge.chat_options_bubbles(42))
    query, replay = await tap(_make_chat_bot([]), 123)
    replay.assert_not_awaited()
    query.answer.assert_awaited_once_with(messages.OPTIONS_ALREADY_ANSWERED, show_alert=True)
