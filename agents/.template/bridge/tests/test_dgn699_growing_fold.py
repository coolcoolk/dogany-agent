"""DGN-699: growing-fold streaming (spec v2, grill-locked).

Covers:
- D1 two-phase render: live = non-expandable <blockquote> HTML, finalize =
  plain caption line + <blockquote expandable>.
- D2 dedicated dispatch: fold bubble separate from streaming drafts;
  fold_msg_id never enters ChatResponse.draft_message_ids (D4).
- D3 time-dominant throttle + non-blocking RetryAfter + finalize tail flush.
- D5 dedup backstop: normalized FULL-match only, containment forbidden,
  non-empty floor.
- D6 rolling window: fold fits its own 4096 UTF-16 budget, cut marked with
  the locked truncation line.
- D7 lifecycle: normal / empty-drop / is_error / timeout / stop / disconnect
  confirm the grown fold (collapse + locked caption), never delete it.
- D8 lazy creation: short turns never open a bubble and keep the DGN-682
  finalize-time compose fallback.
"""

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from telegram.error import BadRequest, RetryAfter

# conftest.py already sets PROJECT_ROOT and TELEGRAM_BOT_TOKEN before import.
from bridge.formatting import (
    FOLD_CAPTION_NORMAL,
    FOLD_CAPTION_STOPPED,
    FOLD_CAPTION_TIMEOUT,
    FOLD_TRUNCATION_LINE,
    _utf16_units,
    render_fold_final,
    render_fold_live,
)
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState
from bridge.streaming import edit_fold_html


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_assistant_msg(stop_reason: Optional[str], blocks: List[Any]) -> AssistantMessage:
    return AssistantMessage(
        content=blocks,
        model="claude-sonnet-4-5",
        stop_reason=stop_reason,
        parent_tool_use_id=None,
    )


def _make_result_msg(result: str = "", is_error: bool = False):
    rm = MagicMock(spec=ResultMessage)
    rm.session_id = "sess-test"
    rm.result = result
    rm.is_error = is_error
    rm.num_turns = 2
    return rm


def _mock_bot(message_id: int = 777) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=message_id))
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _mock_handler(bot: Optional[MagicMock] = None) -> MagicMock:
    handler = MagicMock()
    handler.bot = bot or _mock_bot()
    handler.drafts = []
    handler.update_if_needed = AsyncMock(return_value=True)
    handler.finalize_all = AsyncMock(return_value=True)
    handler.cancel = AsyncMock(return_value=True)
    return handler


def _make_pending_req(streaming_handler=None) -> _PendingRequest:
    future = asyncio.get_event_loop().create_future()
    return _PendingRequest(
        user_id=1,
        chat_id=1,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=future,
        streaming_handler=streaming_handler,
    )


def _edit_texts(bot: MagicMock) -> List[str]:
    return [c.kwargs.get("text", "") for c in bot.edit_message_text.call_args_list]


# ---------------------------------------------------------------------------
# D1/D6: pure render helpers
# ---------------------------------------------------------------------------


