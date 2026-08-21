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
- DGN-777 final-sacred: the final answer keeps every word, always; overlap
  is subtracted from the FOLD (_subtract_paras), never from the answer.
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
        # Spec LOCK (owner A-case 2026-08-02): do not drift. DGN-851 moved
        # the caption TEXT to the bridge i18n catalogs; the ko catalog
        # carries the locked copy, and the module constants must bind
        # through the catalogs (active locale, en fallback) -- never
        # diverge from both.
        from bridge.i18n import en, ko
        self.assertEqual(ko.STRINGS["fold_caption_normal"], "진행 기록")
        self.assertEqual(ko.STRINGS["fold_caption_stopped"], "중단됨 · 진행 기록")
        self.assertEqual(ko.STRINGS["fold_caption_timeout"], "시간 초과 · 진행 기록")
        self.assertEqual(ko.STRINGS["fold_truncation_line"], "…(생략)")
        for key, const in (
            ("fold_caption_normal", FOLD_CAPTION_NORMAL),
            ("fold_caption_stopped", FOLD_CAPTION_STOPPED),
            ("fold_caption_timeout", FOLD_CAPTION_TIMEOUT),
            ("fold_truncation_line", FOLD_TRUNCATION_LINE),
        ):
            self.assertIn(const, (ko.STRINGS[key], en.STRINGS[key]))

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


class TestSubtractParas(unittest.TestCase):
    """DGN-777 final-sacred: overlap is subtracted from the FOLD, not the
    answer -- pure helper contract."""

    def test_partial_overlap_removes_only_matching_entries(self):
        out = SdkBridge._subtract_paras(
            ["I did step A", "I did step B"], "I did step A\n\nFinal answer"
        )
        self.assertEqual(out, ["I did step B"])

    def test_multi_paragraph_entry_split_and_rejoined(self):
        # A single fold entry may hold several paragraphs: only the
        # overlapping paragraph is dropped, the rest re-join in order.
        out = SdkBridge._subtract_paras(
            ["P1\n\nP2\n\nP3"], "P2\n\nFinal answer"
        )
        self.assertEqual(out, ["P1\n\nP3"])

    def test_no_overlap_unchanged(self):
        fold = ["step one", "step two"]
        out = SdkBridge._subtract_paras(fold, "Completely new answer")
        self.assertEqual(out, ["step one", "step two"])

    def test_all_overlap_returns_empty_list(self):
        out = SdkBridge._subtract_paras(["A", "B"], "A\n\nB\n\nC")
        self.assertEqual(out, [])

    def test_normalized_whitespace_match(self):
        out = SdkBridge._subtract_paras(
            ["I  did\nstep A", "other"], "I did step A\n\nRest"
        )
        self.assertEqual(out, ["other"])

    def test_containment_never_removes(self):
        # Fold paragraph is a SUBSTRING of a final paragraph: must stay
        # (normalized FULL-paragraph match only, same as the dedup helpers).
        out = SdkBridge._subtract_paras(
            ["step A"], "I did step A and then step B"
        )
        self.assertEqual(out, ["step A"])

    def test_empty_final_passthrough(self):
        fold = ["step one"]
        self.assertEqual(SdkBridge._subtract_paras(fold, ""), fold)

    def test_empty_fold_passthrough(self):
        self.assertEqual(SdkBridge._subtract_paras([], "answer"), [])

    def test_order_preserved(self):
        out = SdkBridge._subtract_paras(
            ["one", "dup", "two", "three"], "dup\n\nanswer"
        )
        self.assertEqual(out, ["one", "two", "three"])


# ---------------------------------------------------------------------------
# DGN-876: superset fold trim -- extra progress paragraphs survive
# ---------------------------------------------------------------------------


