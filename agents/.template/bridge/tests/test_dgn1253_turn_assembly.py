"""DGN-1253: turn assembly -- a Stop-hook block must not eat the final answer.

A Stop-hook blocking error lets the model CONTINUE the same turn and emit a
second terminal AssistantMessage. Pre-fix, the reader loop reset
last_assistant_texts on every AssistantMessage and finalize assembled from
that buffer only, so the first terminal message (the real answer: body,
send_file:: attachments, [[OPTIONS]] keyboard) vanished entirely.

Fix under test: each terminal message's text is captured as an ordered
segment (_PendingRequest.final_segments); with 2+ segments the finalize
assembles them in turn order, dropping later paragraphs that EXACTLY
reproduce earlier text (reuse of the DGN-777/876 _subtract_paras judgment).
Single-segment turns keep the legacy expression byte-identical.

Regression matrix (hook blocks {0,1,2} x attachment {yes,no} x OPTIONS
{yes,no}) plus:
- duplicate-text handling (full restatement / partial overlap),
- send_file:: path sent exactly once across segments (resolve_send_paths),
- [[OPTIONS]] last-declaration-wins across segments (extract_marker_labels),
- fold/inline interim never leaks into the assembled body,
- ChatResponse.turn_assembled flag + the bot-side force_edit skip bypass.
"""

import pytest

from bridge import sdk_bridge as _locale_bridge
import asyncio
import unittest
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

# conftest.py already sets PROJECT_ROOT and TELEGRAM_BOT_TOKEN before import.
from bridge.formatting import resolve_send_paths
from bridge.options import extract_marker_labels
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState


@pytest.fixture(autouse=True)
def _en_instance(monkeypatch):
    # These fixtures narrate in English: pin an en instance so the interim
    # locale gate (sdk_bridge._interim_off_locale) stays out of the interim
    # mechanics under test, whatever LOCALE the shell exports. The ko side
    # is covered by test_interim_locale_gate.py.
    monkeypatch.setattr(_locale_bridge.config, "locale", "en")



def _text(t: str) -> TextBlock:
    return TextBlock(text=t)


def _tool() -> ToolUseBlock:
    return ToolUseBlock(id="tool_1", name="Bash", input={"command": "ls"})


def _asst(stop_reason: Optional[str], blocks: List[Any]) -> AssistantMessage:
    return AssistantMessage(
        content=blocks,
        model="claude-sonnet-4-5",
        stop_reason=stop_reason,
        parent_tool_use_id=None,
    )


def _result(result: str = "", is_error: bool = False):
    rm = MagicMock(spec=ResultMessage)
    rm.session_id = "sess-test"
    rm.result = result
    rm.is_error = is_error
    rm.num_turns = 2
    return rm


def _pending(streaming_handler=None) -> _PendingRequest:
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


def run_turn(messages_seq, interim_mode: str = "suppress"):
    """Feed a message sequence through _reader_loop; return (response, handler)."""

    async def _inner():
        handler = MagicMock()
        handler.drafts = []
        handler.update_if_needed = AsyncMock(return_value=True)
        handler.finalize_all = AsyncMock(return_value=True)
        handler.seal_segment = AsyncMock(return_value=None)

        bridge_obj = SdkBridge()
        req = _pending(streaming_handler=handler)
        state = _UserStreamState(client=MagicMock(), model=None)
        state.pending.append(req)
        req.sent = True

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
        return response, handler, req

    return asyncio.run(_inner())


# Two-terminal-message turn: msg1 = real answer, hook blocks, model continues
# with tool work, msg2 = post-hook addendum.
def hook_turn(seg1_blocks, seg2_blocks, seg3_blocks=None, interim_mode="suppress"):
    msgs = [
        _asst("end_turn", seg1_blocks),          # real answer (terminal)
        _asst("tool_use", [_tool()]),            # post-hook continuation work
        _asst("end_turn", seg2_blocks),          # post-hook terminal
    ]
    if seg3_blocks is not None:
        msgs += [_asst("tool_use", [_tool()]), _asst("end_turn", seg3_blocks)]
    msgs.append(_result(result="ignored"))
    return run_turn(msgs, interim_mode=interim_mode)


