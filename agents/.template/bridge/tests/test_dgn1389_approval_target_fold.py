"""DGN-1389: copy the owner must approve must never exist only in the fold.

Measured 2026-09-11 06:32-06:34 KST (dev agent transcript): the agent put
the v2.4.0 release-notice copy in a `>!` fold quote and ended the message
with "confirm this copy?" -- an end_turn answer, not narration between tool
calls. A Stop hook (declaration-fulfillment check) blocked the turn; the
model dispatched, marked its ledger and wrote a short closing note. The live
stream marks no message terminal (DGN-1703), so in fold mode the approval
copy was captured as interim, and only the closing note became the final
body: the copy sat inside the collapsed progress record. Cause branch (B):
the bridge routed an answer into the fold, the sender did not narrate it.

DGN-1838 (2026-10-03) promotes substantive interim answers, which covers
this long copy. It did not cover a SHORT approval target: a few-line `>!`
copy draft, an [[OPTIONS]] menu or a fold:: block under the 200-char prose
floor still folded away. Fix under test: formatting.is_approval_target makes
such a block substantive at any length.

The regression check (_only_in_fold) is the ticket's: an approval target
whose text exists in the progress record and nowhere else on the screen.
"""

import unittest
from typing import List
from unittest.mock import patch

from bridge.formatting import (
    FOLD_CAPTION_NORMAL,
    is_approval_target,
    is_substantive_interim,
)
from bridge.tests.test_dgn1651_stop_block_duplicate import _result
from bridge.tests.test_dgn1838_interim_answer_promotion import (
    _feedback,
    _text,
    _tools,
    run_turn,
)

# The 06:32:57 answer, trimmed to the approval copy and its question.
APPROVAL = """✅ **사용자이 맞습니다. 실측했습니다.**

공개 미러에 이미 나가 있는 v2.2.0을 열어봤습니다. 잘린 건 도가니가 아니라 OSS 브릿지였습니다.

📌 그래서 세 줄이 빠지고 넷만 남습니다

>! **▸ 업데이트 요약**
> 백그라운드 작업, 메시지 접수, 작업대와 설치 화면 표시 관련 수정입니다.
>
> **▸ 업데이트 상세**
> · 백그라운드 작업: 켜 둔 사용량 허용 설정을 작업 시작 경로가 읽지 않던 것을 반영합니다. 허용으로 통과하면 그 사실이 기록에 남습니다.
> · 긴 메시지: 텔레그램이 길이 제한 때문에 여러 개로 쪼개 보낸 긴 붙여넣기를 이제 한 건의 지시로 합쳐서 받습니다.
> · 작업대: 진행 중 항목 앞에 영어 내부 용어가 붙어 한 줄 안에서 두 언어가 섞이던 문제를 고쳤습니다.
> · 설치 화면: 설치 단계 번호가 전체 단계 수를 서로 다르게 표시하던 문제를 고쳤습니다.

**이 문구로 확정할까요?**"""
APPROVAL_NEEDLE = "설치 단계 번호가 전체 단계 수를"

# The 06:34:03 closing note (the turn's final body).
CLOSING = """✅ 게이트가 맞게 잡았습니다. 스킬 수정은 방금 나갔습니다.

**문구는 아까 넷으로 줄인 그대로 확정하실지만 답 주시면 됩니다.**"""

SHORT_COPY = """>! **▸ 업데이트 요약**
> 작업대 표시 관련 수정입니다.

이 문구로 확정할까요?"""
SHORT_COPY_NEEDLE = "작업대 표시 관련 수정입니다"

SHORT_MENU = """어느 쪽으로 갈까요?

[[OPTIONS]]
이대로 확정
두 지점 손보기"""
SHORT_MENU_NEEDLE = "두 지점 손보기"

SHORT_FOLD_BLOCK = """fold:: 통지 문구 초안
작업대 표시 관련 수정입니다.
fold::end"""
SHORT_FOLD_BLOCK_NEEDLE = "통지 문구 초안"

FINAL = "기록 정리했습니다."


def _is_progress(text: str) -> bool:
    # The fold bubble carries the progress caption; a promoted bubble or a
    # final body may legitimately render its own `>!` quote as a blockquote.
    return FOLD_CAPTION_NORMAL in text


def _only_in_fold(view: List[str], needle: str) -> bool:
    """The ticket's regression check: the approval target is on the screen
    only inside the progress record."""
    in_fold = any(needle in t for t in view if _is_progress(t))
    elsewhere = any(needle in t for t in view if not _is_progress(t))
    return in_fold and not elsewhere


