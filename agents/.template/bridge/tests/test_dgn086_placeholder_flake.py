"""DGN-086 + DGN-670: subagent placeholder-flake detection and recovery tests.

Covers (DGN-086, detection):
- _is_placeholder_flake() matches known Korean delegation-placeholder patterns.
- _is_placeholder_flake() does not false-positive on real result text.
- tool_use_count is incremented by ToolUseBlock entries in main-agent messages
  but NOT for subagent (parent_tool_use_id-set) messages.
- _finalize_result logs a DGN-086 warning when the flake pattern fires.

Covers (DGN-670, recovery + prevention):
- placeholder blocked from the user; single retry re-dispatch with the
  original user_message under the executor-contract prefix (same session).
- loop guard: exactly one retry; second flake / empty retry result resolve
  to the i18n failure notice, never the placeholder.
- M1 false-positive gates: no retry without subagent activity observed this
  turn (Warg-style main plain text, Bash-dispatched juniors, short
  meta-discussion), no retry on a legitimate background Task launch, no
  retry on long content that merely quotes flake vocabulary.
- M2 draft scrub: placeholder draft bubbles are cancelled BEFORE
  finalize_all() so the flaked bubble never reaches the user permanently.
- M3 retry send failure: the request future resolves to the failure notice
  (no orphaned future / stream wedge) and the reader pops the request.
- reader-loop per-request flag contract (subagent_activity /
  background_task_launched capture; conditional popleft on retry).
- F1 prevention: PreToolUse hook rewrites Task prompts with the executor
  contract (idempotent, non-Task untouched, wired into ClaudeAgentOptions).
"""

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bridge import messages
from bridge.sdk_bridge import (
    SdkBridge,
    _EXECUTOR_CONTRACT_MARKER,
    _EXECUTOR_CONTRACT_PREFIX,
    _PLACEHOLDER_FLAKE_RE,
    _PendingRequest,
    _UserStreamState,
    _task_executor_contract_hook,
)
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock, TextBlock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kw):
    client = MagicMock()
    client.query = AsyncMock()
    st = _UserStreamState(client=client, model=None)
    st.last_chat_id = kw.get("chat_id", 111)
    st.proactive_push = kw.get("push", AsyncMock())
    st.last_session_id = kw.get("session_id", "sess-1")
    return st


def _make_req(**kw) -> _PendingRequest:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    loop.close()
    req = _PendingRequest(
        user_id=kw.get("user_id", 1),
        chat_id=kw.get("chat_id", 111),
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=fut,
        user_message=kw.get("user_message", "do the thing"),
    )
    return req


def _make_result_msg(result_text: str, is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=is_error,
        num_turns=3,
        session_id="sess-1",
        result=result_text,
    )


def _make_assistant_msg(
    text: str = "",
    tool_names: list = None,
    parent_tool_use_id: str = None,
) -> AssistantMessage:
    content = []
    if text:
        content.append(TextBlock(text=text))
    for name in (tool_names or []):
        content.append(ToolUseBlock(id="tu-1", name=name, input={}))
    return AssistantMessage(
        content=content,
        model="claude-sonnet-4-5",
        parent_tool_use_id=parent_tool_use_id,
    )


# ---------------------------------------------------------------------------
# Unit: _is_placeholder_flake
# ---------------------------------------------------------------------------

