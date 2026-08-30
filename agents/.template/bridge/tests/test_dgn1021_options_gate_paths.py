"""DGN-1021: the [[OPTIONS]] GATES must route through the canonical recognizer.

DGN-992 introduced the labeled marker "[[OPTIONS: a | b]]" and taught the
parser (extract_marker_labels / build_option_keyboard) about it, but the
concept "is a marker present?" had FOUR implementations: the canonical
line-based recognizer (formatting.is_options_marker_line, wrapped by
options.has_options_marker) plus three hand-rolled `OPTIONS_MARKER in
content` substring checks in sdk_bridge. Two of those substring checks
(:1451 proactive flush, :2074 model-turn finalize) never learned the labeled
form, so has_options=False -> force_options=False -> the whole button block
in bot._send_content_artifacts was skipped: ZERO buttons, ZERO warnings
(the DGN-992 fail-loud lives INSIDE the skipped block).

The existing DGN-992 suite tested the parser only (always passing
force_options=True by hand), which is exactly why this defect survived.
This suite tests the GATES, per delivery PATH (not per recognizer):

  1. model-turn finalize  (_finalize_result -> ChatResponse.has_options)
  2. proactive push       (_flush_proactive -> proactive_push(has_options))
  3. classifier           (_maybe_mark_options marker_present suppression)

Each labeled-marker case runs end-to-end into the real render seat
(bot._send_smart -> _send_content_artifacts) and asserts an actual keyboard.
No-regression fixtures ride along for the four legacy shapes: bare marker,
bare marker + numbered run, numbered run only (source 3), no marker at all.
"""

import asyncio
import importlib.util
import logging
import sys
from unittest.mock import AsyncMock, MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState  # noqa: E402
from claude_agent_sdk import ResultMessage  # noqa: E402


LABELED_BODY = (
    "방향을 골라주세요.\n"
    "\n"
    "· 흡수 -- 기존 티켓에 합친다\n"
    "· 분리 -- 새 티켓으로 뗀다\n"
    "[[OPTIONS: 흡수 | 분리]]"
)

BARE_TRAILING_BODY = (
    "...본문...\n"
    "\n"
    "[[OPTIONS]]\n"
    "흡수\n"
    "분리"
)

BARE_NUMBERED_BODY = "Pick:\n1. proceed\n2. hold\n[[OPTIONS]]"

NUMBERED_ONLY_BODY = "다음 중 골라주세요.\n1. proceed\n2. hold"

PLAIN_BODY = "마커도 목록도 없는 평문 보고입니다."


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


def _make_chat_bot(bot_mod, sent):
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()

    async def _send_message(chat_id, text, parse_mode=None, reply_markup=None,
                            link_preview_options=None, disable_notification=None):
        sent.append({"text": text, "reply_markup": reply_markup})

    bot.application.bot = MagicMock()
    bot.application.bot.send_message = AsyncMock(side_effect=_send_message)
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = {}
    return bot


def _run(coro):
    return asyncio.run(coro)


def _make_state() -> _UserStreamState:
    client = MagicMock()
    client.query = AsyncMock()
    st = _UserStreamState(client=client, model=None)
    st.last_chat_id = 42
    st.proactive_push = AsyncMock()
    st.last_session_id = "sess-dgn1021"
    return st


def _make_req(synthetic=None) -> _PendingRequest:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    loop.close()
    req = _PendingRequest(
        user_id=42,
        chat_id=42,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=fut,
        user_message="결정해주세요",
    )
    if synthetic is not None:
        req.synthetic_response = synthetic
    return req


def _make_result_msg() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=50,
        duration_api_ms=40,
        is_error=False,
        num_turns=1,
        session_id="sess-dgn1021",
        result=None,
    )


