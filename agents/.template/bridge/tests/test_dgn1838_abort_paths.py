"""DGN-1838 follow-up: /stop, timeout and interrupt end paths promote a
substantive mid-turn answer out of the fold instead of collapsing it under the
stop/timeout caption. Short progress lines keep folding."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bridge.formatting import FOLD_CAPTION_STOPPED, FOLD_CAPTION_TIMEOUT
from bridge.sdk_bridge import SdkBridge, _UserStreamState
from bridge.tests.test_dgn1651_stop_block_duplicate import _pending
from bridge.tests.test_dgn1838_interim_answer_promotion import (
    ANSWER,
    ANSWER_NEEDLE,
)

PROGRESS = "reading the config file"


# FDGN-686: these cases test fold mechanics with English fixture text; pin the
# language so the off-language narration filter does not hide the fixtures.
_SAVED_AGENT_LANG = None


def setUpModule():
    global _SAVED_AGENT_LANG
    from bridge.config import config as _cfg
    _SAVED_AGENT_LANG = getattr(_cfg, "agent_lang", None)
    _cfg.agent_lang = "en"


def tearDownModule():
    from bridge.config import config as _cfg
    _cfg.agent_lang = _SAVED_AGENT_LANG


def _setup(grown: bool, with_push: bool = True, push_fails: bool = False):
    bridge_obj = SdkBridge()
    req = _pending(None)
    req.sent = True
    blocks = [PROGRESS, ANSWER]
    if grown:
        req.fold_msg_id = 7
        req.fold_buf = list(blocks)
    else:
        req.interim_texts = list(blocks)
    state = _UserStreamState(client=MagicMock(), model=None)
    state.pending.append(req)
    pushes = []

    async def _push(chat_id, content, has_options, classifier_injected):
        if push_fails:
            raise RuntimeError("down")
        pushes.append(content)

    state.proactive_push = _push if with_push else None
    bridge_obj._streams[1] = state
    return bridge_obj, state, req, pushes


_LOOP = None


def _run(coro_fn):
    async def _inner():
        with patch("bridge.sdk_bridge.INTERIM_MODE", "fold"), patch(
            "bridge.sdk_bridge.STREAM_INTERIM", False
        ):
            return await coro_fn()

    return _LOOP.run_until_complete(_inner())


class TestAbortPromotion(unittest.TestCase):
    def setUp(self):
        global _LOOP
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)

    def tearDown(self):
        _LOOP.close()
        asyncio.set_event_loop(None)

    def _check(self, req, pushes, grown):
        self.assertEqual(len(pushes), 1)
        self.assertIn(ANSWER_NEEDLE, pushes[0])
        if grown:
            self.assertEqual(req.fold_buf, [PROGRESS])

    def test_stop_cancel_user_streaming(self):
        for grown in (True, False):
            b, state, req, pushes = _setup(grown)
            _run(lambda: b.cancel_user_streaming(1))
            self._check(req, pushes, grown)

    def test_timeout_preserve(self):
        for grown in (True, False):
            b, state, req, pushes = _setup(grown)
            b._disconnect_user_stream = AsyncMock()
            _run(lambda: b.handle_timeout_preserve(1))
            self._check(req, pushes, grown)

    def test_interrupt(self):
        for trigger in ("stop", "timeout", "auto"):
            for grown in (True, False):
                b, state, req, pushes = _setup(grown)
                state.client.interrupt = AsyncMock()
                state.client._query = object()
                ok = _run(lambda: b.interrupt(1, trigger=trigger))
                self.assertTrue(ok)
                self._check(req, pushes, grown)

    def test_caption_passed_through(self):
        b, state, req, pushes = _setup(True)
        b._fold_finalize = AsyncMock(return_value=True)
        _run(lambda: b._fold_finalize_promoting(1, state, req, FOLD_CAPTION_TIMEOUT))
        b._fold_finalize.assert_awaited_once_with(req, FOLD_CAPTION_TIMEOUT)

    def test_only_progress_pushes_nothing(self):
        b, state, req, pushes = _setup(True)
        req.fold_buf = [PROGRESS]
        _run(lambda: b._fold_finalize_promoting(1, state, req, FOLD_CAPTION_STOPPED))
        self.assertEqual(pushes, [])
        self.assertEqual(req.fold_buf, [PROGRESS])

    def test_no_push_seat_keeps_answer_in_fold(self):
        b, state, req, pushes = _setup(True, with_push=False)
        _run(lambda: b._fold_finalize_promoting(1, state, req, FOLD_CAPTION_STOPPED))
        self.assertEqual(req.fold_buf, [PROGRESS, ANSWER])

    def test_push_failure_does_not_raise(self):
        b, state, req, pushes = _setup(True, push_fails=True)
        _run(lambda: b._fold_finalize_promoting(1, state, req, FOLD_CAPTION_STOPPED))
        self.assertEqual(pushes, [])

    def test_push_failure_keeps_answer_in_fold(self):
        """DGN-1967 (review 21): the fold is deleted only after the push. A
        failed push leaves the answer in the confirmed fold, never gone."""
        b, state, req, pushes = _setup(True, push_fails=True)
        b._fold_delete = AsyncMock()
        _run(lambda: b._fold_finalize_promoting(1, state, req, FOLD_CAPTION_STOPPED))
        self.assertEqual(req.fold_buf, [PROGRESS, ANSWER])
        b._fold_delete.assert_not_awaited()

    def test_answer_only_fold_deleted_after_push(self):
        b, state, req, pushes = _setup(True)
        req.fold_buf = [ANSWER]
        order = []
        push = state.proactive_push

        async def _push(*a):
            order.append("push")
            await push(*a)

        async def _delete(r):
            order.append("delete")
            return True

        state.proactive_push = _push
        b._fold_delete = _delete
        _run(lambda: b._fold_finalize_promoting(1, state, req, FOLD_CAPTION_STOPPED))
        self.assertEqual(order, ["push", "delete"])

    def test_non_fold_mode_untouched(self):
        b, state, req, pushes = _setup(True)

        async def _go():
            with patch("bridge.sdk_bridge.INTERIM_MODE", "inline"):
                await b._fold_finalize_promoting(1, state, req, FOLD_CAPTION_STOPPED)

        _LOOP.run_until_complete(_go())
        self.assertEqual(pushes, [])


if __name__ == "__main__":
    unittest.main()
