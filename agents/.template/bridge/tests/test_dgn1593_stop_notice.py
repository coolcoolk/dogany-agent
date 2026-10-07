"""DGN-1593: the kill notice never names a job by its internal label, and
never follows the owner's own /stop.

Observed 2026-09-19: /stop replied
    "진행하던 작업을 멈췄습니다.\n⚠️ Resolve the default constants 작업이 멈췄습니다."
The {names} slot carried the SDK TaskStartedMessage description -- an
English tool-call label written for logs, not a name the owner knows -- and
the warning sign re-announced a stop the owner had just ordered.

Contract after the fix:
  (B) /stop -> the one STOP_INTERRUPTED sentence, nothing appended, even
      when the interrupt killed tracked background work (the owner knows
      it stopped; the kill list is still drained so it cannot leak).
  (A) automatic interrupt (defer-cap kill) keeps the notice -- that death
      is the one the owner does not know about (DGN-1015).  r1 stated a
      count only.  r2 (owner 2026-10-02 08:47 "작업들 이름도 알려줘야돼
      불릿 항목으로"): the count line, then one "- <name>" bullet per job
      with the name the owner already saw on its START push / workbench
      row (bg_job_notice state, DGN-1820) or, for a subagent, its Agent
      description when it passes the owner-name gate.  A job with no such
      name is counted without a bullet.  Never an internal label, run id,
      tool name or path.
  (C) the notice covers background tasks of any kind (subagent or
      background command), so the key is bg_task_killed_notice.
"""

import asyncio
import importlib.util
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge import bot as bot_mod
from bridge import messages
from bridge import sdk_bridge as sdk_bridge_mod
from bridge.i18n import en, ko
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState

INTERNAL = "Resolve the default constants"


def _allow(b, monkeypatch):
    async def allow_access(update):
        return True

    monkeypatch.setattr(b, "_check_access", allow_access)


@pytest.mark.asyncio
async def test_stop_reply_is_one_line_even_when_work_died(monkeypatch):
    b = bot_mod.TelegramBot()
    b.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    _allow(b, monkeypatch)
    popped = []
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "interrupt", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge,
        "pop_interrupt_killed",
        lambda uid: popped.append(uid) or [INTERNAL],
    )
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=9201),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )

    await b._cmd_stop(upd, SimpleNamespace(args=[]))

    upd.message.reply_text.assert_awaited_once_with(messages.STOP_INTERRUPTED)
    b.application.bot.send_message.assert_not_awaited()
    assert popped == [9201]  # drained, so a later interrupt cannot report it


async def _auto_interrupt_notice(monkeypatch, killed):
    """Drive a defer-cap auto-interrupt whose kill list is `killed`; return
    the fake bot the notice went to."""
    b = bot_mod.TelegramBot()
    fake_bot = SimpleNamespace(send_message=AsyncMock())
    b.application = SimpleNamespace(bot=fake_bot)
    user_id = 9202
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    monkeypatch.setattr(bot_mod, "BRIDGE_INFLIGHT_DEBOUNCE_S", 0.05)
    monkeypatch.setattr(bot_mod, "BRIDGE_INFLIGHT_DEFER_CAP_S", 10.0)
    monkeypatch.setattr(sdk_bridge_mod.sdk_bridge, "live_task_count", lambda uid: len(killed))
    barrier = asyncio.Event()

    async def fake_interrupt(uid, **kwargs):
        barrier.set()
        return True

    monkeypatch.setattr(sdk_bridge_mod.sdk_bridge, "interrupt", fake_interrupt)
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge,
        "pop_interrupt_killed",
        lambda uid: list(killed),
    )

    async def mock_process(update, uid, text, **kwargs):
        if text == "anchor":
            await barrier.wait()

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    def _upd(ts):
        chat = SimpleNamespace(id=1, send_action=AsyncMock())
        msg = SimpleNamespace(chat=chat, date=ts, caption=None, replies=[])
        return SimpleNamespace(
            effective_chat=chat,
            message=msg,
            effective_user=SimpleNamespace(id=user_id),
            callback_query=None,
        )

    ts0 = datetime(2026, 9, 19, 8, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "anchor", ts0, _upd(ts0))
    b._interrupt_deferred_since[user_id] = time.monotonic() - 11.0
    ts1 = datetime(2026, 9, 19, 8, 0, 1, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "urgent", ts1, _upd(ts1))
    await asyncio.sleep(0.2)
    for _ in range(12):
        tasks = list(b._user_run_tasks.get(user_id, set()))
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)

    return fake_bot


