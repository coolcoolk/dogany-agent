"""DGN-1813: the [[OPTIONS]] keyboard rides the body bubble.

test-lane: fast
test-tier: 1
component: bridge opt callback / update report handoff
promise: the report button injects exactly one agent turn carrying private failure context and the scrub/show/confirm skill flow, without filing or restarting

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
import importlib.util
import inspect
import json
import os
from pathlib import Path
import textwrap
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram.error

for _key in list(os.environ):
    # ESTATE:BEGIN isolate inherited estate settings as well as Git configuration
    if _key.startswith(('GIT_CONFIG', 'DOGANY_', 'DGN_')):
    # ESTATE:ELSE
    # if _key.startswith('GIT_CONFIG'):
    # ESTATE:END
        del os.environ[_key]

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages
from bridge.options import build_option_keyboard


def _texts(kb):
    return [row[0].text for row in kb.inline_keyboard]


# ---------------------------------------------------------------------------
# 1. Button prefix rule
# ---------------------------------------------------------------------------


class TestPrefixRule:
    def test_lone_button_has_no_number(self):
        assert _texts(build_option_keyboard(["later"])) == ["later"]

    def test_multi_default_keeps_numbers(self):
        assert _texts(build_option_keyboard(["a", "b"])) == ["1. a", "2. b"]

    def test_three_labels_numbered(self):
        # DGN-1928: any 2+ keyboard is numbered, whatever shape produced it.
        assert _texts(build_option_keyboard(["a", "b", "c"])) == ["1. a", "2. b", "3. c"]

    def test_numbered_overflow_still_degrades(self):
        long = "a fairly long option label that overflows the width"
        kb = build_option_keyboard([long, "hold"])
        assert all(not t.startswith("1. a fairly") for t in _texts(kb))


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
        assert _texts(kwargs["reply_markup"]) == ["1. proceed", "2. hold"]

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
        assert _texts(sent[0]["reply_markup"]) == ["1. approve", "2. reject"]
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
        assert _texts(edit.await_args.kwargs["reply_markup"]) == ["1. proceed", "2. hold"]

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
@pytest.mark.parametrize('data,target,label', [
    ('opt:repair-update:kit_dev', 'kit_dev', '업데이트 복구하기'),
    ('opt:repair-update:kit_dev', 'kit_dev', 'Repair update'),
    ('opt:drift-check:agent_a', 'agent_a', '차이 확인 맡기기'),
    ('opt:drift-check:agent_a', 'agent_a', 'Check differences'),
])
async def test_update_action_tap_is_owner_turn_naming_target(data, target, label):
    """The real opt: handler queues an owner turn, with no action process."""
    message = _tapped('Update failed.', 'Update failed.')
    message.reply_markup.inline_keyboard = [[SimpleNamespace(text=label, callback_data=data)]]
    query = MagicMock(data=data, message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=OWNER_ID),
                             effective_chat=message.chat)
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    bridge.application.bot.send_chat_action = AsyncMock()
    bridge.application.bot.send_message = AsyncMock()

    async def execute(_user, task, _overflow, **_kwargs):
        await task()
        return True

    with (
        patch.object(bridge, '_check_access', new=AsyncMock(return_value=True)),
        patch.object(bridge, '_maybe_capture_outside_approval', new=AsyncMock()),
        patch.object(bridge, '_enqueue_user_task', new=AsyncMock(side_effect=execute)),
        patch.object(bridge, '_with_outbound_context', side_effect=lambda text, *_args, **_kwargs: text),
        patch.object(bridge, '_effective_session_id', return_value='session'),
        patch.object(bridge, '_get_real_model', return_value='model'),
        patch.object(bridge, '_finish_turn_with_auto_resume', new=AsyncMock(return_value=None)),
        patch.object(bridge, '_drain_pending_texts', new=AsyncMock()),
        patch.object(bot_mod.session_manager, 'get_session', new=AsyncMock()),
        patch.object(bot_mod.sdk_bridge, 'process_message', new=AsyncMock()) as process,
        patch.object(bot_mod.subprocess, 'run') as run,
        patch.object(bot_mod.subprocess, 'Popen') as popen,
    ):
        await bridge._handle_callback(update, MagicMock())
    process.assert_awaited_once()
    assert process.await_args.kwargs['user_message'] == f'{label} ({target})'
    assert process.await_args.kwargs['user_id'] == OWNER_ID
    assert process.await_args.kwargs['inbound']['source'] == 'callback'
    run.assert_not_called()
    popen.assert_not_called()


async def _report_tap(tmp_path, monkeypatch, *, mutate=False, fail_first=False):
    monkeypatch.setattr(bot_mod, 'PROJECT_ROOT', tmp_path)
    spec = importlib.util.spec_from_file_location('dec594_surface',
        Path(__file__).resolve().parents[4] / 'scripts/pack/update_surface.py')
    surface = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(surface)
    context = dict(reason_code='update.refit-preflight', writes='none',
                   current='2.7.0-dev.12', target='2.7.0-dev.13', stage='compile',
                   members=[dict(name='alpha', root=str(tmp_path), label='Agent A')],
                   log_path=str(tmp_path / 'retained-member.log'))
    record = dict(units=[dict(result='blocked', **context,
                             reason_payload=dict(stage=context['stage'], log_path=context['log_path']))])
    button = surface.result_buttons(record, dict(instance_root=str(tmp_path)), 'ko')[0][0]
    data = button['callback_data']
    message = _tapped('Update not applied.', 'Update not applied.')
    message.reply_markup.inline_keyboard = [[SimpleNamespace(**button)]]
    query = MagicMock(data=data, message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=OWNER_ID),
                             effective_chat=message.chat)
    bridge = bot_mod.TelegramBot()
    bridge.application = MagicMock()
    bridge.application.bot.send_chat_action = AsyncMock()
    bridge.application.bot.send_message = AsyncMock()

    async def execute(_user, task, _overflow, **_kwargs):
        await task()
        return True

    if mutate:
        source = textwrap.dedent(inspect.getsource(bot_mod.TelegramBot._handle_callback))
        mutant = source.replace('turn_choice = report_turn[0] if report_turn else choice', 'turn_choice = choice')
        assert mutant != source
        namespace = dict(bot_mod.__dict__)
        exec(compile(mutant, '<dec594-callback-mutant>', 'exec'), namespace)
        monkeypatch.setattr(bridge, '_handle_callback', namespace['_handle_callback'].__get__(bridge))

    with (
        patch.object(bridge, '_check_access', new=AsyncMock(return_value=True)),
        patch.object(bridge, '_maybe_capture_outside_approval', new=AsyncMock()),
        patch.object(bridge, '_enqueue_user_task', new=AsyncMock(side_effect=execute)),
        patch.object(bridge, '_effective_session_id', return_value='session'),
        patch.object(bridge, '_get_real_model', return_value='model'),
        patch.object(bridge, '_finish_turn_with_auto_resume', new=AsyncMock(return_value=None)),
        patch.object(bridge, '_drain_pending_texts', new=AsyncMock()),
        patch.object(bot_mod.session_manager, 'get_session', new=AsyncMock()),
        patch.object(bot_mod.sdk_bridge, 'process_message', new=AsyncMock()) as process,
        patch.object(bot_mod.subprocess, 'run') as run,
        patch.object(bot_mod.subprocess, 'Popen') as popen,
        patch.object(bot_mod.httpx, 'AsyncClient', side_effect=AssertionError('no HTTP from callback')),
    ):
        if fail_first:
            process.side_effect = [RuntimeError('turn failed to start'), MagicMock()]
            await bridge._handle_callback(update, MagicMock())
            assert not list((tmp_path / '.telegram_bot' / 'update-reports').glob('*.claimed'))
            await bridge._handle_callback(update, MagicMock())  # retry opens the turn
            assert len(list((tmp_path / '.telegram_bot' / 'update-reports').glob('*.claimed'))) == 1
            await bridge._handle_callback(update, MagicMock())  # live turn: no second one
            assert process.await_count == 2
            return
        await bridge._handle_callback(update, MagicMock())
        await bridge._handle_callback(update, MagicMock())  # redelivery stays one turn
    process.assert_awaited_once()
    prompt = process.await_args.kwargs['user_message']
    assert 'Local failure context (private evidence; scrub before showing or publishing):\n' in prompt
    assert json.loads(prompt.split('Local failure context (private evidence; scrub before showing or publishing):\n')[1]) == dict(failures=[context])
    for instruction in ('Scrub', 'Show the complete scrubbed title and body',
                        'ask for explicit\nconfirmation before filing', 'existing version', 'not retry'):
        assert instruction in prompt
    # ESTATE:BEGIN report skill and repository routing belong to the canonical adapter
    for instruction in ('dogany-upstream-report', 'coolcoolk/dogany-agent', 'internal worklog ticket'):
        assert instruction in prompt
    # ESTATE:ELSE
    # assert 'Draft an upstream bug report' in prompt
    # assert "Target the project's public issue tracker." in prompt
    # ESTATE:END
    assert process.await_args.kwargs['inbound']['source'] == 'callback'
    run.assert_not_called()
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_update_report_tap_drafts_once_with_context_and_no_submission(tmp_path, monkeypatch):
    await _report_tap(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_update_report_claim_released_when_turn_fails_to_start(tmp_path, monkeypatch):
    await _report_tap(tmp_path, monkeypatch, fail_first=True)


@pytest.mark.asyncio
async def test_update_report_turn_context_mutation_is_killed(tmp_path, monkeypatch):
    with pytest.raises(AssertionError):
        await _report_tap(tmp_path, monkeypatch, mutate=True)


def test_repair_choice_fallback_and_other_options_keep_existing_resolution():
    from bridge.options import resolve_choice
    assert resolve_choice('opt:repair-update:kit_dev', None) == 'Repair update (kit_dev)'
    button = SimpleNamespace(text='Regular option', callback_data='opt:1')
    assert resolve_choice('opt:1', [[button]]) == 'Regular option'
    assert resolve_choice('opt:repair-update:bad/kit', None) == 'repair-update:bad/kit'


@pytest.mark.parametrize('label', ['차이 확인 맡기기', 'Check differences'])
def test_drift_choice_keeps_label_and_validated_agent_slug(label):
    from bridge.options import resolve_choice
    data = 'opt:drift-check:agent_a'
    button = SimpleNamespace(text=label, callback_data=data)
    assert resolve_choice(data, [[button]]) == f'{label} (agent_a)'
    with patch('bridge.options.t', return_value=label):
        assert resolve_choice(data, None) == f'{label} (agent_a)'
    for slug in ('bad/agent', '../agent_a', 'agent_a:other', '', 'x' * 33, 'Agent_a'):
        # Invalid identifiers are rejected as structured drift actions, like
        # lease repair: only the ordinary raw opt: fallback remains.
        assert resolve_choice('opt:drift-check:' + slug, None) == 'drift-check:' + slug


def test_drift_choice_korean_fallback():
    from bridge.options import resolve_choice
    assert resolve_choice('opt:drift-check:metal', None) == '차이 확인 맡기기 (metal)'  # estate-scrub:allow(owner-requested callback regression fixture)


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
