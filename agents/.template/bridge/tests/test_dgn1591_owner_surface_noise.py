"""DGN-1591: owner-surface noise bundle -- the restart-day measurements.

One restart put four message classes on the owner's screen; only "restart
complete" carries information the owner can use. This file pins the two
framework-owned fixes that live outside DGN-1209's own suite:

  (D) self_restart.sh prefix idempotence -- a model-composed --notice already
      carrying the persona prefix must NOT get a second prefix stacked on it
      (measured 2026-09-19: "PREFIX PREFIX restart done" on the owner's
      screen). The shipped compose block is extracted from the script and
      executed for real in bash.

  (B) quiet injected turns (DGN-1588) -- an outbound-record injection asked
      for silence in prose ("응답 불필요") and the model answered the owner
      anyway. The forcing point is delivery-side: inject_background_turn
      (quiet=True) suppresses the turn's output unless the model opts in by
      ending with the bare sentinel line PUSH (inverse of NO_PUSH). These
      tests drive the REAL SdkBridge._flush_proactive.

The (A) half (machine-line alert audience, DGN-1589) is pinned in
test_dgn1209_machine_line_gate.py sections R3/G; the end-to-end "one restart
-> ONE owner-surface message" total is pinned in
test_dgn1528_outbound_injection_e2e.py.
"""

import asyncio
import shlex
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bridge.sdk_bridge import SdkBridge, _UserStreamState

BRIDGE_DIR = Path(__file__).resolve().parents[1]
SELF_RESTART = BRIDGE_DIR / "self_restart.sh"


def _make_state(**kw):
    client = MagicMock()
    client.query = AsyncMock()
    st = _UserStreamState(client=client, model=None)
    st.last_chat_id = kw.get("chat_id", 111)
    st.proactive_push = kw.get("push", AsyncMock())
    st.last_session_id = kw.get("session_id", "sess-1")
    return st


# ===========================================================================
# (D) self_restart.sh: prefix application is idempotent
# ===========================================================================

def _extract_compose_block() -> str:
    """Slice the shipped NOTICE-compose block (if/else/fi) out of
    self_restart.sh so the test executes the real lines, not a copy."""
    src = SELF_RESTART.read_text(encoding="utf-8")
    anchor = 'if [[ -n "$PREFIX" && "$NOTICE" == "$PREFIX"* ]]; then'
    start = src.index(anchor)
    end = src.index("fi", start) + len("fi")
    return src[start:end]


def _compose(prefix: str, notice: str) -> str:
    block = _extract_compose_block()
    script = (
        "PREFIX=%s; NOTICE=%s; %s; printf '%%s' \"$MSG\""
        % (shlex.quote(prefix), shlex.quote(notice), block)
    )
    p = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10
    )
    assert p.returncode == 0, p.stderr
    return p.stdout


class TestNoticePrefixIdempotent(unittest.TestCase):
    @unittest.skipUnless(SELF_RESTART.exists(), "self_restart.sh not shipped here")
    def test_notice_already_prefixed_is_sent_as_is(self):
        # The measured defect shape: caller composed the prefix themselves.
        self.assertEqual(
            _compose("☠️", "☠️ 재시작 완료 · 2.6.0 반영"),
            "☠️ 재시작 완료 · 2.6.0 반영",
        )

    @unittest.skipUnless(SELF_RESTART.exists(), "self_restart.sh not shipped here")
    def test_unprefixed_notice_still_gets_exactly_one_prefix(self):
        # self-update.sh composes prefix-less notices and relies on this.
        self.assertEqual(
            _compose("☠️", "재시작 완료 · v2.6.0 업데이트 완료"),
            "☠️ 재시작 완료 · v2.6.0 업데이트 완료",
        )

    @unittest.skipUnless(SELF_RESTART.exists(), "self_restart.sh not shipped here")
    def test_empty_prefix_leaves_no_leading_space(self):
        # DGN-828 rule must survive the idempotence guard.
        self.assertEqual(_compose("", "재시작 완료"), "재시작 완료")


# ===========================================================================
# (B) quiet injected turns: silence is the default, PUSH is the opt-in
# ===========================================================================