@pytest.mark.asyncio
async def test_auto_interrupt_notice_lists_names_as_bullets(monkeypatch):
    fake_bot = await _auto_interrupt_notice(
        monkeypatch, ["헬스팩 정리", "주간 리포트 작성"]
    )
    fake_bot.send_message.assert_awaited_once_with(
        1,
        "⚠️ 진행 중이던 작업 2건이 함께 멈췄습니다.\n"
        "- 헬스팩 정리\n"
        "- 주간 리포트 작성"
        if messages.BG_TASK_KILLED_NOTICE == ko.STRINGS["bg_task_killed_notice"]
        else "⚠️ 2 running task(s) stopped too.\n"
        "- 헬스팩 정리\n"
        "- 주간 리포트 작성",
    )


@pytest.mark.asyncio
async def test_auto_interrupt_nameless_job_counted_without_bullet(monkeypatch):
    fake_bot = await _auto_interrupt_notice(monkeypatch, ["헬스팩 정리", ""])
    sent = fake_bot.send_message.await_args.args[1]
    lines = sent.split("\n")
    assert lines == [
        messages.BG_TASK_KILLED_NOTICE.format(count=2),
        messages.BG_TASK_KILLED_ITEM.format(name="헬스팩 정리"),
    ]


@pytest.mark.asyncio
async def test_auto_interrupt_all_nameless_is_the_count_line_alone(monkeypatch):
    fake_bot = await _auto_interrupt_notice(monkeypatch, ["", ""])
    fake_bot.send_message.assert_awaited_once_with(
        1, messages.BG_TASK_KILLED_NOTICE.format(count=2)
    )


def test_catalog_copy_is_owner_confirmed():
    assert ko.STRINGS["bg_task_killed_notice"] == (
        "⚠️ 진행 중이던 작업 {count}건이 함께 멈췄습니다."
    )
    assert en.STRINGS["bg_task_killed_notice"] == "⚠️ {count} running task(s) stopped too."
    for mod, loc in ((ko, ko.STRINGS), (en, en.STRINGS)):
        assert "bg_subagent_killed_notice" not in loc
        assert "{names}" not in loc["bg_task_killed_notice"]
        assert loc["bg_task_killed_item"] == "- {name}"
        # The DRAFT marker is gone from these lines (owner-confirmed r2).
        src = open(mod.__file__, encoding="utf-8").read()
        block = src[: src.index('"bg_task_killed_item"')]
        block = block[block.rindex(": fact-based, fires only when"):]
        assert "DRAFT" not in block and "pending" not in block


# ---------------------------------------------------------------------------
# Name resolution at kill time: the same name the owner already saw
# ---------------------------------------------------------------------------


# ESTATE:BEGIN loads the estate background-job registry from routines/lib, which the public bridge does not ship
def _real_bg_job_notice(tmp_path, monkeypatch, state):
    lib_path = (
        sdk_bridge_mod.Path(__file__).resolve().parents[2]
        / "routines" / "lib" / "bg_job_notice.py"
    )
    spec = importlib.util.spec_from_file_location("bg_job_notice_t1593", lib_path)
    lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lib)
    state_file = tmp_path / "bg-notice-state.json"
    state_file.write_text(json.dumps(state, ensure_ascii=False))
    # The lib's own accessors, not its env knobs: the knob names are estate
    # vocabulary and this suite ships in the public bridge (DGN-1276 gate 2).
    monkeypatch.setattr(lib, "state_path", lambda: str(state_file))
    monkeypatch.setattr(lib, "owner_lang", lambda: "ko")
    monkeypatch.setattr(sdk_bridge_mod, "_bg_job_notice", lambda: lib)
    return lib
