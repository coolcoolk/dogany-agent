"""Tests for DGN-953 -- /btw silent empty-response observability.

Background: the user-facing BTW_FORK_FAILED notice was reachable via a fully
silent path -- run_fork_turn assembled zero TextBlocks, returned "", and
bot.py swapped in the failure message with ZERO log lines. DGN-953 adds
cause-signal logging (DGN-876 fold-drop enumeration pattern) so the next
reproduction is diagnosable from the log alone.

Covers:
  - _run_first_turn: ResultMessage-only stream (no AssistantMessage at all)
    -> one WARNING line with assistant_msgs=0 / text_blocks=0 / result_seen.
  - _run_first_turn: TextBlocks present but guards strip every block
    -> WARNING with text_blocks>0, raw_len>0, guarded_len=0.
  - _run_first_turn: normal non-empty response -> NO DGN-953 log, response
    text returned unchanged (no behavior change on the healthy path).
  - _run_continuation_turn: empty stream -> WARNING tagged "continuation".
  - bot.py run_fork_task: empty response from run_fork_turn -> WARNING
    logged before the BTW_FORK_FAILED fallback (previously zero-log).
  - bot.py run_fork_continuation: same for the continuation fallback.
  - bot.py: non-empty response -> no DGN-953 warning (healthy path silent).
  - Privacy: the diagnostic line carries counts/lengths only, never the
    question or response content.

Does NOT import a live PTB application; mirrors the test_dgn902 harness.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from bridge import messages
from bridge.btw import BtwForkManager, BtwForkState


# ---------------------------------------------------------------------------
# SDK message fabrication (bypass dataclass required-field churn)
# ---------------------------------------------------------------------------

def _mk_assistant(blocks, session_id=None, parent_tool_use_id=None):
    msg = object.__new__(AssistantMessage)
    msg.content = blocks
    msg.session_id = session_id
    msg.parent_tool_use_id = parent_tool_use_id
    return msg


def _mk_result(session_id="fork-sid"):
    msg = object.__new__(ResultMessage)
    msg.session_id = session_id
    return msg


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient: replays a fixed message list."""

    def __init__(self, msgs):
        self._msgs = msgs
        self.queried = []

    async def connect(self):
        pass

    async def query(self, question, session_id=None):
        self.queried.append((question, session_id))

    async def receive_messages(self):
        for m in self._msgs:
            yield m

    async def disconnect(self):
        pass


def _mk_fork(**kw):
    defaults = dict(anchor_message_id=1, spawned_from_session_id="main-sid")
    defaults.update(kw)
    return BtwForkState(**defaults)


# ---------------------------------------------------------------------------
# btw.py: empty-turn diagnostic line
# ---------------------------------------------------------------------------