def _count_outside_fold(view: List[str], needle: str) -> int:
    return sum(t.count(needle) for t in view if not _is_progress(t))


def incident(**kw):
    """The 06:32 turn, message for message: tools, approval answer, Stop
    block, dispatch + ledger tools, closing note."""
    return run_turn(
        [
            *_tools("t1", "t2"),
            _text(APPROVAL),
            _feedback(),
            *_tools("t3", "t4"),
            _text(CLOSING),
            _result(result=CLOSING),
        ],
        **kw,
    )


def stop_blocked(target: str):
    """Short approval target, Stop block, tool, short final."""
    return run_turn(
        [
            _text(target),
            _feedback(),
            *_tools("t1"),
            _text(FINAL),
            _result(result=FINAL),
        ]
    )


class TestIncidentReplay(unittest.TestCase):
    def test_without_promotion_copy_is_only_in_the_fold(self):
        # Pins cause (B): the bridge, not the sender, put an end_turn answer
        # into the progress record.
        with patch(
            "bridge.sdk_bridge.split_promoted_interim",
            side_effect=lambda blocks, final: ([], list(blocks)),
        ):
            response, _pushes, view, _screen = incident()
        self.assertTrue(_only_in_fold(view, APPROVAL_NEEDLE), view)
        self.assertEqual(response.content.strip(), CLOSING.strip())

    def test_copy_reaches_owner_outside_the_fold(self):
        response, pushes, view, _screen = incident()
        self.assertFalse(_only_in_fold(view, APPROVAL_NEEDLE), view)
        self.assertEqual(_count_outside_fold(view, APPROVAL_NEEDLE), 1)
        self.assertEqual(pushes, [APPROVAL.strip()])
        self.assertEqual(response.content.strip(), CLOSING.strip())


class TestShortApprovalTargets(unittest.TestCase):
    def test_short_targets_never_live_only_in_the_fold(self):
        for target, needle in (
            (SHORT_COPY, SHORT_COPY_NEEDLE),
            (SHORT_MENU, SHORT_MENU_NEEDLE),
            (SHORT_FOLD_BLOCK, SHORT_FOLD_BLOCK_NEEDLE),
        ):
            with self.subTest(needle=needle):
                self.assertLess(len(target), 200)
                response, pushes, view, _screen = stop_blocked(target)
                self.assertFalse(_only_in_fold(view, needle), view)
                self.assertEqual(_count_outside_fold(view, needle), 1, view)
                self.assertEqual(pushes, [target.strip()])
                # The final body is untouched.
                self.assertEqual(response.content.strip(), FINAL)

    def test_restated_copy_is_delivered_once_by_the_final(self):
        final = "문구 다시 붙입니다.\n\n" + SHORT_COPY
        response, pushes, view, _screen = run_turn(
            [
                _text(SHORT_COPY),
                *_tools("t1"),
                _text(final),
                _result(result=final),
            ]
        )
        self.assertEqual(pushes, [])
        self.assertEqual(_count_outside_fold(view, SHORT_COPY_NEEDLE), 1, view)
        self.assertFalse(_only_in_fold(view, SHORT_COPY_NEEDLE))

    def test_short_progress_lines_still_fold(self):
        response, pushes, view, _screen = run_turn(
            [
                _text("파일을 확인할게요."),
                *_tools("t1"),
                _text("> 인용만 하고 넘어갑니다."),
                *_tools("t2"),
                _text(FINAL),
                _result(result=FINAL),
            ]
        )
        self.assertEqual(pushes, [])
        folds = [t for t in view if _is_progress(t)]
        self.assertEqual(len(folds), 1)
        self.assertIn("파일을 확인할게요", folds[0])
        self.assertEqual(response.content.strip(), FINAL)


class TestClassifier(unittest.TestCase):
    def test_approval_shapes(self):
        for text in (
            SHORT_COPY,
            SHORT_MENU,
            SHORT_FOLD_BLOCK,
            ">!summary only",
            "  >! indented fold quote",
            "pick one\n[[OPTIONS: yes | no]]",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_approval_target(text))
                self.assertTrue(is_substantive_interim(text))

    def test_non_approval_shapes(self):
        for text in (
            "",
            None,
            "checking the log next",
            "> a plain quote is not a fold draft",
            "the >! marker mid-line is not a quote run",
            ">!",
            "fold::end",
            "mentions [[OPTIONS]] inline, not as a marker line",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_approval_target(text))
                self.assertFalse(is_substantive_interim(text or ""))


if __name__ == "__main__":
    unittest.main()