class TestNonHookByteIdentity(unittest.TestCase):
    """Hook-block count 0: the plain turn must be byte-identical to legacy."""

    def test_plain_turn_single_terminal(self):
        resp, handler, req = run_turn([
            _asst("tool_use", [_text("interim narration")]),
            _asst("end_turn", [_text("final answer")]),
            _result(result="final answer"),
        ])
        self.assertEqual(resp.content, "final answer")
        self.assertFalse(resp.turn_assembled)
        self.assertEqual(req.final_segments, ["final answer"])

    def test_plain_turn_multi_block_terminal_join(self):
        # Legacy expression joins blocks of ONE message with "\n".
        resp, _, req = run_turn([
            _asst("end_turn", [_text("part one"), _text("part two")]),
            _result(result=""),
        ])
        self.assertEqual(resp.content, "part one\npart two")
        self.assertFalse(resp.turn_assembled)

    def test_no_terminal_text_keeps_legacy_fallback(self):
        # No terminal message at all: final_segments stays empty and the
        # legacy expression delivers the LAST message's TextBlock (the
        # pre-fix behavior, dgn426 test 4 shape) -- byte-identical.
        resp, _, req = run_turn([
            _asst("tool_use", [_text("tool step"), _tool()]),
            _result(result="from result"),
        ])
        self.assertEqual(resp.content, "tool step")
        self.assertFalse(resp.turn_assembled)
        self.assertEqual(req.final_segments, [])

    def test_attachment_and_options_unchanged_without_hook(self):
        body = "answer\nsend_file::files/outbox/a.png\n[[OPTIONS: go | hold]]"
        resp, _, _ = run_turn([
            _asst("end_turn", [_text(body)]),
            _result(result=""),
        ])
        self.assertEqual(resp.content, body)
        self.assertTrue(resp.has_options)
        self.assertFalse(resp.turn_assembled)


class TestHookBlockAssembly(unittest.TestCase):
    """Hook-block count 1 and 2: every segment reaches the body in order."""

    def test_one_block_body_survives(self):
        resp, _, req = hook_turn(
            [_text("real answer")], [_text("post-hook addendum")]
        )
        self.assertEqual(resp.content, "real answer\n\npost-hook addendum")
        self.assertTrue(resp.turn_assembled)
        self.assertEqual(
            req.final_segments, ["real answer", "post-hook addendum"]
        )

    def test_two_blocks_three_segments(self):
        resp, _, _ = hook_turn(
            [_text("real answer")], [_text("first fix")], [_text("second fix")]
        )
        self.assertEqual(
            resp.content, "real answer\n\nfirst fix\n\nsecond fix"
        )
        self.assertTrue(resp.turn_assembled)

    def test_attachment_in_first_segment_survives(self):
        resp, _, _ = hook_turn(
            [_text("map ready\nsend_file::files/outbox/map.png")],
            [_text("ledger updated")],
        )
        self.assertIn("send_file::files/outbox/map.png", resp.content)
        self.assertIn("map ready", resp.content)
        self.assertIn("ledger updated", resp.content)

    def test_options_in_first_segment_survive(self):
        resp, _, _ = hook_turn(
            [_text("pick one\n[[OPTIONS: go | hold]]")],
            [_text("ledger updated")],
        )
        self.assertTrue(resp.has_options)
        self.assertEqual(extract_marker_labels(resp.content), ["go", "hold"])

    def test_attachment_and_options_both_survive_two_blocks(self):
        resp, _, _ = hook_turn(
            [_text("here\nsend_file::files/outbox/map.png\n[[OPTIONS: a | b]]")],
            [_text("fix one")],
            [_text("fix two")],
        )
        self.assertIn("send_file::files/outbox/map.png", resp.content)
        self.assertTrue(resp.has_options)
        self.assertEqual(extract_marker_labels(resp.content), ["a", "b"])