# ESTATE:ELSE
# def _real_bg_job_notice(tmp_path, monkeypatch, state):
#     # No registry ships with this build (sdk_bridge._bg_job_notice is None);
#     # a stand-in with the registry's three-call contract keeps the
#     # name-resolution and notify-once paths covered.
#     state_file = tmp_path / "bg-notice-state.json"
#     state_file.write_text(json.dumps(state, ensure_ascii=False))
#
#     def _unlabelled(name, slot):
#         name = (name or "").strip()
#         prefix = "%s: " % (slot or "").strip()
#         if slot and name.startswith(prefix) and name[len(prefix):].strip():
#             return name[len(prefix):].strip()
#         return name
#
#     def name_problem(name):
#         text = (name or "").strip()
#         if text and not any(ch in text for ch in "\t\n\r|") and any(
#                 0xAC00 <= ord(ch) <= 0xD7A3 for ch in text):
#             return None
#         return "not a name in the instance language"
#
#     lib = SimpleNamespace(state_path=lambda: str(state_file),
#                           _unlabelled=_unlabelled, name_problem=name_problem)
#     monkeypatch.setattr(sdk_bridge_mod, "_bg_job_notice", lambda: lib)
#     return lib
# ESTATE:END


@pytest.mark.asyncio
async def test_interrupt_resolves_owner_names_from_the_bg_registry(
    tmp_path, monkeypatch
):
    _real_bg_job_notice(tmp_path, monkeypatch, {
        # START push sent under this name (DGN-1820 description alone).
        "bshell1": {"tool": "Bash", "name": "헬스팩 정리", "slot": "python3",
                    "start_sent": True, "end_sent": False, "recorded": 0},
        # Pre-1820 entry stored "<slot>: <description>": prefix dropped.
        "bshell2": {"tool": "Bash", "name": "git: 릴리스 노트 정리",
                    "slot": "git", "start_sent": True, "end_sent": False,
                    "recorded": 0},
        # Name refused by the language gate: no START, so no name.
        "bshell3": {"tool": "Bash", "name": "Run the build", "slot": "bash",
                    "start_sent": False, "refused": "lang", "recorded": 0},
    })
    client = SimpleNamespace(_query=object(), interrupt=AsyncMock())
    state = _UserStreamState(client=client, model=None)
    handler = MagicMock(finalize_all=AsyncMock(), cancel=AsyncMock(), drafts=[])
    state.pending.append(_PendingRequest(
        user_id=9203, chat_id=1, model=None, requested_session_id=None,
        permission_callback=None, typing_callback=None,
        future=asyncio.get_running_loop().create_future(),
        user_message="msg", sent=True, streaming_handler=handler,
    ))
    order = ["bshell1", "bshell2", "bshell3", "agent1", "agent2", "bshell4"]
    state.active_tasks = {tid: 0.0 for tid in order}
    state.task_descriptions = {
        "bshell1": "python3 scripts/healthpack.py",
        "bshell3": "Run the build",
        "agent1": "로그 분석",          # Agent description, owner language
        "agent2": INTERNAL,            # English internal label: gated out
        "bshell4": "sleep 600 && echo done",  # shell, not in the registry
    }
    state.task_types = {
        "bshell1": "local_bash", "bshell3": "local_bash",
        "agent1": "local_agent", "agent2": "local_agent",
        "bshell4": "local_bash",
    }
    b = SdkBridge()
    b._streams[9203] = state

    assert await b.interrupt(9203, trigger="auto") is True

    killed = b.pop_interrupt_killed(9203)
    assert killed == ["헬스팩 정리", "릴리스 노트 정리", "", "로그 분석", "", ""]
    assert state.task_types == {}
    joined = "\n".join(killed)
    for leak in ("bshell", "agent1", "python3", "git", "sleep", "scripts/",
                 INTERNAL, "Run the build"):
        assert leak not in joined