def _finalize(content: str, monkeypatch, synthetic=None, classifier=False):
    """Run the REAL model-turn finalize path and return its ChatResponse.

    classify_is_choice is patched (no Haiku CLI in tests); its return value
    drives the classifier-injection axis for numbered-only content.
    """
    import bridge.sdk_bridge as sdk_mod

    monkeypatch.setattr(
        sdk_mod, "classify_is_choice", lambda *a, **k: classifier
    )
    bridge = SdkBridge()
    state = _make_state()
    req = _make_req(synthetic=synthetic)
    req.last_assistant_texts = [content] if content is not None else []
    _run(bridge._finalize_result(req.user_id, state, req, _make_result_msg()))
    assert req.future.done(), "finalize must resolve the future"
    return req.future.result()


def _keyboard_sends(sent):
    return [e for e in sent if e["reply_markup"] is not None]


# ---------------------------------------------------------------------------
# Path 1: model-turn finalize (sdk_bridge._finalize_result has_options gate)
# ---------------------------------------------------------------------------


class TestFinalizePathGate:
    def test_labeled_marker_gate_true_and_keyboard_attached(self, monkeypatch):
        """THE DGN-1021 incident: a labeled-marker turn must open the gate
        AND, fed into the real render seat exactly as bot.py does
        (force_options=response.has_options), attach an actual keyboard."""
        resp = _finalize(LABELED_BODY, monkeypatch)
        assert resp.has_options is True, (
            "finalize gate missed the labeled marker (DGN-1021 silent death)"
        )

        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        _run(bot._send_smart(42, resp.content, force_options=resp.has_options))
        kb = _keyboard_sends(sent)
        assert len(kb) == 1, f"no keyboard attached: {sent}"
        rows = kb[0]["reply_markup"].inline_keyboard
        assert [r[0].text for r in rows] == ["1. 흡수", "2. 분리"]

    def test_bare_marker_trailing_labels_gate_true_keyboard(self, monkeypatch):
        """No-regression: bare marker + trailing label lines (DGN-992 rev2)."""
        resp = _finalize(BARE_TRAILING_BODY, monkeypatch)
        assert resp.has_options is True

        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        _run(bot._send_smart(42, resp.content, force_options=resp.has_options))
        assert len(_keyboard_sends(sent)) == 1

    def test_bare_marker_numbered_run_gate_true_keyboard(self, monkeypatch):
        """No-regression: bare marker + numbered run (DGN-665 path)."""
        resp = _finalize(BARE_NUMBERED_BODY, monkeypatch)
        assert resp.has_options is True

        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        _run(bot._send_smart(42, resp.content, force_options=resp.has_options))
        kb = _keyboard_sends(sent)
        assert len(kb) == 1
        rows = kb[0]["reply_markup"].inline_keyboard
        assert [r[0].text for r in rows] == ["1. proceed", "2. hold"]

    def test_numbered_only_gate_true_source3_preserved(self, monkeypatch):
        """No-regression (source 3): a numbered run WITHOUT any marker must
        still open the gate -- the `or has_numbered_list` arm carries the
        classifier-injection path and must survive the recognizer swap."""
        resp = _finalize(NUMBERED_ONLY_BODY, monkeypatch, classifier=False)
        assert resp.has_options is True

    def test_numbered_only_classifier_injects_keyboard(self, monkeypatch):
        """Source 3 end-to-end: classifier says pick-one -> marker injected ->
        keyboard renders AND the body list is kept (DGN-665 provenance)."""
        resp = _finalize(NUMBERED_ONLY_BODY, monkeypatch, classifier=True)
        assert resp.has_options is True
        assert resp.options_classifier_injected is True

        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        _run(bot._send_smart(
            42, resp.content,
            force_options=resp.has_options,
            classifier_injected=resp.options_classifier_injected,
        ))
        kb = _keyboard_sends(sent)
        assert len(kb) == 1
        bodies = [e for e in sent if e["reply_markup"] is None]
        assert any("1. proceed" in b["text"] for b in bodies)

    def test_plain_body_gate_false(self, monkeypatch):
        """No-regression: marker-less, list-less turns keep the gate closed."""
        resp = _finalize(PLAIN_BODY, monkeypatch)
        assert resp.has_options is False

    def test_synthetic_response_gate_true(self, monkeypatch):
        """No-regression: synthetic responses always force options."""
        resp = _finalize(None, monkeypatch, synthetic="Pick:\n1. a\n2. b\n[[OPTIONS]]")
        assert resp.has_options is True


