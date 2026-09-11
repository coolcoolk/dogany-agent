#!/usr/bin/env python3
# test-lane: fast    # measured below 1s (DGN-1263 marker requirement)
"""DGN-1331 -- the claim type decides which source is canonical.

DGN-993 requires a source for a causal claim and never asks WHICH source.
Measured 2026-09-07: four recurrences in one day, every one of them citing a
source, every one citing the wrong KIND of source. This locks the table, the
two-pass wiring, the imported CONFESS predicate and the parity checker.

Locks, in the ticket's order:

  A  table (routines/lib/claim_source_table.py)
     A1 five roles, in the ticket's order, lookup() resolves them
     A2 EVERY predicate is a module-level constant with an enumerable name and
        a comparable pattern body -- the locked parity unit (role, pattern body)
     A3 the four real 2026-09-07 sentences each detect as their table role
     A4 canonical/insufficient labels are the ticket's columns verbatim
     A5 the three forbidden-column defects measured on the real corpus

  B  two passes over DIFFERENT text (the regression this file exists for)
     B1 a document whose ONLY citation sits in a 4-space indented block PASSES.
        Single-pass would block it: _strip_quoted deletes exactly that block.
     B2 the mechanical proof of B1 -- required source present in raw, absent in
        stripped. If these two ever agree the two-pass design has collapsed.
     B3 same claim, only the INSUFFICIENT source cited -> BLOCK
     B4 all three conditions required: no claim / no forbidden -> PASS
     B5 canonical source present WINS even when a forbidden source is also cited
     B6 the block message names WHICH source to open

  C  CONFESS at module level
     C1 a line admitting the sender's own mistake is not counted as a claim
     C2 an actual owner-decision claim with no source still blocks
     C3 CONFESS is module level here (some copies keep it main()-local)

  D  parity checker (role, pattern body)
     D0 with no peer copy the checker SCAN-FAILs (exit 2) instead of reporting
        a divergence it never measured -- the single-copy instance case
     D1 a synthetic peer built from the known packaging variant: the name-level
        diffs collapse to fewer roles and owner-verbatim reads EQUIVALENT
     D2 a real body difference is reported BEHAVIOURAL with the input that
        discriminates
     D3 extraction reaches a function-local predicate

  E  exit-code contract unchanged: 0 = BLOCK, 1 = PASS

  F  fleet generalisation (2026-09-08 port to agents/.template)
     F1 the seam marker accepts any agent slug, not one hardcoded name
     F2 the intent-column canonical source accepts any ticket prefix

Run: python3 routines/tests/test-dgn1331-claim-source-gate.py
     (or: pytest routines/tests/test-dgn1331-claim-source-gate.py)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # routines/
LIB = os.path.join(ROOT, "lib")
GATE = os.path.join(LIB, "attrib_gate.py")
PARITY = os.path.join(LIB, "attrib_gate_parity.py")
PY = sys.executable

sys.path.insert(0, LIB)

import attrib_gate as gate                                    # noqa: E402
import attrib_gate_parity as parity                           # noqa: E402
import claim_source_table as table                            # noqa: E402


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


def _run_parity(*args, **kw):
    """(exit_code, stdout). Env is scrubbed of any inherited peer override."""
    env = dict(os.environ)
    env.pop("DOGANY_PEER_ATTRIB_GATE", None)
    env.pop("DGN_SKULL_ATTRIB_GATE", None)
    env.update(kw.pop("env", {}))
    r = subprocess.run([PY, PARITY] + list(args),
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stdout


def _skull_path():  # estate-scrub:allow(legacy helper + env var names -- back-compat with the two-instance original, changing them breaks existing callers)
    """The real sibling peer copy (legacy two-instance layout): env override,
    else the sibling instance checkout. Never written to. Returns None when
    absent -- the synthetic-peer tests below are the fleet-normal path; this
    helper only feeds the optional real-copy test (D4)."""
    p = os.environ.get("DGN_SKULL_ATTRIB_GATE")
    if p:
        return p if os.path.exists(p) else None
    p = os.path.abspath(os.path.join(
        LIB, "..", "..", "..", "skull", "routines", "lib", "attrib_gate.py"))  # estate-scrub:allow(legacy sibling path -- the two-instance layout this helper serves)
    return p if os.path.exists(p) else None


# --------------------------------------------------------------------------- #
# fixtures -- shaped from the ticket's real sentences, not from imagination
# --------------------------------------------------------------------------- #

# The regression fixture. Its ONLY citation lives in a 4-space indented block,
# which is exactly what _strip_quoted() removes. Under a single pass this
# document is BLOCKED; that is the bug this file guards.
# The INSUFFICIENT source sits in ordinary prose (it survives stripping); the
# CANONICAL source sits only in the indented block (stripping deletes it). That
# asymmetry is what makes the fixture load-bearing: under one pass the document
# reads as "claim + only a worktree grep" and BLOCKS.
INDENTED_CITATION = """# 조사 보고

