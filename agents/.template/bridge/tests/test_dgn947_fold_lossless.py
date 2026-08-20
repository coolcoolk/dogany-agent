"""DGN-947 FOLD-1/2: fold budget-drop rescue + drop observability.

Fold mode suppresses interim narration from the live stream (capture-and-fold),
so when the finalize-time compose path discards a fold the narration vanishes
from EVERY surface. The single lossy cause is a budget-drop: the combined
fold+separator+final answer overruns one bubble, so compose_interim_fold
returns "" and the whole fold is dropped. FOLD-1 rescues that case by emitting
the surviving interim as its OWN bubble (grown-path final form). FOLD-2 makes
the remaining lossless drop (echo -- interim fully reproduced by the final)
observable and cause-confirmed.

Cases (spec dgn947-fold-loss-analysis.md sec 3 tests):
  1. 1 interim + a ~4000-char final -> compose over-budget -> own fold bubble
     sent, interim content preserved (FOLD-1, the lossy case rescued).
  2. full-echo turn (final reproduces the interim) -> no fold bubble, no
     rescue (spam guard -- lossless, nothing to preserve).
  3. no interim at all -> no fold, no rescue bubble, no drop log.
  4. rescue send itself fails (RetryAfter / network) -> fail-soft: the turn
     still completes, no crash, an ERROR is logged (residual-loss observable).

The harness reuses test_dgn877's compose-path driver (FOLD_CREATE_MIN_INTERIMS
pinned high to force the finalize-time compose branch).
"""

import logging

from claude_agent_sdk import TextBlock

import asyncio
from unittest.mock import MagicMock, patch

from bridge.sdk_bridge import SdkBridge, _UserStreamState
from bridge.tests.test_dgn699_growing_fold import (
    _make_assistant_msg,
    _make_pending_req,
    _make_result_msg,
)
from bridge.tests.test_dgn877_compose_footer_subtraction import _run_msgs


def _run_msgs_no_handler(messages_seq, fold_min_chars: int = 300):
    """Drive the fold-mode reader loop with NO streaming handler.

    Mirrors an injected / background turn (proactive path): interim narration
    is still captured into req.interim_texts, but there is no handler to open a
    live fold bubble or to carry the FOLD-1 rescue. Forces the finalize-time
    compose branch (FOLD_CREATE_MIN_INTERIMS high) so the handler-None budget-
    drop path is exercised.
    """

    async def _inner():
        bridge_obj = SdkBridge()
        req = _make_pending_req(streaming_handler=None)
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
        ), patch(
            "bridge.sdk_bridge.FOLD_CREATE_MIN_CHARS", fold_min_chars
        ), patch("bridge.sdk_bridge.FOLD_CREATE_MIN_INTERIMS", 10**6):
            await bridge_obj._reader_loop(1, state)
        return await asyncio.wait_for(req.future, timeout=1.0), req

    return asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Case 1: budget-drop -> rescued as its own bubble (FOLD-1, lossy case)
# ---------------------------------------------------------------------------

def test_budget_drop_emits_own_fold_bubble(caplog):
    # interim carries UNIQUE narration (not echoed by the final) but the final
    # answer is large enough that fold+final cannot share one bubble. Hangul
    # filler keeps the fixture inert under the DGN-686 register guard.
    interim = "unique progress detail " * 20  # ~460 chars, survives subtraction
    msgs = [
        _make_assistant_msg("tool_use", [TextBlock(text=interim)]),
        _make_assistant_msg("end_turn", [TextBlock(text="가" * 3998)]),
        _make_result_msg(result=""),
    ]
    with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        response, handler, req = _run_msgs(msgs, fold_min_chars=10**6)

    # fold NOT prepended to the answer (would overflow) ...
    assert ">!" not in response.content
    # ... but rescued as its own bubble (grown-path renderer -> send_message)
    assert handler.bot.send_message.await_count >= 1
    rescues = [
        r for r in caplog.records if "budget-drop rescued" in r.getMessage()
    ]
    assert len(rescues) == 1


# ---------------------------------------------------------------------------
# Case 2: full echo -> lossless drop, NO rescue bubble (spam guard)
# ---------------------------------------------------------------------------