class TestFullRegressionMatrix(unittest.TestCase):
    """Ticket matrix: hook blocks {0,1,2} x attachment {y,n} x OPTIONS {y,n}.

    For every combination: the whole body arrives, the attachment resolves
    exactly once, the keyboard follows the LAST declaration, and the 0-block
    (plain) turn is byte-identical to the legacy expression.
    """

    def test_matrix(self):
        root = Path(__file__).resolve().parents[1]
        rel = "tests/conftest.py"  # a real, small file for resolve_send_paths
        for blocks in (0, 1, 2):
            for attach in (False, True):
                for options in (False, True):
                    with self.subTest(
                        blocks=blocks, attach=attach, options=options
                    ):
                        seg_bodies = ["실제 답변 본문"]
                        if attach:
                            seg_bodies[0] += f"\nsend_file::{rel}"
                        if options:
                            seg_bodies[0] += "\n\n[[OPTIONS: go | hold]]"
                        if blocks >= 1:
                            b = "훅 추가 본문 하나"
                            if attach:
                                b += f"\nsend_file::{rel}"  # repeat -> once
                            seg_bodies.append(b)
                        if blocks >= 2:
                            b = "훅 추가 본문 둘"
                            if options:
                                b += "\n\n[[OPTIONS: final-a | final-b]]"
                            seg_bodies.append(b)

                        if blocks == 0:
                            resp, _, _ = run_turn([
                                _asst("end_turn", [_text(seg_bodies[0])]),
                                _result(result=""),
                            ])
                            # Byte-identical to the legacy expression.
                            self.assertEqual(resp.content, seg_bodies[0])
                            self.assertFalse(resp.turn_assembled)
                        else:
                            resp, _, _ = hook_turn(
                                *[[ _text(b) ] for b in seg_bodies]
                            )
                            self.assertTrue(resp.turn_assembled)

                        # Body: every segment's prose arrives.
                        self.assertIn("실제 답변 본문", resp.content)
                        if blocks >= 1:
                            self.assertIn("훅 추가 본문 하나", resp.content)
                        if blocks >= 2:
                            self.assertIn("훅 추가 본문 둘", resp.content)
                        # Attachment: exactly one resolved send, even when
                        # both segments declared the same path.
                        if attach:
                            self.assertEqual(
                                resolve_send_paths(resp.content, root),
                                [root / rel],
                            )
                        else:
                            self.assertEqual(
                                resolve_send_paths(resp.content, root), []
                            )
                        # Keyboard: armed iff declared; LAST declaration wins.
                        if options:
                            self.assertTrue(resp.has_options)
                            want = (
                                ["final-a", "final-b"]
                                if blocks >= 2
                                else ["go", "hold"]
                            )
                            self.assertEqual(
                                extract_marker_labels(resp.content), want
                            )
                        else:
                            self.assertFalse(resp.has_options)


class TestDuplicateTextJudgment(unittest.TestCase):
    """Reused _subtract_paras judgment: exact normalized full-paragraph match."""

    def test_full_restatement_dropped(self):
        resp, _, _ = hook_turn([_text("real answer")], [_text("real answer")])
        self.assertEqual(resp.content, "real answer")
        self.assertTrue(resp.turn_assembled)

    def test_partial_overlap_dropped_once(self):
        resp, _, _ = hook_turn(
            [_text("para A\n\npara B")],
            [_text("para B\n\npara C")],
        )
        self.assertEqual(resp.content, "para A\n\npara B\n\npara C")

    def test_whitespace_normalized_match(self):
        resp, _, _ = hook_turn(
            [_text("real  answer here")], [_text("real answer\there")]
        )
        self.assertEqual(resp.content, "real  answer here")

    def test_containment_is_not_a_match(self):
        # A reworded (non-identical) paragraph must NOT be dropped --
        # containment judgments are forbidden (DGN-699 precedent).
        resp, _, _ = hook_turn(
            [_text("real answer")], [_text("real answer, corrected")]
        )
        self.assertEqual(resp.content, "real answer\n\nreal answer, corrected")

    def test_three_segments_dedup_against_all_prior(self):
        resp, _, _ = hook_turn(
            [_text("para A")], [_text("para B")], [_text("para A\n\npara C")]
        )
        self.assertEqual(resp.content, "para A\n\npara B\n\npara C")


