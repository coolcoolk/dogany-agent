"""Pack-declared flow taps: real fake command, full callback routing, no model.

promise: a flow| tap of a pack engine replaced at install never launches the
retired command or a model turn; it gets the stale-tap toast (DGN-2085 M4).
"""

import asyncio
import json
import logging
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
        "delay = root / 'delay'\n"
        "if delay.exists():\n"
        "    import time\n"
        "    time.sleep(float(delay.read_text()))\n"
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
            text="Choose a value", caption=None, message_id=55,
            message_thread_id=None, chat=SimpleNamespace(id=42),
            reply_markup=SimpleNamespace(inline_keyboard=[[
                SimpleNamespace(text="Record", callback_data=DATA)]]),
            reply_text=AsyncMock()),
    )
    update = SimpleNamespace(callback_query=query,
                             effective_user=SimpleNamespace(id=42),
                             effective_chat=SimpleNamespace(id=42))
    return SimpleNamespace(root=root, conf=conf, command=command, bot=bridge,
                           query=query, update=update, model=model, monkeypatch=monkeypatch)


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
    marked = env.query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert marked.inline_keyboard[0][0].text == "\u2713 Record"


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
    # DGN-2375: the visible button label, never the raw flow| callback.
    assert sent["user_message"] == "Record"
    assert not sent["user_message"].startswith("flow|")
    assert sent["inbound"]["source"] == "callback"
    assert sent["inbound"]["callback_data"] == DATA
    env.query.edit_message_text.assert_not_awaited()


def test_slow_success_waits_without_fallback_or_agent_handoff(env, caplog):
    caplog.set_level(logging.INFO, logger="bridge.flow_tap")
    env.monkeypatch.setattr(flow_tap, "TIMEOUT_SECONDS", 0.01)
    env.monkeypatch.setattr(flow_tap, "LATE_TIMEOUT_SECONDS", 1)
    (env.root / "delay").write_text("0.05")

    run_tap(env)

    assert (env.root / "record.json").is_file()
    env.query.message.reply_text.assert_not_awaited()
    env.bot._enqueue_user_task.assert_not_awaited()
    env.model.assert_not_awaited()
    assert "flow tap command timed out, still running" in caplog.text
    assert "flow tap command late outcome" in caplog.text
    assert "rc=0" in caplog.text


def test_slow_failure_falls_back_once_after_exit(env, caplog):
    caplog.set_level(logging.INFO, logger="bridge.flow_tap")
    env.monkeypatch.setattr(flow_tap, "TIMEOUT_SECONDS", 0.01)
    env.monkeypatch.setattr(flow_tap, "LATE_TIMEOUT_SECONDS", 1)
    (env.root / "delay").write_text("0.05")
    (env.root / "rc").write_text("7")

    async def enqueue(user_id, task, overflow, **kwargs):
        await task()
    env.bot._enqueue_user_task.side_effect = enqueue
    run_tap(env)

    env.query.message.reply_text.assert_awaited_once_with(messages.FLOW_TAP_FAILED)
    env.bot._enqueue_user_task.assert_awaited_once()
    env.model.assert_awaited_once()
    assert "flow tap command timed out, still running" in caplog.text
    assert "flow tap command late outcome" in caplog.text
    assert "rc=7" in caplog.text


def test_refusal_text_with_nonzero_exit_uses_failure_path(env):
    env.command.write_text(
        "#!" + sys.executable + "\n"
        "print('FLOW_REFUSED stale')\n"
        "raise SystemExit(7)\n"
    )
    env.command.chmod(0o755)

    run_tap(env)

    env.query.message.reply_text.assert_awaited_once_with(messages.FLOW_TAP_FAILED)
    env.bot._enqueue_user_task.assert_awaited_once()


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