class TestBtwEmptyTurnDiagnostic(unittest.IsolatedAsyncioTestCase):
    """_run_first_turn / _run_continuation_turn emit one enumerated
    cause-signal WARNING when the assembled text is empty."""

    async def test_first_turn_result_only_logs_zero_counts(self):
        """Stream = ResultMessage only: assistant_msgs=0, text_blocks=0,
        result_seen=True must all appear in one log line."""
        mgr = BtwForkManager()
        fork = _mk_fork()
        client = _FakeClient([_mk_result("fork-sid")])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            with self.assertLogs("bridge.btw", level="WARNING") as cm:
                result = await mgr._run_first_turn(1, fork, "why empty?")
        self.assertEqual(result, "")
        line = "\n".join(cm.output)
        self.assertIn("DGN-953", line)
        self.assertIn("first", line)
        self.assertIn("assistant_msgs=0", line)
        self.assertIn("text_blocks=0", line)
        self.assertIn("raw_len=0", line)
        self.assertIn("guarded_len=0", line)
        self.assertIn("result_seen=True", line)
        self.assertIn("fork_session_found=True", line)

    async def test_first_turn_guard_full_strip_logs_raw_vs_guarded(self):
        """TextBlocks arrived but the register guard stripped every block:
        text_blocks>0 and raw_len>0 with guarded_len=0 disambiguates the
        guard-strip cause from the no-blocks cause."""
        mgr = BtwForkManager()
        fork = _mk_fork()
        raw_text = "english-only leak text"
        client = _FakeClient([
            _mk_assistant([TextBlock(text=raw_text)], session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             patch("bridge.btw._register_guard", side_effect=lambda t: ""):
            with self.assertLogs("bridge.btw", level="WARNING") as cm:
                result = await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(result, "")
        line = "\n".join(cm.output)
        self.assertIn("DGN-953", line)
        self.assertIn("assistant_msgs=1", line)
        self.assertIn("text_blocks=1", line)
        self.assertIn(f"raw_len={len(raw_text)}", line)
        self.assertIn("guarded_len=0", line)

    async def test_first_turn_privacy_no_content_in_log(self):
        """The diagnostic line must not carry the question or block text."""
        mgr = BtwForkManager()
        fork = _mk_fork()
        secret = "SECRET-BLOCK-CONTENT"
        client = _FakeClient([
            _mk_assistant([TextBlock(text=secret)], session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             patch("bridge.btw._register_guard", side_effect=lambda t: ""):
            with self.assertLogs("bridge.btw", level="WARNING") as cm:
                await mgr._run_first_turn(1, fork, "SECRET-QUESTION-TEXT")
        line = "\n".join(cm.output)
        self.assertNotIn(secret, line)
        self.assertNotIn("SECRET-QUESTION-TEXT", line)

    async def test_first_turn_normal_response_no_dgn953_log(self):
        """Healthy path: response returned unchanged, no DGN-953 line."""
        mgr = BtwForkManager()
        fork = _mk_fork()
        client = _FakeClient([
            _mk_assistant([TextBlock(text="hello world")], session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            with self.assertNoLogs("bridge.btw", level="WARNING"):
                result = await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(result, "hello world")
        self.assertTrue(fork.initialized)
        self.assertEqual(fork.fork_session_id, "fork-sid")

    async def test_continuation_turn_empty_logs_continuation_tag(self):
        """Continuation empty turn logs with the 'continuation' tag and the
        fork_session_found flag (False when the id was never discovered)."""
        mgr = BtwForkManager()
        fork = _mk_fork(initialized=True, fork_session_id=None)
        fork.client = _FakeClient([_mk_result("x")])
        with self.assertLogs("bridge.btw", level="WARNING") as cm:
            result = await mgr._run_continuation_turn(1, fork, "follow-up")
        self.assertEqual(result, "")
        line = "\n".join(cm.output)
        self.assertIn("DGN-953", line)
        self.assertIn("continuation", line)
        self.assertIn("fork_session_found=False", line)

    async def test_continuation_turn_normal_no_dgn953_log(self):
        mgr = BtwForkManager()
        fork = _mk_fork(initialized=True, fork_session_id="fsid")
        fork.client = _FakeClient([
            _mk_assistant([TextBlock(text="follow-up answer")]),
            _mk_result("fsid"),
        ])
        with self.assertNoLogs("bridge.btw", level="WARNING"):
            result = await mgr._run_continuation_turn(1, fork, "follow-up")
        self.assertEqual(result, "follow-up answer")


# ---------------------------------------------------------------------------
# bot.py: fallback warning (previously zero-log)
# ---------------------------------------------------------------------------

from bridge import bot as _bot  # noqa: E402 -- after conftest bootstrap


def _make_update(user_id=1, reply_to_mid=None):
    user = MagicMock()
    user.id = user_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    message.message_id = 100
    if reply_to_mid is not None:
        reply_to = MagicMock()
        reply_to.message_id = reply_to_mid
        message.reply_to_message = reply_to
    else:
        message.reply_to_message = None
    chat = MagicMock()
    chat.id = 42
    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.message = message
    return update


def _make_context(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


def _make_bot_with_app():
    instance = object.__new__(_bot.TelegramBot)
    instance._btw_forks = BtwForkManager()
    instance._user_run_tasks = {}
    instance._btw_fork_tasks = {}
    instance.application = MagicMock()
    instance.application.bot = AsyncMock()
    instance.application.bot.edit_message_text = AsyncMock()
    instance.application.bot.send_message = AsyncMock()
    instance._drain_pending_texts = AsyncMock()
    instance._check_access = AsyncMock(return_value=True)
    return instance


class TestBotEmptyFallbackWarning(unittest.IsolatedAsyncioTestCase):
    """bot.py `if not response` fallbacks must log one WARNING (DGN-953)."""

    async def _run_cmd_btw(self, bot, update, args):
        completed = []

        def _fake_track(uid, task):
            completed.append(task)

        with patch.object(bot, "_effective_session_id", return_value="session-abc"), \
             patch("bridge.session.session_manager.get_session",
                   new=AsyncMock(return_value={"session_id": "session-abc"})), \
             patch.object(bot, "_track_btw_fork_task", side_effect=_fake_track):
            await bot._cmd_btw(update, _make_context(args=args))
        self.assertEqual(len(completed), 1)
        return completed[0]

    async def test_fork_task_empty_response_logs_warning(self):
        bot = _make_bot_with_app()
        update = _make_update()
        thinking_msg = MagicMock()
        thinking_msg.message_id = 999
        update.message.reply_text = AsyncMock(return_value=thinking_msg)
        bot._btw_forks.run_fork_turn = AsyncMock(return_value="")

        task = await self._run_cmd_btw(bot, update, ["why empty?"])
        with self.assertLogs("bridge.bot", level="WARNING") as cm:
            await task
        line = "\n".join(cm.output)
        self.assertIn("DGN-953", line)
        self.assertIn("empty response", line)
        # The user-facing fallback text is unchanged.
        edit_kwargs = bot.application.bot.edit_message_text.call_args.kwargs
        self.assertIn(messages.BTW_FORK_FAILED, edit_kwargs.get("text", ""))

    async def test_fork_task_normal_response_no_dgn953_warning(self):
        bot = _make_bot_with_app()
        update = _make_update()
        thinking_msg = MagicMock()
        thinking_msg.message_id = 999
        update.message.reply_text = AsyncMock(return_value=thinking_msg)
        bot._btw_forks.run_fork_turn = AsyncMock(return_value="a fine answer")

        task = await self._run_cmd_btw(bot, update, ["all good?"])
        with self.assertNoLogs("bridge.bot", level="WARNING"):
            await task
        edit_kwargs = bot.application.bot.edit_message_text.call_args.kwargs
        self.assertIn("a fine answer", edit_kwargs.get("text", ""))

    async def test_fork_continuation_empty_response_logs_warning(self):
        bot = _make_bot_with_app()
        user_id = 1
        anchor_mid = 777
        fork = _mk_fork(anchor_message_id=anchor_mid,
                        spawned_from_session_id="session-xyz")
        bot._btw_forks.register_fork(user_id, fork)
        bot._btw_forks.run_fork_turn = AsyncMock(return_value="")
        update = _make_update(user_id=user_id, reply_to_mid=anchor_mid)

        completed = []

        def _fake_track(uid, task):
            completed.append(task)

        with patch.object(bot, "_track_btw_fork_task", side_effect=_fake_track):
            claimed = await bot._maybe_route_btw_reply(
                user_id, "follow-up", update.message, update
            )
        self.assertTrue(claimed)
        with self.assertLogs("bridge.bot", level="WARNING") as cm:
            await completed[0]
        line = "\n".join(cm.output)
        self.assertIn("DGN-953", line)
        self.assertIn("continuation", line)
        send_kwargs = bot.application.bot.send_message.call_args.kwargs
        self.assertIn(messages.BTW_FORK_FAILED, send_kwargs.get("text", ""))


if __name__ == "__main__":
    unittest.main()
