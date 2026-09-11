"""Tests for DGN-1343 -- an empty /btw turn must not leave an orphan fork.

Background (live, n=1): a /btw first turn came back with a ResultMessage but
zero assistant text. The bridge logged the empty-turn diagnostic, replied
BTW_FORK_FAILED and stopped reading that fork -- but nothing ended the fork.
Its CLI child went on working by itself for another minute with the same
working directory, identity and tool permissions, and wrote a work item into
a real ledger. The user's screen only ever said "failed".

Two separate defects made that possible, and both are covered here:

  1. The empty-text branch never called the reap at all. DGN-953 had wired the
     reap into the timeout and exception branches only, so "a result arrived
     but it carries no text" fell straight through. The reap now lives at the
     single gate every turn passes (run_fork_turn), so no per-branch wiring can
     be missed again -- including a branch added later.

  2. The reap could not kill a busy child even when it was called. The SDK
     transport's close() waits for a graceful exit before it escalates to
     SIGTERM/SIGKILL, and that escalation runs under an anyio shield --
     which an asyncio.wait_for cancellation pierces. With the old 3.0s budget,
     close() was cancelled DURING the graceful wait, so the kill never ran and
     the child survived its own reap. Measured against the real transport with
     a child that ignores stdin EOF: budget 3.0 -> child alive, budget 20.0 ->
     child dead.

Orphan absence is asserted against a REAL OS process (os.kill(pid, 0)), never
against a log line: a log line is what the live incident already had.
"""

import asyncio
import os
import signal
import unittest
from unittest.mock import patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from bridge import btw as btw_module
from bridge.btw import BtwForkManager, BtwForkState


# ---------------------------------------------------------------------------
# Real child process harness
# ---------------------------------------------------------------------------