# ---------------------------------------------------------------------------
# Path 2: proactive push (sdk_bridge._flush_proactive has_options gate)
# ---------------------------------------------------------------------------


def _flush_proactive(content: str, monkeypatch, classifier=False):
    """Run the REAL proactive flush wired to the REAL render seat, mirroring
    bot._proactive_push (which forwards has_options as force_options into
    _send_smart). Returns (sent, push_calls)."""
    import bridge.sdk_bridge as sdk_mod

    monkeypatch.setattr(
        sdk_mod, "classify_is_choice", lambda *a, **k: classifier
    )
    bot_mod = _load_bot()
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    push_calls = []

    async def _push(chat_id, text, has_options, classifier_injected):
        push_calls.append({"has_options": has_options,
                           "classifier_injected": classifier_injected})
        await bot._send_smart(
            chat_id, text,
            force_options=has_options,
            classifier_injected=classifier_injected,
        )

    bridge = SdkBridge()
    state = _make_state()
    state.proactive_push = _push
    state.proactive_texts = [content]
    _run(bridge._flush_proactive(42, state))
    return sent, push_calls


class TestProactivePathGate:
    def test_labeled_marker_proactive_keyboard_attached(self, monkeypatch):
        """DGN-1021 second broken seat (:1451): a proactive push carrying a
        labeled marker must arrive with buttons."""
        sent, calls = _flush_proactive(LABELED_BODY, monkeypatch)
        assert calls and calls[0]["has_options"] is True, (
            "proactive gate missed the labeled marker (DGN-1021)"
        )
        kb = _keyboard_sends(sent)
        assert len(kb) == 1, f"no keyboard attached: {sent}"
        rows = kb[0]["reply_markup"].inline_keyboard
        assert [r[0].text for r in rows] == ["1. 흡수", "2. 분리"]

    def test_bare_marker_numbered_proactive_keyboard(self, monkeypatch):
        """No-regression: bare marker + numbered run on the proactive path."""
        sent, calls = _flush_proactive(BARE_NUMBERED_BODY, monkeypatch)
        assert calls and calls[0]["has_options"] is True
        assert len(_keyboard_sends(sent)) == 1

    def test_bare_marker_trailing_labels_proactive_keyboard(self, monkeypatch):
        sent, calls = _flush_proactive(BARE_TRAILING_BODY, monkeypatch)
        assert calls and calls[0]["has_options"] is True
        assert len(_keyboard_sends(sent)) == 1

    def test_numbered_only_proactive_gate_true_source3(self, monkeypatch):
        """No-regression (source 3): numbered run without a marker must keep
        the proactive gate open (classifier may still decline injection)."""
        sent, calls = _flush_proactive(NUMBERED_ONLY_BODY, monkeypatch,
                                       classifier=False)
        assert calls and calls[0]["has_options"] is True

    def test_plain_proactive_gate_false_no_keyboard(self, monkeypatch):
        sent, calls = _flush_proactive(PLAIN_BODY, monkeypatch)
        assert calls and calls[0]["has_options"] is False
        assert not _keyboard_sends(sent)


# ---------------------------------------------------------------------------
# Path 3: classifier suppression (sdk_bridge._maybe_mark_options)
# ---------------------------------------------------------------------------