class TestMarkerOnceAndLastWins(unittest.TestCase):
    """Attachment sent once; the LAST [[OPTIONS]] declaration wins."""

    def test_same_send_file_path_resolves_once(self):
        root = Path(__file__).resolve().parents[1]  # bridge/ -- real files
        rel = "tests/conftest.py"
        resp, _, _ = hook_turn(
            [_text(f"first\nsend_file::{rel}")],
            [_text(f"second mention\nsend_file::{rel}")],
        )
        paths = resolve_send_paths(resp.content, root)
        self.assertEqual(paths, [root / rel])  # exactly once

    def test_options_last_declaration_wins(self):
        resp, _, _ = hook_turn(
            [_text("pick\n[[OPTIONS: old-a | old-b]]")],
            [_text("re-pick\n[[OPTIONS: new-a | new-b]]")],
        )
        self.assertTrue(resp.has_options)
        self.assertEqual(
            extract_marker_labels(resp.content), ["new-a", "new-b"]
        )

    def test_identical_options_marker_dedups_to_one(self):
        # Marker written as its own paragraph (the standalone-line authoring
        # shape) in BOTH segments: the repeated marker paragraph is an exact
        # dup and is dropped by the segment dedup.
        resp, _, _ = hook_turn(
            [_text("pick\n\n[[OPTIONS: a | b]]")],
            [_text("done\n\n[[OPTIONS: a | b]]")],
        )
        # The duplicated marker paragraph is dropped by the segment dedup;
        # the surviving declaration still arms the keyboard.
        self.assertTrue(resp.has_options)
        self.assertEqual(resp.content.count("[[OPTIONS: a | b]]"), 1)
        self.assertEqual(extract_marker_labels(resp.content), ["a", "b"])


class TestInterimBoundary(unittest.TestCase):
    """Fold/inline interim narration must not leak into the assembled body."""

    def test_fold_interim_quoted_not_in_answer_body(self):
        with patch.object(SdkBridge, "_fold_dispatch", new=AsyncMock()):
            resp, _, req = run_turn(
                [
                    _asst("end_turn", [_text("real answer")]),
                    _asst("tool_use", [_text("hook narration")]),
                    _asst("end_turn", [_text("addendum")]),
                    _result(result=""),
                ],
                interim_mode="fold",
            )
        # Interim was captured on the fold rail, never as a turn segment.
        self.assertEqual(req.interim_texts, ["hook narration"])
        self.assertEqual(req.final_segments, ["real answer", "addendum"])
        # The answer body is the assembled segments; the narration appears
        # ONLY inside the prepended fold quote (every mention is a "> " line).
        self.assertTrue(resp.content.endswith("real answer\n\naddendum"))
        for ln in resp.content.split("\n"):
            if "hook narration" in ln:
                self.assertTrue(ln.startswith(">"))

    def test_inline_interim_stays_out_of_body(self):
        resp, handler, req = run_turn(
            [
                _asst("end_turn", [_text("real answer")]),
                _asst("tool_use", [_text("inline narration")]),
                _asst("end_turn", [_text("addendum")]),
                _result(result=""),
            ],
            interim_mode="inline",
        )
        # Inline narration streams live but never enters final_segments.
        self.assertEqual(req.final_segments, ["real answer", "addendum"])
        self.assertNotIn("inline narration", resp.content)
        self.assertEqual(resp.content, "real answer\n\naddendum")


class TestEmptyTrailingTerminalRescue(unittest.TestCase):
    def test_guard_dropped_second_terminal_recovers_first(self):
        # Post-hook continuation whose text is entirely guard-dropped: only
        # ONE segment was captured, and the legacy buffer (last message's
        # blocks) is empty. The rescue must deliver the captured terminal
        # answer, never the untrusted msg.result.
        resp, _, req = hook_turn([_text("real answer")], [_text("")])
        self.assertEqual(req.final_segments, ["real answer"])
        self.assertEqual(resp.content, "real answer")
        self.assertFalse(resp.turn_assembled)


class TestFlakeRetryScrub(unittest.TestCase):
    def test_flake_retry_resets_segments(self):
        async def _inner():
            bridge_obj = SdkBridge()
            req = _pending()
            req.user_message = "hello"
            req.final_segments = ["stale segment"]
            state = _UserStreamState(client=MagicMock(), model=None)
            state.client.query = AsyncMock()
            with patch.object(
                SdkBridge, "_scrub_flake_drafts", new=AsyncMock()
            ):
                ok = await bridge_obj._dispatch_flake_retry(
                    1, state, req, _result(result="x")
                )
            return ok, req.final_segments

        ok, segs = asyncio.run(_inner())
        self.assertTrue(ok)
        self.assertEqual(segs, [])


