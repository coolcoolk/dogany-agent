"""DGN-991 stopgap: /stop copy honesty.

A (rev3): the FIRST /stop (soft-interrupt success) carries the honest
   background note -- the soft interrupt itself aborts the in-flight turn's
   tree, so its subagents die at the FIRST stop (measured, 2026-08-21
   incident log: soft interrupt only, no hard kill, subagents dead) --
   plus the true second-/stop escalation fact, without inventing a task
   count (no live-task registry exists).
B: the hard-teardown result uses a SEPARATE honest copy (stop_forced); the
   soft copies (stop_paused / stop_interrupted, "session intact") stay
   unchanged and are only used where they are true. cleared-only (queued
   messages dropped, no live process touched) keeps stop_paused.

Root fix (background work outside the session process) is v2.0 -- out of
scope here by ticket lock.
"""

import asyncio
import importlib.util
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

mock_sdk_pkg = MagicMock()
mock_sdk_pkg.PermissionResultAllow = MagicMock
mock_sdk_pkg.PermissionResultDeny = MagicMock
sys.modules.setdefault("claude_agent_sdk", mock_sdk_pkg)

from bridge import messages  # noqa: E402
from bridge.i18n import en, ko  # noqa: E402

USER_ID = 42


# ---------------------------------------------------------------------------
# 1. Copy keys: presence, mirroring, honesty constraints
# ---------------------------------------------------------------------------


class TestStopCopyKeys:
    def test_keys_exist_in_both_locales(self):
        for key in ("stop_bg_note", "stop_forced"):
            assert ko.STRINGS[key].strip(), f"ko {key} empty"
            assert en.STRINGS[key].strip(), f"en {key} empty"

    def test_hard_copy_differs_from_soft_copy(self):
        for loc in (ko.STRINGS, en.STRINGS):
            assert loc["stop_forced"] != loc["stop_paused"]
            assert loc["stop_forced"] != loc["stop_interrupted"]

    def test_hard_copy_never_claims_session_intact(self):
        """The false claim this ticket fixes must not reappear."""
        assert "세션과 대화는 그대로" not in ko.STRINGS["stop_forced"]
        assert "session and conversation are intact" not in en.STRINGS[
            "stop_forced"
        ].lower()

    def test_bg_note_names_no_count(self):
        """No live-task registry exists; the note must not invent a
        number of background tasks. (Digits appear only inside '/stop'.)"""
        for loc in (ko.STRINGS, en.STRINGS):
            text = loc["stop_bg_note"].replace("/stop", "")
            assert re.search(r"\d", text) is None, text

    def test_bg_note_offers_an_action(self):
        """Dead-end rule: the warning carries a way forward (new message)."""
        assert "새 메시지" in ko.STRINGS["stop_bg_note"]
        assert "message" in en.STRINGS["stop_bg_note"].lower()

    def test_bg_note_states_immediate_consequence(self):
        """rev3 regression: the background death happens at the FIRST stop
        (soft interrupt aborts the turn tree). The note must state it as an
        immediate fact and must NOT defer it to the second /stop (the
        disproven claim of the retired stop_forewarn copy)."""
        assert "함께 중단됩니다" in ko.STRINGS["stop_bg_note"]
        assert "그때" not in ko.STRINGS["stop_bg_note"]
        assert "ends with it" in en.STRINGS["stop_bg_note"].lower()

    def test_no_raw_markdown_or_headers(self):
        """Telegram contract: no # headers, no raw md tables in the copy."""
        for loc in (ko.STRINGS, en.STRINGS):
            for key in ("stop_bg_note", "stop_forced"):
                assert not loc[key].lstrip().startswith("#")
                assert "|--" not in loc[key]


# ---------------------------------------------------------------------------
# 2. Wiring: _cmd_stop / _hard_stop reply selection
# ---------------------------------------------------------------------------


class TestStopWiring:
    def _make_update(self):
        update = MagicMock()
        update.effective_user.id = USER_ID
        update.message.reply_text = AsyncMock()
        return update

    def _patched_bot(self, mock_sdk):
        from bridge.bot import TelegramBot

        access = patch.object(
            TelegramBot, "_check_access", new=AsyncMock(return_value=True)
        )
        sdk = patch("bridge.bot.sdk_bridge", mock_sdk)
        return TelegramBot, access, sdk

    def _mock_sdk(self, interrupt_result=None, stop_result=False):
        mock_sdk = MagicMock()
        mock_sdk.interrupt = AsyncMock(return_value=interrupt_result)
        mock_sdk.stop = AsyncMock(return_value=stop_result)
        mock_sdk.cancel_user_streaming = AsyncMock(return_value=False)
        return mock_sdk

    def test_first_stop_soft_success_carries_bg_note(self):
        async def scenario():
            mock_sdk = self._mock_sdk(interrupt_result=True)
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                await bot._cmd_stop(update, None)
            sent = update.message.reply_text.await_args.args[0]
            assert messages.STOP_INTERRUPTED in sent
            assert messages.STOP_BG_NOTE in sent

        asyncio.run(scenario())

    def test_hard_teardown_kill_uses_forced_copy(self):
        async def scenario():
            mock_sdk = self._mock_sdk(interrupt_result=False, stop_result=True)
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                await bot._cmd_stop(update, None)
            update.message.reply_text.assert_awaited_once_with(
                messages.STOP_FORCED
            )

        asyncio.run(scenario())

    def test_cleared_only_keeps_old_paused_copy(self):
        """Queue-only clear touches no live process: old copy is still true."""
        async def scenario():
            mock_sdk = self._mock_sdk(interrupt_result=False, stop_result=False)
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                with patch.object(
                    TelegramBot, "_clear_user_queue", return_value=True
                ):
                    await bot._cmd_stop(update, None)
            update.message.reply_text.assert_awaited_once_with(
                messages.STOP_PAUSED
            )

        asyncio.run(scenario())

    def test_idle_still_reports_nothing(self):
        async def scenario():
            mock_sdk = self._mock_sdk(interrupt_result=False, stop_result=False)
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                await bot._cmd_stop(update, None)
            update.message.reply_text.assert_awaited_once_with(
                messages.STOP_NOTHING
            )

        asyncio.run(scenario())