class TestClassifierPathGate:
    def _mark(self, content, monkeypatch, classifier=True):
        import bridge.sdk_bridge as sdk_mod

        monkeypatch.setattr(
            sdk_mod, "classify_is_choice", lambda *a, **k: classifier
        )
        return _run(SdkBridge._maybe_mark_options("prev", content))

    def test_labeled_marker_suppresses_injection(self, monkeypatch):
        content = "1. one\n2. two\n[[OPTIONS: one | two]]"
        out, injected = self._mark(content, monkeypatch)
        assert out == content and injected is False

    def test_bare_marker_suppresses_injection(self, monkeypatch):
        content = "1. one\n2. two\n[[OPTIONS]]"
        out, injected = self._mark(content, monkeypatch)
        assert out == content and injected is False

    def test_numbered_only_injects_when_choice(self, monkeypatch):
        out, injected = self._mark(NUMBERED_ONLY_BODY, monkeypatch)
        assert injected is True
        assert out.endswith("[[OPTIONS]]")

    def test_fenced_marker_example_still_suppresses(self, monkeypatch):
        """A standalone marker line inside a code fence still counts as
        marker-present for the classifier (the canonical recognizer is
        line-based, fence-unaware) -- unchanged from the substring era for
        the bare form; now ALSO true for a fenced labeled example."""
        content = "예시:\n```\n[[OPTIONS]]\n```\n1. one\n2. two"
        out, injected = self._mark(content, monkeypatch)
        assert injected is False and out == content

    def test_inline_mention_no_longer_suppresses(self, monkeypatch):
        """DGN-1021 intent lock: a MID-LINE prose mention of "[[OPTIONS]]"
        is not an armable marker (it never builds buttons, never strips) --
        it must not suppress classifier injection over a genuine pick-one
        run. Under the old substring check it incidentally did."""
        content = "마커 문법 [[OPTIONS]] 이야기입니다.\n1. one\n2. two"
        out, injected = self._mark(content, monkeypatch)
        assert injected is True
        assert out.endswith("[[OPTIONS]]")


# ---------------------------------------------------------------------------
# Recognizer unification: no hand-rolled substring gate may return
# ---------------------------------------------------------------------------


class TestRecognizerUnification:
    def test_no_substring_marker_recognizer_in_sdk_bridge(self):
        """The concept "is a marker present?" has ONE implementation
        (formatting.is_options_marker_line). `OPTIONS_MARKER in content`
        substring recognizers are what let DGN-992's new form silently
        miss two gates -- they must never come back."""
        import inspect
        import re
        import bridge.sdk_bridge as sdk_mod

        src = inspect.getsource(sdk_mod)
        offenders = [
            ln.strip() for ln in src.splitlines()
            if re.search(r"OPTIONS_MARKER\s+in\s", ln)
            and not ln.lstrip().startswith("#")
        ]
        assert offenders == [], (
            f"hand-rolled substring marker recognizers found: {offenders}"
        )


# ---------------------------------------------------------------------------
# Fail-loud hoist: gate-off + marker present must WARN (DGN-1021)
# ---------------------------------------------------------------------------