class TestFoldRender(unittest.TestCase):
    def test_live_render_is_plain_text(self):
        # Owner 2026-08-02: live phase streams as PLAIN TEXT, no blockquote.
        html = render_fold_live(["step one", "step two"])
        self.assertNotIn("<blockquote>", html)
        self.assertNotIn("<blockquote expandable>", html)
        self.assertIn("step one", html)
        self.assertIn("step two", html)

    def test_final_render_caption_inside_expandable_fold(self):
        # Owner 2026-08-02: caption is the FIRST line INSIDE the blockquote,
        # not a plain line above it.
        html = render_fold_final(["step one"], FOLD_CAPTION_NORMAL)
        self.assertTrue(html.startswith("<blockquote expandable>" + FOLD_CAPTION_NORMAL))
        self.assertIn("<blockquote expandable>", html)
        self.assertIn("step one", html)
        # Caption sits before the body content, inside the quote.
        self.assertLess(html.index(FOLD_CAPTION_NORMAL), html.index("step one"))
        self.assertGreater(
            html.index(FOLD_CAPTION_NORMAL), html.index("<blockquote expandable>") - 1
        )

    def test_locked_caption_copy(self):
        # Spec LOCK (owner A-case 2026-08-02): do not drift.
        self.assertEqual(FOLD_CAPTION_NORMAL, "진행 기록")
        self.assertEqual(FOLD_CAPTION_STOPPED, "중단됨 · 진행 기록")
        self.assertEqual(FOLD_CAPTION_TIMEOUT, "시간 초과 · 진행 기록")
        self.assertEqual(FOLD_TRUNCATION_LINE, "…(생략)")

    def test_html_escaping(self):
        html = render_fold_live(["a <tag> & stuff"])
        self.assertIn("&lt;tag&gt;", html)
        self.assertIn("&amp;", html)

    def test_code_fence_neutralized(self):
        html = render_fold_live(["```python\nprint(1)\n```"])
        self.assertNotIn("```", html)
        self.assertNotIn("<pre>", html)

    def test_display_markers_stripped(self):
        html = render_fold_live(["doing work\nsend_file:: /tmp/x.png\nmore work"])
        self.assertNotIn("send_file::", html)
        self.assertIn("more work", html)

    def test_rolling_window_truncates_front_keeps_tail(self):
        lines = ["line-%04d with some padding text to bulk it up" % i for i in range(400)]
        html = render_fold_final(["\n".join(lines)], FOLD_CAPTION_NORMAL)
        self.assertLessEqual(_utf16_units(html), 4096)
        self.assertIn(FOLD_TRUNCATION_LINE, html)
        self.assertIn("line-0399", html)  # newest window kept
        self.assertNotIn("line-0000", html)  # oldest rolled off
        self.assertIn("<blockquote expandable>", html)

    def test_small_fold_has_no_truncation_marker(self):
        html = render_fold_live(["short narration"])
        self.assertNotIn(FOLD_TRUNCATION_LINE, html)

    def test_empty_input_renders_empty(self):
        self.assertEqual(render_fold_live([]), "")
        self.assertEqual(render_fold_live(["   \n  "]), "")


# ---------------------------------------------------------------------------
# D5: dedup backstop
# ---------------------------------------------------------------------------


class TestDedupBackstop(unittest.TestCase):
    def test_exact_paragraph_match_removed(self):
        content = "I did step A\n\nFinal answer here"
        out = SdkBridge._dedup_final_against_interim(content, ["I did step A"])
        self.assertEqual(out, "Final answer here")

    def test_normalized_whitespace_match_removed(self):
        content = "I  did\nstep A\n\nResult"
        out = SdkBridge._dedup_final_against_interim(content, ["I did step A"])
        self.assertEqual(out, "Result")

    def test_containment_never_removes(self):
        # The short final answer is a SUBSTRING of the narration: must stay.
        content = "step A"
        out = SdkBridge._dedup_final_against_interim(
            content, ["I did step A and then step B"]
        )
        self.assertEqual(out, "step A")

    def test_non_empty_floor_skips_filter(self):
        content = "I did step A"
        out = SdkBridge._dedup_final_against_interim(content, ["I did step A"])
        self.assertEqual(out, "I did step A")

    def test_no_interim_passthrough(self):
        self.assertEqual(SdkBridge._dedup_final_against_interim("x", []), "x")


class TestFinalFullyInInterim(unittest.TestCase):
    """Owner 2026-08-02 (option 1): full-overlap predicate behind fold delete."""

    def test_all_paragraphs_present_is_full_dup(self):
        self.assertTrue(
            SdkBridge._final_fully_in_interim("A\n\nB", ["A", "B"])
        )

    def test_partial_overlap_is_not_full_dup(self):
        self.assertFalse(
            SdkBridge._final_fully_in_interim("A\n\nFinal answer", ["A", "B"])
        )

    def test_no_interim_is_false(self):
        self.assertFalse(SdkBridge._final_fully_in_interim("A", []))

    def test_empty_content_is_false(self):
        self.assertFalse(SdkBridge._final_fully_in_interim("", ["A"]))

    def test_normalized_whitespace_match(self):
        self.assertTrue(
            SdkBridge._final_fully_in_interim("A  did\nstep", ["A did step"])
        )


# ---------------------------------------------------------------------------
# D2/D3/D4/D7/D8: reader-loop integration
# ---------------------------------------------------------------------------


