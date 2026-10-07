#!/usr/bin/env python3
# test-lane: fast    # measured below 1s (DGN-1263 marker requirement)
"""DGN-1507 -- two real false positives measured 2026-09-16.

Both are mechanical defects in attrib_gate.py's pass-1 stripping, found while
an instance's own outgoing handoffs were blocked and while reporting that
block to a peer. This file locks the fixes and documents the ONE thing not fixed here.

  G  newline-tolerant quote stripping (cause 2, cheapest, fixed first)
     G1 the same sentence must PASS regardless of where the paragraph wraps
        -- the pre-fix gate blocked the wrapped variant and passed the
        one-line variant of an IDENTICAL sentence (measured on the pre-fix
        source, see G1's docstring for the exact rc it produced)
     G2 single/double/backtick/CJK quote styles all tolerate an internal
        newline now
     G3 an unmatched quote mark still does not eat the rest of the document
        (the 300-char cap still applies)

  H  attrib-mention: <line> marker (cause 1's mitigation -- use vs mention)
     H1 a claim-shaped sentence inside the marked paragraph does not block
     H2 the marker does not blanket-exempt the rest of the document -- a
        real unmarked claim after the marked paragraph still blocks
     H3 the marker only reaches the ONE paragraph after it (next blank line
        ends the exemption), not the rest of the file
     H4 CONTRACT unchanged: exit 0 = BLOCK, exit 1 = PASS

  I  the honest limit (do not silently "fix" what was not fixed)
     I1 negation ("오너 확정은 없다") is UNCHANGED by this ticket -- CLAIM has
        no negation awareness and DGN-1507's adopted design (newline fix +
        mention marker + reason token) does not add any. This locks that the
        gate still blocks it today, so nobody reads a later behaviour change
        here as this ticket's doing.

Run: python3 routines/tests/test-dgn1507-newline-and-mention.py
     (or: pytest routines/tests/test-dgn1507-newline-and-mention.py)
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # routines/
LIB = os.path.join(ROOT, "lib")
GATE = os.path.join(LIB, "attrib_gate.py")
PY = sys.executable

sys.path.insert(0, LIB)

import attrib_gate as gate                                    # noqa: E402


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
# fixtures -- the real 2026-09-16 meta-case sentence, wrapped two ways
# --------------------------------------------------------------------------- #

ONE_LINE_QUOTE = (
    '본문 두 번 다 부정문 오탐이었다("오너 확정은 없다" 를 확정 주장으로 잡았다)\n'
)
WRAPPED_QUOTE = (
    '본문 두 번 다 부정문 오탐이었다("오너 확정은\n없다" 를 확정 주장으로 잡았다)\n'
)


def test_G1_same_sentence_passes_regardless_of_wrap_point():
    rc_one, err_one = _run_gate(ONE_LINE_QUOTE)
    rc_wrap, err_wrap = _run_gate(WRAPPED_QUOTE)
    assert rc_one == 1, "one-line quote must PASS: %s" % err_one
    assert rc_wrap == 1, (
        "THE REGRESSION GUARD -- wrapped quote must PASS same as one-line: %s"
        % err_wrap
    )


def test_G2_all_quote_styles_tolerate_an_internal_newline():
    styles = [
        ('"오너 확정은\n없다"', '"..."'),
        ("'오너 확정은\n없다'", "'...'"),
        ("`오너 확정은\n없다`", "`...`"),
        ("「오너 확정은\n없다」", "CJK brackets"),
    ]
    for quoted, label in styles:
        body = "인용: %s 라고 썼었다.\n" % quoted
        stripped = gate._strip_quoted(body)
        assert not gate.CLAIM.search(stripped), (
            "%s did not strip across the newline: %r" % (label, stripped))


def test_G3_unmatched_quote_does_not_eat_the_rest_of_the_document():
    body = (
        '따옴표 하나 " 만 있고 안 닫힌다.\n\n'
        '형님이 확정했다. 출처 없음.\n'
    )
    stripped = gate._strip_quoted(body)
    assert gate.SUMMARY.search(stripped) or gate.CLAIM.search(stripped), (
        "an unpaired quote character must not silently strip the real "
        "unquoted claim that follows: %r" % stripped)


# --------------------------------------------------------------------------- #
# H -- attrib-mention marker
# --------------------------------------------------------------------------- #

MENTION_BODY = (
    "# 보고\n\n"
    "attrib-mention: 아래는 게이트가 잡는 주장 형식에 대한 서술이다, 실제 주장이 아니다\n"
    "형님이 확정했다 같은 문장이 CLAIM 패턴에 걸려서 오탐이 난다.\n"
    "게이트 얘기를 하면 게이트에 걸리는 구조다.\n"
)


def test_H1_claim_inside_marked_paragraph_does_not_block():
    rc, err = _run_gate(MENTION_BODY)
    assert rc == 1, "attrib-mention paragraph must not block: %s" % err


def test_H2_marker_does_not_blanket_exempt_the_rest_of_the_document():
    body = MENTION_BODY + "\n형님 승인 받았으니 이대로 간다.\n"
    rc, err = _run_gate(body)
    assert rc == 0, "a real unmarked claim after the marker must still block"
    assert "출처 표지가 없다" in err


def test_H3_exemption_ends_at_the_next_blank_line():
    body = (
        "attrib-mention: 서술 시작\n"
        "이 줄은 서술이다.\n"
        "\n"
        "형님 승인 받았으니 이대로 간다.\n"
    )
    rc, err = _run_gate(body)
    assert rc == 0, "the claim AFTER the blank line is outside the marked paragraph"
    assert "출처 표지가 없다" in err


def test_H4_exit_code_contract_unchanged():
    rc, _ = _run_gate("평범한 문장. 아무 주장 없음.\n")
    assert rc == 1
    rc, _ = _run_gate("형님 승인 받았으니 이대로 간다.\n")
    assert rc == 0


# --------------------------------------------------------------------------- #
# I -- the honest limit: what this ticket does NOT fix
# --------------------------------------------------------------------------- #

def test_I1_negation_now_handled_by_dgn1542():
    """DGN-1507 locked design = newline fix + mention marker + reason token;
    it did NOT add negation awareness, and the original version of this test
    locked the BLOCK so a later behaviour change could never be silently
    attributed to DGN-1507. That change has now happened, on the record:
    DGN-1542 (2026-09-18) added clause-mood reading (negation / deontic /
    pending-slot voids) to attrib_gate.py, so this honest disclosure of
    absence now PASSES. The full before/after matrix lives in
    test-dgn1542-mood-and-negation.py; this test keeps the attribution
    straight -- the flip is DGN-1542's doing, not DGN-1507's."""
    rc, err = _run_gate("지금 시점에 오너 확정은 없다. 승인이 나오면 진행한다.\n")
    assert rc == 1, (
        "negation-voided disclosure of absence must PASS since DGN-1542: %s"
        % err)


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