def test_task_type_tracked_in_lockstep():
    state = _UserStreamState(client=None, model=None)
    started = sdk_bridge_mod.TaskStartedMessage(
        subtype="task_started", data={}, task_id="a1", description="로그 분석",
        uuid="u", session_id="s", task_type="local_agent",
    )
    SdkBridge._track_task_lifecycle(state, started)
    assert state.task_types == {"a1": "local_agent"}
    done = sdk_bridge_mod.TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="a1",
        status="completed", output_file="", summary="", uuid="u2",
        session_id="s",
    )
    SdkBridge._track_task_lifecycle(state, done)
    assert state.task_types == {}


def test_no_registry_means_nameless_never_raw_description(monkeypatch):
    monkeypatch.setattr(sdk_bridge_mod, "_bg_job_notice", lambda: None)
    assert sdk_bridge_mod._owner_task_name("a1", "로그 분석", "local_agent") == ""


# ---------------------------------------------------------------------------
# r3: the DGN-1499 timeout stop signal kills background jobs too -- same
# notice as the auto-interrupt (count line + one bullet per named job).
# ---------------------------------------------------------------------------


def _live_state(user_id, handler=None):
    client = SimpleNamespace(_query=object(), interrupt=AsyncMock())
    state = _UserStreamState(client=client, model=None)
    state.last_session_id = "sid-live"
    if handler is None:
        handler = MagicMock(finalize_all=AsyncMock(), cancel=AsyncMock(), drafts=[])
    state.pending.append(_PendingRequest(
        user_id=user_id, chat_id=1, model=None, requested_session_id=None,
        permission_callback=None, typing_callback=None,
        future=asyncio.get_running_loop().create_future(),
        user_message="msg", sent=True, streaming_handler=handler,
    ))
    state.active_tasks = {"bshell1": 0.0, "bshell4": 0.0, "agent1": 0.0}
    state.task_descriptions = {
        "bshell1": "python3 scripts/healthpack.py",
        "bshell4": "sleep 600 && echo done",
        "agent1": "로그 분석",
    }
    state.task_types = {
        "bshell1": "local_bash", "bshell4": "local_bash",
        "agent1": "local_agent",
    }
    return state


_NAMED_STATE = {
    "bshell1": {"tool": "Bash", "name": "헬스팩 정리", "slot": "python3",
                "start_sent": True, "end_sent": False, "recorded": 0},
}
_EXPECTED_KILLED = ["헬스팩 정리", "", "로그 분석"]


@pytest.mark.asyncio
async def test_timeout_stop_carries_killed_jobs_on_the_response(
    tmp_path, monkeypatch
):
    _real_bg_job_notice(tmp_path, monkeypatch, _NAMED_STATE)
    monkeypatch.setattr(sdk_bridge_mod, "PROCESS_TIMEOUT", 600)
    monkeypatch.setattr(sdk_bridge_mod, "TIMEOUT_STOP_GRACE", 20)
    b = SdkBridge()
    b._streams[9204] = _live_state(9204)

    response = await b._timeout_stop_then_preserve(9204)

    assert response is not None and response.timed_out
    assert response.killed_jobs == _EXPECTED_KILLED
    # Drained onto the response (read-once): nothing left for another reader.
    assert b.pop_interrupt_killed(9204) == []


def _resume_bot(monkeypatch):
    b = bot_mod.TelegramBot()
    fake_bot = SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock())
    b.application = SimpleNamespace(bot=fake_bot)
    b._send_resume_notice = AsyncMock()
    b._send_guaranteed = AsyncMock()
    return b, fake_bot


def _timed_out(killed):
    return sdk_bridge_mod.ChatResponse(
        content="paused", success=False, error="timeout", timed_out=True,
        resume_session_id="sid-live", killed_jobs=list(killed),
    )


@pytest.mark.asyncio
async def test_timeout_kill_sends_the_auto_interrupt_notice(monkeypatch):
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", False)
    b, fake_bot = _resume_bot(monkeypatch)

    out = await b._finish_turn_with_auto_resume(
        user_id=9204, chat_id=1, response=_timed_out(_EXPECTED_KILLED),
        resume_caller=AsyncMock(),
    )

    assert out is None  # tap-to-continue path, unchanged
    b._send_resume_notice.assert_awaited_once()
    fake_bot.send_message.assert_awaited_once_with(
        1,
        "\n".join([
            messages.BG_TASK_KILLED_NOTICE.format(count=3),
            messages.BG_TASK_KILLED_ITEM.format(name="헬스팩 정리"),
            messages.BG_TASK_KILLED_ITEM.format(name="로그 분석"),
        ]),
    )