class TestGateMismatchFailLoud:
    def test_marker_present_gate_off_warns(self, caplog):
        """The DGN-992 fail-loud sat INSIDE `if force_options:` -- a gate miss
        skipped the defense along with the buttons. The hoisted branch must
        WARN whenever an armable marker line reaches the render seat with the
        gate off (recognizer drift telemetry), body delivered intact."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        with caplog.at_level(logging.WARNING, logger="bridge.bot"):
            _run(bot._send_smart(42, LABELED_BODY, force_options=False))

        assert any(
            "options gate" in rec.message for rec in caplog.records
        ), f"no gate-mismatch warning: {[r.message for r in caplog.records]}"
        assert not _keyboard_sends(sent)

    def test_plain_body_gate_off_no_warning(self, caplog):
        """Normal marker-less messages must stay silent -- the hoisted branch
        may not spam every ordinary send."""
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)

        with caplog.at_level(logging.WARNING, logger="bridge.bot"):
            _run(bot._send_smart(42, PLAIN_BODY, force_options=False))

        assert not any(
            "options gate" in rec.message for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Path 4: fast-path push (bot._fastpath_push_guaranteed)
#
# The real incident (2026-08-23): a fast-path body (a domain handler's
# exit-0 commit witness -- e.g. a rendered usage-meter table) that ALSO
# carries an [[OPTIONS]] marker. _fastpath_push_guaranteed called
# `self._send_smart(chat_id, content)` with no force_options argument at
# all (silently defaulting False), the one delivery path DGN-1021's own
# audit never enumerated (it only covered model-turn finalize, proactive
# push, and the classifier). Result: has_code triggers the DGN-085 body
# split fine, but force_options stays False, so strip_consumed_options and
# build_option_keyboard never run -- the marker is stripped, the DGN-1021
# gate-mismatch WARNING fires ("upstream recognizer drift?"), and ZERO
# buttons reach the owner. The decision never renders. Fix: compute
# has_options_marker(content) here too, same canonical recognizer the
# other three paths already use, and forward it as force_options.
# ---------------------------------------------------------------------------


CODE_PLUS_LABELED_BODY = (
    "사용량:\n\n"
    "```\n"
    "| 항목 | 값 |\n"
    "| 5h   | 80% |\n"
    "```\n\n"
    "계속 진행할까요?\n"
    "[[OPTIONS: proceed | hold]]"
)

FENCED_MARKER_ONLY_BODY = (
    "예시입니다:\n\n"
    "```\n"
    "Proceed?\n"
    "[[OPTIONS: proceed | hold]]\n"
    "```\n\n"
    "이건 설명일 뿐입니다.\n"
)

CODE_ONLY_BODY = "```\n| 항목 | 값 |\n| 5h | 80% |\n```\n"


def _run_fastpath(content: str):
    bot_mod = _load_bot()
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    _run(bot._fastpath_push_guaranteed(42, content))
    return sent


class TestFastpathPathGate:
    def test_incident_code_plus_labeled_marker_gets_buttons(self):
        """THE tonight incident, reproduced against the real render seat:
        a fast-path body with a fenced table AND a labeled [[OPTIONS]]
        marker must arrive with an actual keyboard, table delivered first."""
        sent = _run_fastpath(CODE_PLUS_LABELED_BODY)
        kb = _keyboard_sends(sent)
        assert len(kb) == 1, f"no keyboard attached: {sent}"
        rows = kb[0]["reply_markup"].inline_keyboard
        assert [r[0].text for r in rows] == ["1. proceed", "2. hold"]
        bodies = [e for e in sent if e["reply_markup"] is None]
        assert any("항목" in b["text"] for b in bodies), (
            "table body must still be delivered, not swallowed by the split"
        )
        # Ordering: every body message precedes the keyboard message.
        kb_idx = max(i for i, e in enumerate(sent) if e["reply_markup"] is not None)
        assert all(
            i < kb_idx for i, e in enumerate(sent) if e["reply_markup"] is None
        )

    def test_code_only_no_keyboard_unchanged(self):
        """No-regression (c): a fast-path body with a code block and NO
        marker must not grow a keyboard."""
        sent = _run_fastpath(CODE_ONLY_BODY)
        assert not _keyboard_sends(sent)

    def test_marker_only_no_code_unchanged(self):
        """No-regression (b): a fast-path body with just a marker (no code)
        still gets buttons -- unaffected by the fix."""
        sent = _run_fastpath(BARE_NUMBERED_BODY)
        kb = _keyboard_sends(sent)
        assert len(kb) == 1

    def test_fenced_marker_example_no_buttons_no_split(self):
        """(d) bridge.md: a marker line INSIDE a fenced code block is a doc
        example -- never arms buttons, never triggers a split. The safety
        property under test is "never arms" (strip_options_marker already
        removes ANY standalone marker line "regardless of position" per its
        own docstring, fence-unaware, for all 4 delivery paths alike -- that
        cosmetic scrub is pre-existing and out of this fix's scope). The
        surrounding fence content (the non-marker line inside it) and the
        trailing prose must still render intact -- no split, no keyboard."""
        sent = _run_fastpath(FENCED_MARKER_ONLY_BODY)
        assert not _keyboard_sends(sent)
        texts = [e["text"] for e in sent]
        assert any("Proceed?" in t for t in texts), (
            f"fenced code content should still render: {texts}"
        )
        assert any("설명일 뿐입니다" in t for t in texts), (
            f"trailing prose should still render: {texts}"
        )

