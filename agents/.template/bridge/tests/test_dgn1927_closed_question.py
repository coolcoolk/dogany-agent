"""DGN-1927: a closing either/or question gets buttons without a marker."""

import asyncio

from bridge.options import extract_alternative_question as alt
from bridge.sdk_bridge import SdkBridge
from bridge.formatting import parse_options_marker_labels
from bridge.options import has_options_marker


def test_ag_sentence_two_buttons():
    text = "다 정리했어요.\n\n회사에서 바로 가세요, 집에 들렀다 가세요?"
    assert alt(text) == ["회사에서 바로", "집에 들렀다"]


def test_inka_pair():
    assert alt("A인가요, B인가요?") == ["A", "B"]
    assert alt("커피인가요, 차인가요?") == ["커피", "차"]


def test_ahnimyeon_split():
    assert alt("오늘 하세요 아니면 내일 하세요?") == ["오늘", "내일"]


def test_open_question_zero():
    assert alt("어디로 갈까요?") == []


def test_yes_no_single_clause_zero():
    assert alt("회의 잡을까요?") == []


def test_english_or():
    assert alt("Tea or coffee?") == ["Tea", "coffee"]


def test_neg_wh_with_or():
    assert alt("Which do you want, tea or coffee?") == []


def test_neg_not_last_sentence():
    assert alt("Tea or coffee? I will wait here.") == []


def test_neg_no_question_mark():
    assert alt("Tea or coffee") == []


def test_neg_ko_clause_without_ending():
    assert alt("커피, 차 드릴까요?") == []


def test_neg_code_fence_question():
    assert alt("```\nTea or coffee?\n```") == []


def test_marker_present_classifier_not_consulted():
    c = "Tea or coffee?\n\n[[OPTIONS]]"
    out, injected = asyncio.run(SdkBridge._maybe_mark_options("", c))
    assert (out, injected) == (c, False)


def test_injects_labeled_marker():
    out, injected = asyncio.run(SdkBridge._maybe_mark_options("", "Tea or coffee?"))
    assert injected and has_options_marker(out)
    assert parse_options_marker_labels(out.split("\n")[-1]) == ["Tea", "coffee"]
