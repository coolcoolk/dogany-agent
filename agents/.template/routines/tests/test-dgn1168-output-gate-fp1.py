#!/usr/bin/env python3
# test-lane: fast    # measured below 1s (DGN-1263 marker requirement)
"""DGN-1168 FP1 -- Korean inflected OPTIONS labels must not fire IDLE_PROMISE.

Root cause: a numbered [[OPTIONS]] item ending in a commit verb (e.g.
"릴리즈에 묶겠습니다") reads exactly like a self-commitment to
IDLE_PROMISE_COMMIT_RE, but it names a CANDIDATE action for the owner to
pick -- the same "hands the decision to the owner" shape as the existing
형님-gate exemptions.  Fix: IDLE_PROMISE_EXEMPT_RE carries a [[OPTIONS]]
marker (routines/lib/output_gate.py).

Ported from the live instance's routines/tests/test-output-gate.py
TestFP1KoreanInflection (commit 19ab25f9) -- this repo has no
test-output-gate.py of its own, so the regression lands as its own file.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)
LIB = os.path.join(ROUTINES, "lib")
sys.path.insert(0, LIB)

from output_gate import check_message  # noqa: E402

KOREAN_INFLECTED_OPTIONS = (
    "결론: 지금 반영하는 쪽을 추천합니다.\n"
    "\n"
    "근거: 수정이 작고 롤백이 쉽습니다. 다음 릴리즈 포함도 안전합니다.\n"
    "\n"
    "1. 지금 반영하겠습니다 -- 오늘 밤 재기동 예약\n"
    "2. 릴리즈에 묶겠습니다 -- v1.31 앵커에 포함\n"
    "[[OPTIONS]]"
)

KOREAN_CONCLUSION_VARIES = (
    "처리됐습니다.\n\n"
    "1. 즉시 이관하도록 하겠습니다 -- 오늘 실행\n"
    "2. 주말로 미루겠습니다 -- 토요일 새벽\n"
    "[[OPTIONS]]"
)


class TestFP1KoreanInflection(unittest.TestCase):
    """FP1: Korean inflected OPTIONS labels must NOT trigger IDLE_PROMISE."""

    def test_korean_inflected_labels_pass(self):
        vs = check_message(KOREAN_INFLECTED_OPTIONS)
        self.assertEqual(
            [],
            vs,
            "FP1 regression: Korean inflected OPTIONS message produced "
            "violations: %s" % [v.rule_id for v in vs],
        )

    def test_korean_conclusion_varies_pass(self):
        vs = check_message(KOREAN_CONCLUSION_VARIES)
        self.assertEqual(
            [],
            vs,
            "FP1 regression: Korean conclusion-varies OPTIONS message "
            "produced violations: %s" % [v.rule_id for v in vs],
        )

    def test_no_substring_containment_in_lib(self):
        """The FP1 root fix requires the library to NOT do label-substring
        containment ('label in body_text' style checks, i.e. no
        _option_satisfied)."""
        import inspect
        import output_gate
        source = inspect.getsource(output_gate)
        self.assertNotIn(
            "_option_satisfied",
            source,
            "FP1: _option_satisfied (label-substring containment) must be "
            "absent from the library",
        )


if __name__ == "__main__":
    unittest.main()