class TestIsPlaceholderFlake(unittest.TestCase):
    def _check(self, text: str) -> bool:
        return SdkBridge._is_placeholder_flake(text)

    # --- known flake patterns ---

    def test_classic_dongsaeng_placeholder(self):
        self.assertTrue(self._check(
            "동생이 아직 작업 중입니다. 완료 알림이 오면 결과를 먼저 보고드리겠습니다."
        ))

    def test_background_agent_waiting(self):
        self.assertTrue(self._check(
            "백그라운드 정찰 에이전트 완료 대기 중입니다. 잠시 후 결과를 전달합니다."
        ))

    def test_subagent_executing_waiting(self):
        self.assertTrue(self._check(
            "구현 서브에이전트 실행 중, 완료 통보 대기"
        ))

    def test_dongsaeng_working(self):
        self.assertTrue(self._check("동생 작업중"))

    def test_completion_notice_await(self):
        self.assertTrue(self._check("완료 알림 대기 중"))

    def test_completion_notify_await(self):
        self.assertTrue(self._check("완료 통보가 오면 전달드리겠습니다"))

    def test_subagent_working(self):
        self.assertTrue(self._check("서브에이전트 작업 중입니다"))

    # --- not a flake ---

    def test_real_result_not_flake(self):
        self.assertFalse(self._check(
            "DGN-096 구현 완료. backport.sh 수정, 테스트 통과."
        ))

    def test_empty_string_not_flake(self):
        self.assertFalse(self._check(""))

    def test_unrelated_korean_not_flake(self):
        self.assertFalse(self._check(
            "파일을 성공적으로 저장했습니다."
        ))

    def test_english_work_done_not_flake(self):
        self.assertFalse(self._check(
            "All tests passed. 3 files changed."
        ))


# ---------------------------------------------------------------------------
# Unit: tool_use_count tracking in _reader_loop (via AssistantMessage handling)
# ---------------------------------------------------------------------------

class TestToolUseCountTracking(unittest.TestCase):
    """Verify that ToolUseBlocks in main-agent messages increment req.tool_use_count
    but subagent (parent_tool_use_id != None) messages do NOT.
    """

    def _run_messages(self, messages_to_feed: list):
        """Feed a list of SDK messages through the reader loop logic
        (the AssistantMessage + ResultMessage branches) without needing
        a live SDK client. Returns the _PendingRequest after processing."""
        bridge = SdkBridge()
        state = _make_state()
        req = _make_req()
        loop = asyncio.new_event_loop()

        async def _drive():
            for msg in messages_to_feed:
                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "session_id", None):
                        state.last_session_id = msg.session_id
                    if getattr(msg, "parent_tool_use_id", None):
                        continue
                    req.last_assistant_texts = []
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            req.last_assistant_texts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            req.tool_use_count += 1

        try:
            loop.run_until_complete(_drive())
        finally:
            loop.close()
        return req

    def test_tool_uses_counted(self):
        msg = _make_assistant_msg(tool_names=["Read", "Edit"])
        req = self._run_messages([msg])
        self.assertEqual(req.tool_use_count, 2)

    def test_subagent_messages_not_counted(self):
        # parent_tool_use_id set => subagent inner message, must be skipped
        msg = _make_assistant_msg(
            tool_names=["Bash"],
            parent_tool_use_id="outer-tool-id",
        )
        req = self._run_messages([msg])
        self.assertEqual(req.tool_use_count, 0)

    def test_mixed_main_and_subagent(self):
        main_msg = _make_assistant_msg(tool_names=["Read", "Read", "Edit"])
        sub_msg = _make_assistant_msg(
            tool_names=["Bash", "Write"],
            parent_tool_use_id="parent-id",
        )
        req = self._run_messages([main_msg, sub_msg])
        self.assertEqual(req.tool_use_count, 3)  # only main_msg's 3 tools


# ---------------------------------------------------------------------------
# Integration: _finalize_result logs DGN-086 warning on flake
# ---------------------------------------------------------------------------

