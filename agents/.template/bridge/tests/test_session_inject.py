"""DGN-217: background-turn injection + NO_PUSH suppression tests."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from bridge.sdk_bridge import SdkBridge, _UserStreamState


def _make_state(**kw):
    client = MagicMock()
    client.query = AsyncMock()
    st = _UserStreamState(client=client, model=None)
    st.last_chat_id = kw.get("chat_id", 111)
    st.proactive_push = kw.get("push", AsyncMock())
    st.last_session_id = kw.get("session_id", "sess-1")
    return st


class TestInjectBackgroundTurn(unittest.TestCase):
    def setUp(self):
        self.bridge = SdkBridge()

    def test_no_stream_returns_false(self):
        ok = asyncio.run(self.bridge.inject_background_turn(1, "hello"))
        self.assertFalse(ok)

    def test_pending_request_defers(self):
        st = _make_state()
        st.pending.append(MagicMock())
        self.bridge._streams[1] = st
        ok = asyncio.run(self.bridge.inject_background_turn(1, "hello"))
        self.assertFalse(ok)
        st.client.query.assert_not_awaited()

    def test_idle_stream_injects_with_session(self):
        st = _make_state(session_id="sess-42")
        self.bridge._streams[1] = st
        ok = asyncio.run(self.bridge.inject_background_turn(1, "notify"))
        self.assertTrue(ok)
        st.client.query.assert_awaited_once_with("notify", session_id="sess-42")

    def test_idle_stream_no_session_uses_default(self):
        st = _make_state(session_id=None)
        self.bridge._streams[1] = st
        ok = asyncio.run(self.bridge.inject_background_turn(1, "notify"))
        self.assertTrue(ok)
        st.client.query.assert_awaited_once_with("notify", session_id="default")


class TestNoPushSentinel(unittest.TestCase):
    def setUp(self):
        self.bridge = SdkBridge()

    def _flush(self, texts):
        st = _make_state()
        st.proactive_texts = list(texts)
        asyncio.run(self.bridge._flush_proactive(1, st))
        return st

    def test_no_push_sentinel_suppresses(self):
        st = self._flush(["NO_PUSH"])
        st.proactive_push.assert_not_awaited()
        self.assertEqual(st.proactive_texts, [])  # buffer still drained

    def test_no_push_with_whitespace_suppresses(self):
        st = self._flush(["  NO_PUSH  \n"])
        st.proactive_push.assert_not_awaited()

    def test_normal_text_still_pushes(self):
        st = self._flush(["real report line"])
        st.proactive_push.assert_awaited_once()

    def test_no_push_inside_longer_text_is_not_suppressed(self):
        st = self._flush(["work done.\nNO_PUSH is not a bare sentinel here"])
        st.proactive_push.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