def test_full_echo_drops_without_rescue(caplog):
    final = "the complete final answer paragraph"
    msgs = [
        _make_assistant_msg("tool_use", [TextBlock(text=final)]),
        _make_assistant_msg("end_turn", [TextBlock(text=final)]),
        _make_result_msg(result=""),
    ]
    with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        response, handler, req = _run_msgs(msgs)

    assert ">!" not in response.content
    # no rescue bubble for an echo turn (nothing unique to preserve)
    assert handler.bot.send_message.await_count == 0
    rescues = [
        r for r in caplog.records if "budget-drop rescued" in r.getMessage()
    ]
    assert rescues == []
    # lossless drop logged, cause-confirmed
    drops = [
        r for r in caplog.records
        if "fold dropped" in r.getMessage() and "lossless" in r.getMessage()
    ]
    assert len(drops) == 1


# ---------------------------------------------------------------------------
# Case 3: no interim at all -> nothing sent, no drop log
# ---------------------------------------------------------------------------

def test_no_interim_no_bubble_no_log(caplog):
    msgs = [
        _make_assistant_msg("end_turn", [TextBlock(text="direct answer")]),
        _make_result_msg(result=""),
    ]
    with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        response, handler, req = _run_msgs(msgs)

    assert handler.bot.send_message.await_count == 0
    rescues = [
        r for r in caplog.records if "budget-drop rescued" in r.getMessage()
    ]
    assert rescues == []
    drops = [r for r in caplog.records if "fold dropped" in r.getMessage()]
    assert drops == []


# ---------------------------------------------------------------------------
# Case 4: rescue send failure -> fail-soft (no crash, ERROR logged)
# ---------------------------------------------------------------------------

def test_rescue_send_failure_is_fail_soft(caplog):
    interim = "unique progress detail " * 20
    msgs = [
        _make_assistant_msg("tool_use", [TextBlock(text=interim)]),
        _make_assistant_msg("end_turn", [TextBlock(text="가" * 3998)]),
        _make_result_msg(result=""),
    ]

    # REAL failure shape: send_fold_html NEVER raises -- on RetryAfter /
    # BadRequest / network it swallows the error and returns None (see
    # streaming.send_fold_html). The rescue must read that None as a loss and
    # log "rescue send failed", not silently claim success. Stub it to return
    # None (the real failure signal), not to raise (which never happens).
    import bridge.streaming as streaming_mod
    orig = streaming_mod.send_fold_html

    async def returns_none(*a, **k):
        return None

    with caplog.at_level(logging.ERROR, logger="bridge.sdk_bridge"):
        streaming_mod.send_fold_html = returns_none
        try:
            response, handler, req = _run_msgs(msgs, fold_min_chars=10**6)
        finally:
            streaming_mod.send_fold_html = orig

    assert response.success is True  # turn completes despite the send failure
    errs = [
        r for r in caplog.records if "fold rescue send failed" in r.getMessage()
    ]
    assert len(errs) == 1
    # a None return must NOT be logged as a rescue success
    rescues = [
        r for r in caplog.records if "budget-drop rescued" in r.getMessage()
    ]
    assert rescues == []


# ---------------------------------------------------------------------------
# Case 5: background budget-drop (no handler) -> lossy log, NOT "lossless"
# ---------------------------------------------------------------------------

def test_background_budget_drop_logs_lossy_not_lossless(caplog):
    # An injected / background turn has no streaming handler, so the FOLD-1
    # rescue path is unavailable. A budget-drop (unique interim survives, but
    # fold+final over budget) must be logged as the LOSSY cause -- it must
    # NEVER fall through to the echo branch and be mislabeled "lossless".
    interim = "unique progress detail " * 20
    msgs = [
        _make_assistant_msg("tool_use", [TextBlock(text=interim)]),
        _make_assistant_msg("end_turn", [TextBlock(text="가" * 3998)]),
        _make_result_msg(result=""),
    ]
    with caplog.at_level(logging.INFO, logger="bridge.sdk_bridge"):
        response, req = _run_msgs_no_handler(msgs, fold_min_chars=10**6)

    assert response.success is True
    # the budget-drop is logged as the lossy background cause ...
    lossy = [
        r for r in caplog.records
        if "budget-drop for user" in r.getMessage()
        and "no streaming handler" in r.getMessage()
    ]
    assert len(lossy) == 1
    # ... and NEVER mislabeled lossless (blocker 2)
    mislabeled = [
        r for r in caplog.records
        if "fold dropped" in r.getMessage() and "lossless" in r.getMessage()
    ]
    assert mislabeled == []