class TestFinalizeResultFlakeWarning(unittest.TestCase):
    """_finalize_result must emit a WARNING log when flake pattern detected."""

    def _run_finalize(self, result_text: str, tool_use_count: int = 0):
        bridge = SdkBridge()
        state = _make_state()
        req = _make_req()
        req.tool_use_count = tool_use_count
        result_msg = _make_result_msg(result_text)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                bridge._finalize_result(1, state, req, result_msg)
            )
        finally:
            loop.close()
        return req

    def test_flake_response_logs_warning(self):
        placeholder = (
            "동생이 아직 작업 중입니다. 완료 알림이 오면 결과를 먼저 보고드리겠습니다."
        )
        with self.assertLogs("bridge.sdk_bridge", level="WARNING") as cm:
            req = self._run_finalize(placeholder, tool_use_count=1)
        self.assertTrue(
            any("DGN-086" in line for line in cm.output),
            f"DGN-086 not found in log output: {cm.output}",
        )
        self.assertTrue(
            any("tool_use_count=1" in line for line in cm.output),
            f"tool_use_count not in log: {cm.output}",
        )

    def test_real_response_no_warning(self):
        real_result = "구현 완료. 3개 파일 수정, 테스트 통과."
        # assertLogs raises AssertionError if no log is emitted at that level.
        # We verify no DGN-086 WARNING appears.
        import logging
        with self.assertLogs("bridge.sdk_bridge", level="DEBUG") as cm:
            # trigger at least one log line so assertLogs doesn't fail on empty
            logging.getLogger("bridge.sdk_bridge").debug("test sentinel")
            self._run_finalize(real_result, tool_use_count=5)
        dgn086_warnings = [l for l in cm.output if "DGN-086" in l and "WARNING" in l]
        self.assertEqual(dgn086_warnings, [], f"Unexpected DGN-086 warning: {dgn086_warnings}")

    def test_flake_with_many_tool_uses_still_warns(self):
        # DGN-086 update 2026-07-08: tool_use_count > 1 (21 reads, 0 edits)
        # should still trigger when the output is a placeholder.
        placeholder = "구현 서브에이전트 실행 중, 완료 통보 대기"
        with self.assertLogs("bridge.sdk_bridge", level="WARNING") as cm:
            self._run_finalize(placeholder, tool_use_count=21)
        self.assertTrue(
            any("DGN-086" in line for line in cm.output),
        )
        self.assertTrue(
            any("tool_use_count=21" in line for line in cm.output),
        )


# ---------------------------------------------------------------------------
# DGN-670 helpers
# ---------------------------------------------------------------------------

FLAKE_TEXT = "동생이 아직 작업 중입니다. 완료 알림이 오면 결과를 먼저 보고드리겠습니다."


