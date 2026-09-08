"""DGN-1128: a SINGLE-option reply must reach the classifier (and get a button).

Incident (2026-08-27 morning): a prep step offered exactly one action
("1. 완료") with no hand-authored [[OPTIONS]] marker. has_numbered_list's
>=2 gate (conservative since DGN-325 "to avoid false positives on incidental
single-item lists") kept the classifier out, so ZERO buttons rendered and the
owner had to type the answer mid-workout.

Fix under test: has_single_trailing_option -- a structural pre-filter that
admits ONLY the one-item decision-menu shape (outside code fences: exactly one
numbered line, it is a "1." item, and it is the LAST non-blank prose line)
into the classifier gate (sdk_bridge._maybe_mark_options). Scope guard: the
has_options OR-arms (proactive flush / model-turn finalize) still use the
>=2 has_numbered_list, so marker-less non-injected content keeps its exact
pre-fix force_options behavior.

H17 boundary (must NOT be collapsed): an engine AUTO_ADVANCE seat never
emits a numbered line at all (the skill consumes the token and executes
immediately), so the bridge only ever classifies seats WITHOUT an engine
signal -- exactly the seats that need buttons. Nothing here renders a menu
for AUTO_ADVANCE seats; the distinction lives upstream by construction.

Positive-verification lineage: the broken state WAS detectable first -- with
the classifier mocked to always-yes, pre-fix code produced classifier_calls=0
and zero keyboards on SINGLE_OPTION_BODY (repro run 2026-08-27, this
worktree). The end-to-end tests below are the "after" half of that pair and
double as revert canaries.
"""

import asyncio
import importlib.util
import sys
from unittest.mock import AsyncMock, MagicMock

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

from bridge.options import has_single_trailing_option  # noqa: E402
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState  # noqa: E402
from claude_agent_sdk import ResultMessage  # noqa: E402


# The incident shape: one prep action, no marker, list is the trailing line.
SINGLE_OPTION_BODY = (
    "프렙 1번입니다. 폼롤러 흉추 릴리즈 30초 진행해주세요.\n"
    "\n"
    "1. 완료"
)

# Authored-marker single option (DGN-325 path) -- must stay classifier-free.
SINGLE_OPTION_AUTHORED = "프렙 확인해주세요.\n\n1. 완료\n[[OPTIONS]]"

# Synthetic prose samples: each contains exactly ONE numbered line that is
# NOT a trailing one-item menu. The deterministic gate must reject ALL of
# them WITHOUT ever spending a classifier (Haiku) call.
PROSE_SINGLE_NUMBERED_SAMPLES = [
    # (1) numbered line mid-message, prose continues after it
    "오늘 한 일 요약입니다.\n1. 로그 정리를 마쳤습니다\n내일은 백업을 돌립니다.",
    # (2) trailing numbered line that does not start at 1
    "참고할 항목이 하나 남았습니다.\n\n3. 부록의 표를 확인하세요",
    # (3) numbered line only inside a code fence, prose tail after it
    "실행 예시:\n```\n1. step one\n```\n위 절차는 예시일 뿐입니다.",
    # (4) trailing "1." but a second numbered line exists earlier -> the
    #     count!=1 arm rejects; the >=2 arm owns this shape (classifier DOES
    #     run there -- asserted separately below, not in this loop)
]

# Trailing single "1." line that IS shape-eligible but is not a menu -- the
# classifier (mocked "no") is the judge; no marker, no buttons.
PROSE_TRAILING_ELIGIBLE = "백업이 끝났습니다.\n\n1. 다음 백업은 내일 새벽에 돕니다"


# ---------------------------------------------------------------------------
# Harness (same shape as test_dgn1021_options_gate_paths)
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
    st.last_session_id = "sess-dgn1128"
    return st


def _make_req() -> _PendingRequest:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    loop.close()
    return _PendingRequest(
        user_id=42,
        chat_id=42,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=fut,
        user_message="다음",
    )


def _make_result_msg() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=50,
        duration_api_ms=40,
        is_error=False,
        num_turns=1,
        session_id="sess-dgn1128",
        result=None,
    )


