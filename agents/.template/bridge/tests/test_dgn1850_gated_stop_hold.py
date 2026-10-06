"""DGN-1850: the bridge holds the terminal answer while a blocking Stop gate
can fire, so a Stop block never rewrites a bubble the owner is watching.

Measured (rehearsal 2026-10-04 09:18:40, onboarding FC_REPEAT_BLOCK): the
reply streamed to Telegram, the onboarding Stop gate blocked it, and the
DGN-1651/1703 inline retraction rewrote the visible bubble with the
regeneration -- the owner watched the text change.

Fix under test: while blocking_stop_gate_window() is open (the onboarding
gate's own ONBOARDING_PENDING marker), main-thread text from a message with
no tool call is held. A later tool call releases it as narration; a Stop-hook
re-prompt discards it; the ResultMessage delivers it as one new message.

Coverage: the signal, the request latch, gated + block (one message, the
regenerated text, no edit of an earlier bubble), gated + no block (same text
as today), outside the window (streaming unchanged), narration release,
text-less regeneration, interrupt release, [[OPTIONS]] buttons on the held
final, and the injected (no-pending) path.
"""

import pytest

from bridge import sdk_bridge as _locale_bridge
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import (
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import bridge.bot as botmod
from bridge import sdk_bridge as sdk
from bridge.sdk_bridge import (
    ONBOARDING_PENDING_MARKER,
    SdkBridge,
    _UserStreamState,
    blocking_stop_gate_window,
)
from bridge.streaming import StreamingMessageHandler
from bridge.tests.test_dgn1651_stop_block_duplicate import (
    _Screen,
    _asst,
    _pending,
    _result,
)

BLOCKED = "Let me introduce myself again."
REGEN = "Nice to meet you! What name should I go by?"
NARRATION = "Checking the setup file first."
FEEDBACK = (
    "Stop hook feedback:\nOnboarding gate (FC_REPEAT_BLOCK): the reply repeats "
    "the opener. Regenerate."
)
OPTIONS_BODY = "Which name do you like?\n1. Haru\n2. Byeol\n[[OPTIONS]]"

# ASCII fixtures; the register guard is not under test (DGN-1842 precedent).
_guard_patch = patch.object(sdk, "_register_guard", lambda text: text)


@pytest.fixture(autouse=True)
def _en_instance(monkeypatch):
    # These fixtures narrate in English: pin an en instance so the interim
    # locale gate (sdk_bridge._interim_off_locale) stays out of the interim
    # mechanics under test, whatever LOCALE the shell exports. The ko side
    # is covered by test_interim_locale_gate.py.
    monkeypatch.setattr(_locale_bridge.config, "locale", "en")



def setUpModule():
    _guard_patch.start()


def tearDownModule():
    _guard_patch.stop()


def _think():
    return _asst(None, [ThinkingBlock(thinking="...", signature="sig")])


def _text(text: str):
    # The measured live shape: every streamed message has stop_reason=None.
    return _asst(None, [TextBlock(text=text)])


def _tool_call(tid: str):
    return _asst(None, [ToolUseBlock(id=tid, name="Read", input={"file_path": "x"})])


def _tool_result(tid: str):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="ok")])


def _feedback():
    return UserMessage(content=[TextBlock(text=FEEDBACK)])


