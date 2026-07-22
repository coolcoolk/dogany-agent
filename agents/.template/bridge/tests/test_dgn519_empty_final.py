"""DGN-519: empty-final turn must be silently dropped; error/normal turns unchanged.

Regression suite for the change in _finalize_result:
  (a) Empty-final non-error turn -> future NOT resolved, exactly one INFO log line.
  (b) Error turn (is_error=True) -> PROCESSING_FAILED path intact (future resolved,
      success=False, content formatted via messages.PROCESSING_FAILED).
  (c) Normal turn with non-empty text -> future resolved success=True, content present.
"""

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock

from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState
from claude_agent_sdk import ResultMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state() -> _UserStreamState:
    client = MagicMock()
    client.query = AsyncMock()
    st = _UserStreamState(client=client, model=None)
    st.last_chat_id = 111
    st.proactive_push = AsyncMock()
    st.last_session_id = "sess-dgn519"
    return st


def _make_req(last_texts=None, synthetic=None) -> _PendingRequest:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    loop.close()
    req = _PendingRequest(
        user_id=42,
        chat_id=111,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=fut,
        user_message="do something",
    )
    if last_texts is not None:
        req.last_assistant_texts = last_texts
    if synthetic is not None:
        req.synthetic_response = synthetic
    return req


def _make_result_msg(result_text=None, is_error=False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=50,
        duration_api_ms=40,
        is_error=is_error,
        num_turns=1,
        session_id="sess-dgn519",
        result=result_text,
    )


def _run_finalize(bridge, state, req, msg):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(bridge._finalize_result(req.user_id, state, req, msg))
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# (a) Empty-final turn: nothing sent, one INFO log line
# ---------------------------------------------------------------------------

class TestEmptyFinalDropped(unittest.TestCase):
    """Empty-final non-error turns must be silently dropped."""

    def setUp(self):
        self.bridge = SdkBridge()
        self.state = _make_state()

    def test_empty_block_text_and_empty_msg_result_drops(self):
        # Both last_assistant_texts empty and msg.result None -> empty-final.
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text=None, is_error=False)
        with self.assertLogs("bridge.sdk_bridge", level="INFO") as cm:
            _run_finalize(self.bridge, self.state, req, msg)
        self.assertFalse(
            req.future.done(),
            "Future must NOT be resolved on empty-final turn",
        )
        info_lines = [l for l in cm.output if "empty-final turn dropped" in l and "INFO" in l]
        self.assertEqual(len(info_lines), 1, f"Expected exactly one INFO drop log, got: {cm.output}")

    def test_whitespace_only_block_text_drops(self):
        # block_text is whitespace-only -> strips to empty -> empty-final.
        req = _make_req(last_texts=["   ", "\n\t"])
        msg = _make_result_msg(result_text=None, is_error=False)
        with self.assertLogs("bridge.sdk_bridge", level="INFO") as cm:
            _run_finalize(self.bridge, self.state, req, msg)
        self.assertFalse(req.future.done())
        info_lines = [l for l in cm.output if "empty-final turn dropped" in l and "INFO" in l]
        self.assertEqual(len(info_lines), 1)

    def test_log_line_includes_user_id(self):
        # The INFO log must record the user id.
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text=None, is_error=False)
        with self.assertLogs("bridge.sdk_bridge", level="INFO") as cm:
            _run_finalize(self.bridge, self.state, req, msg)
        drop_lines = [l for l in cm.output if "empty-final turn dropped" in l]
        self.assertTrue(drop_lines, "Drop log line missing")
        self.assertIn("42", drop_lines[0], "User id must appear in the drop log line")

    def test_empty_clean_response_on_synthetic_response_drops(self):
        # synthetic_response that cleans to empty -> should also drop.
        # _clean_response strips whitespace; a whitespace-only synthetic is empty after clean.
        req = _make_req(last_texts=[], synthetic="   ")
        msg = _make_result_msg(result_text=None, is_error=False)
        with self.assertLogs("bridge.sdk_bridge", level="INFO") as cm:
            _run_finalize(self.bridge, self.state, req, msg)
        self.assertFalse(req.future.done())
        info_lines = [l for l in cm.output if "empty-final turn dropped" in l and "INFO" in l]
        self.assertEqual(len(info_lines), 1)


# ---------------------------------------------------------------------------
# (b) Error turn: PROCESSING_FAILED path intact
# ---------------------------------------------------------------------------

class TestErrorTurnProcessingFailed(unittest.TestCase):
    """is_error=True must still reach PROCESSING_FAILED regardless of content."""

    def setUp(self):
        self.bridge = SdkBridge()
        self.state = _make_state()

    def test_error_turn_with_content_resolves_failed(self):
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text="SDK connection refused", is_error=True)
        _run_finalize(self.bridge, self.state, req, msg)
        self.assertTrue(req.future.done(), "Future must be resolved on error turn")
        result = req.future.result()
        self.assertFalse(result.success)
        self.assertIn("SDK connection refused", result.error)

    def test_error_turn_empty_content_still_resolves_failed(self):
        # Even when the error result text is None/empty, PROCESSING_FAILED must fire.
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text=None, is_error=True)
        _run_finalize(self.bridge, self.state, req, msg)
        self.assertTrue(req.future.done(), "Future must be resolved on error turn")
        result = req.future.result()
        self.assertFalse(result.success)

    def test_error_turn_uses_processing_failed_format(self):
        from bridge import messages as bridge_messages
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text="timeout", is_error=True)
        _run_finalize(self.bridge, self.state, req, msg)
        result = req.future.result()
        expected_content = bridge_messages.PROCESSING_FAILED.format(error=result.error)
        self.assertEqual(result.content, expected_content)


# ---------------------------------------------------------------------------
# (c) Normal turn with text: sent as before
# ---------------------------------------------------------------------------

class TestNormalTurnSent(unittest.TestCase):
    """Non-empty non-error turns must resolve the future with success=True."""

    def setUp(self):
        self.bridge = SdkBridge()
        self.state = _make_state()

    def test_normal_text_resolves_success(self):
        req = _make_req(last_texts=["Fix applied. 2 files changed."])
        msg = _make_result_msg(result_text=None, is_error=False)
        _run_finalize(self.bridge, self.state, req, msg)
        self.assertTrue(req.future.done(), "Future must be resolved on normal turn")
        result = req.future.result()
        self.assertTrue(result.success)
        self.assertIn("Fix applied", result.content)

    def test_normal_text_via_msg_result_fallback(self):
        # When last_assistant_texts is empty but msg.result is non-empty, the
        # fallback path must deliver the content (not drop it).
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text="Result from fallback path.", is_error=False)
        _run_finalize(self.bridge, self.state, req, msg)
        self.assertTrue(req.future.done())
        result = req.future.result()
        self.assertTrue(result.success)
        self.assertIn("Result from fallback path", result.content)

    def test_no_drop_log_on_normal_turn(self):
        # The empty-final INFO log must NOT appear on a normal non-empty turn.
        req = _make_req(last_texts=["Done."])
        msg = _make_result_msg(result_text=None, is_error=False)
        import logging as _logging
        with self.assertLogs("bridge.sdk_bridge", level="DEBUG") as cm:
            _logging.getLogger("bridge.sdk_bridge").debug("sentinel")
            _run_finalize(self.bridge, self.state, req, msg)
        drop_lines = [l for l in cm.output if "empty-final turn dropped" in l]
        self.assertEqual(drop_lines, [], f"Unexpected drop log on normal turn: {drop_lines}")


if __name__ == "__main__":
    unittest.main()