def _finalize(bridge, state, req, result_msg):
    """Run _finalize_result on a fresh loop; return its bool result."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            bridge._finalize_result(1, state, req, result_msg)
        )
    finally:
        loop.close()


def _flake_req(**kw) -> _PendingRequest:
    """A request whose turn showed genuine foreground subagent activity."""
    req = _make_req(**kw)
    req.subagent_activity = True
    req.last_assistant_texts = [kw.get("text", FLAKE_TEXT)]
    return req


def _make_task_block(run_in_background=False, prompt="do X"):
    tool_input = {"prompt": prompt, "subagent_type": "general-purpose"}
    if run_in_background:
        tool_input["run_in_background"] = True
    return ToolUseBlock(id="tu-task", name="Task", input=tool_input)


class _FakeReaderClient:
    """Async-iterable stand-in for ClaudeSDKClient in reader-loop tests."""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.query = AsyncMock()

    async def receive_messages(self):
        for m in self._msgs:
            yield m


def _mock_handler():
    handler = MagicMock()
    handler.cancel = AsyncMock(return_value=True)
    handler.finalize_all = AsyncMock(return_value=True)
    handler.drafts = []
    handler.bot = MagicMock()
    handler.chat_id = 111
    handler.user_id = 1
    return handler


# ---------------------------------------------------------------------------
# DGN-670: recovery core (block + single retry + loop guard)
# ---------------------------------------------------------------------------

class TestFlakeRecoveryRetry(unittest.TestCase):
    def test_placeholder_blocked_and_retry_dispatched(self):
        """T1: flake + gates pass -> future NOT resolved, one retry query with
        the executor prefix AND the original user_message, same session."""
        bridge = SdkBridge()
        state = _make_state()
        req = _flake_req(user_message="fix the parser bug")
        retried = _finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT))
        self.assertTrue(retried)
        self.assertFalse(req.future.done())
        self.assertEqual(req.flake_retry_count, 1)
        state.client.query.assert_awaited_once()
        args, kwargs = state.client.query.call_args
        sent_text = args[0]
        self.assertIn("[BRIDGE FLAKE RETRY", sent_text)
        self.assertIn("fix the parser bug", sent_text)
        self.assertTrue(sent_text.endswith("fix the parser bug"))
        self.assertEqual(kwargs.get("session_id"), "sess-1")

    def test_retry_then_success(self):
        """T3: after a retry, a real result resolves the ORIGINAL future."""
        bridge = SdkBridge()
        state = _make_state()
        req = _flake_req(user_message="fix the parser bug")
        self.assertTrue(_finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT)))
        # Retry turn produces a real answer.
        real = "Parser bug fixed: 2 files changed, tests green."
        req.last_assistant_texts = [real]
        retried = _finalize(bridge, state, req, _make_result_msg(real))
        self.assertFalse(retried)
        self.assertTrue(req.future.done())
        resp = req.future.result()
        self.assertTrue(resp.success)
        self.assertEqual(resp.content, real)
        self.assertNotIn("작업 중", resp.content)

    def test_retry_fires_once_then_failure_notice(self):
        """T2 + loop guard: a second flake resolves the failure notice with
        success=False; no second retry query; never the placeholder."""
        bridge = SdkBridge()
        state = _make_state()
        req = _flake_req()
        self.assertTrue(_finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT)))
        # Retry turn flakes again (no activity needed on the 2nd gate: the
        # flaked retry turn typically has zero tool calls).
        req.last_assistant_texts = [FLAKE_TEXT]
        retried = _finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT))
        self.assertFalse(retried)
        state.client.query.assert_awaited_once()  # still just the one retry
        self.assertTrue(req.future.done())
        resp = req.future.result()
        self.assertFalse(resp.success)
        self.assertEqual(resp.error, "placeholder_flake")
        self.assertEqual(resp.content, messages.FLAKE_RECOVERY_FAILED)
        self.assertNotIn("작업 중", resp.content)

    def test_empty_retry_result_failure_notice(self):
        """Loop guard: an EMPTY retry result resolves the failure notice
        instead of the silent empty-final drop (which would wedge the turn)."""
        bridge = SdkBridge()
        state = _make_state()
        req = _flake_req()
        self.assertTrue(_finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT)))
        req.last_assistant_texts = []
        retried = _finalize(bridge, state, req, _make_result_msg(""))
        self.assertFalse(retried)
        self.assertTrue(req.future.done())
        resp = req.future.result()
        self.assertFalse(resp.success)
        self.assertEqual(resp.content, messages.FLAKE_RECOVERY_FAILED)

    def test_original_message_with_flake_vocab_replayed(self):
        """T9: a user_message that itself contains flake vocabulary is still
        replayed verbatim, and the retry prefix never self-triggers."""
        bridge = SdkBridge()
        state = _make_state()
        original = "동생 작업중 메시지가 또 떴어, 왜 그런지 고쳐줘"
        req = _flake_req(user_message=original)
        retried = _finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT))
        self.assertTrue(retried)
        sent_text = state.client.query.call_args[0][0]
        self.assertIn(original, sent_text)
        # The prefix alone (model-facing English) must not match the regex.
        self.assertIsNone(_PLACEHOLDER_FLAKE_RE.search(messages.FLAKE_RETRY_PREFIX))

    def test_error_result_with_flake_text_no_retry(self):
        """T5: error results keep the PROCESSING_FAILED path, no retry."""
        bridge = SdkBridge()
        state = _make_state()
        req = _flake_req()
        req.last_assistant_texts = []
        msg = _make_result_msg(FLAKE_TEXT, is_error=True)
        retried = _finalize(bridge, state, req, msg)
        self.assertFalse(retried)
        state.client.query.assert_not_awaited()
        self.assertTrue(req.future.done())
        resp = req.future.result()
        self.assertFalse(resp.success)
        self.assertNotEqual(resp.content, messages.FLAKE_RECOVERY_FAILED)

    def test_synthetic_response_no_retry(self):
        """T5: AskUserQuestion degradation (synthetic_response) never retries."""
        bridge = SdkBridge()
        state = _make_state()
        req = _flake_req()
        req.synthetic_response = "1. option A\n2. option B"
        retried = _finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT))
        self.assertFalse(retried)
        state.client.query.assert_not_awaited()
        self.assertTrue(req.future.done())
        self.assertTrue(req.future.result().success)


# ---------------------------------------------------------------------------
# DGN-670 M1: false-positive gates
# ---------------------------------------------------------------------------

class TestFlakeFalsePositiveGates(unittest.TestCase):
    def _deliver_as_is(self, req, state, text=FLAKE_TEXT):
        bridge = SdkBridge()
        retried = _finalize(bridge, state, req, _make_result_msg(text))
        self.assertFalse(retried)
        state.client.query.assert_not_awaited()
        self.assertTrue(req.future.done())
        resp = req.future.result()
        self.assertTrue(resp.success)
        return resp

    def test_legit_background_task_launch_not_retried(self):
        """A turn that launched a real background Task legitimately reports
        '동생 작업중' -- must be delivered as-is, never retried."""
        state = _make_state()
        req = _make_req()
        req.subagent_activity = True
        req.background_task_launched = True
        req.last_assistant_texts = [FLAKE_TEXT]
        resp = self._deliver_as_is(req, state)
        self.assertEqual(resp.content, FLAKE_TEXT)

    def test_main_plain_text_false_positive_not_retried(self):
        """Warg-style: flake-looking MAIN-agent plain text with no subagent
        at all this turn (subagent_activity False) -- no retry."""
        state = _make_state()
        req = _make_req()
        req.last_assistant_texts = [FLAKE_TEXT]
        self.assertFalse(req.subagent_activity)
        self._deliver_as_is(req, state)

    def test_bash_dispatched_junior_status_not_retried(self):
        """A Bash-dispatched background junior ('동생') status turn has no
        Task ToolUseBlock and no parent_tool_use_id -> activity False -> no
        retry, genuine status delivered."""
        state = _make_state()
        req = _make_req()
        req.tool_use_count = 3  # e.g. Bash dispatch + checks, but NO Task
        req.last_assistant_texts = [FLAKE_TEXT]
        self._deliver_as_is(req, state)

    def test_short_meta_discussion_not_retried(self):
        """Short owner-chat meta-discussion quoting the flake vocabulary with
        no subagent this turn -- no retry."""
        state = _make_state()
        req = _make_req()
        text = "네, '동생 작업중' 문구가 그 플레이크 증상 맞습니다."
        req.last_assistant_texts = [text]
        self._deliver_as_is(req, state, text=text)

    def test_long_genuine_report_not_retried(self):
        """Short-content gate: a LONG genuine report that quotes flake
        vocabulary (with real subagent activity) is delivered as-is."""
        state = _make_state()
        req = _make_req()
        req.subagent_activity = True
        long_report = (
            "서브에이전트 실행 중 로그를 함께 확인했습니다. " * 10
            + "결론: 3개 파일 수정 완료, 테스트 17건 통과, 회귀 없음. "
            + "상세 내역은 다음과 같습니다. " * 5
        )
        self.assertGreater(len(long_report), 300)
        req.last_assistant_texts = [long_report]
        self._deliver_as_is(req, state, text=long_report)


# ---------------------------------------------------------------------------
# DGN-670 M2: draft scrub (placeholder bubble never finalized)
# ---------------------------------------------------------------------------

class TestFlakeDraftScrub(unittest.TestCase):
    def test_retry_cancels_drafts_before_finalize(self):
        """On retry the streamed placeholder drafts are CANCELLED (deleted),
        finalize_all is never called on them, and the request gets a fresh
        handler for the retry turn."""
        from bridge.streaming import StreamingMessageHandler

        bridge = SdkBridge()
        state = _make_state()
        req = _flake_req()
        handler = _mock_handler()
        req.streaming_handler = handler
        retried = _finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT))
        self.assertTrue(retried)
        handler.cancel.assert_awaited_once()
        handler.finalize_all.assert_not_awaited()
        self.assertIsNot(req.streaming_handler, handler)
        self.assertIsInstance(req.streaming_handler, StreamingMessageHandler)

    def test_second_flake_scrubs_drafts_too(self):
        """The failure-notice path also scrubs the placeholder drafts."""
        bridge = SdkBridge()
        state = _make_state()
        req = _flake_req()
        req.flake_retry_count = 1  # retry already used
        handler = _mock_handler()
        req.streaming_handler = handler
        retried = _finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT))
        self.assertFalse(retried)
        handler.cancel.assert_awaited_once()
        handler.finalize_all.assert_not_awaited()
        resp = req.future.result()
        self.assertEqual(resp.content, messages.FLAKE_RECOVERY_FAILED)

    def test_normal_turn_still_finalizes_drafts(self):
        """Regression: non-flake turns keep today's finalize_all behavior."""
        bridge = SdkBridge()
        state = _make_state()
        req = _make_req()
        req.last_assistant_texts = ["done, all green"]
        handler = _mock_handler()
        req.streaming_handler = handler
        retried = _finalize(bridge, state, req, _make_result_msg("done, all green"))
        self.assertFalse(retried)
        handler.finalize_all.assert_awaited_once()
        handler.cancel.assert_not_awaited()


