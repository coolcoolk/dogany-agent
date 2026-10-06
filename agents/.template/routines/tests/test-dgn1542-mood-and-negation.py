#!/usr/bin/env python3
# test-lane: fast    # subprocess per case, measured ~1s
"""DGN-1542 -- the attrib gate read requirement/negation sentences as claims.

Measured specimens (2026-09-17 three, 2026-09-18 one more, all one root):
the CLAIM predicate judged by WORD PRESENCE (owner word + decision word), so
a future requirement ("그건 별도 오너 승인 건이다"), a deontic sentence
("승인이 필요하다"), and a negation-bound stem ("오너 미확정") all printed as
"claims owner confirmation, no source". The 09-18 one is the sharpest: the
sentence disclosed the ABSENCE of owner approval and the source-demanding
gate blocked that very disclosure.

The fix narrows the DEFINITION -- a claim asserts a COMPLETED owner act --
instead of growing a suppression list (head-session ruling 2026-09-17, on the
claim lint, applied here). Voids are closed grammatical classes:

  J  false positives must now PASS
     J1 pending-slot nominal frame:   "오너 승인 건이다" / "게이트다" / "전"
     J2 bound negation prefix:        "오너 미확정" (the exact 09-18 line)
     J3 deontic mood:                 "필요하다" / "받아야 한다"
     J4 clause negation:              "오너 확정은 없다"
     J5 lazy pairing regression guard: greedy [^\n]{0,40} used to skip the
        negated stem and pair 오너 with a decision word in the NEXT sentence

  K  true positives must STILL BLOCK (a fix that drops one is a regression)
     K1 completed act, decision word as instrument: "오너 승인으로 태그를 컷했다"
        (a real 2026-09-17 true block -- passed only once 시각+축자 added)
     K2 plain completed claim:        "형님 승인 받았으니 이대로 간다"
     K3 settled-item noun form:       "오너 확정 사항: A안으로 간다"
        (사항 is deliberately NOT a pending-slot noun -- this is why)
     K4 void is CLAUSE-scoped: a requirement clause on the same line does not
        shield a real claim in the next clause
     K5 a properly sourced claim still passes via 시각+축자 (unchanged path)
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # routines/
GATE = os.path.join(ROOT, "lib", "attrib_gate.py")
PY = sys.executable


def _run_gate(text):
    """(exit_code, stderr). 0 = BLOCK, 1 = PASS."""
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        r = subprocess.run([PY, GATE, path], capture_output=True, text=True)
        return r.returncode, r.stderr
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------- #
# J -- the measured false positives must PASS
# --------------------------------------------------------------------------- #

def test_J1_pending_slot_nominal_frame_passes():
    for body in ("그건 별도 오너 승인 건이다.\n",
                 "그건 릴리스 오너 승인 게이트다.\n",
                 "이건 오너 확정 대상이고, 아직 아니다.\n",
                 "오너 승인 전이다.\n"):
        rc, err = _run_gate(body)
        assert rc == 1, "pending-slot frame must PASS: %r -> %s" % (body, err)


def test_J2_bound_negation_prefix_passes_the_0918_specimen():
    body = ("그래서 SKILL step 3 의 점유 화면 두 문장은 ** 오너 미확정 ** "
            "PLACEHOLDER 상태 그대로다.\n")
    rc, err = _run_gate(body)
    assert rc == 1, (
        "the gate blocked the very sentence disclosing the absence of owner "
        "approval (DGN-1542 재발 2026-09-18) -- must PASS: %s" % err)


def test_J3_deontic_mood_passes():
    for body in ("오너 승인이 필요하다.\n",
                 "오너 승인을 받아야 한다.\n",
                 "오너 확정이 요건이다.\n"):
        rc, err = _run_gate(body)
        assert rc == 1, "deontic sentence must PASS: %r -> %s" % (body, err)


def test_J4_clause_negation_passes():
    rc, err = _run_gate("지금 시점에 오너 확정은 없다.\n")
    assert rc == 1, "negated claim must PASS: %s" % err


def test_J5_lazy_pairing_does_not_skip_the_negated_stem():
    # greedy pairing matched 오너...승인 across the sentence boundary and
    # judged the mood of the WRONG clause
    rc, err = _run_gate("지금 시점에 오너 확정은 없다. 승인이 나오면 진행한다.\n")
    assert rc == 1, (
        "greedy-gap regression: 오너 must pair with the nearest decision "
        "word (확정, negated), not 승인 in the next sentence: %s" % err)


# --------------------------------------------------------------------------- #
# K -- true positives must still BLOCK
# --------------------------------------------------------------------------- #

def test_K1_completed_act_with_instrument_still_blocks():
    rc, err = _run_gate("오너 승인으로 태그를 컷했다.\n")
    assert rc == 0, "the real true block must stay a block"
    assert "출처 표지가 없다" in err


def test_K2_plain_completed_claim_still_blocks():
    rc, _ = _run_gate("형님 승인 받았으니 이대로 간다.\n")
    assert rc == 0


def test_K3_settled_item_noun_form_still_blocks():
    # 사항 is tense-ambiguous and usually settled -- it must NOT be in the
    # pending-slot set, or this whole claim class silently passes
    rc, _ = _run_gate("오너 확정 사항: A안으로 간다.\n")
    assert rc == 0, "확정 사항 (settled item) must still block without a source"


def test_K4_void_is_clause_scoped_not_line_scoped():
    rc, _ = _run_gate("오너 승인이 필요하다. 오너가 컷을 승인 확정했다.\n")
    assert rc == 0, (
        "a requirement clause must not shield the completed-act claim in "
        "the next clause of the same line")


def test_K5_sourced_claim_still_passes_via_time_and_utterance():
    rc, err = _run_gate("오너 승인 축자 2026-09-17 07:25 발화 기록 그대로다.\n")
    assert rc == 1, "시각+축자 sourced claim path unchanged: %s" % err


def _main():
    fails = []
    names = sorted(k for k in globals() if k.startswith("test_"))
    for name in names:
        try:
            globals()[name]()
            print("%s PASS" % name)
        except AssertionError as exc:
            print("%s FAIL %s" % (name, exc))
            fails.append(name)
        except Exception as exc:                          # pragma: no cover
            print("%s ERROR %r" % (name, exc))
            fails.append(name)
    print("\n%d/%d passed" % (len(names) - len(fails), len(names)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_main())
