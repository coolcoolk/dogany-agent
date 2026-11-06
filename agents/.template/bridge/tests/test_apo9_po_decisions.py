# test-component: po-owner-input
# test-tier: 0
# test-prereq: tmp-files, subprocess
# test-new-component: The pack response handoff needs frozen-menu and per-request evidence isolation coverage.
"""Frozen menus, native tap identity, and owner-input retry boundaries."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bridge import po_decisions as P
import bridge.sdk_bridge as SDK
from bridge.live_surface import Coordinator, atomic


def decision(root):
    root = Path(root)
    home = root / '.telegram_bot/po-seat'
    home.mkdir(parents=True, exist_ok=True)
    menu = {'labels': ['First', 'Second']}
    body = dict(chat='-123', text='Result and question. dec-1', menu=menu)
    receipt = dict(id='dec-1', labels=menu['labels'])
    with sqlite3.connect(home / 'seat.sqlite3') as db:
        db.execute('CREATE TABLE decisions (key TEXT, home TEXT, body TEXT, receipt TEXT)')
        db.execute('INSERT INTO decisions VALUES(?,?,?,?)',
                   ('a'*64, str(root), json.dumps(body), json.dumps(receipt)))
    return body


def test_frozen_menu_and_native_tap(tmp_path):
    body = decision(tmp_path)
    menu = P.frozen(tmp_path, 'a'*64, -123, body['text'])
    assert P.buttons(menu)[0][0]['callback_data'] == 'po:dec-1:1'
    for chat, text in [(-124, body['text']), (-123, 'changed')]:
        with pytest.raises(ValueError):
            P.frozen(tmp_path, 'a'*64, chat, text)
    c = Coordinator(tmp_path)
    control = c.control(-123)
    control['notices']['notice'] = dict(status='ok',
        intended=dict(notification_class='owner_decision', payload=dict(po_decision=menu)),
        delivery=dict(chat_id=-123, message_id=44))
    c.save_control(-123, control)
    assert P.callback_prompt(tmp_path, -123, 44, 'po:dec-1:1') == 'dec-1: 1'
    assert P.callback_prompt(tmp_path, -123, 45, 'po:dec-1:1') is None
    assert P.callback_prompt(tmp_path, -124, 44, 'po:dec-1:1') is None
    assert P.callback_prompt(tmp_path, -123, 44, 'po:dec-1:3') is None


def test_frozen_terminal_notices_preserve_plain_text(tmp_path):
    decision(tmp_path)
    for receipt in [dict(terminal="event"), dict(availability="unavailable"), dict(job_limit="job")]:
        with sqlite3.connect(tmp_path / '.telegram_bot/po-seat/seat.sqlite3') as db:
            db.execute('UPDATE decisions SET body=?, receipt=?',
                       (json.dumps(dict(chat='-123', text='Terminal result')), json.dumps(receipt)))
        assert P.frozen(tmp_path, 'a'*64, -123, 'Terminal result') is None
        with pytest.raises(ValueError):
            P.frozen(tmp_path, 'a'*64, -123, 'changed')


def test_retry_retains_exact_evidence_and_request(tmp_path):
    prompt = 'dec-1: 1'
    context = dict(request_id='request-1', text_sha=hashlib.sha256(prompt.encode()).hexdigest(),
                   source='callback', chat_id=-123, message_id=44, ts=time.time())
    with patch.object(P, 'installed', return_value=True), patch.object(P, 'retry_input', side_effect=ValueError('writer offline')):
        with pytest.raises(ValueError):
            P.record_input(tmp_path, 'owner', prompt, context)
    directory = next((tmp_path / '.telegram_bot/po-owner-input').iterdir())
    assert json.loads((directory / 'inbound-context.json').read_text()) == context
    assert json.loads((directory / 'input.json').read_text())['prompt'] == prompt
    with patch.object(P, 'installed', return_value=True):
        with pytest.raises(ValueError):
            P.record_input(tmp_path, 'owner', 'dec-1: 2', dict(context, text_sha=hashlib.sha256(b'dec-1: 2').hexdigest()))


@pytest.mark.asyncio
@pytest.mark.parametrize('inflight', [False, True])
async def test_every_inbound_query_runs_pack_handler_before_model(inflight, tmp_path):
    bridge = SDK.SdkBridge()
    client = MagicMock()
    client.query = AsyncMock()
    state = SDK._UserStreamState(client=client, model=None)
    if inflight:
        state.injected_turn_mode = 'dispatch-return'
    head = MagicMock(sent=False, inbound_request_id='request-1', user_id=42,
                     inbound=dict(chat_id=-123, source='message'), user_message='1', sent_session_id='owner')
    state.pending.append(head)
    calls = []
    client.query.side_effect = lambda *a, **kw: calls.append('model')
    with patch.object(SDK, 'PROJECT_ROOT', tmp_path), patch.object(P, 'installed', return_value=True), \
            patch.object(P, 'record_input', side_effect=lambda *a: calls.append('handler')), \
            patch.object(SDK, '_record_inflight_owner', AsyncMock()):
        await bridge._dispatch_next_query(state)
    assert calls == ['handler', 'model']