class TestBotForceEdit(unittest.TestCase):
    """DGN-1253 bot seat: assembled turns bypass the no-op draft-edit skip."""

    def _run_edit(self, force_edit: bool):
        import bridge.bot as botmod

        async def _inner():
            instance = object.__new__(botmod.TelegramBot)
            bot = MagicMock()
            bot.edit_message_text = AsyncMock()
            ok = await botmod.TelegramBot._edit_streamed_prose_html(
                instance, bot, 1, "plain text", False, [42],
                force_edit=force_edit,
            )
            return ok, bot.edit_message_text.await_count

        return asyncio.run(_inner())

    def test_noop_skip_without_force(self):
        ok, edits = self._run_edit(force_edit=False)
        self.assertTrue(ok)
        self.assertEqual(edits, 0)  # legacy skip: draft assumed correct

    def test_force_edit_always_edits(self):
        ok, edits = self._run_edit(force_edit=True)
        self.assertTrue(ok)
        self.assertEqual(edits, 1)  # assembled turn: draft glue replaced


# DGN-2159: delivery directives survive Stop regeneration independently of
# the final prose. Exercise the real SDK reader through Telegram's file API.
from types import SimpleNamespace
from claude_agent_sdk import UserMessage
import bridge.bot as botmod
from bridge import sdk_bridge as sdk
from bridge.streaming import StreamingMessageHandler
from bridge.tests.test_dgn1651_stop_block_duplicate import (
    _Screen as _FileScreen, _asst as _file_asst,
    _pending as _file_pending, _result as _file_result,
)


def feedback():
    return UserMessage(content=[TextBlock(
        text="Stop hook feedback:\nIDLE_PROMISE: regenerate the final reply."
    )])


def text(body, terminal=None, parent=None):
    msg = _file_asst(terminal, [TextBlock(text=body)])
    msg.parent_tool_use_id = parent
    return msg


async def deliver(root, sequences, hold, mode, first_push=False):
    """Keep reader/stream/reply/artifact/path/image-send code real; mock only
    the SDK event source and Telegram API. Multiple turns share one stream."""
    screen = _FileScreen()
    api = screen.bot()
    photos = []

    async def send_photo(chat_id, photo):
        photos.append((chat_id, photo.name, photo.read()))

    api.send_photo = AsyncMock(side_effect=send_photo)
    api.send_document = AsyncMock()
    instance = object.__new__(botmod.TelegramBot)
    instance.application = SimpleNamespace(bot=api)
    bridge = sdk.SdkBridge()
    state = sdk._UserStreamState(client=MagicMock(), model=None)
    if first_push:
        state.last_chat_id = 1
        state.dispatch_return_turn_id = "test-return"

        async def push(chat_id, body, has_options, classifier):
            await instance._send_smart(chat_id, body, force_options=has_options)

        state.proactive_push = push
    bridge._streams[1] = state
    responses = []
    for seq in sequences:
        handler = StreamingMessageHandler(api, chat_id=1, user_id=1)
        handler.min_chars = 1
        handler.min_interval = 0
        req = _file_pending(handler)
        req.sent = True
        req.hold_terminal = hold
        state.pending.append(req)

        async def receive():
            for msg in seq:
                yield msg

        state.client.receive_messages = receive
        message = MagicMock()
        message.chat.id = 1
        message.get_bot.return_value = api

        async def reply_text(body, **kwargs):
            return await api.send_message(text=body, **kwargs)

        message.reply_text = AsyncMock(side_effect=reply_text)
        with patch.object(sdk, "INTERIM_MODE", mode), \
                patch.object(sdk, "STREAM_INTERIM", mode == "inline"), \
                patch.object(sdk, "PROJECT_ROOT", root), \
                patch.object(sdk, "_write_dispatch_return_context"), \
                patch.object(sdk, "_clear_dispatch_return_context"), \
                patch.object(botmod, "PROJECT_ROOT", root), \
                patch.object(instance, "_reply_link_id", return_value=None):
            await bridge._reader_loop(1, state)
            response = await asyncio.wait_for(req.future, 1)
            await instance._reply_smart(
                message, response.content, force_options=response.has_options,
                streamed=response.streamed,
                draft_message_ids=response.draft_message_ids,
                classifier_injected=response.options_classifier_injected,
                assembled=response.turn_assembled,
            )
        responses.append(response)
    return responses, photos, api, screen


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "files" / "outbox" / "picture.png"
    path.parent.mkdir(parents=True)
    # Valid 1x1 PNG; the real header probe and file opening run on it.
    import base64
    path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+aB1sAAAAASUVORK5CYII="
    ))
    return path


