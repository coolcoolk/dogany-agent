"""Pack-declared flow taps: real fake command, full callback routing, no model."""

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge import bot as bot_mod, flow_tap, messages

DATA = "flow|sess-1|2|record|8"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "instance root"
    (root / "config").mkdir(parents=True)
    command = root / "service" / "fixture" / "flow"
    command.parent.mkdir(parents=True)
    command.write_text(
        "#!" + sys.executable + "\n"
        "import json, pathlib, sys\n"
        "root = pathlib.Path.cwd()\n"
        "(root / 'record.json').write_text(json.dumps(\n"
        "    {'cwd': str(root), 'args': sys.argv[1:]}))\n"
        "sys.exit(int((root / 'rc').read_text()) if (root / 'rc').exists() else 0)\n"
    )
    command.chmod(0o755)
    conf = root / "config" / "agent.conf"
    conf.write_text("FLOW_TAP_COMMAND='[\"service/fixture/flow\", \"tap\"]'\n")
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", root)
    bridge = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bridge.application = SimpleNamespace(bot=SimpleNamespace(
        send_chat_action=AsyncMock(), send_message=AsyncMock()))
    bridge._check_access = AsyncMock(return_value=True)
    bridge._enqueue_user_task = AsyncMock()
    bridge._maybe_capture_outside_approval = AsyncMock()
    bridge._clear_open_decision = MagicMock()
    bridge._live_menu_registry = MagicMock()
    bridge._effective_session_id = MagicMock(return_value=None)
    bridge._permission_callback = AsyncMock()
    bridge._proactive_push = AsyncMock()
    bridge._finish_turn_with_auto_resume = AsyncMock(return_value=None)
    bridge._drain_pending_texts = AsyncMock()
    monkeypatch.setattr(bot_mod.session_manager, "get_session", AsyncMock(return_value={}))
    model = AsyncMock()
    monkeypatch.setattr(bot_mod.sdk_bridge, "process_message", model)
    query = SimpleNamespace(
        data=DATA, id="cb-2036", answer=AsyncMock(),
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        message=SimpleNamespace(
            text="Choose a value", text_html="Choose a value", caption=None, message_id=55,
            message_thread_id=None, chat=SimpleNamespace(id=42),
            reply_markup=SimpleNamespace(inline_keyboard=[[
                SimpleNamespace(text="Record", callback_data=DATA)]]),
            reply_text=AsyncMock()),
    )
    update = SimpleNamespace(callback_query=query,
                             effective_user=SimpleNamespace(id=42),
                             effective_chat=SimpleNamespace(id=42))
    return SimpleNamespace(root=root, conf=conf, command=command, bot=bridge,
                           query=query, update=update, model=model)


def run_tap(env):
    asyncio.run(env.bot._handle_callback(env.update, None))


def test_success_real_command_no_model_turn(env):
    run_tap(env)
    assert json.loads((env.root / "record.json").read_text()) == {
        "cwd": str(env.root), "args": ["tap", DATA]}
    env.query.answer.assert_awaited_once()
    env.bot._enqueue_user_task.assert_not_awaited()
    env.model.assert_not_awaited()
    env.query.message.reply_text.assert_not_awaited()
    env.query.edit_message_text.assert_not_awaited()
    env.query.edit_message_reply_markup.assert_not_awaited()


def test_callback_is_one_literal_argument(env):
    data = "flow|s|1|e|$(touch injected); ' \" | ${HOME}"
    env.query.data = data
    run_tap(env)
    assert json.loads((env.root / "record.json").read_text())["args"] == ["tap", data]
    assert not (env.root / "injected").exists()
    env.model.assert_not_awaited()


@pytest.mark.parametrize("failure", ["missing-conf", "missing-key", "invalid-json",
                                      "missing-command", "nonzero"])
def test_failure_one_notice_and_label_to_model(env, monkeypatch, failure):
    if failure == "missing-conf":
        env.conf.unlink()
    elif failure == "missing-key":
        env.conf.write_text("OTHER_KEY=1\n")
    elif failure == "invalid-json":
        env.conf.write_text("FLOW_TAP_COMMAND=oops\n")
    elif failure == "missing-command":
        env.command.unlink()
    elif failure == "nonzero":
        (env.root / "rc").write_text("7")
    async def enqueue(user_id, task, overflow, **kwargs):
        await task()
    env.bot._enqueue_user_task.side_effect = enqueue
    run_tap(env)
    env.query.answer.assert_awaited_once()
    env.query.message.reply_text.assert_awaited_once_with(messages.FLOW_TAP_FAILED)
    assert "\n" not in messages.FLOW_TAP_FAILED
    env.bot._enqueue_user_task.assert_awaited_once()
    env.model.assert_awaited_once()
    sent = env.model.await_args.kwargs
    assert sent["user_message"] == "Record"
    # 2.6.4 uses its existing chat/thread/message metadata schema (no 2.7
    # envelope), but the user message is the visible label, never the callback.
    assert sent["inbound"] == {
        "chat_id": 42, "thread_id": None, "message_id": 55, "source": "callback"}
    env.query.edit_message_text.assert_awaited_once_with(
        "Choose a value\n\n" + messages.SELECTED.format(choice="Record"),
        parse_mode="HTML", reply_markup=None)


