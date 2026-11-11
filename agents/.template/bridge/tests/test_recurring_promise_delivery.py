# test-component: recurring-promise-delivery
# test-tier: 1
# test-prereq: tmp-files, bridge-sdk
# test-new-component: the framework receipt-backed delivery check is independent of SDK Stop buffering; the existing retraction suites run without framework routines.
"""Framework recurrence validation survives Stop rewrites and keeps menus."""
# ESTATE:BEGIN FDGN-579 framework routines are absent in the OSS export
from pathlib import Path
import pytest

from bridge import sdk_bridge as sdk
from bridge.options import extract_marker_labels


TEMPLATE = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def gate_enabled(monkeypatch):
    monkeypatch.setenv('GATE_ALL', 'on')
    monkeypatch.setenv('GATE_STOP_ALL', 'on')
    monkeypatch.setenv('GATE_STOP_OUTPUT_IDLE_PROMISE', 'on')
    monkeypatch.setenv('GATE_SWITCH_CONF', str(TEMPLATE / 'absent-test.conf'))


def test_unresolved_promise_is_delivered_unchanged(monkeypatch):
    monkeypatch.setattr(sdk, 'PROJECT_ROOT', TEMPLATE)
    text = 'Concrete result ready.\nFrom now on I will always clear notifications.\nDetails checked.'
    assert sdk.SdkBridge._recurring_delivery_guard(text) == text


def test_rewritten_promise_is_delivered_unchanged(monkeypatch):
    monkeypatch.setattr(sdk, 'PROJECT_ROOT', TEMPLATE)
    assert sdk.SdkBridge._recurring_delivery_guard("Going forward we won't skip notifications.") == "Going forward we won't skip notifications."


def test_save_question_retains_functioning_choice_labels(monkeypatch):
    monkeypatch.setattr(sdk, 'PROJECT_ROOT', TEMPLATE)
    text = 'Should I save notify-and-clear as a skill?\n[[OPTIONS: Save as skill | This turn only]]'
    assert sdk.SdkBridge._recurring_delivery_guard(text) == text
    assert extract_marker_labels(text) == ['Save as skill', 'This turn only']


def test_standalone_bridge_without_framework_keeps_existing_behavior(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk, 'PROJECT_ROOT', tmp_path)
    text = 'The result is ready.'
    assert sdk.SdkBridge._recurring_delivery_guard(text) == text


def test_family_switch_also_disarms_delivery_check(monkeypatch):
    monkeypatch.setattr(sdk, 'PROJECT_ROOT', TEMPLATE)
    monkeypatch.setenv('GATE_STOP_OUTPUT_IDLE_PROMISE', 'off')
    text = 'From now on I will always clear notifications.'
    assert sdk.SdkBridge._recurring_delivery_guard(text) == text


def test_ordinary_reply_does_not_load_receipts_or_transcript(monkeypatch):
    import importlib.util
    monkeypatch.setattr(sdk, 'PROJECT_ROOT', TEMPLATE)
    def unexpected(*args, **kwargs):
        raise AssertionError('ordinary text must not load receipt machinery')
    monkeypatch.setattr(importlib.util, 'spec_from_file_location', unexpected)
    assert sdk.SdkBridge._recurring_delivery_guard('Status ready.') == 'Status ready.'


def test_unplanned_shell_edit_does_not_withhold_an_answer(monkeypatch, tmp_path):
    import importlib.util
    import json
    import shutil
    for rel in ('lib/output_gate.py', 'skill-layer.py', 'gate-switch.py'):
        dst = tmp_path / 'routines' / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATE / 'routines' / rel, dst)
    target = tmp_path / '.claude/skills/sample/SKILL.md'
    target.parent.mkdir(parents=True)
    target.write_text('## Steps\n- Old.\n')
    spec = importlib.util.spec_from_file_location('test_delivery_layer', tmp_path / 'routines/skill-layer.py')
    layer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(layer)
    layer.capture_update_turn(str(tmp_path), 's1', 't1')
    target.write_text('## Steps\n- Changed through shell.\n')
    config = tmp_path / 'config'
    encoded = sdk.re.sub(r'[^A-Za-z0-9]', '-', str(tmp_path))
    transcript = config / 'projects' / encoded / 's1.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({'type': 'user', 'uuid': 't1',
                         'message': {'content': 'Update the procedure.'}}) + '\n')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
    monkeypatch.setattr(sdk, 'PROJECT_ROOT', tmp_path)
    assert sdk.SdkBridge._recurring_delivery_guard('Procedure updated.', 's1') == 'Procedure updated.'
# ESTATE:END