@pytest.mark.asyncio
async def test_timeout_kill_notice_precedes_auto_resume(monkeypatch):
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", True)
    monkeypatch.setattr(bot_mod, "AUTO_RESUME_MAX", 3)
    b, fake_bot = _resume_bot(monkeypatch)
    b._resolve_resume_sid = AsyncMock(return_value="sid-live")
    store = {}
    monkeypatch.setattr(bot_mod, "session_manager", SimpleNamespace(
        get_session=AsyncMock(return_value=store),
        update_session=AsyncMock(),
    ))
    order = []
    fake_bot.send_message.side_effect = lambda chat, text: order.append(text)
    b._send_guaranteed.side_effect = lambda chat, text, **kw: order.append(text)
    # Resume #1 times out again and kills one more (nameless) job; resume
    # #2 settles.  Each kill is reported once, right where it happened.
    resume_caller = AsyncMock(side_effect=[
        _timed_out([""]),
        sdk_bridge_mod.ChatResponse(content="done"),
    ])

    out = await b._finish_turn_with_auto_resume(
        user_id=9204, chat_id=1, response=_timed_out(["", "헬스팩 정리"]),
        resume_caller=resume_caller,
    )

    assert out.content == "done"
    assert order == [
        messages.BG_TASK_KILLED_NOTICE.format(count=2)
        + "\n" + messages.BG_TASK_KILLED_ITEM.format(name="헬스팩 정리"),
        messages.STILL_WORKING,
        messages.BG_TASK_KILLED_NOTICE.format(count=1),
    ]


@pytest.mark.asyncio
async def test_timeout_without_kills_sends_no_notice(monkeypatch):
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", False)
    b, fake_bot = _resume_bot(monkeypatch)
    await b._finish_turn_with_auto_resume(
        user_id=9204, chat_id=1, response=_timed_out([]),
        resume_caller=AsyncMock(),
    )
    fake_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["auto", "timeout"])
