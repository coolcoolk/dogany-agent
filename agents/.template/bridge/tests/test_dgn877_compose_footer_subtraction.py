"""DGN-877 / DGN-878: compose-fallback fold vs footer sidecar.

DGN-877: _consume_footer_sidecar joins the canonical footer with a single
"\\n" (``stripped + "\\n" + footer``), merging it into the final answer's
LAST paragraph. The compose-path fold subtraction (_subtract_paras) matches
normalized FULL paragraphs only, so running it against the POST-footer
content loses the last paragraph's boundary: an interim-narrated final
paragraph misses the match and leaks into the fold once (visible duplicate).
Fix: the subtraction runs against a PRE-footer snapshot of the body.
The grown-bubble path is unaffected (its subtraction already runs before the
footer consumption) -- covered by test_dgn699_growing_fold.py.

DGN-878: the compose-empty info log must enumerate all three possible causes
(whitespace-only capture / fold budget exhausted / full final-overlap
subtraction) instead of singling out subtraction -- the branch cannot
distinguish them.
"""

import asyncio
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from claude_agent_sdk import TextBlock

from bridge import sdk_bridge
from bridge.sdk_bridge import SdkBridge, _UserStreamState
from bridge.tests.test_dgn699_growing_fold import (
    _make_assistant_msg,
    _make_pending_req,
    _make_result_msg,
    _mock_handler,
)


FOOTER = "[라이브]\n- 배포 동생"


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    """Point the module at a per-test sidecar file; return a writer helper."""
    path = tmp_path / "footer-sidecar.json"
    monkeypatch.setattr(sdk_bridge, "_FOOTER_SIDECAR", path)

    def write(footer):
        path.write_text(
            json.dumps({"footer": footer, "ts": 1}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    return write


def _run_msgs(messages_seq, fold_min_chars: int = 300):
    """Drive _reader_loop in fold mode (same harness as test_dgn699)."""

    async def _inner():
        handler = _mock_handler()
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
            "bridge.sdk_bridge.FOLD_UPDATE_INTERVAL", 0.0
        ), patch("bridge.sdk_bridge.FOLD_CREATE_MIN_CHARS", fold_min_chars):
            await bridge_obj._reader_loop(1, state)

        result = await asyncio.wait_for(req.future, timeout=1.0)
        return result, handler, req

    return asyncio.run(_inner())


# ---------------------------------------------------------------------------
# DGN-877: pre-footer subtraction basis
# ---------------------------------------------------------------------------


class TestDgn877PreFooterSubtraction:
    def test_footer_does_not_defeat_last_para_subtraction(self, sidecar):
        # Interim narrated an extra step AND the (single-paragraph) final
        # answer. With the footer merged into that paragraph the old code
        # missed the match and the fold echoed the answer once.
        sidecar(FOOTER)
        final = "final para done"
        msgs = [
            _make_assistant_msg(
                "tool_use", [TextBlock(text="working on it\n\n" + final)]
            ),
            _make_assistant_msg("end_turn", [TextBlock(text=final)]),
            _make_result_msg(result=""),
        ]
        response, handler, req = _run_msgs(msgs)
        assert req.fold_msg_id is None  # compose path, no bubble
        # Extra progress survives as the fold; the answer paragraph does not.
        assert response.content.startswith(">! working on it")
        assert response.content.count(final) == 1
        # Footer still appended at the tail.
        assert response.content.rstrip().endswith(FOOTER)

    def test_footer_full_dup_attaches_no_fold(self, sidecar):
        # interim == final: nothing survives subtraction even with a footer
        # joined -- no fold prepended, clean final + footer only.
        sidecar(FOOTER)
        final = "final para done"
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text=final)]),
            _make_assistant_msg("end_turn", [TextBlock(text=final)]),
            _make_result_msg(result=""),
        ]
        response, handler, req = _run_msgs(msgs)
        assert req.fold_msg_id is None
        assert ">!" not in response.content
        assert response.content.count(final) == 1
        assert response.content.rstrip().endswith(FOOTER)

    def test_empty_sidecar_superset_still_trims(self, sidecar):
        # Noise-suppression turn (empty footer): pre-footer snapshot equals
        # the stripped body -- behavior identical to the no-footer case.
        sidecar("")
        msgs = [
            _make_assistant_msg(
                "tool_use", [TextBlock(text="extra step\n\nfinal answer")]
            ),
            _make_assistant_msg("end_turn", [TextBlock(text="final answer")]),
            _make_result_msg(result=""),
        ]
        response, handler, req = _run_msgs(msgs)
        assert response.content.startswith(">! extra step")
        assert response.content.count("final answer") == 1


# ---------------------------------------------------------------------------
# DGN-878: compose-empty log enumerates all three causes
# ---------------------------------------------------------------------------


class TestDgn878ComposeEmptyLog:
    def test_budget_drop_log_enumerates_causes(self, caplog):
        # No overlap at all: the fold empties purely on budget (final leaves
        # no room, DGN-682 D7). The log must not blame subtraction alone.
        # Hangul filler keeps the fixture inert under the DGN-686 register
        # guard even when a live ko shell leaks LOCALE=ko into the run.
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text="n" * 1000)]),
            _make_assistant_msg("end_turn", [TextBlock(text="가" * 3998)]),
            _make_result_msg(result=""),
        ]
        with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
            # Keep the compose path: raise the bubble char floor so the
            # 1000-char interim never opens a grown bubble.
            response, handler, req = _run_msgs(msgs, fold_min_chars=10**6)
        assert req.fold_msg_id is None
        assert ">!" not in response.content  # fold dropped on budget
        drops = [r for r in caplog.records if "composed empty" in r.getMessage()]
        assert len(drops) == 1
        message = drops[0].getMessage()
        for cause in ("whitespace-only", "over budget", "fully subtracted"):
            assert cause in message

    def test_full_subtraction_uses_same_enumerating_log(self, caplog):
        # Full final-overlap subtraction lands in the SAME branch and the
        # same enumerating message (no single-cause claim).
        final = "final answer"
        msgs = [
            _make_assistant_msg("tool_use", [TextBlock(text=final)]),
            _make_assistant_msg("end_turn", [TextBlock(text=final)]),
            _make_result_msg(result=""),
        ]
        with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
            response, handler, req = _run_msgs(msgs)
        assert ">!" not in response.content
        drops = [r for r in caplog.records if "composed empty" in r.getMessage()]
        assert len(drops) == 1
        assert "over budget" in drops[0].getMessage()