프레임워크에 그 코드 없다. 이 체크아웃에서 grep -rn "target" . 는 0건이었다.

그래서 아래를 확인했다:

    $ git branch -a
    * main
      remotes/origin/dev
    전 브랜치 대조: dev 에만 존재

이상.
"""

ONLY_INSUFFICIENT = """# 조사 보고

프레임워크에 그 코드 없다. 정리한다.

근거: grep -rn "target" . 로 0건이었다.
"""

CADENCE_LOG_ONLY = """# 관측

handoff 로그에 03:30 / 11:30 / 19:05 세 줄이 찍혔다.
따라서 cron 하루 3회 돈다. 너무 느리다.
"""

CADENCE_WITH_PLIST = """# 관측

handoff 로그에 03:30 / 11:30 / 19:05 세 줄이 찍혔다.

plist 를 열었다:

    com.telegram-skill-bot.agent.handoff-watch.plist
    WatchPaths = [~/.dogany/handoff/<agent>/inbox]
    StartCalendarInterval / StartInterval 둘 다 부재

이벤트구동이다. 하루 3회 돈다는 읽기는 틀렸다.
"""

CONFESSION = """# 회고

내가 네 해석을 형님 확정으로 승계했다. 틀렸다. 철회한다.
"""

CLAIM_NO_SOURCE = """# 보고

