"""DGN-801 fast-path interceptor unit tests.

Contract under test (spec DGN-801 v1 + grill fixes F3/F4/M1/M2/M3/M6):
  (a) default OFF: no FASTPATH_HANDLER -> interceptor fully inert.
  (b) prefilter is domain-agnostic and generous: short + has-digit only.
  (c) exit 0 -> stdout body pushed, SDK model turn suppressed.
  (d) exit 2 FALLBACK (and any other non-zero exit) -> normal SDK turn on the
      original text, never a drop.
  (e) F3: timeout kills the handler's WHOLE process group (background child
      sharing the group dies too, no orphan), then falls back to the SDK turn.
  (f) F4: fires only when the user is idle; messages arriving during a
      fast-path task buffer behind it (DGN-616 coalescing), never concurrent.
  (g) M3: exit-0 push failure retries then emits the committed-but-silent
      death notice; NEVER re-feeds the message to the model.
  (h) M6: the bridge never touches handler-side marker files
      (files/program/.tip-pending stays byte-identical through all flows).
"""

import asyncio
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bridge import bot as bot_mod
from bridge import config as config_mod
from bridge import fastpath as fastpath_mod
from bridge import messages
from bridge import sdk_bridge as sdk_bridge_mod


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))


class _FakeApp:
    def __init__(self, fake_bot):
        self.bot = fake_bot


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id

    async def send_action(self, *a, **k):
        pass


class _FakeMessage:
    def __init__(self, chat, date=None):
        self.chat = chat
        self.date = date or datetime.now(timezone.utc)
        self.caption = None

    async def reply_text(self, *a, **k):
        pass


class _FakeUpdate:
    def __init__(self, chat_id=1, date=None):
        chat = _FakeChat(chat_id)
        self.effective_chat = chat
        self.message = _FakeMessage(chat, date=date)


def _make_bot():
    b = bot_mod.TelegramBot()
    b.application = _FakeApp(_FakeBot())
    return b


def _upd(ts=None, chat_id=1):
    return _FakeUpdate(chat_id=chat_id, date=ts or datetime.now(timezone.utc))


def _write_handler(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-handler"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


async def _await_all_tasks(b, user_id, max_rounds=12):
    seen = set()
    for _ in range(max_rounds):
        tasks = [t for t in b._user_run_tasks.get(user_id, set()) if t not in seen]
        if not tasks:
            break
        seen.update(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def _quiet_streamed_output(monkeypatch):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )


# --- (a) default OFF ---------------------------------------------------------


def test_disabled_when_unset():
    # conftest gives a hermetic PROJECT_ROOT with no .env -> unset by default.
    assert config_mod.FASTPATH_HANDLER == ""
    assert fastpath_mod.handler_path() is None
    assert not fastpath_mod.enabled()


def test_disabled_when_file_missing(monkeypatch):
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", "files/program/nonexistent")
    assert fastpath_mod.handler_path() is not None
    assert not fastpath_mod.enabled()


@pytest.mark.asyncio
async def test_interceptor_inert_when_disabled(monkeypatch):
    b = _make_bot()
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", "")
    spawned = {"n": 0}

    async def spy_run(text):
        spawned["n"] += 1
        return fastpath_mod.FastpathResult(processed=False)

    monkeypatch.setattr(fastpath_mod, "run_handler", spy_run)
    claimed = await b._try_fastpath(700, "8 2", _upd())
    assert claimed is False
    assert spawned["n"] == 0
    assert b._user_run_tasks.get(700, set()) == set()


# --- (b) prefilter -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("8 2", True),  # canonical set-result shape
        ("12", True),
        ("60.5 8", True),
        ("3시에 봐", True),  # generous: digits present -> handler decides
        ("", False),
        ("   ", False),
        ("no digits here", False),
        ("/stop", False),  # commands never fast-path
        ("8 2\n9 3", False),  # multiline is conversational
        ("x" * 63 + "1", True),  # at the length bound (has digit)
        ("1" + "x" * 64, False),  # over the bound
    ],
)
def test_prefilter(text, expected):
    assert fastpath_mod.prefilter(text) is expected


# --- runner contract: exit codes ----------------------------------------------


