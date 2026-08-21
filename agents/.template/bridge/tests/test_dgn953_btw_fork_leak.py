"""Tests for DGN-953 Phase-1b -- /btw fork CLI orphan-leak eradication.

Background: the first /btw fork turn kept exceeding BTW_TURN_TIMEOUT (120s).
On that path _run_first_turn raised without ever disconnecting the fork
client, and bot.py's run_fork_task swallowed the error -- the forked CLI
subprocess (claude --fork-session) stayed alive as a permanent orphan
(measured: 2 zombies ~640MB RAM), one new zombie per retry.

Phase-1b: on the timeout (and general exception) paths of
_run_first_turn / _run_continuation_turn, the fork client is quietly
disconnected and the fork is deregistered from BtwForkManager BEFORE the
original failure re-raises. Happy-path lifecycle (client reuse across
continuation turns) is unchanged.

Covers:
  (a) timeout -> fork client disconnect is called;
  (b) timeout -> fork deregistered (lookup_fork returns None);
  (c) disconnect itself raising is swallowed; the original timeout still
      propagates to the caller unchanged;
  (d) happy path untouched: normal turn keeps the client connected, the fork
      stays registered, and the continuation turn reuses the same client;
  grill edges: connect-failure reap, generic exception path, continuation
  failure, client=None continuation, double cleanup, and identity-checked
  deregister (never evicts a different fork under the same anchor id).

Does NOT import a live PTB application; mirrors the test_dgn902 harness.
"""

import asyncio
import unittest
from unittest.mock import patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from bridge.btw import BtwForkManager, BtwForkState


# ---------------------------------------------------------------------------
# SDK message / client fabrication
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
    """Replays a fixed message list; counts disconnects."""

    def __init__(self, msgs):
        self._msgs = msgs
        self.disconnect_calls = 0

    async def connect(self):
        pass

    async def query(self, question, session_id=None):
        pass

    async def receive_messages(self):
        for m in self._msgs:
            yield m

    async def disconnect(self):
        self.disconnect_calls += 1


class _HangingClient(_FakeClient):
    """Never yields a message -- forces the turn into the timeout path."""

    def __init__(self):
        super().__init__([])
        self._never = asyncio.Event()

    async def receive_messages(self):
        await self._never.wait()
        yield  # pragma: no cover -- unreachable

    async def disconnect(self):
        self.disconnect_calls += 1


class _ExplodingDisconnectClient(_HangingClient):
    """Timeout path client whose disconnect ALSO raises."""

    async def disconnect(self):
        self.disconnect_calls += 1
        raise RuntimeError("disconnect blew up")


class _QueryFailClient(_FakeClient):
    """Raises from query -- exercises the generic exception path."""

    async def query(self, question, session_id=None):
        raise RuntimeError("query failed")


class _ConnectFailClient(_FakeClient):
    """Raises from connect -- exercises the connect-failure reap."""

    async def connect(self):
        raise RuntimeError("connect failed")


def _mk_fork(**kw):
    defaults = dict(anchor_message_id=1, spawned_from_session_id="main-sid")
    defaults.update(kw)
    return BtwForkState(**defaults)


def _registered(mgr, user_id=1, **kw):
    """Build a fork and register it, mirroring bot.py's register-before-turn."""
    fork = _mk_fork(**kw)
    mgr.register_fork(user_id, fork)
    return fork


_FAST_TIMEOUT = patch("bridge.btw.BTW_TURN_TIMEOUT", 0.05)


# ---------------------------------------------------------------------------
# First turn: timeout / exception paths reap the fork
# ---------------------------------------------------------------------------

class TestFirstTurnLeakReap(unittest.IsolatedAsyncioTestCase):

    async def test_timeout_disconnects_client_and_deregisters(self):
        """(a)+(b): timeout path disconnects the fork CLI client and removes
        the fork from the table so it can never be reused."""
        mgr = BtwForkManager()
        fork = _registered(mgr)
        client = _HangingClient()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             _FAST_TIMEOUT:
            with self.assertRaises(asyncio.TimeoutError):
                await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNone(fork.client)
        self.assertIsNone(mgr.lookup_fork(1, fork.anchor_message_id))

    async def test_disconnect_error_swallowed_original_timeout_propagates(self):
        """(c): a raising disconnect is swallowed; the caller still sees the
        original TimeoutError (bot.py converts it to BTW_FORK_FAILED)."""
        mgr = BtwForkManager()
        fork = _registered(mgr)
        client = _ExplodingDisconnectClient()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             _FAST_TIMEOUT:
            with self.assertRaises(asyncio.TimeoutError):
                await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNone(mgr.lookup_fork(1, fork.anchor_message_id))

    async def test_generic_exception_path_also_reaps(self):
        """Non-timeout turn failure (query raises) takes the same reap path."""
        mgr = BtwForkManager()
        fork = _registered(mgr)
        client = _QueryFailClient([])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            with self.assertRaises(RuntimeError):
                await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNone(fork.client)
        self.assertIsNone(mgr.lookup_fork(1, fork.anchor_message_id))

    async def test_connect_failure_reaps_partial_spawn(self):
        """connect() raising: any partially-spawned CLI is disconnected and
        the dead fork is dropped from the table."""
        mgr = BtwForkManager()
        fork = _registered(mgr)
        client = _ConnectFailClient([])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            with self.assertRaises(RuntimeError):
                await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNone(mgr.lookup_fork(1, fork.anchor_message_id))