def run_turn(seq, hold: bool, interim_mode: str = "inline", artifacts=None):
    """Drive the REAL reader loop + REAL bot delivery seat over a screen that
    records every send/edit/delete. Returns (response, mid_turn_log,
    final_surface, full_log). mid_turn_log is what the owner saw happen
    before the ResultMessage was processed."""

    async def _inner():
        screen = _Screen()
        bot = screen.bot()
        handler = StreamingMessageHandler(bot, chat_id=1, user_id=1)
        handler.min_chars = 1
        handler.min_interval = 0.0
        bridge_obj = SdkBridge()
        req = _pending(handler)
        req.sent = True
        req.hold_terminal = hold
        state = _UserStreamState(client=MagicMock(), model=None)
        state.pending.append(req)
        mid_turn = []

        async def fake_receive():
            for m in seq:
                if m is seq[-1]:
                    mid_turn.extend(screen.log)
                yield m

        state.client.receive_messages = fake_receive
        bridge_obj._streams[1] = state
        with patch("bridge.sdk_bridge.STREAM_INTERIM", interim_mode == "inline"), \
                patch("bridge.sdk_bridge.INTERIM_MODE", interim_mode):
            await bridge_obj._reader_loop(1, state)
        response = await asyncio.wait_for(req.future, timeout=1.0)

        message = MagicMock()
        message.get_bot.return_value = bot
        message.chat.id = 1

        async def _reply_text(text, **kwargs):
            screen._next += 1
            screen.texts[screen._next] = text
            screen.order.append(screen._next)
            screen.log.append(("send", screen._next))
            return MagicMock(message_id=screen._next)

        message.reply_text = AsyncMock(side_effect=_reply_text)
        instance = object.__new__(botmod.TelegramBot)
        art = artifacts or AsyncMock()
        with patch.object(botmod.TelegramBot, "_reply_link_id", return_value=None), \
                patch.object(botmod.TelegramBot, "_send_content_artifacts", new=art):
            await botmod.TelegramBot._reply_smart(
                instance,
                message,
                response.content,
                force_options=response.has_options,
                streamed=response.streamed,
                draft_message_ids=response.draft_message_ids,
                classifier_injected=response.options_classifier_injected,
                assembled=getattr(response, "turn_assembled", False),
            )
        return response, mid_turn, screen.view(), list(screen.log)

    return asyncio.run(_inner())


def blocked_turn(regenerated=REGEN):
    """The 09:18 shape: tools -> answer -> Stop block -> tools -> answer."""
    seq = [
        _think(),
        _tool_call("t1"),
        _tool_result("t1"),
        _text(BLOCKED),
        _feedback(),
        _think(),
        _tool_call("t2"),
        _tool_result("t2"),
    ]
    if regenerated is not None:
        seq.append(_text(regenerated))
    seq.append(_result(result=regenerated or BLOCKED))
    return seq


def plain_turn(answer=REGEN):
    return [_think(), _tool_call("t1"), _tool_result("t1"), _text(answer),
            _result(result=answer)]