async def test_overlapping_timeout_and_auto_interrupt_notify_once(
    tmp_path, monkeypatch, first
):
    """The timeout stop signal and a defer-cap auto-interrupt race on one
    stream.  Whichever snapshots first must not have its kill list erased by
    the other's (empty) snapshot, and the jobs are reported exactly once."""
    _real_bg_job_notice(tmp_path, monkeypatch, _NAMED_STATE)
    monkeypatch.setattr(sdk_bridge_mod, "PROCESS_TIMEOUT", 600)
    monkeypatch.setattr(sdk_bridge_mod, "TIMEOUT_STOP_GRACE", 20)
    uid = 9205
    gate = asyncio.Event()

    async def slow_drain(*a, **kw):
        # The first interrupt's drain step yields after its snapshot --
        # the window in which the second interrupt runs to completion.
        for _ in range(20):
            await asyncio.sleep(0)

    handler = MagicMock(
        finalize_all=AsyncMock(side_effect=slow_drain),
        cancel=AsyncMock(side_effect=slow_drain),
        drafts=[],
    )
    state = _live_state(uid, handler)

    async def gated_interrupt():
        await gate.wait()

    state.client.interrupt = AsyncMock(side_effect=gated_interrupt)
    b = SdkBridge()
    b._streams[uid] = state

    async def auto_path():
        # bot.py's defer-cap path: interrupt, then pop at once.
        if await b.interrupt(uid, trigger="auto"):
            return b.pop_interrupt_killed(uid)
        return []

    async def timeout_path():
        resp = await b._timeout_stop_then_preserve(uid)
        return resp.killed_jobs if resp is not None else []

    paths = {"auto": auto_path, "timeout": timeout_path}
    second = "timeout" if first == "auto" else "auto"
    t1 = asyncio.ensure_future(paths[first]())
    for _ in range(5):
        await asyncio.sleep(0)
    t2 = asyncio.ensure_future(paths[second]())
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    got_first, got_second = await asyncio.gather(t1, t2)

    # Whichever caller pops first reports every killed job; the other gets
    # nothing (read-once).  Never lost, never twice.
    assert sorted([got_first, got_second], key=len) == [[], _EXPECTED_KILLED]
    assert b.pop_interrupt_killed(uid) == []

    # Both consumers hand their list to the one notice sender: one message.
    tb = bot_mod.TelegramBot()
    tb.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await tb._send_bg_killed_notice(1, uid, got_first)
    await tb._send_bg_killed_notice(1, uid, got_second)
    tb.application.bot.send_message.assert_awaited_once_with(
        1,
        "\n".join([
            messages.BG_TASK_KILLED_NOTICE.format(count=3),
            messages.BG_TASK_KILLED_ITEM.format(name="헬스팩 정리"),
            messages.BG_TASK_KILLED_ITEM.format(name="로그 분석"),
        ]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect_fails", [False, True])
@pytest.mark.parametrize("soft_outcome", ["failed", "unreported", "reported"])
async def test_hard_timeout_carries_kills_and_notifies_once(
    tmp_path, monkeypatch, disconnect_fails, soft_outcome
):
    """Hard teardown preserves names/counts without repeating a soft notice."""
    _real_bg_job_notice(tmp_path, monkeypatch, _NAMED_STATE)
    monkeypatch.setattr(sdk_bridge_mod, "PROCESS_TIMEOUT", 0.02)
    monkeypatch.setattr(sdk_bridge_mod, "TIMEOUT_STOP_GRACE", 0.01)
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", False)
    uid = 9206
    bridge = SdkBridge()
    state = _live_state(uid)
    state.pending.clear()
    state.client.disconnect = AsyncMock(
        side_effect=asyncio.TimeoutError() if disconnect_fails else None
    )
    bridge._streams[uid] = state
    monkeypatch.setattr(bridge, "_get_or_create_stream", AsyncMock(return_value=state))
    force_kill = MagicMock()
    monkeypatch.setattr(bridge, "_force_kill_client_subprocess", force_kill)
    tb, fake_bot = _resume_bot(monkeypatch)

    async def dispatch(st):
        st.pending[0].sent = True

    monkeypatch.setattr(bridge, "_dispatch_next_query", AsyncMock(side_effect=dispatch))
    if soft_outcome == "failed":
        state.client.interrupt.side_effect = asyncio.TimeoutError()
    else:
        # A concurrent automatic interrupt lands first. Its kill list is
        # either already reported or still waiting for the timeout to drain.
        original_stop = bridge._timeout_stop_then_preserve

        async def stop_after_auto(user_id):
            assert await bridge.interrupt(user_id, trigger="auto")
            if soft_outcome == "reported":
                await tb._send_bg_killed_notice(
                    1, uid, bridge.pop_interrupt_killed(uid)
                )
            return await original_stop(user_id)

        monkeypatch.setattr(bridge, "_timeout_stop_then_preserve", stop_after_auto)

    response = await bridge.process_message(
        user_message="long turn", user_id=uid, chat_id=1
    )

    assert response.timed_out
    assert response.resume_session_id == "sid-live"
    assert response.killed_jobs == (
        [] if soft_outcome == "reported" else _EXPECTED_KILLED
    )
    assert uid not in bridge._streams
    assert state.active_tasks == state.task_descriptions == state.task_types == {}
    assert state.interrupt_killed_descriptions == []
    state.client.disconnect.assert_awaited_once()
    if disconnect_fails:
        force_kill.assert_called_once_with(state.client, uid)
    else:
        force_kill.assert_not_called()

    await tb._finish_turn_with_auto_resume(
        user_id=uid, chat_id=1, response=response, resume_caller=AsyncMock()
    )
    fake_bot.send_message.assert_awaited_once_with(
        1,
        "\n".join([
            messages.BG_TASK_KILLED_NOTICE.format(count=3),
            messages.BG_TASK_KILLED_ITEM.format(name=_EXPECTED_KILLED[0]),
            messages.BG_TASK_KILLED_ITEM.format(name=_EXPECTED_KILLED[2]),
        ]),
    )
