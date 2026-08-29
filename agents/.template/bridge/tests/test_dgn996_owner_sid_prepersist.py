"""DGN-996: pre-persist the owner session id at SDK init time.

After a bridge restart the first turns are often INJECTED turns (cron-inject /
session-inbox) that never produce a ChatResponse, so bot._save_session_id (the
turn-completion save path) never runs and sessions.json keeps the OLD process's
session id.  status-footer's _is_owner_session gate then stays closed and the
workbench (dashboard.md) is never regenerated -- live subagents become
invisible indefinitely (measured, not the "benign residual / next turn catches
up" the old comment claimed).

Fix under test: sdk_bridge._persist_session_id flows the sid to sessions.json
the moment the SDK delivers it (init SystemMessage, both the pending-request
reader path and the no-pending proactive path, plus the injected-turn
ResultMessage), WITHOUT touching bot._runtime_active_sessions -- the
cross-process resume guard in bot._effective_session_id must keep refusing a
previous process's sid.

Covers:
  (a) init SystemMessage -> sessions.json updated immediately (both paths).
  (b) restart simulation: old sid on disk, new sid arrives -> disk updated.
  (c) turn-completion save path (bot._save_session_id) still works.
  (d) guard: pre-persist alone never opens _effective_session_id (resume),
      and never re-writes an old sid over a /new reset (dedupe marker).
"""

import asyncio
import json
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import ResultMessage, SystemMessage

import bridge.session as session_mod
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState
import bridge.sdk_bridge as sdk_mod


USER_ID = 7
OLD_SID = "3426b3ab-old-process-sid"
NEW_SID = "c3eb9789-new-process-sid"