class TestGateWindowSignal(unittest.TestCase):
    def _root(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, body in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        return root

    def test_marker_in_identity_file_opens_the_window(self):
        root = self._root({"identity/hot.custom.agent.md":
                           "# AGENT\n" + ONBOARDING_PENDING_MARKER + "\n"})
        self.assertTrue(blocking_stop_gate_window(root))

    def test_onboarded_or_missing_identity_closes_it(self):
        self.assertFalse(blocking_stop_gate_window(
            self._root({"identity/hot.custom.agent.md": "# AGENT\n"})))
        self.assertFalse(blocking_stop_gate_window(self._root({})))

    def test_resolution_order_matches_the_gate(self):
        # identity/ wins over a legacy PROFILE.md (resolve_persona_md order).
        root = self._root({
            "identity/hot.custom.agent.md": "# AGENT\n",
            "PROFILE.md": ONBOARDING_PENDING_MARKER,
        })
        self.assertFalse(blocking_stop_gate_window(root))
        self.assertTrue(blocking_stop_gate_window(
            self._root({"PROFILE.md": ONBOARDING_PENDING_MARKER})))

    def test_marker_literal_is_lockstep_with_the_gate(self):
        gate = Path(sdk.__file__).resolve().parents[1] / "routines" / \
            "onboarding-gate-stop.py"
        self.assertIn('MARKER = "%s"' % ONBOARDING_PENDING_MARKER,
                      gate.read_text(encoding="utf-8"))

    def test_process_message_latches_the_window_per_turn(self):
        seen = []

        async def _go(open_window):
            bridge_obj = SdkBridge()
            state = _UserStreamState(client=MagicMock(), model=None)

            async def _stream(*_a, **_k):
                return state

            async def _dispatch(st):
                head = st.pending[0]
                seen.append(head.hold_terminal)
                head.future.set_result(sdk.ChatResponse(content="ok"))

            with patch.object(bridge_obj, "_get_or_create_stream", _stream), \
                    patch.object(bridge_obj, "_dispatch_next_query", _dispatch), \
                    patch.object(sdk, "blocking_stop_gate_window",
                                 return_value=open_window):
                await bridge_obj.process_message("hi", 1, 1)

        asyncio.run(_go(True))
        asyncio.run(_go(False))
        self.assertEqual(seen, [True, False])


class TestGatedStopBlock(unittest.TestCase):
    def test_block_delivers_one_message_and_edits_nothing(self):
        for mode in ("inline", "fold", "suppress"):
            with self.subTest(mode=mode):
                response, mid_turn, final, log = run_turn(
                    blocked_turn(), hold=True, interim_mode=mode
                )
                self.assertEqual(mid_turn, [])  # nothing shown before the end
                self.assertEqual(response.content, REGEN)
                self.assertFalse(response.streamed)
                self.assertEqual(final, [REGEN])
                self.assertEqual([op for op, _ in log], ["send"])

    def test_identical_regeneration_is_one_message(self):
        response, _mid, final, log = run_turn(blocked_turn(BLOCKED), hold=True)
        self.assertEqual(final, [BLOCKED])
        self.assertEqual([op for op, _ in log], ["send"])

    def test_textless_regeneration_keeps_the_held_answer(self):
        # DGN-1651/1703 rule: a regeneration with no text leaves the answer.
        for mode in ("inline", "fold"):
            with self.subTest(mode=mode):
                _response, mid_turn, final, log = run_turn(
                    blocked_turn(None), hold=True, interim_mode=mode
                )
                self.assertEqual(mid_turn, [])
                self.assertEqual(final, [BLOCKED])
                self.assertEqual([op for op, _ in log], ["send"])

    def test_blocked_answer_is_never_promoted_out_of_the_fold(self):
        # DGN-1838 promotes substantive interim answers; a held-and-discarded
        # answer never entered the interim capture.
        response, _mid, final, _log = run_turn(
            blocked_turn(), hold=True, interim_mode="fold"
        )
        self.assertNotIn(BLOCKED, "\n".join(final))
        self.assertNotIn(BLOCKED, response.content)


class TestGatedNoBlock(unittest.TestCase):
    def test_same_text_as_today_in_one_message(self):
        for mode in ("inline", "fold", "suppress"):
            with self.subTest(mode=mode):
                today, *_ = run_turn(plain_turn(), hold=False, interim_mode=mode)
                held, mid_turn, final, log = run_turn(
                    plain_turn(), hold=True, interim_mode=mode
                )
                self.assertEqual(held.content, today.content)
                self.assertEqual(mid_turn, [])
                self.assertEqual(final, [REGEN])
                self.assertEqual([op for op, _ in log], ["send"])

    def test_narration_still_streams_and_stays(self):
        seq = [_text(NARRATION), _tool_call("t1"), _tool_result("t1"),
               _text(REGEN), _result(result=REGEN)]
        _response, mid_turn, final, log = run_turn(seq, hold=True)
        # The narration showed live (released by the tool call) ...
        self.assertTrue(mid_turn and mid_turn[0][0] == "send")
        # ... stands as its own bubble, and the answer is a NEW message.
        self.assertEqual(final, [NARRATION, REGEN])
        narration_mid = mid_turn[0][1]
        answer_sends = [mid for op, mid in log if op == "send" and mid != narration_mid]
        self.assertEqual(len(answer_sends), 1)
        self.assertNotIn(("edit", answer_sends[0]), log)

    def test_narration_in_the_same_message_as_a_tool_call(self):
        seq = [_asst(None, [TextBlock(text=NARRATION),
                            ToolUseBlock(id="t1", name="Read", input={})]),
               _tool_result("t1"), _text(REGEN), _result(result=REGEN)]
        _response, mid_turn, final, _log = run_turn(seq, hold=True)
        self.assertTrue(mid_turn)
        self.assertEqual(final, [NARRATION, REGEN])

    def test_held_final_with_options_builds_buttons(self):
        art = AsyncMock()
        response, _mid, _final, _log = run_turn(
            plain_turn(OPTIONS_BODY), hold=True, artifacts=art
        )
        self.assertTrue(response.has_options)
        self.assertFalse(response.streamed)
        art.assert_awaited_once()
        _target, content, force_options = art.await_args.args[:3]
        self.assertTrue(force_options)
        self.assertIn("[[OPTIONS]]", content)
        self.assertEqual(art.await_args.kwargs["options"], ["Haru", "Byeol"])


class TestOutsideTheWindow(unittest.TestCase):
    def test_streaming_is_unchanged(self):
        _response, mid_turn, final, _log = run_turn(plain_turn(), hold=False)
        self.assertEqual(mid_turn[0][0], "send")  # streamed live, as today
        self.assertEqual(final, [REGEN])

    def test_dgn1703_retraction_still_rewrites_outside(self):
        _response, mid_turn, final, _log = run_turn(blocked_turn(), hold=False)
        self.assertTrue(mid_turn)
        self.assertEqual(final, [REGEN])


class TestInterruptReleasesHeldText(unittest.TestCase):
    def test_interrupt_shows_the_held_text(self):
        async def _go():
            screen = _Screen()
            handler = StreamingMessageHandler(screen.bot(), chat_id=1, user_id=1)
            handler.min_chars = 1
            handler.min_interval = 0.0
            bridge_obj = SdkBridge()
            req = _pending(handler)
            req.sent = True
            req.hold_terminal = True
            req.held_blocks.append((REGEN, REGEN, False, False, 1))
            client = MagicMock()
            client._query = object()
            client.interrupt = AsyncMock()
            state = _UserStreamState(client=client, model=None)
            state.pending.append(req)
            bridge_obj._streams[1] = state
            with patch("bridge.sdk_bridge.STREAM_INTERIM", True), \
                    patch("bridge.sdk_bridge.INTERIM_MODE", "inline"):
                self.assertTrue(await bridge_obj.interrupt(1, trigger="user"))
            return screen.view(), req

        view, req = asyncio.run(_go())
        self.assertEqual(view, [REGEN])
        self.assertEqual(req.held_blocks, [])


class TestInjectedPathInWindow(unittest.TestCase):
    """The no-pending path buffers every block until the ResultMessage, so a
    gated turn there never shows text mid-turn; DGN-1842 drops the blocked
    draft. Pinned here with the window open."""

    def _run(self, seq):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "identity").mkdir()
        (root / "identity" / "hot.custom.agent.md").write_text(
            ONBOARDING_PENDING_MARKER, encoding="utf-8")
        push = AsyncMock()
        state = _UserStreamState(client=MagicMock(), model=None)
        state.last_chat_id = 1
        state.proactive_push = push
        bridge_obj = SdkBridge()
        pushed_mid_turn = []

        async def _go():
            for m in seq:
                if m is seq[-1]:
                    pushed_mid_turn.extend(push.await_args_list)
                await bridge_obj._handle_proactive_message(1, state, m)

        with patch.object(sdk, "PROJECT_ROOT", root):
            self.assertTrue(blocking_stop_gate_window())
            asyncio.run(_go())
        return pushed_mid_turn, [c.args[1] for c in push.await_args_list]

    def test_block_pushes_the_regeneration_once(self):
        mid_turn, pushed = self._run(blocked_turn())
        self.assertEqual(mid_turn, [])
        self.assertEqual(pushed, [REGEN])

    def test_no_block_pushes_the_answer_once(self):
        mid_turn, pushed = self._run(plain_turn())
        self.assertEqual(mid_turn, [])
        self.assertEqual(pushed, [REGEN])


if __name__ == "__main__":
    unittest.main()
