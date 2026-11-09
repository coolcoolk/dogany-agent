"""DGN-1523: a stop that lands must be followed by an actual resume attempt.

Root cause (measured, bot.log 2026-09-16/17, three soft-stop events, 0/3
resume queries): AUTO_RESUME=True and AUTO_RESUME_MAX=3 were confirmed live
(config import matching the deployed .env), and _timeout_stop_then_preserve
unconditionally sets timed_out=True -- so bot._auto_resume_loop's own first
two gate conditions were never the failure. The remaining, reproducible
failure mode is the resume sid: TestFinishTurnWithAutoResume below shows the
loop is a correct, silent no-op whenever _resolve_resume_sid comes back
empty, which is bit-for-bit what the log shows (no dispatch, no error, the
turn ends holding the ORIGINAL timed-out response).

Independently of that mystery, static reading of bot.py turned up a fully
confirmed, fully reproducible second defect: three of the five
sdk_bridge.process_message call sites (slash-command execution, the [[OPTIONS]]
button tap, and the [retry] button tap) never ran bot._auto_resume_loop at
all -- a turn that soft-stops on any of those three paths leaks
messages.TIMEOUT_PAUSED (a fact-only string, DGN-1523 copy fix below) straight
to the user with NO resume attempt and NO button. TestBypassPathsNowResume
pins the fix: every process_message caller now goes through the single gate
_finish_turn_with_auto_resume.
"""

import asyncio
import json
import types
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.session as session_mod
import bridge.bot as bot_mod
import bridge.sdk_bridge as sdk_mod
from bridge import messages
from bridge.sdk_bridge import ChatResponse, SdkBridge, _UserStreamState

USER_ID = 100000001  # synthetic fixture id -- never a real operator id
CHAT_ID = USER_ID


def run(coro):
    return asyncio.run(coro)


# --- shared disk-backed session store (mirrors test_dgn996's harness) -----