class TestDgn876FoldTrim(unittest.TestCase):
    """DGN-876: _subtract_paras unification removes the full-dup fast-path.
    Extra (non-duplicated) progress paragraphs must survive even when the
    final answer fully duplicates the narrated subset."""

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

    # (A) interim == final -> fold DELETED (grown path)
    def test_a_grown_full_dup_deletes_fold(self):
        whole = "I did step A\n\nI did step B"
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="I did step A")]),
            _make_assistant_msg("tool_use", [TextBlock(text="I did step B")]),
            _make_assistant_msg("end_turn", [TextBlock(text=whole)]),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.delete_message.assert_awaited_once()
        self.assertTrue(req.fold_finalized)
        self.assertEqual(response.content, whole)

    # (A) interim == final -> live bubble opened, then emptied -> deleted
    # (DGN-930 1st-interim gate; was the compose no-fold path).
    def test_a_single_interim_full_dup_deletes_bubble(self):
        whole = "step one"
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text=whole)]),
            _make_assistant_msg("end_turn", [TextBlock(text=whole)]),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.send_message.assert_awaited_once()
        handler.bot.delete_message.assert_awaited_once()
        self.assertTrue(req.fold_finalized)
        self.assertNotIn(">!", response.content)
        self.assertEqual(response.content, whole)

    # (B) interim STRICT SUPERSET of final -> fold KEPT with extra paragraphs
    # (grown bubble path -- the DGN-876 bug fix)
    def test_b_grown_superset_keeps_extra_progress_para(self):
        # Interim has: "extra progress para" + "final para A".
        # Final answer is only: "final para A" + "final para B".
        # Old behavior: _final_fully_in_interim returns False (not fully in),
        #   then _subtract_paras removes "final para A" -> fold has "extra progress para".
        # Also correct after fix. But the true bug: interim narrated "extra progress para"
        # AND "final para A" AND "final para B" -- all of final IS in interim.
        # Old code: _final_fully_in_interim=True -> delete entire fold (BUG).
        # New code: _subtract_paras removes A+B -> fold keeps "extra progress para".
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="extra progress para")]),
            _make_assistant_msg("tool_use", [TextBlock(text="final para A")]),
            _make_assistant_msg("tool_use", [TextBlock(text="final para B")]),
            _make_assistant_msg(
                "end_turn",
                [TextBlock(text="final para A\n\nfinal para B")],
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        # Fold must NOT be deleted -- extra progress para survives.
        handler.bot.delete_message.assert_not_called()
        # "extra progress para" must be in the surviving fold_buf.
        self.assertIn("extra progress para", req.fold_buf)
        # Final paragraphs subtracted from fold.
        combined_fold = " ".join(req.fold_buf)
        self.assertNotIn("final para A", combined_fold)
        self.assertNotIn("final para B", combined_fold)
        # Answer untouched.
        self.assertEqual(response.content, "final para A\n\nfinal para B")
        # No new send_message beyond the fold bubble itself.
        # (Only one send for fold creation, no extra Telegram message for the trim.)
        self.assertEqual(handler.bot.send_message.await_count, 1)

    # (B) DGN-930 1st-interim gate: single interim superset of final -> live
    # bubble opened; survivor kept in the GROWN fold (not compose-prepended).
    def test_b_single_interim_superset_keeps_survivor_in_grown_fold(self):
        # Interim: "extra step\n\nfinal answer" (superset).
        # Final: "final answer".
        # After subtract: "extra step" survives -> grown fold confirmed.
        msgs = [
            _make_assistant_msg(
                "tool_use",
                [TextBlock(text="extra step\n\nfinal answer")],
            ),
            _make_assistant_msg("end_turn", [TextBlock(text="final answer")]),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.send_message.assert_awaited_once()
        handler.bot.delete_message.assert_not_called()
        # Final answer stands alone, not duplicated, no compose fold prepended.
        self.assertEqual(response.content, "final answer")
        self.assertNotIn(">!", response.content)
        # Grown fold keeps the extra step, overlap subtracted.
        self.assertEqual(req.fold_buf, ["extra step"])
        final_html = _edit_texts(handler.bot)[-1]
        self.assertIn("extra step", final_html)

    # (C) partial overlap -> trim behaves as before (unchanged)
    def test_c_partial_overlap_trims_correctly_grown(self):
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="I did step A")]),
            _make_assistant_msg("tool_use", [TextBlock(text="I did step B")]),
            _make_assistant_msg(
                "end_turn",
                [TextBlock(text="I did step A\n\nFinal answer here")],
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        # Answer untouched.
        self.assertEqual(response.content, "I did step A\n\nFinal answer here")
        # Fold kept: step B survives, step A subtracted.
        handler.bot.delete_message.assert_not_called()
        self.assertEqual(req.fold_buf, ["I did step B"])

    # DGN-832 Notification-0: trim-and-keep path sends NO new Telegram message.
    # On the trim-and-keep path only edit_message_text (finalize) is called,
    # never a second send_message.
    def test_notification0_trim_and_keep_no_new_send(self):
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="extra progress para")]),
            _make_assistant_msg("tool_use", [TextBlock(text="final para A")]),
            _make_assistant_msg("tool_use", [TextBlock(text="final para B")]),
            _make_assistant_msg(
                "end_turn",
                [TextBlock(text="final para A\n\nfinal para B")],
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        # Only the fold bubble creation send -- no additional message for trim.
        self.assertEqual(handler.bot.send_message.await_count, 1)
        # Final answer delivered without a new send (goes through future.set_result).
        self.assertEqual(response.content, "final para A\n\nfinal para B")


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

    def test_dgn930_single_interim_opens_live_bubble(self):
        # DGN-930: the creation gate is now the 1st interim (live-then-fold),
        # so a single interim block opens the live progress bubble instead of
        # falling through to the finalize-time compose fallback. The final
        # answer is a separate message; the fold bubble confirms with the
        # locked caption.
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="step one")]),
            _make_assistant_msg("end_turn", [TextBlock(text="final answer")]),
            _make_result_msg(result="final answer"),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.send_message.assert_awaited_once()  # live bubble opened
        self.assertEqual(req.fold_msg_id, 777)
        # Final answer stands alone -- no compose fold prepended (D4).
        self.assertEqual(response.content, "final answer")
        self.assertNotIn(">!", response.content)
        self.assertNotIn(777, response.draft_message_ids)
        # Fold confirmed with the normal caption + collapse.
        final_html = _edit_texts(handler.bot)[-1]
        self.assertTrue(
            final_html.startswith("<blockquote expandable>" + FOLD_CAPTION_NORMAL)
        )

    def test_dgn930_partial_dup_body_kept_whole_fold_emptied_deletes(self):
        # DGN-710 + DGN-777 final-sacred, under the DGN-930 1st-interim gate:
        # the opening streams as a live bubble, then the end_turn body
        # re-includes it. The body keeps every word; the duplicated opening is
        # subtracted from the FOLD. Here the capture holds nothing else, so the
        # emptied fold bubble is DELETED and the clean body stands alone.
        opening = "확인됐습니다 -- 배선이 살아있다는 첫 실증입니다."
        final_body = opening + "\n\n스컬이 두 건을 물고 있습니다."
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text=opening)]),
            _make_assistant_msg("end_turn", [TextBlock(text=final_body)]),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        # Live bubble opened then emptied by subtraction -> deleted.
        handler.bot.send_message.assert_awaited_once()
        handler.bot.delete_message.assert_awaited_once()
        self.assertTrue(req.fold_finalized)
        # Opening appears exactly once, in the WHOLE body -- never trimmed.
        self.assertEqual(response.content.count(opening), 1)
        self.assertNotIn(">!", response.content)
        self.assertEqual(response.content, final_body)

    def test_dgn930_single_interim_two_paras_keeps_nonoverlap_in_grown_fold(self):
        # DGN-777 under the DGN-930 1st-interim gate: the single interim block
        # holds an overlapping AND a non-overlapping paragraph. It opens a live
        # bubble; at finalize the overlap is subtracted, the survivor stays in
        # the GROWN fold (confirmed, not compose-prepended), the body is whole.
        msgs = [
            _make_assistant_msg(
                "tool_use", [TextBlock(text="P1 shared para\n\nP2 fold only")]
            ),
            _make_assistant_msg(
                "end_turn", [TextBlock(text="P1 shared para\n\nP3 new answer")]
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.send_message.assert_awaited_once()  # live bubble opened
        handler.bot.delete_message.assert_not_called()  # survivor kept
        # Body whole: both final paragraphs intact, no compose fold prepended.
        self.assertEqual(response.content, "P1 shared para\n\nP3 new answer")
        self.assertNotIn(">!", response.content)
        # Grown fold holds ONLY the non-overlapping paragraph; shared subtracted.
        self.assertEqual(req.fold_buf, ["P2 fold only"])
        final_html = _edit_texts(handler.bot)[-1]
        self.assertIn("P2 fold only", final_html)
        self.assertNotIn("P1 shared para", final_html)

    def test_dgn930_single_interim_full_dup_deletes_fold(self):
        # DGN-710 under the DGN-930 1st-interim gate: when the whole final body
        # already sits in the single interim, the live bubble opens then is
        # emptied by subtraction -> deleted; the clean final stands alone.
        whole = "확인됐습니다 -- 첫 실증입니다."
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text=whole)]),
            _make_assistant_msg("end_turn", [TextBlock(text=whole)]),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.send_message.assert_awaited_once()
        handler.bot.delete_message.assert_awaited_once()
        self.assertTrue(req.fold_finalized)
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
        # DGN-930: bubble created on the 1st interim, so the 2nd and 3rd interim
        # each land a live growth edit + one finalize swap = 3 edits (was 2
        # under the old 2nd-interim creation gate).
        self.assertEqual(handler.bot.edit_message_text.await_count, 3)
        live_html = _edit_texts(handler.bot)[0]
        # Owner 2026-08-02: live growth edit is plain text (no blockquote).
        self.assertNotIn("<blockquote>", live_html)
        self.assertNotIn("<blockquote expandable>", live_html)
        # The last live edit (index -2, before finalize) carries all 3 steps.
        pre_finalize = _edit_texts(handler.bot)[-2]
        self.assertIn("step three", pre_finalize)

    def test_dgn777_partial_overlap_keeps_answer_whole_trims_fold(self):
        # DGN-777 final-sacred case (a): final = narrated P1 + new P2. The
        # answer keeps BOTH paragraphs; the fold loses P1 (subtracted) but
        # keeps its other narration.
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="I did step A")]),
            _make_assistant_msg("tool_use", [TextBlock(text="I did step B")]),
            _make_assistant_msg(
                "end_turn", [TextBlock(text="I did step A\n\nFinal answer here")]
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        # Answer sacred: every word kept.
        self.assertEqual(response.content, "I did step A\n\nFinal answer here")
        # Fold bubble kept (not deleted) and confirmed with the trimmed body.
        handler.bot.delete_message.assert_not_called()
        self.assertEqual(req.fold_buf, ["I did step B"])
        final_html = _edit_texts(handler.bot)[-1]
        self.assertTrue(
            final_html.startswith("<blockquote expandable>" + FOLD_CAPTION_NORMAL)
        )
        self.assertIn("I did step B", final_html)
        self.assertNotIn("I did step A", final_html)

    def test_dgn777_no_overlap_final_and_fold_unchanged(self):
        # DGN-777 case (c): no overlap at all -- answer and fold untouched.
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="step one")]),
            _make_assistant_msg("tool_use", [TextBlock(text="step two")]),
            _make_assistant_msg(
                "end_turn", [TextBlock(text="Completely new answer")]
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        self.assertEqual(response.content, "Completely new answer")
        handler.bot.delete_message.assert_not_called()
        self.assertEqual(req.fold_buf, ["step one", "step two"])
        final_html = _edit_texts(handler.bot)[-1]
        self.assertIn("step one", final_html)
        self.assertIn("step two", final_html)

    def test_dgn777_fold_emptied_by_subtraction_deleted(self):
        # DGN-777 case (d): every fold paragraph reappears in the final (which
        # also carries NEW material, so _final_fully_in_interim is False).
        # The trim empties the fold -> the bubble is deleted (no empty fold),
        # answer fully intact.
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="I did step A")]),
            _make_assistant_msg("tool_use", [TextBlock(text="I did step B")]),
            _make_assistant_msg(
                "end_turn",
                [
                    TextBlock(
                        text="I did step A\n\nI did step B\n\nPlus a new conclusion"
                    )
                ],
            ),
            _make_result_msg(result=""),
        ]
        response, handler, req = self._run(msgs)
        handler.bot.delete_message.assert_awaited_once()
        self.assertTrue(req.fold_finalized)  # latched -> finalize swap is a no-op
        self.assertEqual(
            response.content,
            "I did step A\n\nI did step B\n\nPlus a new conclusion",
        )

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
