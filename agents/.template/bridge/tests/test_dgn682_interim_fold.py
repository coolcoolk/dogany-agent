"""DGN-682: interim narration -> fold expandable-blockquote synthesis.

Covers the spec v2 locks:
- D1: INTERIM_MODE resolution (explicit wins; STREAM_INTERIM=true -> inline
  deprecated alias; unset -> fold [v1.31.0 baseline lift; was suppress]).
- D2/D10: fold prepends at finalize tail end; quoted narration numbering is
  never mistaken for an options menu.
- D4/D5: only TextBlocks are captured, raw text with scaffold guard only
  (register guard NOT applied -- grill B1 ko-locale English narration survives).
- D6: empty/whitespace-only capture -> no fold attached.
- D7: 1500-char fold cap with head+tail middle-truncate; fold + final answer
  fits ONE chunk (raw 4000 / HTML 4096) with the fold shrinking first.
- D8: no caption; first meaningful line is the collapsed preview.
- D9: request-owned buffer -- is_error turns get no fold; a retry (new
  request) never inherits the previous turn's interim.
- D11: '>! ' first line, '> ' others, bare '>' blanks (quote run unbroken),
  code fences neutralized; renders as ONE <blockquote expandable>.
- Regressions: suppress and inline modes unchanged.
"""

import asyncio
import unittest
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

# conftest.py already sets PROJECT_ROOT and TELEGRAM_BOT_TOKEN before import.
import bridge.config as _cfg_mod
from bridge.config import _resolve_interim_mode
from bridge.formatting import (
    INTERIM_FOLD_CAP,
    INTERIM_FOLD_SEPARATOR,
    _FOLD_OMISSION_LINE,
    compose_interim_fold,
    markdown_to_telegram_html,
    split_text,
)
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_block(text: str) -> TextBlock:
    return TextBlock(text=text)


def _make_tool_use_block() -> ToolUseBlock:
    return ToolUseBlock(id="tool_1", name="Bash", input={"command": "ls"})


def _make_assistant_msg(
    stop_reason: Optional[str],
    blocks: List[Any],
    parent_tool_use_id: Optional[str] = None,
) -> AssistantMessage:
    return AssistantMessage(
        content=blocks,
        model="claude-sonnet-4-5",
        stop_reason=stop_reason,
        parent_tool_use_id=parent_tool_use_id,
    )


def _make_result_msg(result: str = "", is_error: bool = False):
    rm = MagicMock(spec=ResultMessage)
    rm.session_id = "sess-test"
    rm.result = result
    rm.is_error = is_error
    rm.num_turns = 2
    return rm


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