# ---------------------------------------------------------------------------
# Continuation turn: same reap; client=None guard
# ---------------------------------------------------------------------------

class TestContinuationTurnLeakReap(unittest.IsolatedAsyncioTestCase):

    async def test_continuation_timeout_disconnects_and_deregisters(self):
        mgr = BtwForkManager()
        client = _HangingClient()
        fork = _registered(mgr, initialized=True, fork_session_id="fsid")
        fork.client = client
        with _FAST_TIMEOUT:
            with self.assertRaises(asyncio.TimeoutError):
                await mgr._run_continuation_turn(1, fork, "follow-up")
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNone(fork.client)
        self.assertIsNone(mgr.lookup_fork(1, fork.anchor_message_id))

    async def test_continuation_generic_exception_reaps(self):
        mgr = BtwForkManager()
        client = _QueryFailClient([])
        fork = _registered(mgr, initialized=True, fork_session_id="fsid")
        fork.client = client
        with self.assertRaises(RuntimeError):
            await mgr._run_continuation_turn(1, fork, "follow-up")
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNone(mgr.lookup_fork(1, fork.anchor_message_id))

    async def test_continuation_client_none_deregisters_dead_fork(self):
        """A clientless (dead) fork is dropped so replies stop hitting it."""
        mgr = BtwForkManager()
        fork = _registered(mgr, initialized=True, fork_session_id="fsid")
        self.assertIsNone(fork.client)
        with self.assertRaises(RuntimeError):
            await mgr._run_continuation_turn(1, fork, "follow-up")
        self.assertIsNone(mgr.lookup_fork(1, fork.anchor_message_id))


# ---------------------------------------------------------------------------
# Cleanup helper edges
# ---------------------------------------------------------------------------

class TestCleanupEdges(unittest.IsolatedAsyncioTestCase):

    async def test_double_cleanup_is_safe(self):
        """Second cleanup on an already-reaped fork is a no-op (no raise,
        no second disconnect)."""
        mgr = BtwForkManager()
        client = _HangingClient()
        fork = _registered(mgr)
        fork.client = client
        await mgr._cleanup_failed_turn(1, fork)
        await mgr._cleanup_failed_turn(1, fork)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNone(mgr.lookup_fork(1, fork.anchor_message_id))

    async def test_deregister_identity_check_spares_other_fork(self):
        """Deregister must never evict a DIFFERENT fork registered under the
        same anchor id (e.g. table entry replaced after re-anchor)."""
        mgr = BtwForkManager()
        stale = _mk_fork(anchor_message_id=5)
        healthy = _mk_fork(anchor_message_id=5)
        mgr.register_fork(1, healthy)
        await mgr._cleanup_failed_turn(1, stale)
        self.assertIs(mgr.lookup_fork(1, 5), healthy)

    async def test_cleanup_unknown_user_is_safe(self):
        mgr = BtwForkManager()
        fork = _mk_fork()
        await mgr._cleanup_failed_turn(99, fork)  # no table for user 99
        self.assertIsNone(mgr.lookup_fork(99, fork.anchor_message_id))


# ---------------------------------------------------------------------------
# (d) Happy path untouched
# ---------------------------------------------------------------------------

class TestHappyPathUnchanged(unittest.IsolatedAsyncioTestCase):

    async def test_normal_first_turn_keeps_client_and_registration(self):
        mgr = BtwForkManager()
        fork = _registered(mgr)
        client = _FakeClient([
            _mk_assistant([TextBlock(text="hello world")], session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            result = await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(result, "hello world")
        self.assertIs(fork.client, client)
        self.assertEqual(client.disconnect_calls, 0)
        self.assertTrue(fork.initialized)
        self.assertIs(mgr.lookup_fork(1, fork.anchor_message_id), fork)

    async def test_continuation_reuses_first_turn_client(self):
        """Full happy flow: first turn connects, continuation reuses the SAME
        client -- no disconnect, fork stays registered throughout."""
        mgr = BtwForkManager()
        fork = _registered(mgr)
        client = _FakeClient([
            _mk_assistant([TextBlock(text="first answer")], session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            first = await mgr.run_fork_turn(1, fork, "q1")
        self.assertEqual(first, "first answer")
        # Rearm the replay for the continuation turn on the SAME client object.
        client._msgs = [
            _mk_assistant([TextBlock(text="second answer")]),
            _mk_result("fork-sid"),
        ]
        second = await mgr.run_fork_turn(1, fork, "q2")
        self.assertEqual(second, "second answer")
        self.assertIs(fork.client, client)
        self.assertEqual(client.disconnect_calls, 0)
        self.assertIs(mgr.lookup_fork(1, fork.anchor_message_id), fork)

    async def test_empty_but_successful_turn_not_reaped(self):
        """An EMPTY (but non-raising) turn is a UX issue, not a failure path:
        the fork must stay registered with its client (bot.py falls back to
        BTW_FORK_FAILED text; the session remains continuable)."""
        mgr = BtwForkManager()
        fork = _registered(mgr)
        client = _FakeClient([_mk_result("fork-sid")])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             self.assertLogs("bridge.btw", level="WARNING"):
            result = await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(result, "")
        self.assertIs(fork.client, client)
        self.assertEqual(client.disconnect_calls, 0)
        self.assertIs(mgr.lookup_fork(1, fork.anchor_message_id), fork)


if __name__ == "__main__":
    unittest.main()