class _TempSessionEnv:
    """A REAL SessionManager over a temp sessions.json, patched into BOTH
    bridge.bot and bridge.sdk_bridge so both layers read/write the same
    on-disk store -- exactly the DGN-996 durability path this ticket relies
    on for its resume-sid fallback."""

    def __enter__(self):
        self._td = TemporaryDirectory()
        root = Path(self._td.name)
        self.store_path = root / "sessions.json"
        fake_config = types.SimpleNamespace(session_store_path=self.store_path)
        with patch.object(session_mod, "config", fake_config):
            self.manager = session_mod.SessionManager()
        self._patches = [
            patch.object(sdk_mod, "session_manager", self.manager),
            patch.object(bot_mod, "session_manager", self.manager),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        self._td.cleanup()
        return False

    def seed(self, user_id, **fields):
        run(self.manager.update_session(user_id, fields))


def _timed_out_response(resume_sid=None):
    return ChatResponse(
        content=messages.TIMEOUT_PAUSED.format(timeout=550),
        success=False,
        error="timeout",
        session_id=resume_sid,
        timed_out=True,
        resume_session_id=resume_sid,
        partial_preserved=False,
        streamed=False,
    )


def _ok_response(text="all done"):
    return ChatResponse(content=text, success=True)


def _make_bot():
    b = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    b._runtime_active_sessions = set()
    b._user_run_tasks = {}
    b._user_queue_locks = {}
    b._active_tasks = {}
    b._user_pending_texts = {}
    b._debounce_texts = {}
    b._debounce_timers = {}
    b._typing_refresh_tasks = {}
    b._interrupt_deferred_since = {}
    b.application = MagicMock()
    b.application.bot = MagicMock()
    b.application.bot.send_chat_action = AsyncMock()
    b.application.bot.send_message = AsyncMock()
    return b


# --- A: _finish_turn_with_auto_resume, the single gate ---------------------

class TestFinishTurnWithAutoResume:
    def test_resume_dispatched_when_sid_available(self):
        b = _make_bot()
        with _TempSessionEnv(), patch.object(bot_mod, "AUTO_RESUME", True), \
             patch.object(bot_mod, "AUTO_RESUME_MAX", 3), \
             patch.object(bot_mod.TelegramBot, "_send_guaranteed", new=AsyncMock(return_value=True)):
            resume_caller = AsyncMock(return_value=_ok_response("resumed fine"))
            result = run(b._finish_turn_with_auto_resume(
                user_id=USER_ID, chat_id=CHAT_ID,
                response=_timed_out_response("sid-live"),
                resume_caller=resume_caller,
            ))
        resume_caller.assert_awaited_once()
        assert result is not None
        assert result.content == "resumed fine"

    def test_no_dispatch_and_notice_sent_when_sid_unavailable_anywhere(self):
        """The measured defect, reproduced in isolation: NEITHER the primary
        sid (response.resume_session_id) NOR the disk fallback
        (session_manager) has anything -- the loop must not call
        resume_caller even once, and the caller must be told to stop (None),
        never handed the untouched timed-out response to reply with."""
        b = _make_bot()
        with _TempSessionEnv(), patch.object(bot_mod, "AUTO_RESUME", True), \
             patch.object(bot_mod, "AUTO_RESUME_MAX", 3), \
             patch.object(bot_mod.TelegramBot, "_send_guaranteed", new=AsyncMock(return_value=True)) as notice:
            resume_caller = AsyncMock(return_value=_ok_response())
            result = run(b._finish_turn_with_auto_resume(
                user_id=USER_ID, chat_id=CHAT_ID,
                response=_timed_out_response(None),
                resume_caller=resume_caller,
            ))
        resume_caller.assert_not_awaited()
        assert result is None
        notice.assert_awaited()
        # Whatever notice fired, it must NEVER be the raw fact-only
        # TIMEOUT_PAUSED string forwarded verbatim -- that string has no
        # button behind it at this layer.
        sent_texts = [c.args[1] for c in notice.await_args_list]
        assert all(messages.TIMEOUT_PAUSED.format(timeout=550) not in t for t in sent_texts)

    def test_disk_fallback_sid_lets_resume_proceed(self):
        """response.resume_session_id is empty but the disk store (DGN-996's
        durability layer) has one -- resume must still fire."""
        b = _make_bot()
        with _TempSessionEnv() as env, patch.object(bot_mod, "AUTO_RESUME", True), \
             patch.object(bot_mod, "AUTO_RESUME_MAX", 3), \
             patch.object(bot_mod.TelegramBot, "_send_guaranteed", new=AsyncMock(return_value=True)):
            env.seed(USER_ID, session_id="sid-from-disk")
            resume_caller = AsyncMock(return_value=_ok_response())
            result = run(b._finish_turn_with_auto_resume(
                user_id=USER_ID, chat_id=CHAT_ID,
                response=_timed_out_response(None),
                resume_caller=resume_caller,
            ))
        resume_caller.assert_awaited_once()
        assert result is not None

    def test_auto_resume_disabled_still_gates_content(self):
        """AUTO_RESUME off must still route through the notice, never leak
        response.content directly."""
        b = _make_bot()
        with _TempSessionEnv(), patch.object(bot_mod, "AUTO_RESUME", False), \
             patch.object(bot_mod.TelegramBot, "_send_guaranteed", new=AsyncMock(return_value=True)) as notice:
            resume_caller = AsyncMock(return_value=_ok_response())
            result = run(b._finish_turn_with_auto_resume(
                user_id=USER_ID, chat_id=CHAT_ID,
                response=_timed_out_response("sid-live"),
                resume_caller=resume_caller,
            ))
        resume_caller.assert_not_awaited()
        assert result is None
        notice.assert_awaited()

    def test_settled_response_passed_through_when_never_timed_out(self):
        b = _make_bot()
        resume_caller = AsyncMock()
        result = run(b._finish_turn_with_auto_resume(
            user_id=USER_ID, chat_id=CHAT_ID,
            response=_ok_response("no timeout here"),
            resume_caller=resume_caller,
        ))
        resume_caller.assert_not_awaited()
        assert result is not None
        assert result.content == "no timeout here"


# --- B: the three previously-bypassing call sites now resume ---------------

class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, chat):
        self.chat = chat
        self.date = datetime.now(timezone.utc)
        self.replies = []

    async def reply_text(self, text, *a, **k):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, chat_id=CHAT_ID, user_id=USER_ID):
        chat = _FakeChat(chat_id)
        self.effective_chat = chat
        self.effective_user = types.SimpleNamespace(id=user_id)
        self.message = _FakeMessage(chat)