# ---------------------------------------------------------------------------
# DGN-670 M3: retry send failure must never wedge the stream
# ---------------------------------------------------------------------------

class TestFlakeRetrySendFailure(unittest.TestCase):
    def test_send_failure_resolves_future_no_wedge(self):
        """query raising on the retry send resolves the future to the failure
        notice and returns False (reader pops the head) -- never an orphaned
        future with sent=True and no in-flight turn."""
        bridge = SdkBridge()
        state = _make_state()
        state.client.query = AsyncMock(side_effect=RuntimeError("stdin closed"))
        req = _flake_req()
        retried = _finalize(bridge, state, req, _make_result_msg(FLAKE_TEXT))
        self.assertFalse(retried)
        self.assertTrue(req.future.done())
        resp = req.future.result()
        self.assertFalse(resp.success)
        self.assertEqual(resp.error, "placeholder_flake")
        self.assertEqual(resp.content, messages.FLAKE_RECOVERY_FAILED)
        # Retry budget was consumed; a later flake cannot re-dispatch.
        self.assertEqual(req.flake_retry_count, 1)


# ---------------------------------------------------------------------------
# DGN-670: reader-loop per-request flag contract + conditional popleft
# ---------------------------------------------------------------------------

class TestReaderLoopFlagContract(unittest.TestCase):
    def _run_reader(self, msgs, req=None):
        bridge = SdkBridge()
        client = _FakeReaderClient(msgs)
        state = _UserStreamState(client=client, model=None)
        state.last_session_id = "sess-1"

        async def _drive():
            r = req or _make_req()
            state.pending.append(r)
            await bridge._reader_loop(1, state)
            return r

        loop = asyncio.new_event_loop()
        try:
            r = loop.run_until_complete(_drive())
        finally:
            loop.close()
        return state, r, client

    def test_parent_tool_use_id_sets_activity_flag(self):
        inner = _make_assistant_msg(tool_names=["Bash"], parent_tool_use_id="p-1")
        state, req, _ = self._run_reader([inner])
        self.assertTrue(req.subagent_activity)
        self.assertEqual(req.tool_use_count, 0)  # inner msgs still not counted

    def test_task_block_sets_activity_flag(self):
        msg = AssistantMessage(
            content=[_make_task_block()], model="m", parent_tool_use_id=None
        )
        state, req, _ = self._run_reader([msg])
        self.assertTrue(req.subagent_activity)
        self.assertFalse(req.background_task_launched)

    def test_background_task_sets_background_flag(self):
        msg = AssistantMessage(
            content=[_make_task_block(run_in_background=True)],
            model="m",
            parent_tool_use_id=None,
        )
        state, req, _ = self._run_reader([msg])
        self.assertTrue(req.subagent_activity)
        self.assertTrue(req.background_task_launched)

    def test_non_task_tools_do_not_set_activity(self):
        msg = _make_assistant_msg(tool_names=["Read", "Bash", "Edit"])
        state, req, _ = self._run_reader([msg])
        self.assertFalse(req.subagent_activity)
        self.assertEqual(req.tool_use_count, 3)

    def test_retry_keeps_request_at_head(self):
        """T6: on a flake retry the request is NOT popped from pending."""
        task_msg = AssistantMessage(
            content=[_make_task_block()], model="m", parent_tool_use_id=None
        )
        flake_msg = _make_assistant_msg(text=FLAKE_TEXT)
        state, req, client = self._run_reader(
            [task_msg, flake_msg, _make_result_msg(FLAKE_TEXT)]
        )
        self.assertEqual(len(state.pending), 1)
        self.assertIs(state.pending[0], req)
        client.query.assert_awaited_once()
        self.assertIn("[BRIDGE FLAKE RETRY", client.query.call_args[0][0])
        self.assertFalse(req.future.done())

    def test_normal_result_pops_request(self):
        """T6: the non-retry path keeps today's popleft contract."""
        text_msg = _make_assistant_msg(text="all done")
        state, req, client = self._run_reader(
            [text_msg, _make_result_msg("all done")]
        )
        self.assertEqual(len(state.pending), 0)
        client.query.assert_not_awaited()
        self.assertTrue(req.future.done())
        self.assertEqual(req.future.result().content, "all done")