class TestGrowingFoldReaderLoop(unittest.TestCase):
    """Feed SDK message sequences through _reader_loop in fold mode."""

    def _run(self, messages_seq, fold_interval: float = 0.0, bot=None):
        async def _inner():
            handler = _mock_handler(bot=bot)
            bridge_obj = SdkBridge()
            req = _make_pending_req(streaming_handler=handler)
            state = _UserStreamState(client=MagicMock(), model=None)
            state.pending.append(req)
            req.sent = True

            async def fake_receive():
                for m in messages_seq:
                    yield m

            state.client.receive_messages = fake_receive
            bridge_obj._streams[1] = state

            with patch("bridge.sdk_bridge.INTERIM_MODE", "fold"), patch(
                "bridge.sdk_bridge.FOLD_UPDATE_INTERVAL", fold_interval
            ):
                await bridge_obj._reader_loop(1, state)

            result = await asyncio.wait_for(req.future, timeout=1.0)
            return result, handler, req

        return asyncio.run(_inner())

    def test_d8_single_short_interim_no_bubble_compose_fallback(self):
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="step one")]),
            _make_assistant_msg("end_turn", [TextBlock(text="final answer")]),
            _make_result_msg(result="final answer"),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.send_message.assert_not_awaited()  # no fold bubble opened
        self.assertIsNone(req.fold_msg_id)
        # DGN-682 finalize-time compose fallback still applies.
        self.assertTrue(response.content.startswith(">! step one"))
        self.assertIn("final answer", response.content)

    def test_dgn710_compose_fallback_partial_dup_trimmed(self):
        # DGN-710: opening streamed as a non-terminal chunk (captured as
        # interim), then the end_turn body re-included it. The compose
        # fallback must trim the duplicated opening from the body so it is
        # NOT shown twice (once in the fold, once in the body).
        opening = "확인됐습니다 -- 배선이 살아있다는 첫 실증입니다."
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text=opening)]),
            _make_assistant_msg(
                "end_turn", [TextBlock(text=opening + "\n\n스컬이 두 건을 물고 있습니다.")]
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        self.assertIsNone(req.fold_msg_id)
        # Opening appears exactly once (inside the fold), never repeated in body.
        self.assertEqual(response.content.count(opening), 1)
        self.assertTrue(response.content.startswith(">! " + opening))
        self.assertIn("스컬이 두 건을", response.content)

    def test_dgn710_compose_fallback_full_dup_drops_fold(self):
        # DGN-710: when the whole final body already sits in the captured
        # interim, no fold is attached -- the clean final stands alone
        # (symmetric with the grown-bubble full-dup delete).
        whole = "확인됐습니다 -- 첫 실증입니다."
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text=whole)]),
            _make_assistant_msg("end_turn", [TextBlock(text=whole)]),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        self.assertIsNone(req.fold_msg_id)
        self.assertNotIn(">!", response.content)  # no fold prepended
        self.assertEqual(response.content, whole)

    def test_d2_d4_second_interim_creates_bubble_and_confirms(self):
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="step one")]),
            _make_assistant_msg("tool_use", [TextBlock(text="step two")]),
            _make_assistant_msg("end_turn", [TextBlock(text="final answer")]),
            _make_result_msg(result="final answer"),
        ]
        response, handler, req = self._run(msgs)
        # Bubble created once.
        handler.bot.send_message.assert_awaited_once()
        create_kwargs = handler.bot.send_message.call_args.kwargs
        self.assertEqual(create_kwargs.get("parse_mode"), "HTML")
        # Owner 2026-08-02: live bubble is plain text (no blockquote).
        self.assertNotIn("<blockquote>", create_kwargs.get("text", ""))
        self.assertIn("step one", create_kwargs.get("text", ""))
        self.assertEqual(req.fold_msg_id, 777)
        # D4: fold bubble is NOT a draft; the final answer body is clean.
        self.assertNotIn(777, response.draft_message_ids)
        self.assertEqual(response.content, "final answer")
        self.assertNotIn(">!", response.content)  # no compose fold prepended
        # D7 normal end: confirm edit carries the locked caption + collapse.
        final_html = _edit_texts(handler.bot)[-1]
        self.assertTrue(final_html.startswith("<blockquote expandable>" + FOLD_CAPTION_NORMAL))
        self.assertIn("<blockquote expandable>", final_html)

    def test_d3_throttle_time_gate_and_tail_flush(self):
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="step one")]),
            _make_assistant_msg("tool_use", [TextBlock(text="step two")]),
            _make_assistant_msg("tool_use", [TextBlock(text="step three")]),
            _make_assistant_msg("end_turn", [TextBlock(text="done")]),
            _make_result_msg(result="done"),
        ]
        # Large interval: the third interim is time-gated (no live edit) but
        # the finalize swap flushes it (tail flush).
        response, handler, req = self._run(msgs, fold_interval=999.0)
        self.assertEqual(handler.bot.edit_message_text.await_count, 1)
        final_html = _edit_texts(handler.bot)[-1]
        self.assertIn("step three", final_html)
        self.assertIn(FOLD_CAPTION_NORMAL, final_html)

    def test_d3_zero_interval_live_edit_happens(self):
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="step one")]),
            _make_assistant_msg("tool_use", [TextBlock(text="step two")]),
            _make_assistant_msg("tool_use", [TextBlock(text="step three")]),
            _make_assistant_msg("end_turn", [TextBlock(text="done")]),
            _make_result_msg(result="done"),
        ]
        response, handler, req = self._run(msgs, fold_interval=0.0)
        # One live growth edit (3rd interim) + one finalize swap.
        self.assertEqual(handler.bot.edit_message_text.await_count, 2)
        live_html = _edit_texts(handler.bot)[0]
        # Owner 2026-08-02: live growth edit is plain text (no blockquote).
        self.assertNotIn("<blockquote>", live_html)
        self.assertNotIn("<blockquote expandable>", live_html)
        self.assertIn("step three", live_html)

    def test_d5_final_body_dedup_on_grown_fold_turn(self):
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="I did step A")]),
            _make_assistant_msg("tool_use", [TextBlock(text="I did step B")]),
            _make_assistant_msg(
                "end_turn", [TextBlock(text="I did step A\n\nFinal answer here")]
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        self.assertEqual(response.content, "Final answer here")

    def test_full_dup_final_deletes_fold(self):
        # Owner 2026-08-02 (option 1): when EVERY final paragraph was already
        # narrated live, the collapsed fold would only echo the answer -> the
        # bubble is deleted and the clean final stands alone (no floor dup).
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="I did step A")]),
            _make_assistant_msg("tool_use", [TextBlock(text="I did step B")]),
            _make_assistant_msg(
                "end_turn", [TextBlock(text="I did step A\n\nI did step B")]
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.delete_message.assert_awaited_once()
        self.assertTrue(req.fold_finalized)  # latched -> normal finalize is a no-op
        # Clean final answer stands alone -- not emptied, not duplicated.
        self.assertEqual(response.content, "I did step A\n\nI did step B")

    def test_d7_is_error_confirms_fold_no_attach(self):
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="step one")]),
            _make_assistant_msg("tool_use", [TextBlock(text="step two")]),
            _make_result_msg(result="upstream connect error", is_error=True),
        ]
        response, handler, req = self._run(msgs)
        self.assertFalse(response.success)
        # Error notice carries no fold markup (DGN-682 D9 preserved).
        self.assertNotIn("<blockquote", response.content)
        self.assertNotIn(">!", response.content)
        # Grown fold confirmed with the stop caption.
        final_html = _edit_texts(handler.bot)[-1]
        self.assertTrue(final_html.startswith("<blockquote expandable>" + FOLD_CAPTION_STOPPED))
        self.assertIn("<blockquote expandable>", final_html)
        self.assertTrue(req.fold_finalized)

    def test_tool_use_blocks_do_not_open_fold(self):
        msgs = [
            _make_assistant_msg(
                "tool_use",
                [ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
            ),
            _make_assistant_msg("end_turn", [TextBlock(text="final")]),
            _make_result_msg(result="final"),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.send_message.assert_not_awaited()
        self.assertEqual(response.content, "final")

    def test_suppress_mode_untouched(self):
        async def _inner():
            handler = _mock_handler()
            bridge_obj = SdkBridge()
            req = _make_pending_req(streaming_handler=handler)
            state = _UserStreamState(client=MagicMock(), model=None)
            state.pending.append(req)
            req.sent = True

            msgs = [
                _make_assistant_msg("tool_use", [TextBlock(text="interim")]),
                _make_assistant_msg("end_turn", [TextBlock(text="final")]),
                _make_result_msg(result="final"),
            ]

            async def fake_receive():
                for m in msgs:
                    yield m

            state.client.receive_messages = fake_receive
            bridge_obj._streams[1] = state
            with patch("bridge.sdk_bridge.INTERIM_MODE", "suppress"), patch(
                "bridge.sdk_bridge.STREAM_INTERIM", False
            ):
                await bridge_obj._reader_loop(1, state)
            result = await asyncio.wait_for(req.future, timeout=1.0)
            return result, handler, req

        response, handler, req = asyncio.run(_inner())
        handler.bot.send_message.assert_not_awaited()
        self.assertEqual(req.fold_buf, [])
        self.assertIsNone(req.fold_msg_id)
        self.assertEqual(response.content, "final")


# ---------------------------------------------------------------------------
# D7: direct lifecycle paths (empty-drop / timeout / stop / disconnect)
# ---------------------------------------------------------------------------


class TestFoldLifecyclePaths(unittest.TestCase):
    def _grown_req(self):
        handler = _mock_handler()
        req = _make_pending_req(streaming_handler=handler)
        req.fold_msg_id = 777
        req.fold_buf = ["step one", "step two"]
        req.interim_texts = list(req.fold_buf)
        return req, handler

    def test_empty_drop_confirms_fold(self):
        async def _inner():
            bridge_obj = SdkBridge()
            req, handler = self._grown_req()
            state = _UserStreamState(client=MagicMock(), model=None)
            with patch("bridge.sdk_bridge.INTERIM_MODE", "fold"):
                await bridge_obj._finalize_result(
                    1, state, req, _make_result_msg(result="", is_error=False)
                )
            return req, handler

        req, handler = asyncio.run(_inner())
        # Turn dropped silently (future untouched) but fold confirmed.
        self.assertFalse(req.future.done())
        self.assertTrue(req.fold_finalized)
        final_html = _edit_texts(handler.bot)[-1]
        self.assertTrue(final_html.startswith("<blockquote expandable>" + FOLD_CAPTION_NORMAL))

    def test_timeout_preserve_recognizes_fold_only_turn(self):
        async def _inner():
            bridge_obj = SdkBridge()
            req, handler = self._grown_req()
            state = _UserStreamState(client=MagicMock(), model=None)
            state.client.disconnect = AsyncMock()
            state.last_session_id = "sess-1"
            state.pending.append(req)
            bridge_obj._streams[1] = state
            sid, partial = await bridge_obj.handle_timeout_preserve(1)
            return sid, partial, handler, req

        sid, partial, handler, req = asyncio.run(_inner())
        self.assertTrue(partial)  # fold-only turn counts as preserved output
        self.assertTrue(req.fold_finalized)
        timeout_html = _edit_texts(handler.bot)[0]
        self.assertTrue(timeout_html.startswith("<blockquote expandable>" + FOLD_CAPTION_TIMEOUT))

    def test_stop_confirms_fold_with_stop_caption(self):
        async def _inner():
            bridge_obj = SdkBridge()
            req, handler = self._grown_req()
            state = _UserStreamState(client=MagicMock(), model=None)
            state.pending.append(req)
            bridge_obj._streams[1] = state
            cancelled = await bridge_obj.cancel_user_streaming(1)
            return cancelled, handler, req

        cancelled, handler, req = asyncio.run(_inner())
        self.assertTrue(cancelled)
        self.assertTrue(req.fold_finalized)
        stop_html = _edit_texts(handler.bot)[0]
        self.assertTrue(stop_html.startswith("<blockquote expandable>" + FOLD_CAPTION_STOPPED))
        # The fold bubble is never deleted.
        handler.bot.delete_message.assert_not_called()

    def test_disconnect_cleanup_hook_confirms_orphan_fold(self):
        async def _inner():
            bridge_obj = SdkBridge()
            req, handler = self._grown_req()
            state = _UserStreamState(client=MagicMock(), model=None)
            state.client.disconnect = AsyncMock()
            state.pending.append(req)
            bridge_obj._streams[1] = state
            await bridge_obj._disconnect_user_stream(1)
            return handler, req

        handler, req = asyncio.run(_inner())
        self.assertTrue(req.fold_finalized)
        html = _edit_texts(handler.bot)[0]
        self.assertTrue(html.startswith("<blockquote expandable>" + FOLD_CAPTION_STOPPED))

    def test_fold_finalize_idempotent(self):
        async def _inner():
            bridge_obj = SdkBridge()
            req, handler = self._grown_req()
            first = await bridge_obj._fold_finalize(req, FOLD_CAPTION_STOPPED)
            second = await bridge_obj._fold_finalize(req, FOLD_CAPTION_NORMAL)
            return first, second, handler

        first, second, handler = asyncio.run(_inner())
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(handler.bot.edit_message_text.await_count, 1)


# NOTE (DGN-719 resync): the vendored test
# test_user_has_streamed_output_sees_fold_only_turn is NOT ported --
# SdkBridge.user_has_streamed_output belongs to DGN-163 (turn-death safety
# net), which is not present in this repo. Port it together with DGN-163.


# ---------------------------------------------------------------------------
# D3: non-blocking RetryAfter semantics
# ---------------------------------------------------------------------------


class TestNonBlockingRetryAfter(unittest.TestCase):
    def test_edit_fold_html_returns_hint_without_sleeping(self):
        async def _inner():
            bot = MagicMock()
            bot.edit_message_text = AsyncMock(side_effect=RetryAfter(5))
            loop = asyncio.get_event_loop()
            t0 = loop.time()
            ok, wait = await edit_fold_html(bot, 1, 777, "<blockquote>x</blockquote>")
            elapsed = loop.time() - t0
            return ok, wait, elapsed

        ok, wait, elapsed = asyncio.run(_inner())
        self.assertFalse(ok)
        self.assertEqual(wait, 5.0)
        self.assertLess(elapsed, 0.5)  # never slept on the reader path

    def test_dispatch_backoff_gates_next_edit(self):
        async def _inner():
            bridge_obj = SdkBridge()
            handler = _mock_handler()
            handler.bot.edit_message_text = AsyncMock(side_effect=RetryAfter(60))
            req = _make_pending_req(streaming_handler=handler)
            req.fold_msg_id = 777
            req.fold_buf = ["step one"]
            with patch("bridge.sdk_bridge.FOLD_UPDATE_INTERVAL", 0.0):
                await bridge_obj._fold_dispatch(req, "step two")  # rate-limited
                await bridge_obj._fold_dispatch(req, "step three")  # backoff-gated
            return handler

        handler = asyncio.run(_inner())
        # Only the FIRST attempt hit the API; the second was gated locally.
        self.assertEqual(handler.bot.edit_message_text.await_count, 1)


# ---------------------------------------------------------------------------
# FATAL-1: system_prompt fold fragment gating
# The fold fragment must appear ONLY when effective interim mode is "fold".
# Modes suppress / inline / off must receive the base SYSTEM_PROMPT untouched.
# ---------------------------------------------------------------------------


class TestFoldFragmentGating(unittest.TestCase):
    """Invariant: fold fragment injected iff effective mode == 'fold'."""

    def _assembled_system_prompt(self, mode_patch: str) -> str:
        """Return the system_prompt string that _create_user_stream would build."""
        from bridge import messages

        # Replicate the gate logic from sdk_bridge._create_user_stream.
        import bridge.sdk_bridge as _sdkmod

        base = messages.SYSTEM_PROMPT
        with patch.object(_sdkmod, "INTERIM_MODE", mode_patch):
            if _sdkmod._effective_interim_mode() == "fold":
                return base + messages.SYSTEM_PROMPT_FOLD_FRAGMENT
        return base

    def _fragment_text(self) -> str:
        from bridge import messages

        return messages.SYSTEM_PROMPT_FOLD_FRAGMENT

    def test_fold_mode_injects_fragment(self):
        result = self._assembled_system_prompt("fold")
        self.assertIn("Progress Narration vs Final Answer", result)
        self.assertIn(self._fragment_text(), result)

    def test_suppress_mode_no_fragment(self):
        result = self._assembled_system_prompt("suppress")
        self.assertNotIn("Progress Narration vs Final Answer", result)

    def test_inline_mode_no_fragment(self):
        result = self._assembled_system_prompt("inline")
        self.assertNotIn("Progress Narration vs Final Answer", result)

    def test_off_mode_no_fragment(self):
        # "off" is a valid INTERIM_MODE value (legacy compatibility).
        result = self._assembled_system_prompt("off")
        self.assertNotIn("Progress Narration vs Final Answer", result)

    def test_base_system_prompt_unchanged_in_non_fold_modes(self):
        from bridge import messages

        for mode in ("suppress", "inline", "off"):
            with self.subTest(mode=mode):
                result = self._assembled_system_prompt(mode)
                self.assertEqual(result, messages.SYSTEM_PROMPT)


# ---------------------------------------------------------------------------
# MAJOR-1: fold HTML -> plain fallback on BadRequest (400)
# ---------------------------------------------------------------------------


class TestFoldHtmlPlainFallback(unittest.TestCase):
    """400 BadRequest on HTML -> degrade once to plain text, never drop."""

    def test_send_fold_html_degrades_on_bad_request(self):
        """HTML 400 -> plain send succeeds -> returns message_id."""

        async def _inner():
            from bridge.streaming import send_fold_html

            bot = MagicMock()
            call_count = [0]

            async def _send(**kwargs):
                call_count[0] += 1
                if kwargs.get("parse_mode") == "HTML":
                    raise BadRequest("Can't parse entities")
                # Plain send succeeds
                return SimpleNamespace(message_id=999)

            bot.send_message = _send
            mid = await send_fold_html(
                bot, 1, "<blockquote>text with <unclosed</blockquote>"
            )
            return mid, call_count[0]

        mid, calls = asyncio.run(_inner())
        self.assertEqual(mid, 999)
        self.assertEqual(calls, 2)  # HTML attempt + plain attempt

    def test_edit_fold_html_degrades_on_bad_request(self):
        """HTML 400 on edit -> plain edit succeeds -> returns (True, 0)."""

        async def _inner():
            from bridge.streaming import edit_fold_html

            bot = MagicMock()
            call_count = [0]

            async def _edit(**kwargs):
                call_count[0] += 1
                if kwargs.get("parse_mode") == "HTML":
                    raise BadRequest("Can't parse entities: unrecognized tag")
                # Plain edit succeeds (returns truthy)
                return True

            bot.edit_message_text = _edit
            ok, retry_after = await edit_fold_html(
                bot, 1, 777, "<blockquote>text </blockquote> extra"
            )
            return ok, retry_after, call_count[0]

        ok, retry_after, calls = asyncio.run(_inner())
        self.assertTrue(ok)
        self.assertEqual(retry_after, 0.0)
        self.assertEqual(calls, 2)

    def test_finalize_fold_html_degrades_on_bad_request(self):
        """HTML 400 on finalize -> plain edit succeeds -> returns True."""

        async def _inner():
            from bridge.streaming import finalize_fold_html

            bot = MagicMock()
            call_count = [0]

            async def _edit(**kwargs):
                call_count[0] += 1
                if kwargs.get("parse_mode") == "HTML":
                    raise BadRequest("Can't parse entities: </blockquote>")
                return True

            bot.edit_message_text = _edit
            ok = await finalize_fold_html(
                bot, 1, 777, "진행 기록\n\n<blockquote expandable>text</blockquote>"
            )
            return ok, call_count[0]

        ok, calls = asyncio.run(_inner())
        self.assertTrue(ok)
        self.assertEqual(calls, 2)

    def test_send_fold_html_plain_fallback_also_fails_logs_and_returns_none(self):
        """Both HTML and plain send fail -> None returned, no exception raised."""

        async def _inner():
            from bridge.streaming import send_fold_html

            bot = MagicMock()
            bot.send_message = AsyncMock(side_effect=BadRequest("rejected"))
            return await send_fold_html(bot, 1, "<blockquote>bad</blockquote>")

        mid = asyncio.run(_inner())
        self.assertIsNone(mid)


if __name__ == "__main__":
    unittest.main()
