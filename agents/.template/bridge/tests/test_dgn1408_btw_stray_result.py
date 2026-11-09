"""Tests for DGN-1408 -- /btw fork reader swallows a stray turn result.

Background (measured live, bot.log 2026-09-08 18:47:58 + fork transcript
45822502): Claude Code auto-enqueues the resumed session's pending
background-task notifications as inputs at CLI startup. In a /btw fork the
notification lands in the input queue BEFORE the bridge's question, the CLI
opens a turn for it, then coalesces it away when the bridge's question
arrives -- emitting a ResultMessage for that notification turn with ZERO
assistant messages. The bridge's reader broke on the FIRST ResultMessage
unconditionally, adopted the stray result as "the answer turn is over",
returned empty text, and the user was told the side question failed while
the real answer streamed into a reader that had already returned.

Fix under test: a ResultMessage that arrives when (a) no AssistantMessage
has been seen yet in this turn AND (b) the result is not an error is a
stray turn boundary, not our answer -- skip it and keep reading. Everything
else keeps the old contract:
  - an error result (is_error=True) still ends the read immediately;
  - a result after any AssistantMessage still ends the read (genuine empty
    answers stay instant, no new waiting);
  - an empty outcome still reaps at the run_fork_turn gate (DGN-1343).

Mirrors the test_dgn953 harness; no live PTB application.
"""

import unittest
from unittest.mock import patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

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


def _mk_result(session_id="fork-sid", is_error=False):
    msg = object.__new__(ResultMessage)
    msg.session_id = session_id
    msg.is_error = is_error
    return msg


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient: replays a fixed message list."""

    def __init__(self, msgs):
        self._msgs = msgs
        self.queried = []
        self.disconnected = False

    async def connect(self):
        pass

    async def query(self, question, session_id=None):
        self.queried.append((question, session_id))

    async def receive_messages(self):
        for m in self._msgs:
            yield m

    async def disconnect(self):
        self.disconnected = True


def _mk_fork(**kw):
    defaults = dict(anchor_message_id=1, spawned_from_session_id="main-sid")
    defaults.update(kw)
    return BtwForkState(**defaults)


class TestStrayResultSkipped(unittest.IsolatedAsyncioTestCase):
    """A no-assistant, non-error ResultMessage must not end the read."""

    async def test_first_turn_stray_result_then_real_answer(self):
        """The measured live shape: stray notification-turn result first,
        the real answer streams after it. The user must get the answer."""
        mgr = BtwForkManager()
        fork = _mk_fork()
        client = _FakeClient([
            _mk_result("fork-sid"),  # stray: notification turn boundary
            _mk_assistant([TextBlock(text="the real answer")],
                          session_id="fork-sid"),
            _mk_result("fork-sid"),  # the answer turn's own result
        ])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            result = await mgr._run_first_turn(1, fork, "side question")
        self.assertEqual(result, "the real answer")
        self.assertTrue(fork.initialized)
        self.assertEqual(fork.fork_session_id, "fork-sid")

    async def test_continuation_stray_result_then_real_answer(self):
        """Same contract on the continuation reader: a notification turn can
        run between bridge turns and its buffered result is read first."""
        mgr = BtwForkManager()
        fork = _mk_fork(initialized=True, fork_session_id="fsid")
        fork.client = _FakeClient([
            _mk_result("fsid"),
            _mk_assistant([TextBlock(text="follow-up answer")]),
            _mk_result("fsid"),
        ])
        result = await mgr._run_continuation_turn(1, fork, "follow-up")
        self.assertEqual(result, "follow-up answer")

    async def test_first_turn_multiple_stray_results_all_skipped(self):
        """Several pending notifications = several stray results."""
        mgr = BtwForkManager()
        fork = _mk_fork()
        client = _FakeClient([
            _mk_result("fork-sid"),
            _mk_result("fork-sid"),
            _mk_assistant([TextBlock(text="answer after two strays")],
                          session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            result = await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(result, "answer after two strays")


class TestOldContractPreserved(unittest.IsolatedAsyncioTestCase):
    """Error results and post-assistant results keep ending the read."""

    async def test_error_result_still_ends_read_immediately(self):
        """is_error=True with no assistant text is an honest failure, not a
        stray boundary: the read must end there, not consume later frames."""
        mgr = BtwForkManager()
        fork = _mk_fork()
        client = _FakeClient([
            _mk_result("fork-sid", is_error=True),
            _mk_assistant([TextBlock(text="must never be read")],
                          session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            with self.assertLogs("bridge.btw", level="WARNING"):
                result = await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(result, "")

    async def test_result_after_assistant_msgs_still_ends_read(self):
        """A genuine empty answer (assistant messages arrived, no usable
        text) keeps the instant-failure behavior -- no stray skipping."""
        mgr = BtwForkManager()
        fork = _mk_fork()
        client = _FakeClient([
            _mk_assistant([TextBlock(text="stripped")], session_id="fork-sid"),
            _mk_result("fork-sid"),
            _mk_assistant([TextBlock(text="must never be read")],
                          session_id="fork-sid"),
            _mk_result("fork-sid"),
        ])
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client), \
             patch("bridge.btw._register_guard", side_effect=lambda t: ""):
            with self.assertLogs("bridge.btw", level="WARNING"):
                result = await mgr._run_first_turn(1, fork, "q")
        self.assertEqual(result, "")

    async def test_stray_only_stream_stays_empty_and_reaps(self):
        """DGN-1343 regression guard: when the stream ends with only stray
        results (no answer ever arrives), the turn is still empty and the
        run_fork_turn gate still reaps the fork client."""
        mgr = BtwForkManager()
        fork = _mk_fork()
        client = _FakeClient([_mk_result("fork-sid")])
        mgr.register_fork(1, fork)
        with patch.object(BtwForkManager, "_make_fork_client", return_value=client):
            with self.assertLogs("bridge.btw", level="WARNING") as cm:
                result = await mgr.run_fork_turn(1, fork, "q")
        self.assertEqual(result, "")
        self.assertTrue(client.disconnected)
        self.assertIsNone(mgr.lookup_fork(1, fork.anchor_message_id))
        # The DGN-953 diagnostic now names the stray count for the log reader.
        line = "\n".join(cm.output)
        self.assertIn("stray_results=1", line)
        self.assertIn("result_seen=True", line)


if __name__ == "__main__":
    unittest.main()