def test_timeout_retries_once_then_success_has_no_fallback(env, monkeypatch):
    real_run = subprocess.run
    launch = MagicMock(side_effect=[
        subprocess.TimeoutExpired([str(env.command), "tap", DATA], 30),
        real_run([str(env.command), "tap", DATA], cwd=env.root,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, timeout=30, check=False),
    ])
    monkeypatch.setattr(flow_tap.subprocess, "run", launch)

    run_tap(env)

    assert launch.call_count == 2
    env.query.message.reply_text.assert_not_awaited()
    env.bot._enqueue_user_task.assert_not_awaited()
    env.model.assert_not_awaited()


def test_timeout_twice_falls_back_with_label_not_callback(env, monkeypatch):
    launch = MagicMock(side_effect=lambda *args, **kwargs: (
        (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], kwargs["timeout"]))))
    monkeypatch.setattr(flow_tap.subprocess, "run", launch)

    async def enqueue(user_id, task, overflow, **kwargs):
        await task()
    env.bot._enqueue_user_task.side_effect = enqueue
    run_tap(env)

    assert launch.call_count == 2
    env.query.message.reply_text.assert_awaited_once_with(messages.FLOW_TAP_FAILED)
    assert env.model.await_args.kwargs["user_message"] == "Record"
    assert not env.model.await_args.kwargs["user_message"].startswith("flow|")
    env.query.edit_message_text.assert_awaited_once_with(
        "Choose a value\n\n" + messages.SELECTED.format(choice="Record"),
        parse_mode="HTML", reply_markup=None)


def test_no_matching_button_notices_without_model_turn(env):
    env.conf.unlink()
    env.query.message.reply_markup.inline_keyboard[0][0].callback_data = "flow|other"

    run_tap(env)

    env.query.message.reply_text.assert_awaited_once_with(messages.FLOW_TAP_FAILED)
    env.bot._enqueue_user_task.assert_not_awaited()
    env.model.assert_not_awaited()


def test_nonzero_is_not_retried(env, monkeypatch):
    launch = MagicMock(return_value=SimpleNamespace(returncode=7))
    monkeypatch.setattr(flow_tap.subprocess, "run", launch)

    run_tap(env)

    launch.assert_called_once()
    env.query.message.reply_text.assert_awaited_once_with(messages.FLOW_TAP_FAILED)


@pytest.mark.parametrize("argv", [None, [], {}, "echo hi", [1], [""], ["x", None], ["x\x00"]])
def test_invalid_declaration_never_launches(env, monkeypatch, argv):
    env.conf.write_text("FLOW_TAP_COMMAND='" + json.dumps(argv) + "'\n")
    launch = MagicMock()
    monkeypatch.setattr(flow_tap.subprocess, "run", launch)
    assert not flow_tap.dispatch(env.root, DATA)
    launch.assert_not_called()


def test_access_denied_never_dispatches(env):
    env.bot._check_access.return_value = False
    run_tap(env)
    assert not (env.root / "record.json").exists()
    env.query.answer.assert_not_awaited()
    env.bot._enqueue_user_task.assert_not_awaited()


def test_unrelated_callback_not_dispatched(env):
    env.query.data = "unknown:tap"
    run_tap(env)
    assert not (env.root / "record.json").exists()
    env.bot._enqueue_user_task.assert_not_awaited()
    env.query.message.reply_text.assert_not_awaited()


def test_answer_failure_does_not_block_success(env):
    env.query.answer.side_effect = RuntimeError("expired query")
    run_tap(env)
    assert (env.root / "record.json").is_file()
    env.bot._enqueue_user_task.assert_not_awaited()


def test_notice_failure_still_falls_back(env):
    env.conf.unlink()
    env.query.message.reply_text.side_effect = RuntimeError("offline")
    run_tap(env)
    env.bot._enqueue_user_task.assert_awaited_once()


def test_absolute_command_and_config_without_interpolation(env):
    argv = [str(env.command), "tap", "${HOME}"]
    env.conf.write_text("FLOW_TAP_COMMAND='" + json.dumps(argv) + "'\n")
    run_tap(env)
    assert json.loads((env.root / "record.json").read_text())["args"] == ["tap", "${HOME}", DATA]


def test_elapsed_ms_logged_for_success_and_timeout(env, monkeypatch, caplog):
    caplog.set_level("INFO", logger=flow_tap.logger.name)
    assert flow_tap.dispatch(env.root, DATA) is True
    assert any("rc=0 elapsed_ms=" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setattr(flow_tap.subprocess, "run", MagicMock(
        side_effect=subprocess.TimeoutExpired(["x"], 30)))
    assert flow_tap.dispatch(env.root, DATA) is False
    assert any("failed" in r.getMessage() and "elapsed_ms=" in r.getMessage()
               for r in caplog.records)
