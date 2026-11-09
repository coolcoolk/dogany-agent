"""DGN-1606: every machine-injected turn opens with the harness mark.

Measured incident (2026-09-20, dogany-health): a session-inbox injection
reached the SDK through the same client.query() as a real owner message and
was indistinguishable in the transcript (plain user entry, isMeta unset).
The onboarding machinery then ran to completion in that ownerless turn and
the agent named itself.  The mark is the machine signal downstream turn
classifiers (the host's onboarding hooks) key on.

Locks:
  * inject_background_turn prepends INJECTED_TURN_MARK as its own first
    line, preserving the spool text verbatim after it.
  * quiet flag behavior is untouched by the mark.
  * the literal is in LOCKSTEP with the host's onboarding Stop gate reader
    (its INJECTED_TURN_MARK) -- the reader and the writer must agree.
"""

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bridge.sdk_bridge import INJECTED_TURN_MARK, SdkBridge, _UserStreamState

# ESTATE:BEGIN the Stop-gate reader is an estate onboarding hook (routines/)
_REPO_TEMPLATE_ROOT = Path(__file__).resolve().parents[2]
_STOP_GATE = _REPO_TEMPLATE_ROOT / "routines" / "onboarding-gate-stop.py"
# ESTATE:END


def _make_state(**kw):
    client = MagicMock()
    client.query = AsyncMock()
    st = _UserStreamState(client=client, model=None)
    st.last_chat_id = kw.get("chat_id", 111)
    st.proactive_push = kw.get("push", AsyncMock())
    st.last_session_id = kw.get("session_id", "sess-1")
    return st


class TestInjectedTurnMark(unittest.TestCase):
    def setUp(self):
        self.bridge = SdkBridge()

    def test_injected_turn_opens_with_the_mark(self):
        st = _make_state()
        self.bridge._streams[1] = st
        ok = asyncio.run(
            self.bridge.inject_background_turn(1, "[outbound-record] note")
        )
        self.assertTrue(ok)
        st.client.query.assert_awaited_once()
        sent = st.client.query.call_args[0][0]
        self.assertEqual(
            sent, INJECTED_TURN_MARK + "\n[outbound-record] note"
        )

    def test_quiet_flag_untouched_by_the_mark(self):
        st = _make_state()
        self.bridge._streams[1] = st
        ok = asyncio.run(
            self.bridge.inject_background_turn(1, "note", quiet=True)
        )
        self.assertTrue(ok)
        self.assertEqual(st.injected_turn_mode, "quiet")
        sent = st.client.query.call_args[0][0]
        self.assertTrue(sent.startswith(INJECTED_TURN_MARK + "\n"))

    # ESTATE:BEGIN the stop-gate reader is estate routines, not bridge: absent from the OSS artifact and the extract gate-3 harness (DGN-1779)
    @unittest.skipUnless(_STOP_GATE.exists(), "onboarding-gate-stop.py not shipped here")
    def test_mark_is_lockstep_with_the_stop_gate_reader(self):
        gate = _STOP_GATE
        text = gate.read_text(encoding="utf-8")
        self.assertIn(
            'INJECTED_TURN_MARK = "%s"' % INJECTED_TURN_MARK, text,
            "bridge mark and routines/onboarding-gate-stop.py reader "
            "literal diverged -- change both together (DGN-1606)",
        )
    # ESTATE:END


if __name__ == "__main__":
    unittest.main()
