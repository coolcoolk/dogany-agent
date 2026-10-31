"""DGN-1651: a Stop-hook block must not leave the superseded answer on screen.

A Stop hook that BLOCKS lets the model keep the turn and emit a SECOND
terminal AssistantMessage. The first one already streamed onto the owner's
screen. DGN-1253 made the FINAL BODY correct (segments assembled, a restated
paragraph dropped by _subtract_paras), but the live surface still carried the
first copy, and the shape of that leftover depends on the interim mode:

  fold / suppress  the regeneration is GLUED to the first answer inside the
                   same bubble (_append_chunk's newline join). Transient: the
                   DGN-1253 force_edit at _reply_smart rewrites that single
                   bubble with the deduped body at turn end. The owner sees
                   the duplicate only WHILE the turn streams.
  inline           the DGN-947 seal fires at the second terminal boundary and
                   SEALS the superseded answer as a standing bubble, then the
                   regeneration opens a fresh draft beside it. Permanent: the
                   sealed bubble is no longer in drafts, so no finalize
                   consumer ever touches it. The owner ends the turn with the
                   same answer twice, as two separate messages.

The ticket's 원인 section names fold as the carrier and the mode the reporting
instance ran. Measured, both are off: an unset INTERIM_MODE on a non-dev agent
resolves to "inline", not fold (config._resolve_interim_mode, DGN-930), and it
is the inline shape -- two standing messages -- that matches the owner's words
("두번씩 메시지가와"). Fold's glue never outlives the turn. Both shapes are
pinned below so the correction cannot silently regress.

Fix under test (repair candidate 3, true retraction): the reader-loop message
boundary calls StreamingMessageHandler.begin_message(terminal, retract=...),
which cuts the EXACT recorded span of the previous terminal message back out
of accumulated_text so the regeneration REWRITES the same bubble. No second
dedup is introduced -- _subtract_paras stays the one judgment of what the
final body says; this is a span cut on the live surface only.

Coverage:
  1. handler unit: the span cut, the no-prior-segment no-op, narration
     survival, offset invalidation on overflow.
  2. line-anchored markers (send_file:: / [[OPTIONS]]) across the cut -- the
     newline glue the retraction cuts through is exactly what keeps them
     parseable.
  3. end-to-end (reader loop + the real bot delivery seat): the inline
     duplicate is gone, the fold/suppress live bubble never shows it, a
     single-segment turn is unchanged, a text-less regeneration keeps the
     original answer, and an overflow turn (offsets invalidated) loses
     nothing.
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

# conftest.py already sets PROJECT_ROOT and TELEGRAM_BOT_TOKEN before import.
import bridge.bot as botmod
from bridge.formatting import extract_send_marker_paths, is_options_marker_line
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState
from bridge.streaming import StreamingMessageHandler

# The measured turn: byte-identical greetings either side of the Stop block
# (transcript ad419100-*.jsonl, 2026-09-22 14:41). Hangul keeps the fixture
# inert under the DGN-686 register guard.
GREETING = "안녕하세요, 새로 온 담당입니다. 잘 부탁드립니다!\n\n제 이름을 정해 주시겠어요?"


# ---------------------------------------------------------------------------
# Harness: a recording Telegram surface
# ---------------------------------------------------------------------------


class _Screen:
    """Records what the owner's chat actually shows, in arrival order.

    send -> a new message; edit -> that message's text changes in place;
    delete -> it disappears. view() is therefore the owner-visible surface,
    which is the only thing this ticket is about.
    """

    def __init__(self) -> None:
        self._next = 100
        self.texts: dict = {}
        self.order: List[int] = []
        self.log: List[tuple] = []

    def bot(self) -> MagicMock:
        bot = MagicMock()

        async def _send(**kwargs):
            self._next += 1
            mid = self._next
            self.texts[mid] = kwargs.get("text", "")
            self.order.append(mid)
            self.log.append(("send", mid))
            return SimpleNamespace(message_id=mid)

        async def _edit(**kwargs):
            mid = kwargs["message_id"]
            self.texts[mid] = kwargs.get("text", "")
            self.log.append(("edit", mid))
            return True

        async def _delete(*args, **kwargs):
            mid = kwargs.get("message_id")
            if mid is None and len(args) > 1:
                mid = args[1]
            self.texts.pop(mid, None)
            self.order = [m for m in self.order if m != mid]
            self.log.append(("delete", mid))
            return True

        bot.send_message = AsyncMock(side_effect=_send)
        bot.edit_message_text = AsyncMock(side_effect=_edit)
        bot.delete_message = AsyncMock(side_effect=_delete)
        return bot

    def view(self) -> List[str]:
        return [self.texts[m] for m in self.order if m in self.texts]


def _handler():
    """A REAL StreamingMessageHandler (the accumulator under test) plus the
    screen it writes to. The update throttle is pinned open so every chunk
    renders deterministically."""
    screen = _Screen()
    handler = StreamingMessageHandler(screen.bot(), chat_id=1, user_id=1)
    handler.min_chars = 1
    handler.min_interval = 0.0
    return handler, screen


def _asst(stop_reason: Optional[str], blocks: List[Any]) -> AssistantMessage:
    return AssistantMessage(
        content=blocks,
        model="claude-sonnet-4-5",
        stop_reason=stop_reason,
        parent_tool_use_id=None,
    )


def _tool() -> ToolUseBlock:
    return ToolUseBlock(id="tool_1", name="Read", input={"file_path": "x"})


def _result(result: str = "", is_error: bool = False):
    rm = MagicMock(spec=ResultMessage)
    rm.session_id = "sess-test"
    rm.result = result
    rm.is_error = is_error
    rm.num_turns = 2
    return rm


def _pending(streaming_handler) -> _PendingRequest:
    return _PendingRequest(
        user_id=1,
        chat_id=1,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=asyncio.get_event_loop().create_future(),
        streaming_handler=streaming_handler,
    )


def run_turn(messages_seq, interim_mode: str = "inline"):
    """Drive _reader_loop over a REAL handler, then the REAL bot delivery seat.

    Returns (response, live_surface, final_surface, screen):
      live_surface  -- what the owner saw while the turn streamed (after the
                       bridge finalize, before the bot's _reply_smart).
      final_surface -- what stands in the chat once the turn is delivered.

    _reply_link_id is pinned to None (no DGN-555 reply link) so the in-place
    HTML finalize path is exercised rather than its delete+resend fallback;
    _send_content_artifacts is stubbed (attachments/buttons are DGN-1253's
    seat, not this one).
    """

    async def _inner():
        screen = _Screen()
        bot = screen.bot()
        handler = StreamingMessageHandler(bot, chat_id=1, user_id=1)
        handler.min_chars = 1
        handler.min_interval = 0.0

        bridge_obj = SdkBridge()
        req = _pending(handler)
        req.sent = True
        state = _UserStreamState(client=MagicMock(), model=None)
        state.pending.append(req)

        async def fake_receive():
            for m in messages_seq:
                yield m

        state.client.receive_messages = fake_receive
        bridge_obj._streams[1] = state

        with patch(
            "bridge.sdk_bridge.STREAM_INTERIM", interim_mode == "inline"
        ), patch("bridge.sdk_bridge.INTERIM_MODE", interim_mode):
            await bridge_obj._reader_loop(1, state)

        response = await asyncio.wait_for(req.future, timeout=1.0)
        live = list(screen.view())

        message = MagicMock()
        message.get_bot.return_value = bot
        message.chat.id = 1

        async def _reply_text(text, **kwargs):
            screen._next += 1
            screen.texts[screen._next] = text
            screen.order.append(screen._next)
            screen.log.append(("send", screen._next))

        message.reply_text = AsyncMock(side_effect=_reply_text)

        instance = object.__new__(botmod.TelegramBot)
        with patch.object(
            botmod.TelegramBot, "_reply_link_id", return_value=None
        ), patch.object(
            botmod.TelegramBot, "_send_content_artifacts", new=AsyncMock()
        ):
            await botmod.TelegramBot._reply_smart(
                instance,
                message,
                response.content,
                force_options=response.has_options,
                streamed=response.streamed,
                draft_message_ids=response.draft_message_ids,
                assembled=getattr(response, "turn_assembled", False),
            )
        return response, live, screen.view(), screen

    return asyncio.run(_inner())


def hook_turn(seg1: List[Any], seg2: List[Any], interim_mode: str = "inline"):
    """The measured shape: terminal answer -> Stop block -> tool -> terminal
    regeneration."""
    return run_turn(
        [
            _asst("end_turn", seg1),
            _asst("tool_use", [_tool()]),
            _asst("end_turn", seg2),
            _result(result="ignored"),
        ],
        interim_mode=interim_mode,
    )


def _count(surface: List[str], needle: str) -> int:
    return sum(t.count(needle) for t in surface)


# ---------------------------------------------------------------------------
# 1. Handler unit: the span cut
# ---------------------------------------------------------------------------


class TestRetractionSpan(unittest.TestCase):
    def test_superseded_terminal_span_is_cut_exactly(self):
        async def _inner():
            h, _screen = _handler()
            h.begin_message(terminal=True)
            await h.update_if_needed(GREETING)
            self.assertEqual(h.accumulated_text, GREETING)
            retracted = h.begin_message(terminal=True, retract=True)
            # The whole surface was the superseded answer -> cut to empty.
            self.assertTrue(retracted)
            self.assertEqual(h.accumulated_text, "")
            await h.update_if_needed(GREETING)
            # Rewritten, not glued: exactly one copy.
            self.assertEqual(h.accumulated_text, GREETING)

        asyncio.run(_inner())

    def test_first_terminal_message_never_retracts(self):
        async def _inner():
            h, _screen = _handler()
            # No prior terminal segment: begin_message is a pure boundary mark.
            self.assertFalse(h.begin_message(terminal=True, retract=True))
            await h.update_if_needed(GREETING)
            self.assertEqual(h.accumulated_text, GREETING)

        asyncio.run(_inner())

    def test_interim_narration_survives_the_retraction(self):
        async def _inner():
            h, _screen = _handler()
            h.begin_message(terminal=False)
            await h.update_if_needed("파일을 읽는 중입니다")
            h.begin_message(terminal=True)
            await h.update_if_needed(GREETING)
            self.assertTrue(h.begin_message(terminal=True, retract=True))
            # Only the answer span went; the narration before it stands.
            self.assertEqual(h.accumulated_text, "파일을 읽는 중입니다")
            await h.update_if_needed(GREETING)
            self.assertEqual(
                h.accumulated_text, "파일을 읽는 중입니다\n" + GREETING
            )

        asyncio.run(_inner())

    def test_non_terminal_block_is_never_retracted(self):
        async def _inner():
            h, _screen = _handler()
            h.begin_message(terminal=False)
            await h.update_if_needed("진행 보고")
            # A terminal message with retract=True but no recorded TERMINAL
            # span must leave narration alone.
            self.assertFalse(h.begin_message(terminal=True, retract=True))
            self.assertEqual(h.accumulated_text, "진행 보고")

        asyncio.run(_inner())

    def test_overflow_invalidates_the_offsets_and_loses_nothing(self):
        async def _inner():
            h, screen = _handler()
            long_answer = "머리표식" + "가" * 4500 + "꼬리표식"
            h.begin_message(terminal=True)
            await h.update_if_needed(long_answer)
            # The overflow drain re-based accumulated_text and sealed a bubble
            # this handler can no longer rewrite: retraction must be skipped.
            self.assertIsNone(h._terminal_span)
            self.assertFalse(h.begin_message(terminal=True, retract=True))
            await h.update_if_needed("두 번째 답변")
            await h.finalize_all()
            # Pre-DGN-1651 behaviour stands: the tail keeps the glue join and
            # no character of the first answer was cut.
            self.assertTrue(h.accumulated_text.endswith("\n두 번째 답변"))
            surface = "".join(screen.view())
            self.assertEqual(surface.count("머리표식"), 1)
            self.assertEqual(surface.count("꼬리표식"), 1)
            self.assertEqual(surface.count("가"), 4500)
            self.assertEqual(surface.count("두 번째 답변"), 1)

        asyncio.run(_inner())

    def test_seal_segment_clears_the_retractable_span(self):
        async def _inner():
            h, _screen = _handler()
            h.begin_message(terminal=True)
            await h.update_if_needed(GREETING)
            await h.seal_segment()
            # The sealed bubble is a standing message now -- nothing on the
            # live surface is retractable.
            self.assertEqual(h.accumulated_text, "")
            self.assertFalse(h.begin_message(terminal=True, retract=True))

        asyncio.run(_inner())


# ---------------------------------------------------------------------------
# 2. Line-anchored markers across the cut
# ---------------------------------------------------------------------------


class TestMarkersSurviveTheCut(unittest.TestCase):
    """_append_chunk's newline glue exists because bare-joining blocks breaks
    send_file:: / [[OPTIONS]] (they neither strip nor act). The retraction cuts
    across that glue, so both sides of the cut are checked here."""

    def test_markers_line_anchored_when_the_cut_empties_the_surface(self):
        async def _inner():
            h, _screen = _handler()
            h.begin_message(terminal=True)
            await h.update_if_needed("첫 답변\nsend_file:: old.png")
            self.assertTrue(h.begin_message(terminal=True, retract=True))
            await h.update_if_needed(
                "다시 쓴 답변\nsend_file:: new.png\n[[OPTIONS]]"
            )
            lines = h.accumulated_text.split("\n")
            # No leading blank/glue newline left behind by the cut, and both
            # markers sit at the start of their own line.
            self.assertEqual(lines[0], "다시 쓴 답변")
            self.assertIn("send_file:: new.png", lines)
            self.assertTrue(any(is_options_marker_line(ln) for ln in lines))
            # The retracted marker is gone; the replacement resolves once.
            self.assertEqual(
                extract_send_marker_paths(h.accumulated_text), ["new.png"]
            )

        asyncio.run(_inner())

    def test_markers_line_anchored_when_narration_survives_the_cut(self):
        async def _inner():
            h, _screen = _handler()
            h.begin_message(terminal=False)
            await h.update_if_needed("파일을 읽는 중입니다")
            h.begin_message(terminal=True)
            await h.update_if_needed("첫 답변\nsend_file:: old.png")
            self.assertTrue(h.begin_message(terminal=True, retract=True))
            # The survivor must not keep a dangling glue newline, and the next
            # block must still get its own -- otherwise the marker below would
            # be glued onto "파일을 읽는 중입니다" and stop parsing.
            self.assertEqual(h.accumulated_text, "파일을 읽는 중입니다")
            await h.update_if_needed("send_file:: new.png\n[[OPTIONS]]")
            lines = h.accumulated_text.split("\n")
            self.assertEqual(
                lines,
                ["파일을 읽는 중입니다", "send_file:: new.png", "[[OPTIONS]]"],
            )
            self.assertEqual(
                extract_send_marker_paths(h.accumulated_text), ["new.png"]
            )
            self.assertTrue(any(is_options_marker_line(ln) for ln in lines))

        asyncio.run(_inner())

    def test_hook_turn_markers_reach_the_delivered_body(self):
        # End-to-end: the retraction must not disturb the DGN-1253 assembly's
        # marker consequences (send_file:: once, last [[OPTIONS]] wins).
        response, _live, _final, _screen = hook_turn(
            [TextBlock(text="첫 답변\nsend_file:: report.png")],
            [TextBlock(text="다시 쓴 답변\n[[OPTIONS]]")],
        )
        self.assertEqual(
            extract_send_marker_paths(response.content), ["report.png"]
        )
        self.assertTrue(
            any(
                is_options_marker_line(ln)
                for ln in response.content.split("\n")
            )
        )
        self.assertTrue(response.has_options)


# ---------------------------------------------------------------------------
# 3. End-to-end: the owner-visible surface
# ---------------------------------------------------------------------------


class TestOwnerSurface(unittest.TestCase):
    def test_inline_hook_turn_leaves_exactly_one_message(self):
        # THE bug. Pre-fix this ends the turn with TWO standing messages, both
        # the same greeting (DGN-947 sealed the superseded one beside the
        # regeneration's fresh draft).
        response, live, final, _screen = hook_turn(
            [TextBlock(text=GREETING)],
            [TextBlock(text=GREETING)],
            interim_mode="inline",
        )
        self.assertEqual(response.content, GREETING)
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0], GREETING)
        self.assertEqual(_count(final, "제 이름을 정해 주시겠어요?"), 1)
        # ... and it was never duplicated mid-stream either.
        self.assertEqual(_count(live, "제 이름을 정해 주시겠어요?"), 1)

    def test_live_bubble_never_shows_the_glued_duplicate(self):
        # fold / suppress: the DGN-1253 force_edit repaired the FINAL bubble
        # even pre-fix, but the owner still watched the answer double inside
        # the live bubble for the length of the regeneration. The retraction
        # removes it from the live surface too.
        for mode in ("fold", "suppress"):
            with self.subTest(mode=mode):
                _response, live, final, _screen = hook_turn(
                    [TextBlock(text=GREETING)],
                    [TextBlock(text=GREETING)],
                    interim_mode=mode,
                )
                self.assertEqual(_count(live, "제 이름을 정해 주시겠어요?"), 1)
                self.assertEqual(_count(final, "제 이름을 정해 주시겠어요?"), 1)
                self.assertEqual(len(final), 1)

    def test_regeneration_that_differs_replaces_rather_than_appends(self):
        # A regeneration need not be byte-identical -- a Stop block usually
        # asks for a CHANGED answer. The live bubble must show the new answer
        # alone, and the delivered body is the DGN-1253 assembly (unchanged).
        response, live, final, _screen = hook_turn(
            [TextBlock(text="이름을 알려드릴게요: 하루입니다.")],
            [TextBlock(text="이름은 형님이 정해 주세요.")],
            interim_mode="inline",
        )
        self.assertEqual(live, ["이름은 형님이 정해 주세요."])
        self.assertEqual(len(final), 1)
        # DGN-1253 keeps BOTH segments in the body (no exact-paragraph match),
        # which is the assembly's judgment, not this seat's.
        self.assertIn("이름은 형님이 정해 주세요.", response.content)
        self.assertIn("이름을 알려드릴게요: 하루입니다.", response.content)
        self.assertEqual(final[0], response.content)

    def test_single_segment_turn_is_unchanged(self):
        # No prior terminal segment exists, so begin_message is a pure boundary
        # mark: the draft is sent, finalize_draft edits it once, and the
        # bot-side no-op skip holds (turn_assembled False -> no force_edit).
        for mode in ("inline", "fold", "suppress"):
            with self.subTest(mode=mode):
                response, live, final, screen = run_turn(
                    [
                        _asst("end_turn", [TextBlock(text=GREETING)]),
                        _result(result="ignored"),
                    ],
                    interim_mode=mode,
                )
                self.assertFalse(response.turn_assembled)
                self.assertEqual(response.content, GREETING)
                self.assertEqual(live, [GREETING])
                self.assertEqual(final, [GREETING])
                # Byte-identical bubble traffic: the draft send + the finalize
                # edit. Nothing added, nothing re-rendered, nothing deleted.
                self.assertEqual(
                    [kind for kind, _ in screen.log], ["send", "edit"]
                )

    def test_interim_narration_is_not_retracted(self):
        # inline mode, narration before the first answer: DGN-947 seals it as
        # its own standing bubble, and the retraction of the SECOND terminal
        # message must not touch it.
        response, _live, final, _screen = run_turn(
            [
                _asst("tool_use", [TextBlock(text="파일을 읽는 중입니다")]),
                _asst("end_turn", [TextBlock(text=GREETING)]),
                _asst("tool_use", [_tool()]),
                _asst("end_turn", [TextBlock(text=GREETING)]),
                _result(result="ignored"),
            ],
            interim_mode="inline",
        )
        self.assertEqual(response.content, GREETING)
        self.assertIn("파일을 읽는 중입니다", final[0])
        self.assertEqual(_count(final, "제 이름을 정해 주시겠어요?"), 1)

    def test_textless_regeneration_keeps_the_original_answer(self):
        # The second terminal message carries no text (all blocks thinking /
        # guard-dropped). There is no replacement, so nothing is retracted and
        # the original answer stays on screen, unaltered.
        for mode in ("inline", "fold", "suppress"):
            with self.subTest(mode=mode):
                response, _live, final, _screen = hook_turn(
                    [TextBlock(text=GREETING)],
                    [_tool()],
                    interim_mode=mode,
                )
                self.assertEqual(response.content, GREETING)
                self.assertEqual(final, [GREETING])

    def test_overflow_hook_turn_loses_nothing(self):
        # The first terminal answer overflows one bubble, so the accumulator
        # is re-based and the live offsets are invalidated: the retraction is
        # SKIPPED and the pre-DGN-1651 behaviour stands. The requirement here
        # is only that nothing is lost.
        head, mid, tail = "머리표식", "중간표식", "꼬리표식"
        long_answer = head + "가" * 2200 + mid + "나" * 2200 + tail
        response, _live, final, _screen = hook_turn(
            [TextBlock(text=long_answer)],
            [TextBlock(text="다시 쓴 답변입니다")],
            interim_mode="inline",
        )
        joined = "\n".join(final)
        for sentinel in (head, mid, tail, "다시 쓴 답변입니다"):
            self.assertIn(sentinel, joined)
        self.assertIn(head, response.content)
        self.assertIn(tail, response.content)
        self.assertIn("다시 쓴 답변입니다", response.content)


if __name__ == "__main__":
    unittest.main()
