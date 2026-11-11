"""DGN-2033: parser/render suppression, callback identity, and gate parity."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from bridge.options import (
    EXIT_HATCH_LABELS, build_option_keyboard,
    filter_exit_hatch_options, keyboard_labels, resolve_choice,
    strip_consumed_options,
)

ROOT = Path(__file__).resolve().parents[2]
# The bridge ships standalone (no routines/), so the shared replay cases live
# with the bridge; routines/tests/test-dgn2033-exit-hatch.py reads them here.
CASES = json.loads((Path(__file__).resolve().parent / 'fixtures/dgn2033-exit-hatch.json').read_text())['cases']


@pytest.mark.parametrize('case', [c for c in CASES if 'options' in c], ids=lambda c: c['name'])
def test_parser_and_render(case):
    _, labels = strip_consumed_options(case['text'])
    assert labels == case['options']
    assert keyboard_labels(case['text']) == labels
    kb = build_option_keyboard(labels)
    if not labels:
        assert kb is None
    else:
        assert len(kb.inline_keyboard) == len(labels)
        assert len({row[0].callback_data for row in kb.inline_keyboard}) == len(labels)


def test_render_backstop_logs_and_preserves_callback_identity(caplog):
    exit_label = next(iter(EXIT_HATCH_LABELS))
    kb = build_option_keyboard(['A', exit_label, 'B'])
    assert [row[0].text for row in kb.inline_keyboard] == ['1. A', '2. B']
    assert resolve_choice(kb.inline_keyboard[1][0].callback_data, kb.inline_keyboard) == '2. B'
    assert 'EXIT_HATCH_OPTION action=drop' in caplog.text
    assert 'remaining=2 keyboard=kept' in caplog.text
    caplog.clear()
    assert build_option_keyboard(['A', exit_label]) is None
    assert 'remaining=1 keyboard=suppressed' in caplog.text
    assert filter_exit_hatch_options(['A']) == ['A']


# ESTATE:BEGIN DGN-2033 deny-list parity with routines/lib/output_gate.py, which the public bridge does not ship
def test_deny_list_parity():
    gate = ROOT / 'routines/lib/output_gate.py'
    if not gate.is_file():
        pytest.skip('routines/lib/output_gate.py not present (bridge staged alone)')
    spec = importlib.util.spec_from_file_location('dgn2033_output_gate', gate)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        assert mod.EXIT_HATCH_LABELS == EXIT_HATCH_LABELS
    finally:
        sys.modules.pop(spec.name, None)
# ESTATE:END


@pytest.mark.parametrize('classifier', [False, True])
@pytest.mark.parametrize('case', [CASES[0], CASES[-1]], ids=lambda c: c['name'])
def test_real_send_never_rearms_suppressed_menu(case, classifier, monkeypatch):
    import bridge.bot as bot_mod
    monkeypatch.setattr(bot_mod, 'DECISION_CARRY', False)
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    sent = []

    async def send(chat_id, text, **kwargs):
        sent.append(dict(text=text, **kwargs))
        return MagicMock(message_id=100 + len(sent))

    bot.application.bot.send_message = AsyncMock(side_effect=send)
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = {}
    asyncio.run(bot._send_smart(42, 'Answer complete.\n' + case['text'], force_options=True,
                              classifier_injected=classifier))
    assert sent
    assert all(not row.get('reply_markup') for row in sent)
    if case['name'] == 'no-fallback-to-unrelated-body':
        assert any('X' in row['text'] and 'Y' in row['text'] for row in sent)
