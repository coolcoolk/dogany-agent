"""HF48 (dec-329 form 1): cut a model-fabricated next-turn block at render time.

Leak shape (measured 5x in 30 days, 3 of 5 in interim text before a tool
call): the model keeps writing past its answer and fabricates the NEXT turn
inside its own text block -- a line starting "user", then a line carrying
"system" and a harness tag (<total_tokens>N tokens left</total_tokens> or
<system-reminder>). _scaffold_guard runs at every text ingestion seat (live
stream, fold capture, final assembly, proactive, btw), so the cut is tested
on the guard itself.

Correctness bar: a false positive eats owner content, so the no-cut fixtures
(inline code, fenced code, ordinary replies) carry equal weight.
"""

import logging
from unittest.mock import patch

import pytest

from bridge import sdk_bridge
from bridge.sdk_bridge import _harness_leak_cut, _scaffold_guard


# ---------------------------------------------------------------------------
# True cuts: the 5 measured leak shapes (ko/en, end-of-reply / mid-reply
# interim before a tool call).
# ---------------------------------------------------------------------------

LEAK_FIXTURES = [
    pytest.param(
        "배포 스크립트 정리했습니다. 이제 `start.sh` 한 번만 돌리면 됩니다.",
        "\n\nuser좋아 그럼 다음 거 진행해\n"
        "system <total_tokens>812345 tokens left</total_tokens>\n"
        "네, 다음 작업 진행하겠습니다.",
        id="ko-end-of-reply-glued-user-system-tokens",
    ),
    pytest.param(
        "Done -- the PR is merged and CI is green on main.",
        "\n\nuser: ok ship it\n"
        "system\n"
        "<system-reminder>\nThe task tools haven't been used recently.\n"
        "</system-reminder>\n"
        "Shipping now.",
        id="en-end-of-reply-system-line-then-reminder",
    ),
    pytest.param(
        "로그 파일 먼저 확인해볼게요.",
        "\nuser 응 봐줘\n"
        "system <system-reminder>Today's date is 2026-10-08.</system-reminder>",
        id="ko-interim-before-tool-call-reminder",
    ),
    pytest.param(
        "Let me check the watchdog logs first.",
        "\n\nUser: sounds good\n"
        "system: <total_tokens>990211 tokens left</total_tokens>\n\n"
        "Continuing with the log check.",
        id="en-interim-before-tool-call-tokens",
    ),
    pytest.param(
        "확인 결과 크론 두 개가 같은 시각에 겹쳐 있었습니다. 하나를 5분 뒤로 옮겼어요.",
        "\nuser그럼 배포는 언제 해?\n"
        "system\n"
        "<total_tokens>14917998 tokens left</total_tokens>\n",
        id="ko-end-of-reply-tag-on-own-line",
    ),
]


@pytest.mark.parametrize("legit, leaked", LEAK_FIXTURES)
def test_leak_shape_is_cut_body_survives_byte_identical(legit, leaked):
    assert _scaffold_guard(legit + leaked) == legit


@pytest.mark.parametrize("legit, leaked", LEAK_FIXTURES)
def test_leak_shape_logs_one_canary_line(legit, leaked, caplog):
    with caplog.at_level(logging.WARNING, logger=sdk_bridge.logger.name):
        _scaffold_guard(legit + leaked)
    canaries = [r for r in caplog.records if "HF48 harness-leak canary" in r.getMessage()]
    assert len(canaries) == 1


def test_block_that_is_wholly_a_fabricated_turn_comes_back_empty():
    leaked = "user 다음은?\nsystem <total_tokens>500000 tokens left</total_tokens>\n"
    assert _scaffold_guard(leaked) == ""


def test_tag_line_without_user_line_still_cut_from_tag_line():
    text = "작업 끝났습니다.\n<system-reminder>\nreminder body\n</system-reminder>"
    assert _scaffold_guard(text) == "작업 끝났습니다."


def test_cut_after_inline_mention_on_earlier_line():
    legit = "The harness writes `<total_tokens>` into its own context."
    leaked = "\nuser ok\nsystem <total_tokens>1000 tokens left</total_tokens>"
    assert _scaffold_guard(legit + leaked) == legit


def test_cut_after_a_closed_fence():
    legit = "결과:\n```\n<system-reminder> 예시\n```\n이상입니다."
    leaked = "\nuser응\nsystem <system-reminder>x</system-reminder>"
    assert _scaffold_guard(legit + leaked) == legit


def test_gate_off_passes_leak_through():
    text = "끝.\nuser 응\nsystem <total_tokens>1 tokens left</total_tokens>"
    with patch.object(sdk_bridge, "BRIDGE_SCAFFOLD_GUARD", False):
        assert _scaffold_guard(text) == text


# ---------------------------------------------------------------------------
# No-cut: inline-code / fenced mentions and normal replies.
# ---------------------------------------------------------------------------

NO_CUT_FIXTURES = [
    pytest.param(
        "HF48 가드는 `<total_tokens>` 나 `<system-reminder>` 가 포함된 줄을 자릅니다.",
        id="ko-inline-code-both-tags",
    ),
    pytest.param(
        "user and system roles: the harness injects `<system-reminder>` blocks\n"
        "and a `<total_tokens>N tokens left</total_tokens>` line.",
        id="en-inline-code-after-user-line",
    ),
    pytest.param(
        "재현 샘플입니다:\n\n```\nuser좋아\nsystem <total_tokens>812345 tokens left</total_tokens>\n```\n\n"
        "위 형태가 새어 나온 패턴이에요.",
        id="ko-fenced-leak-sample",
    ),
    pytest.param(
        "Fixture:\n~~~text\nuser: ok\nsystem\n<system-reminder>body</system-reminder>\n~~~\nThat is the shape.",
        id="en-tilde-fenced-leak-sample",
    ),
    pytest.param(
        "```xml\n<system-reminder>\n  <total_tokens>10</total_tokens>\n</system-reminder>\n```",
        id="fenced-only-reply",
    ),
    pytest.param(
        "오늘 일정 정리했습니다.\n\n- 10:00 스탠드업\n- 14:00 리뷰\n\nuser 권한 설정도 같이 확인했어요.",
        id="ko-normal-reply-with-user-word",
    ),
    pytest.param(
        "Users can now log in. The system prompt was updated and the user\n"
        "settings page shows the new token budget.",
        id="en-normal-reply-user-system-words",
    ),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("text", NO_CUT_FIXTURES)
def test_no_cut(text, caplog):
    with caplog.at_level(logging.WARNING, logger=sdk_bridge.logger.name):
        assert _scaffold_guard(text) == text
        assert _harness_leak_cut(text) == text
    assert not [r for r in caplog.records if "HF48" in r.getMessage()]


def test_existing_signature_guard_still_applies():
    text = "답변입니다.\nsystem UserPromptSubmit hook additional context: x"
    assert _scaffold_guard(text) == "답변입니다."


def test_tag_quoted_mid_sentence_is_not_cut():
    # Real false cut found on the 30-day corpus: a report describing the
    # leak shape in prose quoted the tag mid-line.
    text = ("- Evidence: all 5 are the model fabricating the next turn "
            "(\"user... system<total_tokens>...\").\n- Next finding.")
    assert _harness_leak_cut(text) == text