@pytest.mark.asyncio
async def test_run_handler_exit0_processed(tmp_path, monkeypatch):
    handler = _write_handler(tmp_path, 'echo "set 3 | 8 reps @ 60kg"\nexit 0\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    result = await fastpath_mod.run_handler("8 2")
    assert result.processed is True
    assert result.body == "set 3 | 8 reps @ 60kg"


@pytest.mark.asyncio
async def test_run_handler_exit2_fallback(tmp_path, monkeypatch):
    handler = _write_handler(tmp_path, 'echo "FALLBACK non_numeric"\nexit 2\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    result = await fastpath_mod.run_handler("3시에 봐")
    assert result.processed is False
    assert result.reason == "non_numeric"


@pytest.mark.asyncio
async def test_run_handler_other_exit_is_fallback(tmp_path, monkeypatch):
    handler = _write_handler(tmp_path, 'echo "boom" >&2\nexit 1\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    result = await fastpath_mod.run_handler("8 2")
    assert result.processed is False
    assert result.reason == "exit_1"


@pytest.mark.asyncio
async def test_run_handler_receives_raw_text_argv(tmp_path, monkeypatch):
    # Contract shape check: <handler> handle --raw "<text>" -- exactly.
    handler = _write_handler(
        tmp_path,
        '[ "$1" = "handle" ] || exit 1\n'
        '[ "$2" = "--raw" ] || exit 1\n'
        'echo "GOT:$3"\nexit 0\n',
    )
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    result = await fastpath_mod.run_handler("8 2")
    assert result.processed is True
    assert result.body == "GOT:8 2"


# --- (e) F3: timeout kills the whole process group -----------------------------


@pytest.mark.asyncio
async def test_timeout_reaps_process_group(tmp_path, monkeypatch):
    child_pid_file = tmp_path / "child.pid"
    handler = _write_handler(
        tmp_path,
        # Background child shares the handler's process group; a lone-pid kill
        # would orphan it for 30s. The group kill must take it down too.
        "sleep 30 &\n"
        f'echo $! > "{child_pid_file}"\n'
        "sleep 30\n",
    )
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    # macOS first-exec of a fresh script can take ~0.5s; keep the timeout
    # comfortably above that so the pid file lands before the group kill,
    # while the 30s sleeps guarantee the timeout still fires.
    monkeypatch.setattr(config_mod, "FASTPATH_TIMEOUT_S", 2.0)

    result = await fastpath_mod.run_handler("8 2")
    assert result.processed is False
    assert result.reason == "timeout"

    assert child_pid_file.exists(), "handler never started -- test env issue"
    child_pid = int(child_pid_file.read_text().strip())
    # The group kill must have taken the background child down as well; give
    # the SIGTERM->SIGKILL escalation a moment to land.
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(child_pid, 9)  # cleanup before failing
        pytest.fail("background child survived the process-group kill (orphan)")


@pytest.mark.asyncio
async def test_timeout_grace_window_exit0_recovered(tmp_path, monkeypatch):
    # B1: the Skull F3 contract degrades a post-commit failure to exit 0, and a
    # handler can do that inside the SIGTERM->SIGKILL grace window while we are
    # timing it out. Once state is committed, treating it as a plain timeout
    # FALLBACK would re-feed the model and double-log the next slot. The runner
    # must recover the exit0 (processed, body lost -> M3 death-notice path),
    # never fall back to the model.
    handler = _write_handler(
        tmp_path,
        # Trap SIGTERM: on the group kill, exit 0 immediately (commit witness),
        # well within KILL_GRACE_S so proc.wait() sees returncode 0.
        'trap "exit 0" TERM\n'
        "sleep 30\n",
    )
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    monkeypatch.setattr(config_mod, "FASTPATH_TIMEOUT_S", 1.0)

    result = await fastpath_mod.run_handler("8 2")
    # exit0 wins even on the timeout path: processed, no body (communicate was
    # cancelled), diagnostic reason marks the grace-window recovery.
    assert result.processed is True
    assert result.body == ""
    assert result.reason == "timeout_exit0"


@pytest.mark.asyncio
async def test_timeout_grace_window_exit0_no_model_fallback(tmp_path, monkeypatch):
    # B1 end-to-end through the interceptor: a grace-window exit0 must push the
    # committed-but-silent death notice and NEVER re-feed the model.
    b = _make_bot()
    handler = _write_handler(
        tmp_path,
        'trap "exit 0" TERM\n'
        "sleep 30\n",
    )
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    monkeypatch.setattr(config_mod, "FASTPATH_TIMEOUT_S", 1.0)
    user_id = 720
    model_calls = []

    async def mock_process(update, uid, text, **kwargs):
        model_calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    claimed = await b._try_fastpath(user_id, "8 2", _upd(chat_id=42))
    assert claimed is True
    await _await_all_tasks(b, user_id)

    assert model_calls == []  # committed -> never re-fed to the model
    assert b.application.bot.sent == [(42, messages.FASTPATH_PUSH_FAILED)]


@pytest.mark.asyncio
async def test_genuine_timeout_still_falls_back(tmp_path, monkeypatch):
    # B1 counterpart: a handler that truly never finishes (ignores SIGTERM until
    # the SIGKILL) must stay a FALLBACK -- exit0 recovery only fires on a real
    # grace-window exit 0, not on every timeout.
    handler = _write_handler(
        tmp_path,
        # Ignore TERM so the SIGKILL is what ends it -> returncode != 0.
        'trap "" TERM\n'
        "sleep 30\n",
    )
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    monkeypatch.setattr(config_mod, "FASTPATH_TIMEOUT_S", 1.0)

    result = await fastpath_mod.run_handler("8 2")
    assert result.processed is False
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_cancellation_reaps_process_group(tmp_path, monkeypatch):
    # /stop clears the user queue and CANCELS the fast-path task mid-handler;
    # the handler group must not keep running unsupervised past the task.
    child_pid_file = tmp_path / "child.pid"
    handler = _write_handler(
        tmp_path,
        "sleep 30 &\n"
        f'echo $! > "{child_pid_file}"\n'
        "sleep 30\n",
    )
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    monkeypatch.setattr(config_mod, "FASTPATH_TIMEOUT_S", 30.0)

    task = asyncio.create_task(fastpath_mod.run_handler("8 2"))
    for _ in range(60):  # wait for the handler to actually start
        if child_pid_file.exists():
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        pytest.fail("handler never started -- test env issue")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    child_pid = int(child_pid_file.read_text().strip())
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(child_pid, 9)  # cleanup before failing
        pytest.fail("background child survived cancellation (orphan)")


@pytest.mark.asyncio
async def test_timeout_falls_back_to_model_turn(tmp_path, monkeypatch):
    b = _make_bot()
    handler = _write_handler(tmp_path, "sleep 30\n")
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    monkeypatch.setattr(config_mod, "FASTPATH_TIMEOUT_S", 0.2)
    user_id = 701
    calls = []

    async def mock_process(update, uid, text, **kwargs):
        calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    claimed = await b._try_fastpath(user_id, "8 2", _upd())
    assert claimed is True
    await _await_all_tasks(b, user_id)
    assert calls == ["8 2"]  # slow-but-correct: SDK turn ran, nothing dropped


# --- (c)/(d) interceptor wiring: exit0 push+suppress / exit2 fallback ----------


@pytest.mark.asyncio
async def test_exit0_pushes_body_and_suppresses_model_turn(tmp_path, monkeypatch):
    b = _make_bot()
    handler = _write_handler(tmp_path, 'echo "rendered table"\nexit 0\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    user_id = 702
    model_calls = []
    pushes = []

    async def mock_process(update, uid, text, **kwargs):
        model_calls.append(text)

    async def mock_send_smart(chat_id, content, **kwargs):
        pushes.append((chat_id, content))

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)
    monkeypatch.setattr(b, "_send_smart", mock_send_smart)

    claimed = await b._try_fastpath(user_id, "8 2", _upd(chat_id=42))
    assert claimed is True
    await _await_all_tasks(b, user_id)

    assert pushes == [(42, "rendered table")]
    assert model_calls == []  # SDK resume turn fully suppressed
    assert b.application.bot.sent == []  # no death notice on the happy path


@pytest.mark.asyncio
async def test_exit2_falls_back_to_model_turn_no_push(tmp_path, monkeypatch):
    b = _make_bot()
    handler = _write_handler(tmp_path, 'echo "FALLBACK tip_pending"\nexit 2\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    user_id = 703
    model_calls = []
    pushes = []

    async def mock_process(update, uid, text, **kwargs):
        model_calls.append(text)

    async def mock_send_smart(chat_id, content, **kwargs):
        pushes.append((chat_id, content))

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)
    monkeypatch.setattr(b, "_send_smart", mock_send_smart)

    claimed = await b._try_fastpath(user_id, "8 2", _upd())
    assert claimed is True  # claimed by the task; fallback happens inside it
    await _await_all_tasks(b, user_id)

    assert model_calls == ["8 2"]  # original text reaches the model verbatim
    assert pushes == []


@pytest.mark.asyncio
async def test_exit0_empty_body_death_notice_no_model_fallback(tmp_path, monkeypatch):
    # exit 0 with no stdout is a handler contract violation, but exit 0 is the
    # commit witness -> never re-feed the model; surface the silent commit.
    b = _make_bot()
    handler = _write_handler(tmp_path, "exit 0\n")
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    user_id = 704
    model_calls = []

    async def mock_process(update, uid, text, **kwargs):
        model_calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)

    claimed = await b._try_fastpath(user_id, "8 2", _upd(chat_id=42))
    assert claimed is True
    await _await_all_tasks(b, user_id)

    assert model_calls == []
    assert b.application.bot.sent == [(42, messages.FASTPATH_PUSH_FAILED)]


# --- (g) M3: push retry + death notice, never model fallback -------------------


@pytest.mark.asyncio
async def test_push_failure_retries_then_death_notice(tmp_path, monkeypatch):
    b = _make_bot()
    handler = _write_handler(tmp_path, 'echo "rendered table"\nexit 0\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    monkeypatch.setattr(bot_mod, "FASTPATH_PUSH_RETRY_DELAYS", (0.0, 0.0))
    user_id = 705
    model_calls = []
    attempts = {"n": 0}

    async def mock_process(update, uid, text, **kwargs):
        model_calls.append(text)

    async def failing_send_smart(chat_id, content, **kwargs):
        attempts["n"] += 1
        raise RuntimeError("telegram down")

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)
    monkeypatch.setattr(b, "_send_smart", failing_send_smart)

    claimed = await b._try_fastpath(user_id, "8 2", _upd(chat_id=42))
    assert claimed is True
    await _await_all_tasks(b, user_id)

    assert attempts["n"] == 1 + len((0.0, 0.0))  # initial + retries
    # Committed state -> death notice on the raw path, NEVER a model fallback.
    assert b.application.bot.sent == [(42, messages.FASTPATH_PUSH_FAILED)]
    assert model_calls == []


@pytest.mark.asyncio
async def test_push_transient_failure_recovers_on_retry(tmp_path, monkeypatch):
    b = _make_bot()
    handler = _write_handler(tmp_path, 'echo "rendered table"\nexit 0\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    monkeypatch.setattr(bot_mod, "FASTPATH_PUSH_RETRY_DELAYS", (0.0, 0.0))
    user_id = 706
    pushes = []
    state = {"n": 0}

    async def flaky_send_smart(chat_id, content, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("blip")
        pushes.append((chat_id, content))

    monkeypatch.setattr(b, "_send_smart", flaky_send_smart)

    claimed = await b._try_fastpath(user_id, "8 2", _upd(chat_id=42))
    assert claimed is True
    await _await_all_tasks(b, user_id)

    assert pushes == [(42, "rendered table")]
    assert b.application.bot.sent == []  # recovered -> no death notice


# --- (f) F4: idle gate + serialization against the coalescing path -------------


@pytest.mark.asyncio
async def test_not_fired_while_turn_in_flight(tmp_path, monkeypatch):
    b = _make_bot()
    handler = _write_handler(tmp_path, 'echo "rendered table"\nexit 0\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    user_id = 707
    gate = asyncio.Event()

    async def blocker():
        await gate.wait()

    inflight = asyncio.create_task(blocker())
    b._track_user_task(user_id, inflight)

    claimed = await b._try_fastpath(user_id, "8 2", _upd())
    assert claimed is False  # busy user -> message must take the coalescing path

    gate.set()
    await inflight


@pytest.mark.asyncio
async def test_messages_during_fastpath_buffer_and_drain(tmp_path, monkeypatch):
    b = _make_bot()
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(tmp_path / "unused"))
    monkeypatch.setattr(fastpath_mod, "enabled", lambda: True)
    user_id = 708
    barrier = asyncio.Event()
    model_calls = []
    pushes = []

    async def slow_handler(text):
        await barrier.wait()
        return fastpath_mod.FastpathResult(processed=True, body="rendered table")

    async def mock_process(update, uid, text, **kwargs):
        model_calls.append(text)

    async def mock_send_smart(chat_id, content, **kwargs):
        pushes.append(content)

    monkeypatch.setattr(fastpath_mod, "run_handler", slow_handler)
    monkeypatch.setattr(b, "_process_user_message_text", mock_process)
    monkeypatch.setattr(b, "_send_smart", mock_send_smart)

    ts = datetime(2026, 8, 12, 5, 0, 0, tzinfo=timezone.utc)
    claimed = await b._try_fastpath(user_id, "8 2", _upd(ts))
    assert claimed is True

    # A message arriving while the fast-path runs must BUFFER (single
    # serialization point), not spawn a concurrent SDK turn.
    await b._enqueue_text_task(user_id, "how am I doing", ts, _upd(ts))
    assert len(b._user_pending_texts.get(user_id, [])) == 1
    assert model_calls == []

    barrier.set()
    await _await_all_tasks(b, user_id)

    assert pushes == ["rendered table"]
    assert model_calls == ["how am I doing"]  # drained AFTER the fast-path
    assert b._user_pending_texts.get(user_id, []) == []


@pytest.mark.asyncio
async def test_no_second_fastpath_while_one_runs(tmp_path, monkeypatch):
    b = _make_bot()
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(tmp_path / "unused"))
    monkeypatch.setattr(fastpath_mod, "enabled", lambda: True)
    user_id = 709
    barrier = asyncio.Event()
    handler_calls = {"n": 0}

    async def slow_handler(text):
        handler_calls["n"] += 1
        await barrier.wait()
        return fastpath_mod.FastpathResult(processed=True, body="body")

    monkeypatch.setattr(fastpath_mod, "run_handler", slow_handler)

    async def mock_send_smart(chat_id, content, **kwargs):
        pass

    monkeypatch.setattr(b, "_send_smart", mock_send_smart)

    first = await b._try_fastpath(user_id, "8 2", _upd())
    second = await b._try_fastpath(user_id, "9 3", _upd())
    assert first is True
    assert second is False  # in-flight fast-path blocks a concurrent one

    barrier.set()
    await _await_all_tasks(b, user_id)
    assert handler_calls["n"] == 1


# --- (h) M6: handler-side marker files stay untouched ---------------------------


@pytest.mark.asyncio
async def test_tip_pending_marker_untouched(tmp_path, monkeypatch):
    marker_dir = fastpath_mod.PROJECT_ROOT / "files" / "program"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / ".tip-pending"
    marker.write_text("10079\n")
    before_bytes = marker.read_bytes()
    before_stat = marker.stat()

    for body in (
        'echo "rendered"\nexit 0\n',
        'echo "FALLBACK tip_pending"\nexit 2\n',
        "sleep 30\n",
    ):
        handler = _write_handler(tmp_path, body)
        monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
        monkeypatch.setattr(config_mod, "FASTPATH_TIMEOUT_S", 0.2)
        await fastpath_mod.run_handler("8 2")

    after = marker.stat()
    assert marker.read_bytes() == before_bytes
    assert after.st_mtime_ns == before_stat.st_mtime_ns


# --- B4 (Option 2): suppress fast-path while an outside-approval prompt pends ---


class _FakeSessionManager:
    """In-memory session store standing in for bridge.session.session_manager."""

    def __init__(self, seed=None):
        self._store = dict(seed or {})

    async def get_session(self, user_id):
        return self._store.setdefault(user_id, {})

    async def update_session(self, user_id, data):
        self._store[user_id] = data


class _MsgUpdate:
    """_handle_text_message-shaped update (effective_user + message.text)."""

    def __init__(self, user_id, text, chat_id=None, date=None):
        chat_id = chat_id if chat_id is not None else user_id
        chat = _FakeChat(chat_id)
        self.effective_chat = chat
        msg = _FakeMessage(chat, date=date)
        msg.text = text
        msg.message_id = 1
        self.message = msg

        class _U:
            id = user_id

        self.effective_user = _U()


def _pending_approval_session(user_id):
    return {
        user_id: {
            "pending_outside_paths": ["/etc/hosts"],
            "pending_outside_at": time.time(),  # fresh, well within TTL
        }
    }


@pytest.mark.asyncio
async def test_capture_returns_true_while_prompt_pending():
    # Unit: the capture helper reports "prompt was live" so the caller can skip
    # fast-path. Allow token, deny token, and a non-decision message all count
    # as "pending" while a fresh prompt exists.
    b = _make_bot()
    for text, expect_grant in (("1", True), ("2", False), ("hello", None)):
        sm = _FakeSessionManager(_pending_approval_session(900))
        import unittest.mock as _mock

        with _mock.patch.object(bot_mod, "session_manager", sm):
            pending = await b._maybe_capture_outside_approval(900, text)
        assert pending is True
        if expect_grant is True:
            assert sm._store[900].get("outside_path_approved_once") is True
        elif expect_grant is False:
            assert sm._store[900].get("outside_path_approved_once") is False


@pytest.mark.asyncio
async def test_capture_returns_false_when_no_prompt_or_expired():
    b = _make_bot()
    import unittest.mock as _mock

    # No prompt pending.
    sm = _FakeSessionManager({901: {}})
    with _mock.patch.object(bot_mod, "session_manager", sm):
        assert await b._maybe_capture_outside_approval(901, "1") is False

    # Prompt pending but expired -> free to fast-path.
    sm = _FakeSessionManager(
        {
            902: {
                "pending_outside_paths": ["/etc/hosts"],
                "pending_outside_at": time.time() - bot_mod.OUTSIDE_APPROVAL_TTL - 1,
            }
        }
    )
    with _mock.patch.object(bot_mod, "session_manager", sm):
        assert await b._maybe_capture_outside_approval(902, "1") is False


@pytest.mark.asyncio
async def test_approval_pending_plus_active_session_skips_fastpath(tmp_path, monkeypatch):
    # (a) Full path: approval pending + active session + "1" -> fast-path is
    # skipped, the token is consumed ONLY as the approval grant (no double
    # consumption), and no set is logged via the fast-path handler.
    b = _make_bot()
    handler = _write_handler(tmp_path, 'echo "SET LOGGED"\nexit 0\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    monkeypatch.setattr(b, "_check_access", lambda update: _true())
    sm = _FakeSessionManager(_pending_approval_session(903))
    monkeypatch.setattr(bot_mod, "session_manager", sm)

    fastpath_calls = {"n": 0}

    async def spy_fastpath(uid, text, update):
        fastpath_calls["n"] += 1
        return await bot_mod.TelegramBot._try_fastpath(b, uid, text, update)

    monkeypatch.setattr(b, "_try_fastpath", spy_fastpath)

    enqueued = []

    async def spy_enqueue(uid, text, ts, update, **kw):
        enqueued.append(text)

    monkeypatch.setattr(b, "_enqueue_text_task", spy_enqueue)

    await b._handle_text_message(_MsgUpdate(903, "1"), None)

    # Fast-path was never even offered the message (skipped before the call).
    assert fastpath_calls["n"] == 0
    # The approval was granted (token consumed on the approval side only).
    assert sm._store[903].get("outside_path_approved_once") is True
    assert "pending_outside_paths" not in sm._store[903]
    # The message still flowed to the normal SDK path, not a fast-path set.
    assert enqueued == ["1"]
    # No fast-path push happened.
    assert b.application.bot.sent == []


@pytest.mark.asyncio
async def test_no_approval_pending_lets_fastpath_fire(tmp_path, monkeypatch):
    # (b) No approval pending: a bare "1" fast-paths normally (speed win kept).
    b = _make_bot()
    handler = _write_handler(tmp_path, 'echo "rendered table"\nexit 0\n')
    monkeypatch.setattr(config_mod, "FASTPATH_HANDLER", str(handler))
    monkeypatch.setattr(b, "_check_access", lambda update: _true())
    sm = _FakeSessionManager({904: {}})  # no pending prompt
    monkeypatch.setattr(bot_mod, "session_manager", sm)

    pushes = []

    async def mock_send_smart(chat_id, content, **kwargs):
        pushes.append((chat_id, content))

    monkeypatch.setattr(b, "_send_smart", mock_send_smart)

    enqueued = []

    async def spy_enqueue(uid, text, ts, update, **kw):
        enqueued.append(text)

    monkeypatch.setattr(b, "_enqueue_text_task", spy_enqueue)

    await b._handle_text_message(_MsgUpdate(904, "1"), None)
    await _await_all_tasks(b, 904)

    # Fast-path fired and pushed; the SDK path was suppressed.
    assert pushes == [(904, "rendered table")]
    assert enqueued == []


async def _true():
    return True