def _read_store(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class _TempSessionEnv:
    """Real SessionManager backed by a temp sessions.json, patched into
    bridge.sdk_bridge so _persist_session_id writes to the temp store."""

    def __enter__(self):
        self._td = TemporaryDirectory()
        root = Path(self._td.name)
        self.store_path = root / "sessions.json"
        fake_config = types.SimpleNamespace(session_store_path=self.store_path)
        # SessionStore reads config.session_store_path at construction time.
        with patch.object(session_mod, "config", fake_config):
            self.manager = session_mod.SessionManager()
        self._patch = patch.object(sdk_mod, "session_manager", self.manager)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._td.cleanup()
        return False

    def seed(self, sid):
        asyncio.run(self.manager.update_session(USER_ID, {"session_id": sid}))


def _make_state() -> _UserStreamState:
    return _UserStreamState(client=MagicMock(), model=None)


def _init_system_message(sid: str) -> SystemMessage:
    return SystemMessage(subtype="init", data={"session_id": sid})


def _result_message(sid) -> ResultMessage:
    rm = MagicMock(spec=ResultMessage)
    rm.session_id = sid
    rm.is_error = False
    rm.result = ""
    return rm


class _OneShotClient:
    """Fake SDK client whose receive_messages yields the given messages once."""

    def __init__(self, messages):
        self._messages = list(messages)

    async def receive_messages(self):
        for m in self._messages:
            yield m


async def _pending_request() -> _PendingRequest:
    return _PendingRequest(
        user_id=USER_ID,
        chat_id=USER_ID,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=asyncio.get_event_loop().create_future(),
    )


# ---------------------------------------------------------------------------
# (a) init SystemMessage -> immediate persist
# ---------------------------------------------------------------------------


class TestInitPersistsImmediately(unittest.TestCase):
    def test_no_pending_proactive_path_persists_on_init(self):
        """The restart-critical path: injected turn, no pending request."""
        with _TempSessionEnv() as env:
            bridge = SdkBridge()
            state = _make_state()
            asyncio.run(
                bridge._handle_proactive_message(
                    USER_ID, state, _init_system_message(NEW_SID)
                )
            )
            self.assertEqual(state.last_session_id, NEW_SID)
            data = _read_store(env.store_path)
            self.assertEqual(
                data[f"telegram_session:{USER_ID}"]["session_id"], NEW_SID
            )

    def test_pending_reader_path_persists_on_init(self):
        """Normal turn: init SystemMessage with a pending request attached."""
        with _TempSessionEnv() as env:
            bridge = SdkBridge()
            state = _make_state()
            state.client = _OneShotClient([_init_system_message(NEW_SID)])

            async def run():
                state.pending.append(await _pending_request())
                await bridge._reader_loop(USER_ID, state)

            asyncio.run(run())
            data = _read_store(env.store_path)
            self.assertEqual(
                data[f"telegram_session:{USER_ID}"]["session_id"], NEW_SID
            )

    def test_injected_turn_result_message_persists(self):
        """ResultMessage of a no-pending injected turn also flows the sid."""
        with _TempSessionEnv() as env:
            bridge = SdkBridge()
            state = _make_state()
            state.proactive_push = AsyncMock()
            state.last_chat_id = USER_ID
            asyncio.run(
                bridge._handle_proactive_message(
                    USER_ID, state, _result_message(NEW_SID)
                )
            )
            data = _read_store(env.store_path)
            self.assertEqual(
                data[f"telegram_session:{USER_ID}"]["session_id"], NEW_SID
            )


# ---------------------------------------------------------------------------
# (b) restart simulation: old sid on disk, new sid arrives
# ---------------------------------------------------------------------------


class TestRestartOverwritesOldSid(unittest.TestCase):
    def test_old_sid_replaced_by_new_process_init(self):
        with _TempSessionEnv() as env:
            env.seed(OLD_SID)  # previous process left its sid behind
            bridge = SdkBridge()
            state = _make_state()  # fresh stream in the "new process"
            asyncio.run(
                bridge._handle_proactive_message(
                    USER_ID, state, _init_system_message(NEW_SID)
                )
            )
            data = _read_store(env.store_path)
            self.assertEqual(
                data[f"telegram_session:{USER_ID}"]["session_id"], NEW_SID
            )

    def test_persist_failure_is_silent_and_retried_next_signal(self):
        """A failing store must not crash the reader path, and must not set
        the dedupe marker (so the next signal retries the write)."""
        with _TempSessionEnv():
            bridge = SdkBridge()
            state = _make_state()
            boom = AsyncMock(side_effect=OSError("disk full"))
            with patch.object(sdk_mod.session_manager, "update_session", boom):
                asyncio.run(
                    bridge._handle_proactive_message(
                        USER_ID, state, _init_system_message(NEW_SID)
                    )
                )
            self.assertIsNone(state.persisted_session_id)  # marker NOT set
            # Next signal (retry) succeeds against the healthy store.
            asyncio.run(bridge._persist_session_id(USER_ID, state, NEW_SID))
            self.assertEqual(state.persisted_session_id, NEW_SID)


# ---------------------------------------------------------------------------
# (c) turn-completion save path still works (bot._save_session_id)
# ---------------------------------------------------------------------------


class TestTurnCompletionPathIntact(unittest.TestCase):
    def test_save_session_id_persists_and_arms_runtime_guard(self):
        from bridge import bot as bot_mod

        fake_self = types.SimpleNamespace(_runtime_active_sessions=set())
        response = types.SimpleNamespace(session_id=NEW_SID)
        fake_sessmgr = MagicMock()
        fake_sessmgr.update_session = AsyncMock()
        with patch.object(bot_mod, "session_manager", fake_sessmgr):
            asyncio.run(
                bot_mod.TelegramBot._save_session_id(fake_self, USER_ID, response)
            )
        fake_sessmgr.update_session.assert_awaited_once_with(
            USER_ID, {"session_id": NEW_SID}
        )
        self.assertIn(USER_ID, fake_self._runtime_active_sessions)

    def test_double_write_same_value_is_harmless(self):
        """Pre-persist then turn-completion persist of the SAME sid: the disk
        value stays correct and the second write is skipped by the dedupe."""
        with _TempSessionEnv() as env:
            bridge = SdkBridge()
            state = _make_state()
            asyncio.run(bridge._persist_session_id(USER_ID, state, NEW_SID))
            spy = AsyncMock(wraps=sdk_mod.session_manager.update_session)
            with patch.object(sdk_mod.session_manager, "update_session", spy):
                asyncio.run(bridge._persist_session_id(USER_ID, state, NEW_SID))
            spy.assert_not_awaited()  # dedupe: no redundant disk write
            data = _read_store(env.store_path)
            self.assertEqual(
                data[f"telegram_session:{USER_ID}"]["session_id"], NEW_SID
            )


# ---------------------------------------------------------------------------
# (d) cross-process resume guard unaffected
# ---------------------------------------------------------------------------


class TestResumeGuardIntact(unittest.TestCase):
    def test_prepersist_does_not_open_effective_session_id(self):
        """Disk freshness must NOT grant resume: _effective_session_id keeps
        returning None until a real turn completes (_runtime_active_sessions)."""
        from bridge import bot as bot_mod

        with _TempSessionEnv() as env:
            bridge = SdkBridge()
            state = _make_state()
            asyncio.run(
                bridge._handle_proactive_message(
                    USER_ID, state, _init_system_message(NEW_SID)
                )
            )
            # Fresh bot process: runtime set empty (pre-persist never adds).
            fake_self = types.SimpleNamespace(_runtime_active_sessions=set())
            session = _read_store(env.store_path)[f"telegram_session:{USER_ID}"]
            self.assertIsNone(
                bot_mod.TelegramBot._effective_session_id(
                    fake_self, USER_ID, session
                )
            )

    def test_stale_reader_cannot_rewrite_sid_over_new_reset(self):
        """/new nulls session_id on disk; a late signal from the OLD stream
        carrying the OLD sid must not resurrect it (dedupe marker holds it)."""
        with _TempSessionEnv() as env:
            bridge = SdkBridge()
            state = _make_state()
            # Old stream persisted its sid during normal operation.
            asyncio.run(bridge._persist_session_id(USER_ID, state, OLD_SID))
            # /new reset: bot nulls the persisted sid.
            asyncio.run(
                env.manager.update_session(USER_ID, {"session_id": None})
            )
            # Late tail signal from the old stream re-delivers OLD_SID.
            asyncio.run(bridge._persist_session_id(USER_ID, state, OLD_SID))
            data = _read_store(env.store_path)
            self.assertIsNone(
                data[f"telegram_session:{USER_ID}"]["session_id"]
            )


if __name__ == "__main__":
    unittest.main()