def install_v2_fixture(env, phase="active"):
    """Installed fixture authority; the shim is a real process, no kit claims."""
    from bridge import callback_routes as routes
    from bridge import session_state as state

    command = env.root / "service/fixture/v2"
    command.write_text(
        "#!" + sys.executable + "\n"
        "import json, pathlib, sys\n"
        "value = json.load(sys.stdin)\n"
        "pathlib.Path('v2-record.json').write_text(json.dumps(value))\n"
        "print(json.dumps({'toast': 'fixture copy', 'alert': False}))\n"
    )
    command.chmod(0o755)
    # DGN-2150: the route budget is wall-clock and these tests assert admission
    # and dedupe, not the budget; under load interpreter startup alone outran
    # timeout_s=1 and the real shim was killed. Wait for the shim to exit.
    class Popen(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            return super().communicate(input)
    env.monkeypatch.setattr(routes, "subprocess", SimpleNamespace(
        Popen=Popen, **{name: getattr(subprocess, name) for name in (
            "PIPE", "DEVNULL", "TimeoutExpired", "CompletedProcess", "SubprocessError")}))
    config = {"schema": "live_adapter/1", "identities": [
        {"chat_id": 42, "user_id": 42, "actor": "owner", "member_id": "member"}],
        "toast_copy": []}
    state.atomic_json(env.root / "config/session/live-adapter.json", config)
    route = {"schema": "callback_routes/1", "pack": "fixture",
             "routes": [{"prefix": "flow2|", "argv": ["service/fixture/v2", "tap"],
                         "fallback": "none", "timeout_s": 1}],
             "due_argv": ["service/fixture/v2", "due"],
             "probe_argv": ["service/fixture/v2", "probe"],
             "env": {"LIVE_ADAPTER_CONFIG": "config/session/live-adapter.json"},
             "retire_prefixes": ["flow|"]}
    state.atomic_json(env.root / "config/packs/fixture.callback_routes.json", route)
    receipt = {"cap.session_flow_v2": routes.CAP_LINE,
               "cap.session_flow_v2.routes": "flow2|",
               "cap.session_flow_v2.retire_prefixes": "flow|",
               "cap.session_flow_v2.route_digest": state.digest(route),
               "cap.session_flow_v2.due_argv": json.dumps(route["due_argv"]),
               "cap.session_flow_v2.probe_argv": json.dumps(route["probe_argv"])}
    (env.root / "config/packs/fixture.receipt").write_text(
        "\n".join(k + "=" + v for k, v in receipt.items()) + "\n")
    state.atomic_json(env.root / "config/packs/fixture.session_activation.json", {
        "schema": "session_activation/1", "generation": 1, "phase": phase,
        "selected_engine": "v2" if phase == "active" else "legacy",
        "route_digest": state.digest(route), "legacy_fence": {"generation": 1},
        "v2_fence": None, "retirement_evidence": {"complete": True, "worker_fenced": True,
                                                   "inventory": [55], "receipts": ["ack:55"]},
        "transition_checkpoint": {"chat_id": 42}})
    state.atomic_json(state.data_dir(env.root) / "42.json", {
        "schema": "live_surface/1", "chat_id": 42, "owner": "fixture", "state": "open",
        "surface_id": "s1", "epoch": 7, "slots": {"card": {"message_id": 56}},
        "principal": config["identities"][0], "session_id": 1})
    env.query.data = "flow2|opaque"
    env.query.message.message_id = 56


def test_v2_actual_callback_admits_before_one_answer_and_no_model(env):
    install_v2_fixture(env)
    run_tap(env)
    value = json.loads((env.root / "v2-record.json").read_text())
    assert value["kind"] == "tap"
    assert value["payload"] == "opaque"
    assert value["envelope_id"] == "tap:cb-2036"
    env.query.answer.assert_awaited_once_with(text="fixture copy", show_alert=False)
    env.model.assert_not_awaited()
    env.query.message.reply_text.assert_not_awaited()
    env.bot._enqueue_user_task.assert_not_awaited()


def test_v2_actual_access_denial_never_admits_or_executes(env):
    from bridge import callback_routes
    install_v2_fixture(env)
    env.bot._check_access.return_value = False
    run_tap(env)
    assert not (env.root / "v2-record.json").exists()
    assert not callback_routes.ingress_path(env.root, 42, "tap:cb-2036").exists()
    env.query.answer.assert_not_awaited()
    env.model.assert_not_awaited()


def test_v2_actual_duplicate_callback_one_process_and_one_answer(env):
    install_v2_fixture(env)
    run_tap(env)
    before = (env.root / "v2-record.json").stat().st_mtime_ns
    run_tap(env)
    assert (env.root / "v2-record.json").stat().st_mtime_ns == before
    env.query.answer.assert_awaited_once()
    env.model.assert_not_awaited()


def test_v2_staged_revocation_intent_preserves_original_legacy_argv(env):
    install_v2_fixture(env, phase="staged")
    env.query.data = DATA
    run_tap(env)
    assert json.loads((env.root / "record.json").read_text()) == {
        "cwd": str(env.root), "args": ["tap", DATA]}
    assert not (env.root / "v2-record.json").exists()
    env.query.answer.assert_awaited_once()
    env.model.assert_not_awaited()


def test_v2_actual_active_legacy_tap_only_stale_toast(env):
    install_v2_fixture(env)
    env.query.data = DATA
    run_tap(env)
    assert not (env.root / "record.json").exists()
    assert not (env.root / "v2-record.json").exists()
    env.query.answer.assert_awaited_once_with(text=messages.LIVE_TAP_STALE, show_alert=False)
    env.query.message.reply_text.assert_not_awaited()
    env.model.assert_not_awaited()


def test_v2_actual_broken_route_fails_without_model_fallback(env):
    install_v2_fixture(env)
    (env.root / "config/packs/fixture.callback_routes.json").unlink()
    run_tap(env)
    env.query.answer.assert_awaited_once_with(text=messages.LIVE_TAP_FAILED, show_alert=False)
    env.query.message.reply_text.assert_not_awaited()
    env.model.assert_not_awaited()


def test_v2_actual_duplicate_receipt_key_fails_without_model(env):
    from bridge import callback_routes
    install_v2_fixture(env)
    receipt = env.root / "config/packs/fixture.receipt"
    receipt.write_text(receipt.read_text() + "cap.session_flow_v2=" + callback_routes.CAP_LINE + "\n")
    run_tap(env)
    assert not (env.root / "v2-record.json").exists()
    env.query.answer.assert_awaited_once_with(text=messages.LIVE_TAP_FAILED, show_alert=False)
    env.model.assert_not_awaited()


# ESTATE:BEGIN replaced-pack callback refusal case -- bridge.pack_revocation is manifest-excluded (DGN-2085 M4)
def test_replaced_pack_flow_prefix_refused_before_dispatch(env):
    from bridge import pack_revocation
    pack_revocation.write(env.root, dict(
        schema=pack_revocation.SCHEMA, pack="fixture", closed_at_ms=1, callback_prefixes=["flow|"],
        arm_command_paths=["service/fixture/flow"], arm_command_keys=["cmd"], retired_arm_ids=[],
        notice_producers=[]))
    run_tap(env)
    assert not (env.root / "record.json").exists()
    env.query.answer.assert_awaited_once_with(text=messages.LIVE_TAP_STALE, show_alert=False)
    env.query.message.reply_text.assert_not_awaited()
    env.query.edit_message_text.assert_not_awaited()
    env.bot._enqueue_user_task.assert_not_awaited()
    env.model.assert_not_awaited()
# ESTATE:END


def test_no_matching_button_notices_without_model_turn(env):
    env.conf.unlink()
    env.query.message.reply_markup.inline_keyboard[0][0].callback_data = "flow|other"

    run_tap(env)

    env.query.message.reply_text.assert_awaited_once_with(messages.FLOW_TAP_FAILED)
    env.bot._enqueue_user_task.assert_not_awaited()
    env.model.assert_not_awaited()


def test_commit_ack_and_lock_precede_dispatch_then_refusal_restores(env, monkeypatch):
    data = DATA + "|c"
    env.query.data = data
    env.query.message.reply_markup.inline_keyboard[0][0].callback_data = data
    order = []
    original = env.query.message.reply_markup

    async def answer(*args, **kwargs):
        order.append("answer")

    async def edit(*args, **kwargs):
        markup = kwargs["reply_markup"]
        order.append("restore" if markup is original else "lock")

    def dispatch(root, callback_data):
        order.append("dispatch")
        return flow_tap.DispatchResult(False, refused=True)

    env.query.answer.side_effect = answer
    env.query.edit_message_reply_markup.side_effect = edit
    monkeypatch.setattr(flow_tap, "dispatch", dispatch)
    run_tap(env)

    assert order[:3] == ["answer", "lock", "dispatch"]
    assert order[3] == "restore"
    waiting = env.query.edit_message_reply_markup.await_args_list[0].kwargs["reply_markup"]
    button = waiting.inline_keyboard[0][0]
    assert (button.text, button.callback_data) == ("⏳ 기록 중…", "flowwait:")
    env.query.message.reply_text.assert_not_awaited()
    env.bot._enqueue_user_task.assert_not_awaited()
    env.model.assert_not_awaited()


def test_flowwait_is_answered_and_inert(env, monkeypatch):
    env.query.data = "flowwait:"
    dispatch = MagicMock()
    monkeypatch.setattr(flow_tap, "dispatch", dispatch)

    run_tap(env)

    env.query.answer.assert_awaited_once()
    dispatch.assert_not_called()
    env.bot._enqueue_user_task.assert_not_awaited()
    env.model.assert_not_awaited()


def test_commit_lock_uses_latest_pick_per_row_in_row_order(env):
    rows = [
        [SimpleNamespace(text="60kg", callback_data="flow|sess-1|0|pick|60"),
         SimpleNamespace(text="65kg", callback_data="flow|sess-1|0|pick|65")],
        [SimpleNamespace(text="10\ud68c", callback_data="flow|sess-1|1|pick|10")],
        [SimpleNamespace(text="RIR 2", callback_data="flow|sess-1|2|pick|2")],
        [SimpleNamespace(text="Save", callback_data="flow|sess-1|3|save|now|c")],
    ]
    env.query.message.reply_markup.inline_keyboard = rows
    for callback in (rows[0][0].callback_data, rows[0][1].callback_data,
                     rows[1][0].callback_data, rows[2][0].callback_data):
        env.query.data = callback
        run_tap(env)
    env.query.data = rows[3][0].callback_data

    run_tap(env)

    waiting = env.query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert waiting.inline_keyboard[0][0].text == "\u23f3 65kg \u00b7 10\ud68c \u00b7 RIR 2 \uae30\ub85d \uc911\u2026"


def test_commit_lock_fallback_without_picks(env):
    data = DATA + "|c"
    env.query.data = data
    env.query.message.reply_markup.inline_keyboard[0][0].callback_data = data

    run_tap(env)

    waiting = env.query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert waiting.inline_keyboard[0][0].text == "\u23f3 \uae30\ub85d \uc911\u2026"


def test_commit_lock_fallback_when_composed_label_is_too_wide():
    assert flow_tap.commit_lock_label(["x" * 40], "en") == "\u23f3 Saving\u2026"


def test_pack_keyboard_replacement_resets_picks(env):
    first = SimpleNamespace(text="65kg", callback_data="flow|sess-1|0|pick|65")
    env.query.message.reply_markup.inline_keyboard = [[first]]
    env.query.data = first.callback_data
    run_tap(env)

    commit = SimpleNamespace(text="Save", callback_data="flow|sess-1|1|save|now|c")
    env.query.message.reply_markup.inline_keyboard = [[commit]]
    env.query.data = commit.callback_data
    run_tap(env)

    waiting = env.query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert waiting.inline_keyboard[0][0].text == "\u23f3 \uae30\ub85d \uc911\u2026"


def test_pressed_marker_moves_within_row_and_keeps_pack_preselect(env):
    row = [
        SimpleNamespace(text="6\ud68c", callback_data="flow|sess-1|0|pick|6"),
        SimpleNamespace(text="\u25cf8\ud68c", callback_data="flow|sess-1|0|pick|8"),
        SimpleNamespace(text="\u2713 9\ud68c", callback_data="flow|sess-1|0|pick|9"),
        SimpleNamespace(text="10\ud68c", callback_data="flow|sess-1|0|pick|10"),
    ]
    env.query.message.reply_markup.inline_keyboard = [row]
    env.query.data = row[-1].callback_data

    run_tap(env)

    marked = env.query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert [button.text for button in marked.inline_keyboard[0]] == [
        "6\ud68c", "\u25cf8\ud68c", "9\ud68c", "\u2713 10\ud68c"]


def test_pressed_marker_setting_off_leaves_keyboard_unchanged(env):
    env.conf.write_text(env.conf.read_text() + "FLOW_TAP_PRESSED_MARKER=off\n")

    run_tap(env)

    env.query.edit_message_reply_markup.assert_not_awaited()


def test_same_session_cards_keep_picks_isolated(env):
    first = SimpleNamespace(text="65kg", callback_data="flow|same|0|pick|65")
    env.query.message.reply_markup.inline_keyboard = [[first]]
    env.query.data = first.callback_data
    run_tap(env)

    env.query.message.message_id = 56
    second = SimpleNamespace(text="10\ud68c", callback_data="flow|same|0|pick|10")
    commit = SimpleNamespace(text="Save", callback_data="flow|same|1|save|now|c")
    env.query.message.reply_markup.inline_keyboard = [[second], [commit]]
    env.query.data = second.callback_data
    run_tap(env)

    env.query.data = commit.callback_data
    run_tap(env)

    waiting = env.query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert waiting.inline_keyboard[0][0].text == "\u23f3 10\ud68c \uae30\ub85d \uc911\u2026"


def test_identical_keyboard_after_commit_starts_with_empty_picks(env):
    pick = SimpleNamespace(text="65kg", callback_data="flow|sess-1|0|pick|65")
    commit = SimpleNamespace(text="Save", callback_data="flow|sess-1|1|save|now|c")
    env.query.message.reply_markup.inline_keyboard = [[pick], [commit]]
    env.query.data = pick.callback_data
    run_tap(env)
    env.query.data = commit.callback_data
    run_tap(env)

    env.query.edit_message_reply_markup.reset_mock()
    env.query.message.reply_markup.inline_keyboard = [[pick], [commit]]
    env.query.data = commit.callback_data
    run_tap(env)

    waiting = env.query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert waiting.inline_keyboard[0][0].text == "\u23f3 \uae30\ub85d \uc911\u2026"


def test_flow_tap_registries_are_bounded_and_locks_drain(env, monkeypatch):
    env.conf.write_text(env.conf.read_text() + "FLOW_TAP_PRESSED_MARKER=off\n")
    monkeypatch.setattr(
        flow_tap, "dispatch", lambda *args: flow_tap.DispatchResult(True))
    for index in range(flow_tap.CARD_REGISTRY_LIMIT + 20):
        env.query.message.message_id = 1000 + index
        button = SimpleNamespace(
            text=str(index), callback_data=f"flow|session-{index}|0|pick|{index}")
        env.query.message.reply_markup.inline_keyboard = [[button]]
        env.query.data = button.callback_data
        run_tap(env)
        assert len(env.bot._flow_tap_picks) <= flow_tap.CARD_REGISTRY_LIMIT
        assert not env.bot._flow_tap_locks

    for card_key in list(env.bot._flow_tap_picks):
        env.query.message.chat.id, env.query.message.message_id = card_key
        commit = SimpleNamespace(
            text="Save", callback_data=f"flow|drain-{card_key[1]}|0|save|now|c")
        env.query.message.reply_markup.inline_keyboard = [[commit]]
        env.query.data = commit.callback_data
        run_tap(env)
    assert not env.bot._flow_tap_picks
    assert not env.bot._flow_tap_locks


def test_dispatch_logs_truncated_first_outcome_line(env, caplog):
    env.command.write_text(
        "#!" + sys.executable + "\n"
        "print('FLOW_TAP_REPLAY ' + 'x' * 200)\n"
        "print('message content must not be logged')\n"
    )
    env.command.chmod(0o755)
    caplog.set_level(logging.INFO, logger="bridge.flow_tap")

    assert flow_tap.dispatch(env.root, DATA)

    outcome = next(record.message for record in caplog.records
                   if record.message.startswith("flow tap outcome"))
    assert len(outcome.removeprefix("flow tap outcome ")) == 120
    assert "message content" not in caplog.text


@pytest.mark.asyncio
async def test_selection_taps_ordered_and_inflight_duplicate_counted_once(env, monkeypatch):
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def to_thread(func, root, data):
        calls.append(data)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return flow_tap.DispatchResult(True)

    monkeypatch.setattr(bot_mod.asyncio, "to_thread", to_thread)
    first = asyncio.create_task(env.bot._handle_callback(env.update, None))
    await entered.wait()
    duplicate = asyncio.create_task(env.bot._handle_callback(env.update, None))
    await asyncio.sleep(0)
    second_data = "flow|sess-1|2|select|9"
    second_query = SimpleNamespace(**vars(env.query))
    second_query.data = second_data
    second_query.answer = AsyncMock()
    second_update = SimpleNamespace(callback_query=second_query,
                                    effective_user=env.update.effective_user,
                                    effective_chat=env.update.effective_chat)
    second = asyncio.create_task(env.bot._handle_callback(second_update, None))
    await asyncio.sleep(0)
    assert calls == [DATA]
    release.set()
    await asyncio.gather(first, duplicate, second)
    assert calls == [DATA, second_data]
    assert env.query.edit_message_reply_markup.await_count == 1
