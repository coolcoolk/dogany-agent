"""Tests for DGN-902 /btw command -- context-fork side conversation.

Covers:
  - Command registration: 'btw' in _setup_handlers and _set_bot_commands.
  - _cmd_btw: access-check gate (no-op when denied).
  - _cmd_btw: empty question -> BTW_NO_QUESTION reply.
  - _cmd_btw: no active session -> BTW_NO_SESSION reply.
  - _cmd_btw: session present -> fork state registered, thinking message sent
    as reply-to, fork task launched.
  - _maybe_route_btw_reply: non-reply message -> returns False (no fork routing).
  - _maybe_route_btw_reply: reply to unknown message_id -> returns False.
  - _maybe_route_btw_reply: reply to known fork anchor -> returns True
    (fork continuation routed).
  - BTW fork isolation: BtwForkManager.lookup_fork returns None for unregistered
    message_ids and the registered state after register_fork.
  - Message constants: all BTW_* constants importable and non-empty.
  - Marker rendering: BTW_MARKER starts with the 💭 emoji.

Does NOT import TelegramBot (live PTB application not available in test env).
Exercises the handler logic via a minimal async harness that stubs PTB objects.
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import messages
from bridge.btw import BtwForkManager, BtwForkState


# ---------------------------------------------------------------------------
# Minimal PTB stubs
# ---------------------------------------------------------------------------

def _make_update(user_id: int = 1, reply_to_mid: int = None):
    """Return a minimal fake PTB Update with an effective_user and message."""
    user = MagicMock()
    user.id = user_id

    message = MagicMock()
    message.reply_text = AsyncMock()
    message.message_id = 100  # the /btw command message_id

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


# ---------------------------------------------------------------------------
# Import the handler under test (after conftest bootstrap)
# ---------------------------------------------------------------------------

from bridge import bot as _bot


# ---------------------------------------------------------------------------
# Helper: build a TelegramBot instance without calling __init__
# ---------------------------------------------------------------------------

def _make_bot():
    instance = object.__new__(_bot.TelegramBot)
    # Inject the btw fork manager (normally set in __init__).
    from bridge.btw import BtwForkManager
    instance._btw_forks = BtwForkManager()
    # Stub _user_run_tasks and helpers used by _track_user_task.
    instance._user_run_tasks = {}
    return instance


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class TestBtwRegistration(unittest.TestCase):
    """_setup_handlers registers 'btw'; _set_bot_commands includes it."""

    def _collect_registered_commands(self):
        from telegram.ext import CommandHandler as _CH
        app = MagicMock()
        handlers_added = []

        def record_add(handler, **_kw):
            handlers_added.append(handler)

        app.add_handler.side_effect = record_add

        bot_instance = object.__new__(_bot.TelegramBot)
        bot_instance.application = app
        bot_instance._setup_handlers()

        names = set()
        for h in handlers_added:
            if isinstance(h, _CH):
                names.update(h.commands)
        return names

    def test_btw_handler_registered(self):
        names = self._collect_registered_commands()
        self.assertIn("btw", names)

    def test_btw_in_bot_commands_list(self):
        from telegram import BotCommand as _BC
        app = MagicMock()
        app.bot = AsyncMock()
        app.bot.delete_my_commands = AsyncMock()
        app.bot.set_my_commands = AsyncMock()

        bot_instance = object.__new__(_bot.TelegramBot)
        bot_instance.application = app

        asyncio.run(bot_instance._set_bot_commands())

        set_calls = app.bot.set_my_commands.call_args_list
        self.assertTrue(set_calls, "set_my_commands was never called")
        commands_arg = set_calls[0][0][0]
        cmd_names = {c.command for c in commands_arg}
        self.assertIn("btw", cmd_names)

    def test_btw_registered_before_help(self):
        """btw must appear before help in the command list (ordering check)."""
        from telegram import BotCommand as _BC
        app = MagicMock()
        app.bot = AsyncMock()
        app.bot.delete_my_commands = AsyncMock()
        app.bot.set_my_commands = AsyncMock()

        bot_instance = object.__new__(_bot.TelegramBot)
        bot_instance.application = app

        asyncio.run(bot_instance._set_bot_commands())

        commands_arg = app.bot.set_my_commands.call_args_list[0][0][0]
        names = [c.command for c in commands_arg]
        self.assertIn("btw", names)
        self.assertIn("help", names)
        self.assertLess(names.index("btw"), names.index("help"))


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------

class TestBtwHandler(unittest.IsolatedAsyncioTestCase):
    """_cmd_btw handler: access gate, missing question, no session, and fork start."""

    async def _call_btw(self, bot_instance, update, ctx, *, access=True):
        with patch.object(
            bot_instance, "_check_access", new=AsyncMock(return_value=access)
        ):
            await bot_instance._cmd_btw(update, ctx)

    async def test_access_denied_no_reply(self):
        bot = _make_bot()
        update = _make_update()
        await self._call_btw(bot, update, _make_context(), access=False)
        update.message.reply_text.assert_not_called()

    async def test_empty_question_sends_usage(self):
        bot = _make_bot()
        update = _make_update()
        await self._call_btw(bot, update, _make_context(args=[]))
        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.BTW_NO_QUESTION, calls)

    async def test_whitespace_only_question_sends_usage(self):
        bot = _make_bot()
        update = _make_update()
        await self._call_btw(bot, update, _make_context(args=["  "]))
        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.BTW_NO_QUESTION, calls)

    async def test_no_active_session_sends_no_session(self):
        """When there is no active session_id, reply BTW_NO_SESSION."""
        bot = _make_bot()
        update = _make_update()

        # _effective_session_id returns None when no active session.
        with patch.object(
            bot,
            "_effective_session_id",
            return_value=None,
        ), patch(
            "bridge.session.session_manager.get_session",
            new=AsyncMock(return_value={}),
        ):
            await self._call_btw(bot, update, _make_context(args=["hello"]))

        calls = [c.args[0] for c in update.message.reply_text.call_args_list]
        self.assertIn(messages.BTW_NO_SESSION, calls)

    async def test_active_session_registers_fork_and_sends_thinking(self):
        """When session is active, a thinking message is sent as reply-to and
        the fork state is registered in the manager."""
        bot = _make_bot()
        update = _make_update()

        thinking_msg = MagicMock()
        thinking_msg.message_id = 999  # anchor message_id

        update.message.reply_text = AsyncMock(return_value=thinking_msg)

        def _fake_track(user_id, task):
            # Immediately cancel the task so the test does not actually run
            # the fork SDK turn (which requires a live SDK process).
            task.cancel()

        with patch.object(
            bot, "_effective_session_id", return_value="session-abc"
        ), patch(
            "bridge.session.session_manager.get_session",
            new=AsyncMock(return_value={"session_id": "session-abc"}),
        ), patch.object(
            bot, "_track_user_task", side_effect=_fake_track
        ):
            await self._call_btw(bot, update, _make_context(args=["quick question"]))

        # thinking reply must have been sent as a reply-to the /btw message
        update.message.reply_text.assert_called_once()
        call_kwargs = update.message.reply_text.call_args
        self.assertEqual(call_kwargs.args[0], messages.BTW_THINKING)

        # fork must be registered under the anchor message_id
        user_id = update.effective_user.id
        fork = bot._btw_forks.lookup_fork(user_id, 999)
        self.assertIsNotNone(fork)
        self.assertEqual(fork.spawned_from_session_id, "session-abc")
        self.assertEqual(fork.anchor_message_id, 999)


# ---------------------------------------------------------------------------
# Reply-to routing tests
# ---------------------------------------------------------------------------

class TestBtwReplyRouting(unittest.IsolatedAsyncioTestCase):
    """_maybe_route_btw_reply: non-reply, unknown anchor, and known anchor."""

    async def test_no_reply_to_returns_false(self):
        bot = _make_bot()
        update = _make_update(reply_to_mid=None)
        result = await bot._maybe_route_btw_reply(
            1, "hello", update.message, update
        )
        self.assertFalse(result)

    async def test_reply_to_unknown_anchor_returns_false(self):
        bot = _make_bot()
        update = _make_update(reply_to_mid=555)
        # No fork registered for message_id 555.
        result = await bot._maybe_route_btw_reply(
            1, "hello", update.message, update
        )
        self.assertFalse(result)

    async def test_reply_to_known_anchor_returns_true_and_dispatches(self):
        """A reply to a known fork anchor returns True and launches a fork task."""
        bot = _make_bot()
        user_id = 1
        anchor_mid = 777

        # Register a fork state anchored at 777.
        fork = BtwForkState(
            anchor_message_id=anchor_mid,
            spawned_from_session_id="session-xyz",
        )
        bot._btw_forks.register_fork(user_id, fork)

        update = _make_update(user_id=user_id, reply_to_mid=anchor_mid)
        bot.application = MagicMock()
        bot.application.bot = AsyncMock()
        bot.application.bot.send_message = AsyncMock()

        dispatched_tasks = []

        # _track_user_task is sync, not async.
        def _fake_track(uid, task):
            dispatched_tasks.append(task)
            task.cancel()

        with patch.object(bot, "_track_user_task", side_effect=_fake_track):
            result = await bot._maybe_route_btw_reply(
                user_id, "follow-up question", update.message, update
            )

        self.assertTrue(result)
        self.assertEqual(len(dispatched_tasks), 1)

    async def test_reply_to_non_fork_anchor_goes_to_main(self):
        """A reply to a message that is NOT a fork anchor falls through to
        main session routing (returns False)."""
        bot = _make_bot()
        user_id = 2
        anchor_mid = 100  # /btw message itself, not a 💭 bubble

        # No fork registered for message_id 100.
        update = _make_update(user_id=user_id, reply_to_mid=anchor_mid)
        result = await bot._maybe_route_btw_reply(
            user_id, "hello", update.message, update
        )
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# BtwForkManager unit tests
# ---------------------------------------------------------------------------

class TestBtwForkManager(unittest.TestCase):
    """BtwForkManager: registration, lookup, LRU eviction."""

    def test_lookup_returns_none_for_missing_anchor(self):
        mgr = BtwForkManager()
        result = mgr.lookup_fork(user_id=1, anchor_message_id=999)
        self.assertIsNone(result)

    def test_register_then_lookup_succeeds(self):
        mgr = BtwForkManager()
        state = BtwForkState(anchor_message_id=42, spawned_from_session_id="sid-1")
        mgr.register_fork(user_id=1, state=state)
        result = mgr.lookup_fork(user_id=1, anchor_message_id=42)
        self.assertIs(result, state)

    def test_different_users_isolated(self):
        """Forks registered for user A are not visible to user B."""
        mgr = BtwForkManager()
        state = BtwForkState(anchor_message_id=100, spawned_from_session_id="sid")
        mgr.register_fork(user_id=1, state=state)
        self.assertIsNone(mgr.lookup_fork(user_id=2, anchor_message_id=100))

    def test_lru_eviction_at_cap(self):
        """When the per-user cap is hit, the oldest entry is evicted."""
        from bridge.btw import _BTW_MAX_FORKS_PER_USER
        mgr = BtwForkManager()
        # Register exactly max forks.
        for i in range(_BTW_MAX_FORKS_PER_USER):
            state = BtwForkState(
                anchor_message_id=i, spawned_from_session_id=f"sid-{i}"
            )
            mgr.register_fork(user_id=1, state=state)

        # All max entries should be present.
        self.assertIsNotNone(mgr.lookup_fork(user_id=1, anchor_message_id=0))

        # Register one more -> should evict the oldest (anchor_message_id=0).
        overflow = BtwForkState(
            anchor_message_id=_BTW_MAX_FORKS_PER_USER,
            spawned_from_session_id="sid-overflow",
        )
        mgr.register_fork(user_id=1, state=overflow)
        self.assertIsNone(
            mgr.lookup_fork(user_id=1, anchor_message_id=0),
            "Oldest entry should have been evicted",
        )
        self.assertIsNotNone(
            mgr.lookup_fork(user_id=1, anchor_message_id=_BTW_MAX_FORKS_PER_USER)
        )


# ---------------------------------------------------------------------------
# Message constants tests
# ---------------------------------------------------------------------------

class TestBtwMessages(unittest.TestCase):
    """All BTW_* constants must be importable and non-empty."""

    def test_all_btw_constants_defined(self):
        attrs = [
            "BTW_MARKER",
            "BTW_NO_QUESTION",
            "BTW_NO_SESSION",
            "BTW_FORK_FAILED",
            "BTW_THINKING",
            "CMD_DESC_BTW",
        ]
        for attr in attrs:
            with self.subTest(attr=attr):
                val = getattr(messages, attr, None)
                self.assertIsNotNone(val, f"messages.{attr} is missing")
                self.assertIsInstance(val, str)
                self.assertTrue(val.strip(), f"messages.{attr} is empty")

    def test_btw_marker_starts_with_thought_bubble(self):
        """BTW_MARKER must start with the 💭 emoji (U+1F4AD)."""
        self.assertTrue(
            messages.BTW_MARKER.startswith("\U0001f4ad"),
            f"BTW_MARKER does not start with 💭: {repr(messages.BTW_MARKER)}",
        )

    def test_help_text_mentions_btw(self):
        """btw must appear in COMMAND_MENU_SPEC so it renders in /help output."""
        # DGN-919: help body is generated from COMMAND_MENU_SPEC; check the spec.
        from bridge.bot import COMMAND_MENU_SPEC
        cmd_names = [cmd for cmd, _ in COMMAND_MENU_SPEC]
        self.assertIn("btw", cmd_names)


if __name__ == "__main__":
    unittest.main()