class TestBypassPathsNowResume:
    """DGN-1523: slash commands, [[OPTIONS]] taps, and [retry] taps used to
    call sdk_bridge.process_message directly and forward response.content
    unconditionally -- a soft-stop on any of them never resumed and leaked
    the fact-only TIMEOUT_PAUSED string with no button behind it. All three
    now route through _finish_turn_with_auto_resume like the main text
    handler and the resume-button callback already did."""

    def _patched_process_message(self):
        return AsyncMock(side_effect=[_timed_out_response("sid-live"), _ok_response("resumed content")])

    def test_slash_command_resumes_before_replying(self):
        b = _make_bot()
        b._save_session_id = AsyncMock()
        b._drain_pending_texts = AsyncMock()
        b._permission_callback = AsyncMock()
        b._proactive_push = None
        update = _FakeUpdate()
        pm = self._patched_process_message()
        with _TempSessionEnv(), patch.object(bot_mod, "AUTO_RESUME", True), \
             patch.object(bot_mod, "AUTO_RESUME_MAX", 3), \
             patch.object(bot_mod.sdk_bridge, "process_message", pm), \
             patch.object(bot_mod.TelegramBot, "_send_guaranteed", new=AsyncMock(return_value=True)):
            run(b._exec_slash_command(update, "/health"))
        assert pm.await_count == 2, "the soft-stopped turn must get a resume dispatch"
        assert update.message.replies, "the caller must still reply once resumed"
        assert update.message.replies[0] == "resumed content"
        assert all(
            messages.TIMEOUT_PAUSED.format(timeout=550) not in r for r in update.message.replies
        ), "the fact-only timeout string must never reach the user verbatim"

    def test_retry_callback_resumes_before_replying(self):
        b = _make_bot()
        b._save_session_id = AsyncMock()
        b._drain_pending_texts = AsyncMock()
        b._permission_callback = AsyncMock()
        b._proactive_push = None
        sent = []
        b._send_smart = AsyncMock(side_effect=lambda chat_id, text, **k: sent.append(text))
        chat = _FakeChat(CHAT_ID)
        query = MagicMock()
        query.data = "retry:tok1"
        query.edit_message_text = AsyncMock()
        update = types.SimpleNamespace(effective_chat=chat)
        pm = self._patched_process_message()
        with _TempSessionEnv() as env, patch.object(bot_mod, "AUTO_RESUME", True), \
             patch.object(bot_mod, "AUTO_RESUME_MAX", 3), \
             patch.object(bot_mod.sdk_bridge, "process_message", pm), \
             patch.object(bot_mod.TelegramBot, "_send_guaranteed", new=AsyncMock(return_value=True)):
            env.seed(USER_ID, pending_retry={"token": "tok1", "user_message": "original text"})
            run(b._handle_retry_callback(update, query, USER_ID, chat))
        assert pm.await_count == 2, "the soft-stopped retry turn must get a resume dispatch"
        assert sent and sent[0] == "resumed content"
        assert all(messages.TIMEOUT_PAUSED.format(timeout=550) not in t for t in sent)

    def test_resume_button_callback_resumes_before_replying(self):
        """This call site already wired _auto_resume_loop pre-DGN-1523 -- kept
        under the SAME shared gate (_finish_turn_with_auto_resume) so a
        second soft-stop, mid tap-to-continue, still resumes instead of
        silently handing the owner a second dead button."""
        b = _make_bot()
        b._save_session_id = AsyncMock()
        b._drain_pending_texts = AsyncMock()
        b._permission_callback = AsyncMock()
        b._proactive_push = None
        b._runtime_active_sessions = set()
        sent = []
        b._send_smart = AsyncMock(side_effect=lambda chat_id, text, **k: sent.append(text))
        chat = _FakeChat(CHAT_ID)
        query = MagicMock()
        query.data = "resume:tok1"
        query.edit_message_text = AsyncMock()
        update = types.SimpleNamespace(effective_chat=chat)
        pm = self._patched_process_message()
        with _TempSessionEnv() as env, patch.object(bot_mod, "AUTO_RESUME", True), \
             patch.object(bot_mod, "AUTO_RESUME_MAX", 3), \
             patch.object(bot_mod.sdk_bridge, "process_message", pm), \
             patch.object(bot_mod.TelegramBot, "_send_guaranteed", new=AsyncMock(return_value=True)):
            env.seed(USER_ID, pending_resume={"token": "tok1", "session_id": "sid-live"})
            run(b._handle_resume_callback(update, query, USER_ID, chat))
        assert pm.await_count == 2, "a second soft-stop during resume must get another resume dispatch"
        assert sent and sent[0] == "resumed content"
        assert all(messages.TIMEOUT_PAUSED.format(timeout=550) not in t for t in sent)

    def test_options_callback_resumes_before_replying(self):
        b = _make_bot()
        b._save_session_id = AsyncMock()
        b._drain_pending_texts = AsyncMock()
        b._permission_callback = AsyncMock()
        b._proactive_push = None
        b._maybe_capture_outside_approval = AsyncMock()
        sent = []
        b._send_smart = AsyncMock(side_effect=lambda chat_id, text, **k: sent.append(text))
        chat = _FakeChat(CHAT_ID)
        msg = MagicMock()
        msg.reply_markup = None
        msg.date = datetime.now(timezone.utc)
        query = MagicMock()
        query.data = "opt:1"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = types.SimpleNamespace(
            effective_chat=chat,
            effective_user=types.SimpleNamespace(id=USER_ID),
            message=None,
            callback_query=query,
        )
        pm = self._patched_process_message()

        async def scenario():
            await b._handle_callback(update, MagicMock())
            # The opt: branch enqueues its turn as a background task.
            tasks = list(b._user_run_tasks.get(USER_ID, set()))
            await asyncio.gather(*tasks)

        with _TempSessionEnv(), patch.object(bot_mod, "AUTO_RESUME", True), \
             patch.object(bot_mod, "AUTO_RESUME_MAX", 3), \
             patch.object(bot_mod.sdk_bridge, "process_message", pm), \
             patch.object(bot_mod.TelegramBot, "_send_guaranteed", new=AsyncMock(return_value=True)), \
             patch.object(bot_mod.config, "allowed_user_ids", [USER_ID]):
            run(scenario())
        assert pm.await_count == 2, "the soft-stopped options turn must get a resume dispatch"
        assert sent and sent[0] == "resumed content"
        assert all(messages.TIMEOUT_PAUSED.format(timeout=550) not in t for t in sent)