@pytest.mark.parametrize("hold", [False, True])
@pytest.mark.parametrize("mode", ["inline", "fold", "suppress"])
@pytest.mark.parametrize("terminal", [None, "end_turn"])
@pytest.mark.parametrize("repeat", [False, True])
def test_stop_regeneration_delivers_existing_image_once(
    tmp_path, image, hold, mode, terminal, repeat,
):
    marker = "send_file::files/outbox/picture.png"
    replacement = "The picture is ready."
    if repeat:
        # Alias + repeated raw path exercise the existing resolved-path dedup.
        replacement += f"\n{marker}\nsend_file::{image}"
    seq = [
        text("Here is the requested picture.\n" + marker, terminal),
        feedback(),
        text(replacement, terminal),
        _file_result(replacement),
    ]
    responses, photos, api, screen = asyncio.run(
        deliver(tmp_path, [seq], hold, mode)
    )
    assert photos == [(1, str(image), image.read_bytes())]
    api.send_photo.assert_awaited_once()
    api.send_document.assert_not_awaited()
    assert marker in responses[0].content
    assert all("send_file::" not in body for body in screen.view())
    if hold:
        assert all("Here is the requested picture." not in body
                   for body in screen.view())


def test_interim_directive_survives_tool_step_and_next_turn_is_clean(tmp_path, image):
    seq = [
        text("Image ready.\nsend_file::files/outbox/picture.png"),
        _file_asst("tool_use", [ToolUseBlock(id="t1", name="Read", input={})]),
        UserMessage(content="Tool step complete."),
        text("The picture is ready."),
        _file_result("The picture is ready."),
    ]
    next_turn = [text("Next answer."), _file_result("Next answer.")]
    responses, photos, _, _ = asyncio.run(
        deliver(tmp_path, [seq, next_turn], True, "suppress")
    )
    assert len(photos) == 1
    assert "send_file::" not in responses[1].content


@pytest.mark.parametrize("final", ["NO_PUSH", "No attachment from this turn."])
def test_subagent_directive_is_not_delivered(tmp_path, image, final):
    seq = [
        text("send_file::files/outbox/picture.png", parent="child-tool"),
        text(final), _file_result(final),
    ]
    _, photos, api, _ = asyncio.run(deliver(tmp_path, [seq], True, "suppress"))
    assert photos == []
    api.send_document.assert_not_awaited()


def test_silent_regeneration_preserves_explicit_file_delivery(tmp_path, image):
    seq = [text("send_file::files/outbox/picture.png"), feedback(),
           text("NO_PUSH"), _file_result("NO_PUSH")]
    _, photos, api, screen = asyncio.run(deliver(tmp_path, [seq], True, "suppress"))
    assert photos == [(1, str(image), image.read_bytes())]
    api.send_photo.assert_awaited_once()
    assert all("NO_PUSH" not in body for body in screen.view())


def test_missing_file_is_still_filtered(tmp_path):
    seq = [text("send_file::files/outbox/missing.png"), feedback(),
           text("The picture is ready."), _file_result("The picture is ready.")]
    _, photos, api, _ = asyncio.run(deliver(tmp_path, [seq], True, "suppress"))
    assert photos == []
    api.send_document.assert_not_awaited()


@pytest.mark.parametrize("repeat", [False, True])
def test_file_already_sent_by_first_text_is_not_sent_again(tmp_path, image, repeat):
    replacement = "The picture is ready."
    if repeat:
        replacement += f"\nsend_file::{image}"
    seq = [text("Image ready.\nsend_file::files/outbox/picture.png"),
           feedback(), text(replacement), _file_result(replacement)]
    _, photos, api, _ = asyncio.run(
        deliver(tmp_path, [seq], True, "suppress", first_push=True)
    )
    assert photos == [(1, str(image), image.read_bytes())]
    api.send_photo.assert_awaited_once()

if __name__ == "__main__":
    unittest.main()
