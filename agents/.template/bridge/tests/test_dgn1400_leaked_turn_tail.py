"""DGN-1400: strip a self-authored next-turn tail before it reaches the owner.

The model sometimes does not stop at its own last sentence -- it keeps going
and writes the NEXT turn: a chat-transcript role label (user/assistant/human,
or a misspelling) glued onto the tail of the message, followed by that other
speaker's line. strip_leaked_turn_tail() (bridge/formatting.py) removes only
that TAIL, anchored on a label glued directly onto Hangul at a line start.

Correctness bar: a false positive eats the owner's real content, which is
strictly worse than the defect. These tests weight heavily toward proving
NON-stripping of legitimate bodies (English "user", Korean particles glued to
the English term "user", code fences, representative real-shaped message
bodies) alongside the two measured real leak samples.
"""

import pytest

from bridge.formatting import sanitize_message_for_telegram, strip_leaked_turn_tail


# ---------------------------------------------------------------------------
# Real measured samples (verbatim from the ticket) -- both origins collapse
# to the same shape, so one strip function catches both.
# ---------------------------------------------------------------------------


def test_real_sample_1_stripped_body_survives_byte_identical():
    legit = "... (`[1/2]`·`[9b/10]` -> 10단계 통일)"
    leaked = "\n\nuser확인해봤어? 원래는 어떤형식으로 나갔는데?"
    result = strip_leaked_turn_tail(legit + leaked)
    assert result == legit


def test_real_sample_2_stripped_body_survives_byte_identical():
    legit = "... 나선을 논증하게 했습니다."
    leaked = "\n\nuser작업대건은?"
    result = strip_leaked_turn_tail(legit + leaked)
    assert result == legit


def test_uset_misspelled_variant_stripped():
    legit = "배포 완료했습니다."
    result = strip_leaked_turn_tail(legit + "\n\nuset확인해봤어?")
    assert result == legit


def test_uset_variant_case_insensitive():
    legit = "다음 단계 진행합니다."
    result = strip_leaked_turn_tail(legit + "\n\nUSET확인해봤어?")
    assert result == legit


# ---------------------------------------------------------------------------
# Must NOT touch: English "user" inside a normal sentence
# ---------------------------------------------------------------------------


def test_english_sentence_with_user_untouched():
    text = "The user can configure settings as needed before the next deploy."
    assert strip_leaked_turn_tail(text) == text


def test_user_as_first_word_of_english_sentence_untouched():
    text = "Intro line.\nUser authentication happens via OAuth, not passwords."
    assert strip_leaked_turn_tail(text) == text


def test_korean_particle_glued_to_user_untouched():
    """'user는' is the English term + the topic particle -- normal Korean
    orthography (Korean technical prose keeps English terms untranslated),
    not a leak."""
    text = "설정 완료.\n\nuser는 이렇게 설정합니다."
    assert strip_leaked_turn_tail(text) == text


# ---------------------------------------------------------------------------
# Must NOT touch: code fences / inline code are a hard exclusion zone
# ---------------------------------------------------------------------------


def test_code_fence_containing_role_label_line_untouched():
    text = (
        "예시 로그입니다:\n"
        "```\n"
        "user확인해봤어?\n"
        "assistant네 확인했습니다\n"
        "```"
    )
    assert strip_leaked_turn_tail(text) == text


def test_inline_code_role_label_untouched():
    text = "라벨은 `user확인` 이런 형식이었습니다."
    assert strip_leaked_turn_tail(text) == text


# ---------------------------------------------------------------------------
# Message that IS only the leaked tail -- never return an empty send
# ---------------------------------------------------------------------------


def test_message_that_is_only_leaked_tail_returned_unchanged():
    text = "user작업대건은?"
    assert strip_leaked_turn_tail(text) == text


def test_message_that_is_only_leaked_tail_with_leading_blank_line_unchanged():
    text = "\n\nuser확인해봤어?"
    assert strip_leaked_turn_tail(text) == text


# ---------------------------------------------------------------------------
# Normal messages pass through byte-identical -- representative real-shaped
# bodies: quoted colloquial Korean, mixed EN/KO prose with a fence mention,
# and an English possessive "user's" (label + non-Hangul glue).
# ---------------------------------------------------------------------------


def test_real_shaped_quoted_korean_body_untouched():
    text = (
        "**19:28** \"메시지가 딱 끝나는 타이밍에 보내면\n"
        "가끔 씹히고 입력중 표시도 안 뜨는 것 같아\""
    )
    assert strip_leaked_turn_tail(text) == text


def test_real_shaped_codeblock_split_note_untouched():
    text = (
        "펜스(```) 기준으로 텍스트를 "
        "코드/산문 세그먼트로 분리, "
        "각각 독립 메시지로 송신."
    )
    assert strip_leaked_turn_tail(text) == text


def test_user_possessive_glued_to_apostrophe_untouched():
    text = "You are not a chatbot. You are the user's **dev agent**."
    assert strip_leaked_turn_tail(text) == text


# ---------------------------------------------------------------------------
# Edge: empty / no-label input is a cheap passthrough
# ---------------------------------------------------------------------------


def test_empty_string_untouched():
    assert strip_leaked_turn_tail("") == ""


def test_no_label_present_untouched():
    text = "오늘 배포는 내일 오전으로 오늘을다."
    assert strip_leaked_turn_tail(text) == text


# ---------------------------------------------------------------------------
# Wiring: sanitize_message_for_telegram (the actual send funnel) applies the
# strip before HTML rendering.
# ---------------------------------------------------------------------------


def test_sanitize_message_for_telegram_strips_leaked_tail():
    legit = "배포 완료했습니다."
    leaked = "\n\nuser확인해봤어?"
    result = sanitize_message_for_telegram(legit + leaked)
    assert "user" not in result.lower()
    assert "배포 완료했습니다" in result


def test_sanitize_message_for_telegram_untouched_when_no_leak():
    legit = "배포 완료했습니다."
    result = sanitize_message_for_telegram(legit)
    assert "배포 완료했습니다" in result