# --- C: sdk_bridge-layer resume-sid durability ------------------------------

class TestTimeoutStopResumeSidFallback:
    """DGN-1523: _timeout_stop_then_preserve must capture SOME resume sid
    whenever any is available anywhere -- live state, the pending head, OR
    the disk-persisted session store -- instead of leaving the disk read to
    a second, later, independent bot.py-side fallback."""

    def test_falls_back_to_persisted_session_id_when_state_is_bare(self):
        async def scenario():
            bridge = SdkBridge()
            client = MagicMock()
            client.interrupt = AsyncMock()
            client._query = object()
            state = _UserStreamState(client=client, model=None)
            # Bare: no last_session_id, no pending head to derive one from.
            bridge._streams[USER_ID] = state
            from bridge.sdk_bridge import _PendingRequest

            req = _PendingRequest(
                user_id=USER_ID, chat_id=CHAT_ID, model=None,
                requested_session_id=None, permission_callback=None,
                typing_callback=None, future=asyncio.get_running_loop().create_future(),
                user_message="msg", sent=True,
            )
            state.pending.append(req)

            with _TempSessionEnv() as env, \
                 patch.object(sdk_mod, "PROCESS_TIMEOUT", 600), \
                 patch.object(sdk_mod, "TIMEOUT_STOP_GRACE", 20):
                await env.manager.update_session(USER_ID, {"session_id": "sid-on-disk"})
                response = await bridge._timeout_stop_then_preserve(USER_ID)

            assert response is not None
            assert response.resume_session_id == "sid-on-disk"

        run(scenario())

    def test_warns_when_no_sid_anywhere(self, caplog):
        async def scenario():
            bridge = SdkBridge()
            client = MagicMock()
            client.interrupt = AsyncMock()
            client._query = object()
            state = _UserStreamState(client=client, model=None)
            bridge._streams[USER_ID] = state
            from bridge.sdk_bridge import _PendingRequest

            req = _PendingRequest(
                user_id=USER_ID, chat_id=CHAT_ID, model=None,
                requested_session_id=None, permission_callback=None,
                typing_callback=None, future=asyncio.get_running_loop().create_future(),
                user_message="msg", sent=True,
            )
            state.pending.append(req)

            with _TempSessionEnv(), \
                 patch.object(sdk_mod, "PROCESS_TIMEOUT", 600), \
                 patch.object(sdk_mod, "TIMEOUT_STOP_GRACE", 20):
                response = await bridge._timeout_stop_then_preserve(USER_ID)

            assert response is not None
            assert response.resume_session_id is None
            return response

        import logging
        with caplog.at_level(logging.WARNING, logger="bridge.sdk_bridge"):
            run(scenario())
        assert any("no resume sid anywhere" in r.getMessage() for r in caplog.records)


# --- D: copy relocation -----------------------------------------------------

class TestTimeoutPausedCopyIsFactOnly:
    """DGN-1523: the tap instruction belongs solely in timeout_tap_notice
    (the layer that also creates the button). timeout_paused reaches layers
    that may never show a button at all (auto-resume success), so it must
    read as a plain statement of fact."""

    @pytest.mark.parametrize("lang", ["ko", "en"])
    def test_timeout_paused_has_no_tap_instruction(self, lang):
        import importlib
        mod = importlib.import_module(f"bridge.i18n.{lang}")
        text = mod.STRINGS["timeout_paused"]
        for banned in ("button", "Button", "tap", "Tap", "버튼", "누르"):
            assert banned not in text, f"{lang}.timeout_paused still instructs a tap: {text!r}"

    @pytest.mark.parametrize("lang", ["ko", "en"])
    def test_timeout_tap_notice_still_instructs_the_tap(self, lang):
        """The instruction did not vanish -- it stayed at the one place that
        creates the button."""
        import importlib
        mod = importlib.import_module(f"bridge.i18n.{lang}")
        text = mod.STRINGS["timeout_tap_notice"]
        assert any(w in text for w in ("Tap", "tap", "누르")), (
            f"{lang}.timeout_tap_notice lost its tap instruction: {text!r}"
        )