def _unquote_fold(fold: str) -> str:
    """Strip the D11 quote prefixes back to the raw narration text."""
    out = []
    for ln in fold.split("\n"):
        if ln.startswith(">! "):
            out.append(ln[3:])
        elif ln.startswith("> "):
            out.append(ln[2:])
        elif ln == ">":
            out.append("")
        else:
            raise AssertionError(f"non-quote line inside fold: {ln!r}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# D1: mode resolution
# ---------------------------------------------------------------------------


class TestInterimModeResolution(unittest.TestCase):
    def test_unset_defaults_to_inline_for_nondev(self):
        # DGN-930: the v1.31.0 blanket "unset -> fold" is superseded. Unset +
        # unset now takes the CLASS default: nondev (the resolve default) ->
        # inline (live, no fold). This is the non-dev/domain-agent behavior.
        self.assertEqual(_resolve_interim_mode(None, False), "inline")
        self.assertEqual(_resolve_interim_mode(None, False, "nondev"), "inline")

    def test_unset_defaults_to_fold_for_dev(self):
        # DGN-930: dev agents keep the fold (live-then-fold) as their unset
        # default via INTERIM_AGENT_CLASS=dev.
        self.assertEqual(_resolve_interim_mode(None, False, "dev"), "fold")

    def test_stream_interim_alias_maps_to_inline(self):
        # The deprecated STREAM_INTERIM=true alias -> inline regardless of class.
        self.assertEqual(_resolve_interim_mode(None, True), "inline")
        self.assertEqual(_resolve_interim_mode(None, True, "dev"), "inline")

    def test_explicit_mode_wins_over_alias(self):
        self.assertEqual(_resolve_interim_mode("fold", True), "fold")
        self.assertEqual(_resolve_interim_mode("suppress", True), "suppress")
        self.assertEqual(_resolve_interim_mode("inline", False), "inline")

    def test_explicit_mode_wins_over_class(self):
        # DGN-930: an explicit INTERIM_MODE always beats the class default.
        self.assertEqual(_resolve_interim_mode("suppress", False, "dev"), "suppress")
        self.assertEqual(_resolve_interim_mode("inline", False, "dev"), "inline")
        self.assertEqual(_resolve_interim_mode("fold", False, "nondev"), "fold")

    def test_config_normalizes_interim_agent_class(self):
        # DGN-930: unknown/empty class value falls back to nondev.
        cfg = _cfg_mod.Config(
            telegram_bot_token="test:token", interim_agent_class="DEV"
        )
        self.assertEqual(cfg.interim_agent_class, "dev")
        cfg = _cfg_mod.Config(
            telegram_bot_token="test:token", interim_agent_class="bogus"
        )
        self.assertEqual(cfg.interim_agent_class, "nondev")
        cfg = _cfg_mod.Config(telegram_bot_token="test:token")
        self.assertEqual(cfg.interim_agent_class, "nondev")

    def test_config_normalizes_interim_mode(self):
        cfg = _cfg_mod.Config(telegram_bot_token="test:token", interim_mode="FOLD")
        self.assertEqual(cfg.interim_mode, "fold")
        cfg = _cfg_mod.Config(telegram_bot_token="test:token", interim_mode="bogus")
        self.assertIsNone(cfg.interim_mode)


# ---------------------------------------------------------------------------
# D6/D7/D8/D11: fold composition (pure helper)
# ---------------------------------------------------------------------------


class TestComposeInterimFold(unittest.TestCase):
    def test_d11_quoting_first_line_marker_and_bare_blank(self):
        fold = compose_interim_fold(
            ["first line\n\nsecond paragraph"], "final answer"
        )
        lines = fold.split("\n")
        self.assertEqual(lines[0], ">! first line")
        self.assertEqual(lines[1], ">")
        self.assertEqual(lines[2], "> second paragraph")

    def test_d11_multi_block_join_keeps_quote_run(self):
        fold = compose_interim_fold(["block one", "block two"], "final")
        # Blocks join across a bare '>' blank so the run never breaks.
        self.assertEqual(fold, ">! block one\n>\n> block two")

    def test_d11_renders_single_expandable_blockquote(self):
        interim = ["progress step one", "second paragraph of narration\n\nthird"]
        final = "최종 답변입니다"
        fold = compose_interim_fold(interim, final)
        combined = fold + INTERIM_FOLD_SEPARATOR + final
        html = markdown_to_telegram_html(combined)
        self.assertEqual(html.count("<blockquote expandable>"), 1)
        self.assertEqual(html.count("<blockquote"), 1)
        self.assertEqual(html.count("</blockquote>"), 1)
        # Everything interim stays INSIDE the quote; the final answer outside.
        close = html.index("</blockquote>")
        self.assertLess(html.index("second paragraph of narration"), close)
        self.assertLess(html.index("third"), close)
        self.assertGreater(html.index(final), close)

    def test_d11_code_fences_neutralized(self):
        fold = compose_interim_fold(
            ["checking:\n```python\nprint(1)\n```\ndone"], "final"
        )
        self.assertNotIn("```", fold)
        self.assertIn("'''", fold)

    def test_d8_leading_blank_lines_promote_first_meaningful_line(self):
        fold = compose_interim_fold(["\n   \nreal preview line\nmore"], "final")
        self.assertTrue(fold.startswith(">! real preview line"))

    def test_d6_empty_and_whitespace_only_yield_no_fold(self):
        self.assertEqual(compose_interim_fold([], "final"), "")
        self.assertEqual(compose_interim_fold(["   ", "\n\n"], "final"), "")

    def test_d7_middle_truncate_keeps_head_and_tail(self):
        lines = [f"line-{i:03d} " + "x" * 40 for i in range(80)]  # ~3800 chars
        fold = compose_interim_fold(["\n".join(lines)], "short final")
        raw = _unquote_fold(fold)
        self.assertLessEqual(len(raw), INTERIM_FOLD_CAP)
        self.assertIn(_FOLD_OMISSION_LINE, raw)
        # Head (first line) preserved as the collapsed preview.
        self.assertTrue(fold.startswith(">! line-000"))
        # Recent tail preserved; early-middle omitted.
        self.assertIn("line-079", raw)
        self.assertNotIn("line-010", raw)

    def test_d7_under_cap_not_truncated(self):
        text = "a\nb\nc"
        fold = compose_interim_fold([text], "final")
        self.assertNotIn(_FOLD_OMISSION_LINE, fold)
        self.assertEqual(_unquote_fold(fold), text)

    def test_d7_single_chunk_near_raw_limit(self):
        final = "F" * 3800
        fold = compose_interim_fold(["n" * 1000], final)
        self.assertTrue(fold)  # fold survives, shrunk to fit
        combined = fold + INTERIM_FOLD_SEPARATOR + final
        self.assertLessEqual(len(combined), 4000)
        self.assertEqual(len(split_text(combined)), 1)

    def test_d7_fold_dropped_when_final_leaves_no_room(self):
        final = "F" * 3998
        self.assertEqual(compose_interim_fold(["n" * 1000], final), "")

    def test_d7_html_utf16_budget_respected(self):
        # Escape-heavy final (& -> &amp; expands 5x in HTML) forces the HTML
        # bound to bite before the raw bound does; the fold must shrink below
        # its raw cap to honor the 4096 UTF-16 limit.
        final = "&" * 700  # 3500 UTF-16 units after escaping
        fold = compose_interim_fold(["n" * 3000], final)
        self.assertTrue(fold)
        self.assertLess(len(_unquote_fold(fold)), INTERIM_FOLD_CAP)
        combined = fold + INTERIM_FOLD_SEPARATOR + final
        html = markdown_to_telegram_html(combined)
        self.assertLessEqual(len(html.encode("utf-16-le")) // 2, 4096)


# ---------------------------------------------------------------------------
# Reader-loop integration (capture + finalize prepend)
# ---------------------------------------------------------------------------


class _ReaderHarness(unittest.TestCase):
    """Shared reader-loop harness (mirrors test_dgn426)."""

    def _run_reader(self, messages_seq, mode="fold", extra_reqs=0):
        async def _inner():
            mock_handler = MagicMock()
            mock_handler.drafts = []
            mock_handler.update_if_needed = AsyncMock(return_value=True)
            mock_handler.finalize_all = AsyncMock(return_value=True)

            bridge_obj = SdkBridge()
            reqs = []
            state = _UserStreamState(client=MagicMock(), model=None)
            for _ in range(1 + extra_reqs):
                req = _make_pending_req(streaming_handler=mock_handler)
                req.sent = True
                state.pending.append(req)
                reqs.append(req)

            async def fake_receive():
                for m in messages_seq:
                    yield m

            state.client.receive_messages = fake_receive
            bridge_obj._streams[1] = state

            with patch("bridge.sdk_bridge.INTERIM_MODE", mode), patch(
                "bridge.sdk_bridge.STREAM_INTERIM", False
            ):
                await bridge_obj._reader_loop(1, state)

            results = [
                await asyncio.wait_for(r.future, timeout=1.0) for r in reqs
            ]
            return results, mock_handler

        return asyncio.run(_inner())


class TestFoldReaderLoop(_ReaderHarness):
    def test_fold_mode_two_interim_blocks_synthesized(self):
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block("step one narration")]),
            _make_assistant_msg("tool_use", [_make_tool_use_block()]),
            _make_assistant_msg("tool_use", [_make_text_block("step two narration")]),
            _make_assistant_msg("end_turn", [_make_text_block("final answer")]),
            _make_result_msg(result="final answer"),
        ]
        (response,), handler = self._run_reader(msgs, mode="fold")

        # Interim never live-streamed: only the terminal block updates.
        self.assertEqual(handler.update_if_needed.await_count, 1)
        self.assertEqual(handler.update_if_needed.call_args[0][0], "final answer")

        # Fold prepended above the final answer as one quote run.
        self.assertTrue(response.content.startswith(">! step one narration"))
        self.assertIn("> step two narration", response.content)
        self.assertTrue(response.content.endswith("final answer"))
        # Renders as exactly one expandable blockquote, answer outside.
        html = markdown_to_telegram_html(response.content)
        self.assertEqual(html.count("<blockquote expandable>"), 1)
        self.assertGreater(html.index("final answer"), html.index("</blockquote>"))
        self.assertTrue(response.success)

    def test_suppress_mode_regression_unchanged(self):
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block("interim narration")]),
            _make_assistant_msg("end_turn", [_make_text_block("final answer")]),
            _make_result_msg(result="final answer"),
        ]
        (response,), handler = self._run_reader(msgs, mode="suppress")
        self.assertEqual(handler.update_if_needed.await_count, 1)
        self.assertEqual(response.content, "final answer")
        self.assertNotIn(">!", response.content)

    def test_inline_mode_regression_unchanged(self):
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block("interim A")]),
            _make_assistant_msg("end_turn", [_make_text_block("final B")]),
            _make_result_msg(result="final B"),
        ]
        (response,), handler = self._run_reader(msgs, mode="inline")
        # Inline streams BOTH blocks live and attaches no fold.
        self.assertEqual(handler.update_if_needed.await_count, 2)
        self.assertEqual(response.content, "final B")
        self.assertNotIn(">!", response.content)

    def test_d6_no_interim_no_fold(self):
        msgs = [
            _make_assistant_msg("tool_use", [_make_tool_use_block()]),
            _make_assistant_msg("end_turn", [_make_text_block("final answer")]),
            _make_result_msg(result="final answer"),
        ]
        (response,), _ = self._run_reader(msgs, mode="fold")
        self.assertEqual(response.content, "final answer")

    def test_d6_whitespace_only_interim_no_fold(self):
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block("   \n  ")]),
            _make_assistant_msg("end_turn", [_make_text_block("final answer")]),
            _make_result_msg(result="final answer"),
        ]
        (response,), _ = self._run_reader(msgs, mode="fold")
        self.assertEqual(response.content, "final answer")

    def test_d9_error_turn_gets_no_fold(self):
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block("interim before error")]),
            _make_result_msg(result="sdk exploded", is_error=True),
        ]
        (response,), _ = self._run_reader(msgs, mode="fold")
        self.assertFalse(response.success)
        self.assertNotIn(">!", response.content)
        self.assertNotIn("interim before error", response.content)

    def test_d9_retry_request_does_not_inherit_previous_interim(self):
        msgs = [
            # Turn 1 (request 1): interim, then error.
            _make_assistant_msg("tool_use", [_make_text_block("turn1 interim")]),
            _make_result_msg(result="overloaded", is_error=True),
            # Turn 2 (request 2 = retry): its own interim, then success.
            _make_assistant_msg("tool_use", [_make_text_block("turn2 interim")]),
            _make_assistant_msg("end_turn", [_make_text_block("turn2 final")]),
            _make_result_msg(result="turn2 final"),
        ]
        (resp1, resp2), _ = self._run_reader(msgs, mode="fold", extra_reqs=1)
        self.assertFalse(resp1.success)
        self.assertNotIn("turn1 interim", resp1.content)
        self.assertTrue(resp2.success)
        self.assertIn(">! turn2 interim", resp2.content)
        # The failed turn's interim never bleeds into the retry's fold.
        self.assertNotIn("turn1 interim", resp2.content)

    def test_d10_numbered_narration_not_mistaken_for_options(self):
        msgs = [
            _make_assistant_msg(
                "tool_use",
                [_make_text_block("plan:\n1. inspect config\n2. patch loader")],
            ),
            _make_assistant_msg("end_turn", [_make_text_block("done, patched")]),
            _make_result_msg(result="done, patched"),
        ]
        (response,), _ = self._run_reader(msgs, mode="fold")
        self.assertFalse(response.has_options)
        # The numbered lines ride inside the quote run only.
        self.assertIn("> 1. inspect config", response.content)
        self.assertIn("> 2. patch loader", response.content)

    def test_d11_multiline_interim_fully_quoted_no_plain_leak(self):
        msgs = [
            _make_assistant_msg(
                "tool_use",
                [_make_text_block("first paragraph\n\nsecond paragraph plain")],
            ),
            _make_assistant_msg("end_turn", [_make_text_block("최종 답변")]),
            _make_result_msg(result="최종 답변"),
        ]
        (response,), _ = self._run_reader(msgs, mode="fold")
        fold_part = response.content.rsplit(INTERIM_FOLD_SEPARATOR, 1)[0]
        for ln in fold_part.split("\n"):
            self.assertTrue(
                ln == ">" or ln.startswith("> ") or ln.startswith(">! "),
                f"plain line leaked out of the quote run: {ln!r}",
            )
        html = markdown_to_telegram_html(response.content)
        self.assertLess(
            html.index("second paragraph plain"), html.index("</blockquote>")
        )

    def test_b1_ko_locale_english_narration_preserved_in_fold(self):
        english = (
            "Scanning the repository for the config loader and tracing the "
            "interim capture path end to end before patching."
        )
        self.assertGreaterEqual(len(english), 80)
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block(english)]),
            _make_assistant_msg("end_turn", [_make_text_block("최종 답변입니다")]),
            _make_result_msg(result="최종 답변입니다"),
        ]
        with patch.object(_cfg_mod.config, "locale", "ko"):
            (response,), _ = self._run_reader(msgs, mode="fold")
        # The register guard would have dropped this block; the fold capture
        # bypasses it (scaffold guard only), so the narration survives quoted.
        self.assertIn(english, response.content)
        self.assertTrue(response.content.startswith(">! "))

    def test_d7_reader_loop_single_chunk_near_limit(self):
        big_final = "F" * 3700
        msgs = [
            _make_assistant_msg("tool_use", [_make_text_block("n" * 2000)]),
            _make_assistant_msg("end_turn", [_make_text_block(big_final)]),
            _make_result_msg(result=big_final),
        ]
        (response,), _ = self._run_reader(msgs, mode="fold")
        self.assertTrue(response.content.startswith(">! "))
        self.assertLessEqual(len(response.content), 4000)
        self.assertEqual(len(split_text(response.content)), 1)


if __name__ == "__main__":
    unittest.main()