def _patch_classifier(monkeypatch, answer):
    """Patch classify_is_choice; returns the call-recorder list."""
    import bridge.sdk_bridge as sdk_mod

    calls = []

    def _fake(prev, asst, cli_path=None):
        calls.append(asst)
        return answer

    monkeypatch.setattr(sdk_mod, "classify_is_choice", _fake)
    return calls


def _finalize(content, monkeypatch, classifier):
    calls = _patch_classifier(monkeypatch, classifier)
    bridge = SdkBridge()
    state = _make_state()
    req = _make_req()
    req.last_assistant_texts = [content]
    _run(bridge._finalize_result(req.user_id, state, req, _make_result_msg()))
    assert req.future.done(), "finalize must resolve the future"
    return req.future.result(), calls


def _keyboard_sends(sent):
    return [e for e in sent if e["reply_markup"] is not None]


# ---------------------------------------------------------------------------
# Unit: the structural pre-filter
# ---------------------------------------------------------------------------


class TestHasSingleTrailingOption:
    def test_incident_shape_true(self):
        assert has_single_trailing_option(SINGLE_OPTION_BODY) is True

    def test_trailing_whitespace_lines_still_true(self):
        assert has_single_trailing_option("골라주세요.\n\n1. 진행\n\n  \n") is True

    def test_two_numbered_lines_false(self):
        """>=2 stays has_numbered_list's territory -- exactly-one only."""
        assert has_single_trailing_option("고르세요\n1. one\n2. two") is False

    def test_prose_after_numbered_line_false(self):
        assert has_single_trailing_option(
            PROSE_SINGLE_NUMBERED_SAMPLES[0]) is False

    def test_trailing_not_starting_at_one_false(self):
        assert has_single_trailing_option(
            PROSE_SINGLE_NUMBERED_SAMPLES[1]) is False

    def test_fenced_numbered_line_excluded(self):
        assert has_single_trailing_option(
            PROSE_SINGLE_NUMBERED_SAMPLES[2]) is False

    def test_fence_only_trailing_false(self):
        assert has_single_trailing_option("예시:\n```\n1. step\n```") is False

    def test_empty_and_none_false(self):
        assert has_single_trailing_option("") is False
        assert has_single_trailing_option(None) is False

    def test_plain_prose_false(self):
        assert has_single_trailing_option("목록 없는 평문입니다.") is False


# ---------------------------------------------------------------------------
# Classifier gate (_maybe_mark_options): who gets a Haiku call, who never does
# ---------------------------------------------------------------------------


class TestClassifierGate:
    def _mark(self, content, monkeypatch, classifier=True):
        calls = _patch_classifier(monkeypatch, classifier)
        out, injected = _run(SdkBridge._maybe_mark_options("prev", content))
        return out, injected, calls

    def test_single_trailing_reaches_classifier_and_injects(self, monkeypatch):
        out, injected, calls = self._mark(SINGLE_OPTION_BODY, monkeypatch)
        assert len(calls) == 1, "single trailing option must reach the classifier"
        assert injected is True
        assert out.endswith("[[OPTIONS]]")

    def test_single_trailing_classifier_no_leaves_unchanged(self, monkeypatch):
        out, injected, calls = self._mark(
            PROSE_TRAILING_ELIGIBLE, monkeypatch, classifier=False)
        assert len(calls) == 1
        assert injected is False and out == PROSE_TRAILING_ELIGIBLE

    def test_authored_marker_single_option_suppresses_classifier(self, monkeypatch):
        """DGN-325 hand-authored path: zero classifier spend, zero re-inject."""
        out, injected, calls = self._mark(SINGLE_OPTION_AUTHORED, monkeypatch)
        assert calls == [], "authored marker must not spend a classifier call"
        assert injected is False and out == SINGLE_OPTION_AUTHORED

    def test_labeled_marker_single_option_suppresses_classifier(self, monkeypatch):
        content = "진행할까요?\n[[OPTIONS: 진행]]"
        out, injected, calls = self._mark(content, monkeypatch)
        assert calls == [] and injected is False and out == content

    def test_prose_samples_never_spend_a_classifier_call(self, monkeypatch):
        """False-positive cost cap: incidental single numbered lines are
        rejected by the DETERMINISTIC gate -- no Haiku spend at all."""
        for sample in PROSE_SINGLE_NUMBERED_SAMPLES:
            out, injected, calls = self._mark(sample, monkeypatch)
            assert calls == [], f"unexpected classifier call for: {sample!r}"
            assert injected is False and out == sample

    def test_multi_item_gate_unchanged(self, monkeypatch):
        """No-regression: the >=2 path still reaches the classifier."""
        out, injected, calls = self._mark("고르세요\n1. one\n2. two", monkeypatch)
        assert len(calls) == 1 and injected is True