def _alive(pid: int) -> bool:
    """True when pid still exists (signal 0 = existence probe, no delivery)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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


class _ChildClient:
    """SDK client stand-in that owns a REAL child process.

    The child is a long sleep: nothing but the reap can end it inside the test
    window, so "no orphan" is a statement about the process table rather than
    about a counter or a log line. disconnect() terminates the child the way
    the SDK transport's close() ultimately does.
    """

    def __init__(self, msgs=None, grace=0.0):
        self._msgs = msgs or []
        # Seconds disconnect() waits for a graceful exit before escalating to
        # a signal -- the SDK transport's close() has the same shape, and the
        # reap budget has to outlast it (see _FORK_REAP_TIMEOUT).
        self._grace = grace
        self.proc = None
        self.disconnect_calls = 0

    async def spawn(self):
        self.proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "sleep 120",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self

    @property
    def pid(self):
        return self.proc.pid

    async def connect(self):
        pass

    async def query(self, question, session_id=None):
        pass

    async def receive_messages(self):
        for m in self._msgs:
            yield m

    async def disconnect(self):
        self.disconnect_calls += 1
        if self._grace:
            # Graceful window first: the child ignores it (it is a plain
            # sleep), exactly like a fork mid-turn ignoring stdin EOF.
            await asyncio.sleep(self._grace)
        if self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
        await self.proc.wait()

    async def force_kill(self):
        """Test-teardown safety net; never part of an assertion."""
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


class _HangingChildClient(_ChildClient):
    """Never yields a message -- drives the turn into the timeout branch."""

    async def receive_messages(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover -- unreachable


class _QueryFailChildClient(_ChildClient):
    """Raises from query -- drives the turn into the exception branch."""

    async def query(self, question, session_id=None):
        raise RuntimeError("query failed")


_FAST_TURN_TIMEOUT = patch("bridge.btw.BTW_TURN_TIMEOUT", 0.05)


class _OrphanTestCase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.mgr = BtwForkManager()
        self._clients = []

    async def asyncTearDown(self):
        for client in self._clients:
            await client.force_kill()

    async def child_client(self, cls=_ChildClient, **kw):
        client = await cls(**kw).spawn()
        self._clients.append(client)
        return client

    def registered_fork(self, user_id=1, **kw):
        """Register a fork the way bot.py does: table entry before the turn."""
        defaults = dict(anchor_message_id=1, spawned_from_session_id="main-sid")
        defaults.update(kw)
        fork = BtwForkState(**defaults)
        self.mgr.register_fork(user_id, fork)
        return fork

    async def assert_reaped(self, client, fork, user_id=1):
        """No orphan: the child is gone from the process table AND the fork is
        gone from the table that reply routing reads."""
        self.assertFalse(
            _alive(client.pid),
            "orphan fork child %d survived the reap" % client.pid,
        )
        self.assertIsNone(self.mgr.lookup_fork(user_id, fork.anchor_message_id))
        self.assertIsNone(fork.client)


# ---------------------------------------------------------------------------
# 1. An empty turn leaves no orphan
# ---------------------------------------------------------------------------

class TestEmptyTurnLeavesNoOrphan(_OrphanTestCase):

    async def test_empty_first_turn_kills_the_child(self):
        """The live case: ResultMessage with no assistant text at all."""
        client = await self.child_client(msgs=[_mk_result("fork-sid")])
        fork = self.registered_fork()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             self.assertLogs("bridge.btw", level="WARNING"):
            answer = await self.mgr.run_fork_turn(1, fork, "hi")
        self.assertEqual(answer, "")
        await self.assert_reaped(client, fork)

    async def test_empty_after_guard_strip_kills_the_child(self):
        """Text blocks arrived but the guards stripped every one of them --
        same empty outcome, same reap."""
        client = await self.child_client(msgs=[
            _mk_assistant([TextBlock(text="stripped by the guard")],
                          session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        fork = self.registered_fork()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             patch("bridge.btw._register_guard", side_effect=lambda t: ""), \
             self.assertLogs("bridge.btw", level="WARNING"):
            answer = await self.mgr.run_fork_turn(1, fork, "hi")
        self.assertEqual(answer, "")
        await self.assert_reaped(client, fork)

    async def test_empty_continuation_turn_kills_the_child(self):
        """A follow-up question that comes back empty reaps the same way: the
        user is told it failed, so the fork must not keep running."""
        client = await self.child_client(msgs=[_mk_result("fsid")])
        fork = self.registered_fork(initialized=True, fork_session_id="fsid")
        fork.client = client
        with self.assertLogs("bridge.btw", level="WARNING"):
            answer = await self.mgr.run_fork_turn(1, fork, "follow-up")
        self.assertEqual(answer, "")
        await self.assert_reaped(client, fork)


# ---------------------------------------------------------------------------
# 2. Failing turns leave no orphan either (DGN-953 paths, real-process check)
# ---------------------------------------------------------------------------

class TestFailedTurnLeavesNoOrphan(_OrphanTestCase):

    async def test_errored_turn_kills_the_child(self):
        client = await self.child_client(cls=_QueryFailChildClient)
        fork = self.registered_fork()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            with self.assertRaises(RuntimeError):
                await self.mgr.run_fork_turn(1, fork, "hi")
        await self.assert_reaped(client, fork)

    async def test_timed_out_turn_kills_the_child(self):
        client = await self.child_client(cls=_HangingChildClient)
        fork = self.registered_fork()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             _FAST_TURN_TIMEOUT:
            with self.assertRaises(asyncio.TimeoutError):
                await self.mgr.run_fork_turn(1, fork, "hi")
        await self.assert_reaped(client, fork)

    async def test_cancelled_turn_kills_the_child(self):
        """/stop cancels outstanding fork tasks. Cancellation is a turn ending
        like any other, so it must not strand the child either."""
        client = await self.child_client(cls=_HangingChildClient)
        fork = self.registered_fork()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            task = asyncio.ensure_future(self.mgr.run_fork_turn(1, fork, "hi"))
            await asyncio.sleep(0.05)  # let the turn reach the read loop
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        # The reap is shielded, so it outlives the cancelled turn; give the
        # background task its turn on the loop before checking the child.
        for _ in range(50):
            if not _alive(client.pid):
                break
            await asyncio.sleep(0.05)
        await self.assert_reaped(client, fork)


# ---------------------------------------------------------------------------
# 3. A normal turn still works end to end
# ---------------------------------------------------------------------------

class TestNormalTurnUntouched(_OrphanTestCase):

    async def test_normal_turn_answers_and_keeps_the_fork_alive(self):
        client = await self.child_client(msgs=[
            _mk_assistant([TextBlock(text="the side answer")],
                          session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        fork = self.registered_fork()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            answer = await self.mgr.run_fork_turn(1, fork, "hi")
        self.assertEqual(answer, "the side answer")
        # Nothing reaped: the fork is answerable, so its child must survive.
        self.assertTrue(_alive(client.pid))
        self.assertEqual(client.disconnect_calls, 0)
        self.assertIs(fork.client, client)
        self.assertTrue(fork.initialized)
        self.assertEqual(fork.fork_session_id, "fork-sid")
        self.assertIs(self.mgr.lookup_fork(1, fork.anchor_message_id), fork)

    async def test_follow_up_turn_reuses_the_same_live_child(self):
        client = await self.child_client(msgs=[
            _mk_assistant([TextBlock(text="first answer")], session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        fork = self.registered_fork()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            first = await self.mgr.run_fork_turn(1, fork, "q1")
        self.assertEqual(first, "first answer")
        client._msgs = [
            _mk_assistant([TextBlock(text="second answer")]),
            _mk_result("fork-sid"),
        ]
        second = await self.mgr.run_fork_turn(1, fork, "q2")
        self.assertEqual(second, "second answer")
        self.assertTrue(_alive(client.pid))
        self.assertEqual(client.disconnect_calls, 0)
        self.assertIs(self.mgr.lookup_fork(1, fork.anchor_message_id), fork)


# ---------------------------------------------------------------------------
# 4. The reap budget has to outlast the shutdown it is waiting on
# ---------------------------------------------------------------------------

class TestReapBudget(_OrphanTestCase):

    async def test_budget_covers_the_sdk_shutdown_ladder(self):
        """Regression lock on the constant itself: a budget under the ladder
        does not degrade to "reaps late", it degrades to "never kills"."""
        self.assertGreaterEqual(
            btw_module._FORK_REAP_TIMEOUT,
            btw_module._SDK_SHUTDOWN_LADDER,
        )

    async def test_budget_shorter_than_shutdown_strands_the_child(self):
        """Demonstrates the mechanism on a real process: with a budget below
        the client's graceful window, the disconnect is cancelled before its
        escalation runs and the child is STILL ALIVE afterwards. This is the
        state the shipped constant exists to prevent -- with it, the same
        scenario reaps (test_empty_first_turn_kills_the_child)."""
        client = await self.child_client(msgs=[_mk_result("fork-sid")], grace=5.0)
        fork = self.registered_fork()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             patch("bridge.btw._FORK_REAP_TIMEOUT", 0.1), \
             self.assertLogs("bridge.btw", level="WARNING"):
            answer = await self.mgr.run_fork_turn(1, fork, "hi")
        self.assertEqual(answer, "")
        self.assertTrue(
            _alive(client.pid),
            "harness is not reproducing the defect: the child should still be "
            "inside its graceful window when the short budget fires",
        )

    async def test_budget_above_shutdown_reaps_the_child(self):
        """Same client, same graceful window, budget above it: reaped."""
        client = await self.child_client(msgs=[_mk_result("fork-sid")], grace=0.3)
        fork = self.registered_fork()
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             patch("bridge.btw._FORK_REAP_TIMEOUT", 10.0), \
             self.assertLogs("bridge.btw", level="WARNING"):
            answer = await self.mgr.run_fork_turn(1, fork, "hi")
        self.assertEqual(answer, "")
        await self.assert_reaped(client, fork)


if __name__ == "__main__":
    unittest.main()