# ---------------------------------------------------------------------------
# DGN-670 F1: PreToolUse executor-contract injection (prevention)
# ---------------------------------------------------------------------------

class TestExecutorContractHook(unittest.TestCase):
    def _call(self, input_data):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _task_executor_contract_hook(input_data, "tu-1", {"signal": None})
            )
        finally:
            loop.close()

    def test_task_prompt_rewritten(self):
        out = self._call(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Task",
                "tool_input": {"prompt": "do X", "subagent_type": "general-purpose"},
            }
        )
        spec = out.get("hookSpecificOutput")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["hookEventName"], "PreToolUse")
        updated = spec["updatedInput"]
        self.assertTrue(updated["prompt"].startswith(_EXECUTOR_CONTRACT_MARKER))
        self.assertTrue(updated["prompt"].endswith("do X"))
        # Non-prompt fields preserved (Task tool-input schema intact).
        self.assertEqual(updated["subagent_type"], "general-purpose")

    def test_idempotent_no_double_inject(self):
        prompt = _EXECUTOR_CONTRACT_PREFIX + "do X"
        out = self._call(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Task",
                "tool_input": {"prompt": prompt},
            }
        )
        self.assertEqual(out, {})

    def test_marker_anywhere_suppresses_inject(self):
        # Model followed the DGN-086 system-prompt instruction and embedded
        # the contract line itself -> no prefixing.
        out = self._call(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Task",
                "tool_input": {
                    "prompt": "Context first.\n" + _EXECUTOR_CONTRACT_MARKER + ". Do X."
                },
            }
        )
        self.assertEqual(out, {})

    def test_non_task_untouched(self):
        out = self._call(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            }
        )
        self.assertEqual(out, {})

    def test_malformed_input_fail_silent(self):
        self.assertEqual(self._call(None), {})
        self.assertEqual(self._call({"tool_name": "Task"}), {})
        self.assertEqual(
            self._call({"tool_name": "Task", "tool_input": {"prompt": 42}}), {}
        )
        self.assertEqual(
            self._call({"tool_name": "Task", "tool_input": "not-a-dict"}), {}
        )

    def test_contract_prefix_never_self_triggers_regex(self):
        self.assertIsNone(_PLACEHOLDER_FLAKE_RE.search(_EXECUTOR_CONTRACT_PREFIX))

    def test_hook_wired_into_options(self):
        """The stream options carry a PreToolUse HookMatcher('Task') pointing
        at the contract hook (can_use_tool is dead for allowed_tools calls)."""
        async def _run():
            with patch("bridge.sdk_bridge.ClaudeSDKClient") as MockClient:
                instance = MockClient.return_value
                instance.connect = AsyncMock()

                async def _empty_stream():
                    return
                    yield  # pragma: no cover

                instance.receive_messages = _empty_stream
                bridge = SdkBridge()
                state = await bridge._create_user_stream(1, None)
                for task in (state.reader_task, state.typing_task):
                    if task:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                opts = MockClient.call_args.kwargs["options"]
                return opts

        loop = asyncio.new_event_loop()
        try:
            opts = loop.run_until_complete(_run())
        finally:
            loop.close()
        hooks = opts.hooks
        self.assertIn("PreToolUse", hooks)
        matcher = hooks["PreToolUse"][0]
        self.assertEqual(matcher.matcher, "Task")
        self.assertIn(_task_executor_contract_hook, matcher.hooks)
        # Task stays in allowed_tools -- the whole reason the hook is needed.
        self.assertIn("Task", opts.allowed_tools)


if __name__ == "__main__":
    unittest.main()