class TestQuietInjection(unittest.TestCase):
    def setUp(self):
        self.bridge = SdkBridge()

    def test_inject_quiet_sets_the_flag(self):
        st = _make_state()
        self.bridge._streams[1] = st
        ok = asyncio.run(self.bridge.inject_background_turn(1, "note", quiet=True))
        self.assertTrue(ok)
        self.assertEqual(st.injected_turn_mode, "quiet")

    def test_inject_default_is_not_quiet(self):
        st = _make_state()
        self.bridge._streams[1] = st
        asyncio.run(self.bridge.inject_background_turn(1, "note"))
        self.assertEqual(st.injected_turn_mode, "loud")

    def _flush(self, texts, quiet=True):
        st = _make_state()
        st.injected_turn_mode = "quiet" if quiet else "loud"
        st.proactive_texts = list(texts)
        asyncio.run(self.bridge._flush_proactive(1, st))
        return st

    def test_quiet_turn_without_sentinel_is_suppressed(self):
        # The measured leak: "기록만 확인했습니다: 별도 조치 없습니다."
        st = self._flush(["기록만 확인했습니다: 별도 조치 없습니다."])
        st.proactive_push.assert_not_awaited()

    def test_quiet_turn_with_trailing_push_delivers_body_without_sentinel(self):
        # The useful case (a real report after resuming interrupted work).
        st = self._flush(["dev.21 소비 확인 완료, 민팅 조건 충족.\nPUSH"])
        st.proactive_push.assert_awaited_once()
        delivered = st.proactive_push.await_args.args[1]
        self.assertIn("민팅 조건 충족", delivered)
        self.assertNotIn("PUSH", delivered)

    def test_quiet_turn_with_bare_push_only_is_suppressed(self):
        # Sentinel with no body = nothing to say; never a blank owner ping.
        st = self._flush(["PUSH"])
        st.proactive_push.assert_not_awaited()

    def test_quiet_turn_no_push_sentinel_still_suppresses(self):
        st = self._flush(["조용히 있겠습니다.\nNO_PUSH"])
        st.proactive_push.assert_not_awaited()

    def test_quiet_options_marker_without_push_delivers(self):
        st = self._flush(["Choose a path:\n1. Keep\n2. Stop\n\n[[OPTIONS]]"])
        st.proactive_push.assert_awaited_once()

    def test_quiet_options_marker_with_no_push_is_suppressed(self):
        st = self._flush(["Choose a path:\n1. Keep\n2. Stop\n\n[[OPTIONS]]"])
        st.proactive_texts = ["Choose a path:\n1. Keep\n2. Stop\n\n[[OPTIONS]]\nNO_PUSH"]
        st.injected_turn_mode = "quiet"
        asyncio.run(self.bridge._flush_proactive(1, st))
        st.proactive_push.assert_awaited_once()

    def test_quiet_options_delivery_keeps_ordinary_prose_suppressed(self):
        st = self._flush(["Choose a path:\n1. Keep\n2. Stop\n\n[[OPTIONS]]"])
        st.proactive_texts = ["ordinary quiet prose"]
        st.injected_turn_mode = "quiet"
        asyncio.run(self.bridge._flush_proactive(1, st))
        st.proactive_push.assert_awaited_once()

    def test_quiet_options_delivery_strips_trailing_push(self):
        st = self._flush(["Choose a path:\n1. Keep\n2. Stop\n\n[[OPTIONS]]"])
        st.proactive_texts = ["Choose a new path:\n1. Keep\n2. Stop\n\n[[OPTIONS]]\nPUSH"]
        st.injected_turn_mode = "quiet"
        asyncio.run(self.bridge._flush_proactive(1, st))
        self.assertEqual(st.proactive_push.await_count, 2)
        self.assertNotIn("PUSH", st.proactive_push.await_args_list[1].args[1])

    def test_quiet_options_marker_still_enables_buttons(self):
        st = self._flush(["Choose a path:\n1. Keep\n2. Stop\n\n[[OPTIONS]]"])
        st.proactive_push.assert_awaited_once()
        self.assertTrue(st.proactive_push.await_args.args[2])
        self.assertFalse(st.proactive_push.await_args.args[3])

    def test_flag_is_consumed_at_flush_never_leaks_to_next_turn(self):
        st = self._flush(["suppressed body"])
        self.assertIsNone(st.injected_turn_mode)
        # Next genuine proactive turn on the SAME state must deliver.
        st.proactive_texts = ["진짜 능동 알림입니다"]
        asyncio.run(self.bridge._flush_proactive(1, st))
        st.proactive_push.assert_awaited_once()

    def test_flag_is_consumed_even_when_turn_emitted_nothing(self):
        st = self._flush([])
        self.assertIsNone(st.injected_turn_mode)

    def test_non_quiet_turn_is_unaffected(self):
        st = self._flush(["평범한 능동 보고"], quiet=False)
        st.proactive_push.assert_awaited_once()

    def test_quiet_turn_error_notice_is_suppressed(self):
        st = _make_state()
        st.injected_turn_mode = "quiet"
        asyncio.run(self.bridge._flush_proactive_error(1, st))
        st.proactive_push.assert_not_awaited()
        self.assertIsNone(st.injected_turn_mode)


if __name__ == "__main__":
    unittest.main()