# ---------------------------------------------------------------------------
# End-to-end: finalize -> real render seat (the incident, fixed)
# ---------------------------------------------------------------------------


class TestFinalizeEndToEnd:
    def test_single_option_gets_keyboard(self, monkeypatch):
        """THE DGN-1128 incident: one prep action, no marker -> classifier
        yes -> marker injected -> ONE tappable button, body list kept
        (classifier provenance, DGN-665 owner lock)."""
        resp, calls = _finalize(SINGLE_OPTION_BODY, monkeypatch, classifier=True)
        assert len(calls) == 1
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
        assert len(kb) == 1, f"no keyboard attached: {sent}"
        rows = kb[0]["reply_markup"].inline_keyboard
        assert [r[0].text for r in rows] == ["1. 완료"]
        bodies = [e for e in sent if e["reply_markup"] is None]
        assert any("1. 완료" in b["text"] for b in bodies), (
            "classifier-injected marker must keep the body list"
        )

    def test_single_option_classifier_no_keeps_prefix_behavior(self, monkeypatch):
        """Scope guard: classifier says no -> no marker, has_options stays
        False (the OR-arms still run >=2 has_numbered_list), zero keyboards --
        byte-identical to pre-fix output."""
        resp, calls = _finalize(
            PROSE_TRAILING_ELIGIBLE, monkeypatch, classifier=False)
        assert len(calls) == 1
        assert resp.has_options is False
        assert "[[OPTIONS]]" not in resp.content

        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        _run(bot._send_smart(42, resp.content, force_options=resp.has_options))
        assert not _keyboard_sends(sent)

    def test_prose_sample_finalize_no_button_no_call(self, monkeypatch):
        """False-positive zero, end-to-end: mid-prose numbered line -> gate
        closed, classifier NEVER consulted (even mocked always-yes), no
        keyboard."""
        resp, calls = _finalize(
            PROSE_SINGLE_NUMBERED_SAMPLES[0], monkeypatch, classifier=True)
        assert calls == []
        assert resp.has_options is False

        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        _run(bot._send_smart(42, resp.content, force_options=resp.has_options))
        assert not _keyboard_sends(sent)

    def test_authored_single_option_end_to_end_unchanged(self, monkeypatch):
        """No-regression (DGN-325): hand-authored single-option marker still
        renders its one button, no classifier spend, body-strip applies
        (authored provenance)."""
        resp, calls = _finalize(SINGLE_OPTION_AUTHORED, monkeypatch, classifier=True)
        assert calls == []
        assert resp.has_options is True
        assert resp.options_classifier_injected is False

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
        rows = kb[0]["reply_markup"].inline_keyboard
        assert [r[0].text for r in rows] == ["1. 완료"]


# ---------------------------------------------------------------------------
# End-to-end: proactive push path
# ---------------------------------------------------------------------------


class TestProactiveEndToEnd:
    def _flush(self, content, monkeypatch, classifier):
        calls = _patch_classifier(monkeypatch, classifier)
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
        return sent, push_calls, calls

    def test_single_option_proactive_gets_keyboard(self, monkeypatch):
        sent, push_calls, calls = self._flush(
            SINGLE_OPTION_BODY, monkeypatch, classifier=True)
        assert len(calls) == 1
        assert push_calls and push_calls[0]["has_options"] is True
        assert push_calls[0]["classifier_injected"] is True
        assert len(_keyboard_sends(sent)) == 1

    def test_prose_sample_proactive_no_button_no_call(self, monkeypatch):
        sent, push_calls, calls = self._flush(
            PROSE_SINGLE_NUMBERED_SAMPLES[1], monkeypatch, classifier=True)
        assert calls == []
        assert push_calls and push_calls[0]["has_options"] is False
        assert not _keyboard_sends(sent)
