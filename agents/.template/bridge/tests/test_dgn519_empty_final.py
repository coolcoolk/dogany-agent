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
# (b) Error turn: DGN-686 classified LOCKED-notice path
# ---------------------------------------------------------------------------

class TestErrorTurnClassifiedNotice(unittest.TestCase):
    """is_error=True is classified into a LOCKED ko notice (DGN-686).

    The raw English detail is preserved in .error (for the stderr log) but the
    user-facing .content is the mapped notice; auth errors carry no retry.
    """

    def setUp(self):
        self.bridge = SdkBridge()
        self.state = _make_state()

    def test_transient_error_maps_to_retry_notice(self):
        from bridge import messages as m
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text="upstream 529 overloaded_error", is_error=True)
        _run_finalize(self.bridge, self.state, req, msg)
        self.assertTrue(req.future.done())
        result = req.future.result()
        self.assertFalse(result.success)
        self.assertIn("overloaded", result.error)  # raw detail retained for log
        self.assertEqual(result.content, m.ERROR_TRANSIENT_RETRY)
        self.assertTrue(result.retry_offer)
        # DGN-686 MAJOR-1: error_kind must be stamped so the BOT seat (not the
        # reader loop) can auto-retry once on transient before showing notice.
        self.assertEqual(result.error_kind, "transient")

    def test_auth_error_maps_to_relogin_no_retry(self):
        from bridge import messages as m
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text="HTTP 401 invalid_api_key", is_error=True)
        _run_finalize(self.bridge, self.state, req, msg)
        result = req.future.result()
        self.assertFalse(result.success)
        self.assertEqual(result.content, m.ERROR_AUTH_RELOGIN)
        self.assertFalse(result.retry_offer)
        # auth is NOT transient -> the bot seat must not auto-retry it.
        self.assertEqual(result.error_kind, "auth")

    def test_other_error_maps_to_generic_retry(self):
        from bridge import messages as m
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text="some unexpected failure", is_error=True)
        _run_finalize(self.bridge, self.state, req, msg)
        result = req.future.result()
        self.assertFalse(result.success)
        self.assertEqual(result.content, m.ERROR_GENERIC_RETRY)
        self.assertTrue(result.retry_offer)

    def test_error_turn_empty_content_still_resolves_failed(self):
        # Even when the error result text is None/empty, the future resolves
        # failed (empty detail classifies as "other" -> generic retry notice).
        from bridge import messages as m
        req = _make_req(last_texts=[])
        msg = _make_result_msg(result_text=None, is_error=True)
        _run_finalize(self.bridge, self.state, req, msg)
        self.assertTrue(req.future.done())
        result = req.future.result()
        self.assertFalse(result.success)
        self.assertEqual(result.content, m.ERROR_GENERIC_RETRY)

    def test_finalize_never_redispatches(self):
        # DGN-686 MAJOR-1 invariant: the reader-loop finalize must NEVER re-run
        # a query itself (re-entrancy risk). The transient auto-retry lives in
        # the bot caller seat; _finalize_result only stamps error_kind. Parse
        # the AST and inspect CALLS (not comments/strings) so a mention of the
        # split in a docstring does not trip the check.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(SdkBridge._finalize_result).lstrip())
        forbidden = {
            "process_message", "resume_caller",
            "_reconnect_and_retry", "_dispatch_next_query",
        }
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name:
                    called.add(name)
        offenders = forbidden & called
        self.assertEqual(
            set(), offenders,
            f"_finalize_result must not re-dispatch (calls: {offenders})")


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