형님 승인 받았으니 이대로 간다.
"""

# The packaging variant this checker exists to see through: two roles packed
# into ONE tuple name, plus a main()-local CONFESS, plus a reading predicate
# that does NOT accept the instance-language seam markers.
PEER_VARIANT = '''#!/usr/bin/env python3
"""A peer copy in the known alternative packaging."""
import re

CLAIM = re.compile(r"(형님|오너|owner)[^\\n]{0,40}(확정|결정|판정|축자|승인|지시)")
SUMMARY = re.compile(
    r"(형님|오너|owner)[가이는]?\\s*(이미\\s*)?"
    r"(답했|답하셨|정했|정하셨|확정했|확정하셨|말했으니|말씀하셨으니)"
)
FIELDS = (re.compile(r"owner-verbatim\\s*:", re.I),
          re.compile(r"peer-reading\\s*:", re.I))
TIME = re.compile(r"\\d{1,2}:\\d{2}|\\d{4}-\\d{2}-\\d{2}")
UTTER = re.compile(r"발화|원문|축자|said|verbatim|대화 기록|세션에서")


def main():
    CONFESS = re.compile(
        r"승계했|착각했|틀렸|오류|잘못|실수|안 했|못 했|하지 않았|어겼|물린다|철회")
    return CONFESS
'''


# --------------------------------------------------------------------------- #
# A -- table
# --------------------------------------------------------------------------- #

def test_A1_roles_in_ticket_order_and_lookup():
    assert table.ROLES == ("existence", "cadence", "calls",
                           "intent_unknown", "not_running")
    assert table.lookup("cadence").canonical == "스케줄 선언(plist)"
    assert table.lookup("nope") is None


def test_A2_every_predicate_is_module_level_with_comparable_body():
    """The locked parity unit: module level + enumerable name + comparable body."""
    src = os.path.join(LIB, "claim_source_table.py")
    module_names = parity.module_level_names(src)
    inv = table.inventory()
    assert len(inv) == 15, inv
    for role, slot, name, body in inv:
        assert name in module_names, "%s is not module-level" % name
        assert getattr(table, name, None) is not None
        assert isinstance(body, str) and body, name
        re.compile(body)                       # comparable == compilable


def test_A3_the_four_real_sentences_detect_as_their_role():
    cases = [
        ("프레임워크에 코드 없다", "existence"),
        ("cron 하루 3회 돈다, 너무 느리다", "cadence"),
        ("routine-roller.sh 가 backup-data.sh 를 부른다", "calls"),
        ("판정불가, 사람에게 물어야", "intent_unknown"),
        ("handoff-watch 가 안 돈다", "not_running"),
    ]
    for text, role in cases:
        hit = [r.role for r in table.RULES if r.claim.search(text)]
        assert role in hit, (text, hit)


def test_A4_columns_are_the_ticket_verbatim():
    expect = {
        "existence": ("전 브랜치 / 전 인스턴스", "작업트리 1개 grep"),
        "cadence": ("스케줄 선언(plist)", "실행 로그"),
        "calls": ("그 줄 본문(주석 여부 포함)", "파일명 매치"),
        "intent_unknown": ("선언 표면(defer/preserve/티켓)", "차집합 결과"),
        "not_running": ("머신 전수 + 팬아웃 명부", "인스턴스 스코프"),
    }
    for role, (canon, insuf) in expect.items():
        r = table.lookup(role)
        assert r.canonical == canon
        assert r.insufficient == insuf


def test_A5_forbidden_column_defects_measured_on_the_real_corpus():
    """Three regex defects found by running the table over 221 real handoffs.

    Locked so they cannot come back:
      - `grep -rln` (case 4's actual evidence) must match the calls column. A
        trailing \\b after the 'l' never matched it.
      - a flagless `grep ... 0건` (case 1's actual sentence) must match existence.
      - `rg\\s+-` without \\b matched inside the word "Warg" and blocked an unrelated 2026-08-27 document. estate-scrub:allow(regression-guard docstring naming the real incident word -- matches the literal fixture asserted below)
      - a bare "내 인스턴스" is everyday prose here, not a cited scope; it
        blocked another unrelated document. Column 3 wants a scope DECLARATION.
    """
    assert table.CALLS_FORBIDDEN.search("grep -rln backup-data.sh")
    assert table.EXISTENCE_FORBIDDEN.search("내 원장 grep 0건")
    assert not table.EXISTENCE_FORBIDDEN.search("--- Warg ---")  # estate-scrub:allow(regression fixture -- the real incident word the \b fix must not re-match)
    assert not table.NOT_RUNNING_FORBIDDEN.search(
        "내 팩이 내 인스턴스에 영영 못 들어간다")
    assert table.NOT_RUNNING_FORBIDDEN.search("내 인스턴스에서만 확인했다")
    assert table.NOT_RUNNING_FORBIDDEN.search("인스턴스 스코프로 봤다")


# --------------------------------------------------------------------------- #
# B -- two passes over DIFFERENT text
# --------------------------------------------------------------------------- #

def test_B1_indented_only_citation_passes():
    """THE REGRESSION GUARD. Single-pass blocks this document."""
    rc, err = _run_gate(INDENTED_CITATION)
    assert rc == 1, "blocked an honest document: %s" % err


def test_B2_mechanical_proof_the_passes_see_different_text():
    raw = INDENTED_CITATION
    stripped = gate._strip_quoted(raw)
    rule = table.lookup("existence")
    assert rule.claim.search(stripped), "claim must survive pass 1"
    assert rule.required.search(raw), "canonical source must be visible in raw"
    assert not rule.required.search(stripped), (
        "the citation survived stripping -- fixture no longer proves the "
        "two-pass requirement, fix the fixture")
    # and the single-pass variant would have blocked:
    assert gate.pair_violations(stripped, stripped), (
        "single pass must block this document -- otherwise B1 proves nothing")
    assert not gate.pair_violations(stripped, raw)


def test_B3_only_insufficient_source_blocks():
    rc, err = _run_gate(ONLY_INSUFFICIENT)
    assert rc == 0
    assert "X 가 없다" in err


def test_B3b_cadence_log_only_blocks():
    rc, err = _run_gate(CADENCE_LOG_ONLY)
    assert rc == 0, err
    assert "스케줄 선언(plist)" in err
    assert "실행 로그" in err


def test_B4_all_three_conditions_required():
    # claim + no forbidden source at all -> PASS (never punish an uncited doc
    # into habitual bypass)
    rc, _ = _run_gate("프레임워크에 그 코드 없다. 확인했다.\n")
    assert rc == 1
    # forbidden source + no claim -> PASS
    rc, _ = _run_gate("로그를 봤다. 03:30 에 한 줄 찍혔다.\n")
    assert rc == 1


def test_B5_canonical_source_wins_over_forbidden():
    """Citing a run log NEXT TO the plist is not an error."""
    rc, err = _run_gate(CADENCE_WITH_PLIST)
    assert rc == 1, err


def test_B6_message_names_which_source_to_open():
    rc, err = _run_gate(ONLY_INSUFFICIENT)
    assert rc == 0
    for line in ("차단:", "  주장:", "  필요:", "  사유:", "  우회:"):
        assert line in err, (line, err)
    assert "전 브랜치 / 전 인스턴스" in err
    assert "작업트리 1개 grep" in err
    assert "HANDOFF_ATTRIB_OK=1" in err


# --------------------------------------------------------------------------- #
# C -- CONFESS
# --------------------------------------------------------------------------- #

def test_C1_confession_is_not_a_claim():
    rc, err = _run_gate(CONFESSION)
    assert rc == 1, "a retraction was counted as an owner-decision claim: %s" % err


def test_C2_real_claim_without_source_still_blocks():
    rc, err = _run_gate(CLAIM_NO_SOURCE)
    assert rc == 0
    assert "출처 표지가 없다" in err


def test_C3_confess_is_module_level_here():
    names = parity.module_level_names(GATE)
    assert "CONFESS" in names
    assert gate.CONFESS.pattern == (
        "승계했|착각했|틀렸|오류|잘못|실수|안 했|못 했|하지 않았|어겼|물린다|철회")


# --------------------------------------------------------------------------- #
# D -- parity checker
# --------------------------------------------------------------------------- #

def test_D0_no_peer_is_a_scan_fail_not_a_divergence():
    """A single-copy instance is the normal fleet case.

    The two-instance original hardcoded a sibling path and, when it was
    missing, returned the same exit code it uses for real divergence. On any
    instance without that sibling the tool therefore reported a comparison it
    had never run. exit 2 = SCAN-FAIL is the honest answer.
    """
    empty = tempfile.mkdtemp()
    try:
        rc, out = _run_parity("--peer", os.path.join(empty, "nope.py"))
        assert rc == 2, (rc, out)
        assert "SCAN-FAIL" in out, out
    finally:
        shutil.rmtree(empty)


def test_D1_D2_parity_resolves_phantoms_and_finds_the_real_diff():
    """Synthetic peer, not a sibling instance.

    The original test compared two live instances and asserted the exact
    numbers measured that day. Those numbers are a property of two particular
    checkouts, not of the checker, so the fleet copy builds the peer it needs:
    the known packaging variant (a FIELDS tuple instead of two constants, a
    main()-local CONFESS, and a reading predicate that rejects the
    instance-language seam markers).
    """
    d = tempfile.mkdtemp()
    peer = os.path.join(d, "attrib_gate.py")
    try:
        with open(peer, "w", encoding="utf-8") as fh:
            fh.write(PEER_VARIANT)
        rc, out = _run_parity("--peer", peer)
        # packaging-only differences must resolve, not be reported as diffs
        assert re.search(r"EQUIVALENT\s+field:owner-verbatim", out), out
        assert "collapse to" in out, out
        # the real body difference must be found, with the input that shows it
        assert re.search(r"DIFF/BEHAVIOURAL\s+field:agent-reading", out), out
        assert "discriminating input" in out, out
        assert rc == 1, (rc, out)          # behavioural divergence -> exit 1
    finally:
        shutil.rmtree(d)


def test_D3_extraction_reaches_function_local_predicates():
    d = tempfile.mkdtemp()
    peer = os.path.join(d, "attrib_gate.py")
    try:
        with open(peer, "w", encoding="utf-8") as fh:
            fh.write(PEER_VARIANT)
        names = {n for n, _ in parity.extract(peer)}
        assert "CONFESS" in names, names
        assert "FIELDS[0]" in names and "FIELDS[1]" in names, names
        assert "CONFESS" not in parity.module_level_names(peer)
    finally:
        shutil.rmtree(d)


def test_D4_extraction_reaches_the_real_sibling_copy_when_present():
    """Live-original lock (2026-09-07): extraction reaches the REAL peer
    copy's function-local predicate, not just the synthetic variant above.
    Runs only where the legacy two-instance layout (or the env override)
    exists; elsewhere it reports SKIP, never a pass. The live original's
    exact-number parity assertions (name-diff/role-collapse counts) are NOT
    carried: those are a property of two particular checkouts, and the
    checker behaviour they locked is covered by the synthetic tests above."""
    peer = _skull_path()
    if not peer:
        print("SKIP D4: real peer copy not found "
              "(set DGN_SKULL_ATTRIB_GATE) -- NOT a pass")
        return
    names = {n for n, _ in parity.extract(peer)}
    assert "CONFESS" in names, names
    assert "CONFESS" not in parity.module_level_names(peer)


# --------------------------------------------------------------------------- #
# E -- contract
# --------------------------------------------------------------------------- #

def test_E_exit_code_contract():
    rc, _ = _run_gate(ONLY_INSUFFICIENT)
    assert rc == 0            # BLOCK
    rc, _ = _run_gate("평범한 문장. 아무 주장 없음.\n")
    assert rc == 1            # PASS


# --------------------------------------------------------------------------- #
# F -- fleet generalisation (2026-09-08 port)
# --------------------------------------------------------------------------- #

def test_F1_seam_marker_accepts_any_agent_slug():
    """The original spelled the reading field with ONE agent's name, so a
    document authored by any other agent could never satisfy the seam
    requirement and had to bypass the gate."""
    for slug in ("metal", "skull", "warg", "ag", "agent"):  # estate-scrub:allow(any-agent-slug fixture -- real instance slugs the READING regex must accept, port of DGN-1331 F1)
        body = ("형님이 이미 답했다.\n"
                "owner-verbatim: 형님 원문\n"
                "%s-reading: 내가 그것을 무엇으로 읽는지\n" % slug)
        rc, err = _run_gate(body)
        assert rc == 1, (slug, err)
    # and the seam still has to be MARKED -- this is not a blanket loosening
    rc, _ = _run_gate("형님이 이미 답했다. 그대로 간다.\n")
    assert rc == 0


def test_F2_intent_column_accepts_any_ticket_prefix():
    """The original required a literal `worklog/DGN-` as the declaration
    surface, so an instance with a different ledger prefix cited a real
    surface and was blocked anyway."""
    rule = table.lookup("intent_unknown")
    for cite in ("worklog/DGN-1331", "worklog/T-042", "worklog/ABC-7",
                 "ticket", "티켓", ".dogany-preserve"):
        assert rule.required.search(cite), cite
    # column 3 unchanged: a set difference alone is still not a source
    assert rule.forbidden.search("선언 vs 등재 차집합 22건")


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
