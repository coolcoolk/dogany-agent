"""DGN-991: /stop copy -- owner-approved final contract (2026-09-03).

The /stop reply is exactly ONE sentence, nothing appended:
  "진행하던 작업을 멈췄습니다." / "Stopped what was running."

Prior drafts appended a standing background-work warning (stop_bg_note) that
was untestable-false in both directions: DGN-991 measured that in-session
Task subagents die on the soft interrupt while dispatch-detached.sh work
(its own setsid session) survives it, so neither "all die" nor "all survive"
is a true blanket claim. That key is retired.

stop_forced (hard-teardown result) is unified to the SAME sentence: the hard
path only kills the CLI subprocess, the bridge stays up and keeps the user's
session_id live, so the owner's next message resumes the same conversation
either way -- there is no honest basis for different copy on the two paths.

bg_subagent_killed_notice (DGN-1015, owner-approved 2026-08-24) is the
surviving fact-based signal: it fires ONLY when a tracked in-session
subagent is confirmed dead, and stays wired in _cmd_stop unchanged.

Root fix (background work outside the session process) is v2.0 -- out of
scope here by ticket lock.
"""

import asyncio
import importlib.util
import os
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
    def test_stop_bg_note_key_is_retired(self):
        assert "stop_bg_note" not in ko.STRINGS
        assert "stop_bg_note" not in en.STRINGS
        assert not hasattr(messages, "STOP_BG_NOTE")

    def test_stop_interrupted_is_exactly_one_sentence(self):
        for loc in (ko.STRINGS, en.STRINGS):
            text = loc["stop_interrupted"]
            # One sentence: exactly one terminal period, at the end.
            assert text.count(".") == 1
            assert text.endswith(".")

    def test_stop_interrupted_makes_no_background_or_kill_claim(self):
        """The retired stop_bg_note vocabulary must not resurface merged
        into stop_interrupted."""
        ko_text = ko.STRINGS["stop_interrupted"]
        en_text = en.STRINGS["stop_interrupted"].lower()
        for banned in ("백그라운드", "중단됩니다", "강제 종료", "새 메시지"):
            assert banned not in ko_text, ko_text
        for banned in ("background", "force", "kill", "new message"):
            assert banned not in en_text, en_text

    def test_stop_forced_equals_stop_interrupted(self):
        """Unification contract: hard-teardown result reads identically to
        the soft-success reply -- the owner sees the same outcome either
        way (session resumes on the next message)."""
        assert ko.STRINGS["stop_forced"] == ko.STRINGS["stop_interrupted"]
        assert en.STRINGS["stop_forced"] == en.STRINGS["stop_interrupted"]

    def test_stop_paused_equals_stop_interrupted(self):
        """Owner decision 2026-09-03: /stop ALWAYS cancels the in-flight turn
        AND clears the queue -- the queue-only branch is a post-hoc read of
        what happened to exist, not a separate mode. A queued instruction is
        still work the owner asked for, so it reads the same. Four /stop
        branches collapse to two: something existed -> this line; genuinely
        nothing -> stop_nothing."""
        assert ko.STRINGS["stop_paused"] == ko.STRINGS["stop_interrupted"]
        assert en.STRINGS["stop_paused"] == en.STRINGS["stop_interrupted"]

    def test_stop_nothing_stays_distinct(self):
        """The one surviving /stop distinction: genuinely nothing to stop.
        Collapsing this one too would make the reply lie in the idle case."""
        assert ko.STRINGS["stop_nothing"] != ko.STRINGS["stop_interrupted"]
        assert en.STRINGS["stop_nothing"] != en.STRINGS["stop_interrupted"]

    def test_exact_approved_wording(self):
        """Locked wording -- do not rephrase without a new owner approval."""
        assert ko.STRINGS["stop_interrupted"] == "진행하던 작업을 멈췄습니다."
        assert en.STRINGS["stop_interrupted"] == "Stopped what was running."

    def test_bg_subagent_killed_notice_survives(self):
        """This is the load-bearing premise of the DGN-991 simplification:
        the fact-based kill notice stays, so dropping the standing warning
        does not remove all signal when something actually died."""
        for loc in (ko.STRINGS, en.STRINGS):
            assert loc["bg_subagent_killed_notice"].strip()
        assert "{names}" in ko.STRINGS["bg_subagent_killed_notice"]
        assert "{names}" in en.STRINGS["bg_subagent_killed_notice"]

    def test_no_raw_markdown_or_headers(self):
        """Telegram contract: no # headers, no raw md tables in the copy."""
        for loc in (ko.STRINGS, en.STRINGS):
            for key in ("stop_interrupted", "stop_forced"):
                assert not loc[key].lstrip().startswith("#")
                assert "|--" not in loc[key]

    def test_ko_en_key_sets_match(self):
        assert set(ko.STRINGS.keys()) == set(en.STRINGS.keys())


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
        mock_sdk.pop_interrupt_killed = MagicMock(return_value=[])
        return mock_sdk

    def test_first_stop_soft_success_is_bare_interrupted_copy(self):
        async def scenario():
            mock_sdk = self._mock_sdk(interrupt_result=True)
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                await bot._cmd_stop(update, None)
            update.message.reply_text.assert_awaited_once_with(
                messages.STOP_INTERRUPTED
            )

        asyncio.run(scenario())

    def test_first_stop_with_confirmed_kill_appends_kill_notice(self):
        async def scenario():
            mock_sdk = self._mock_sdk(interrupt_result=True)
            mock_sdk.pop_interrupt_killed = MagicMock(
                return_value=["DGN-991 test build"]
            )
            TelegramBot, access, sdk = self._patched_bot(mock_sdk)
            with access, sdk:
                bot = TelegramBot()
                update = self._make_update()
                await bot._cmd_stop(update, None)
            expected = (
                f"{messages.STOP_INTERRUPTED}\n"
                + messages.BG_SUBAGENT_KILLED_NOTICE.format(
                    names="DGN-991 test build"
                )
            )
            update.message.reply_text.assert_awaited_once_with(expected)

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

    def test_cleared_only_uses_paused_copy(self):
        """Queue-only clear still routes through STOP_PAUSED -- which since
        2026-09-03 reads identically to STOP_INTERRUPTED (see the unification
        test above). The branch stays in code because stop_nothing must remain
        reachable; only the copy collapsed."""
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
