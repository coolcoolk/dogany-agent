#!/usr/bin/env python3
"""Owner-attribution gate for outgoing handoffs.

WHY THIS EXISTS
  2026-09-07 -- an agent wrote "the owner already answered this" to a peer
  agent, quoting a real verbatim. The verbatim was real; the claim that it
  ANSWERED THIS QUESTION was the sender's reading. The peer inherited it as an
  owner confirmation and reported it back to the owner as "what you decided".
  The owner got his own words returned as a decision he never made. (DGN-1323)

  Discipline alone does not fix this: the sender landed the governing clause
  into the canonical doctrine and broke it in prose two hours later.

THE PREDICATE
  The naive predicate (time marker + utterance word) PASSES the exact message
  that caused the incident -- because that message HAD both. The failure was not
  a missing source. It was a real quote with an unmarked seam between the quote
  and the conclusion drawn from it.

  So the predicate is tightened: a summary that ASSERTS the owner answered or
  decided is blocked unless the body carries explicit field separation.

  When more than one instance carries a copy of this file, a rule spanning
  copies owes either a single site or a machine comparison (2026-09-07
  amendment A3-iii(c)). routines/lib/attrib_gate_parity.py is that comparison:
  it compares two copies by (role, pattern body) pairs -- name comparison alone
  reported 3 phantom diffs and missed the one real one.

DGN-1331 -- CLAIM TYPE DECIDES THE CANONICAL SOURCE
  DGN-993 asks for a source and never asks WHICH source. Four recurrences in one
  day all cited a source and all cited the wrong KIND (single-worktree grep read
  as "the repo", run logs read as a schedule, a filename list read as a call
  site, a set difference read as "undecidable"). The table lives in
  routines/lib/claim_source_table.py -- one site, every copy reads it.

  TWO PASSES OVER DIFFERENT TEXT (locked, DGN-1331 sec 3):
      pass 1  claim detection    _strip_quoted(body)   -- a quoted claim is a citation
      pass 2  source detection   RAW body              -- citations LIVE in the
                                                         fences/indents/backticks
                                                         that pass 1 removes
  Measured on 12 outgoing messages of one day: 9 carried citations in raw, 3
  survived stripping. One pass for both predicates would have blocked 6 sendable
  documents -> habitual bypass -> dead gate (a prior gate died this way in 25
  days). The two predicates address complementary sets of the text; they cannot
  share one input.

  BLOCK needs all three: claim type present (stripped) AND canonical source
  absent (raw) AND insufficient source present (raw).

CONTRACT
  argv[1] = path to the document body.
  exit 0 = BLOCK
  exit 1 = PASS
  Escape hatch lives in the CALLER (HANDOFF_ATTRIB_OK=<reason>) and is logged
  there. DGN-1507: the flag's VALUE must be a reason token (false-positive /
  urgent-correction / gate-report / other), not the literal `1` -- a bare
  on/off counter can only tell you "the flag fired a lot", a reason token
  tells you WHICH repair applies (false-positive -> tighten the matcher,
  urgent-correction -> lower the friction threshold, gate-report -> widen the
  exception path). This file only emits the expectation in its block
  messages below; the caller (routines/handoff-send.sh, instance-owned, not
  shipped by this template) owns validating and logging the value --
  see docs/proposals/dgn-1507/ for the reference wiring.
  Reads one file, writes nothing.

NO EXCEPTION CLAUSE
  A document that DISCUSSES this failure mode always looks like the failure mode
  -- the vocabulary of the thought is the vocabulary of the predicate.
  Retrospectives, tickets and handoffs are the top match source and they are NOT
  false positives. The prescription is format, not an exemption: attach
  (date, verbatim, source id) to the sentence that names the incident. Measured
  to work four times on the day the gate landed.

FLEET NOTE (ported to agents/.template 2026-09-08)
  Nothing here reads an instance path, a ticket ledger, or a peer directory.
  The seam-marker predicate used to hardcode one agent's own slug
  (`<that-agent>-reading:`); it now accepts `<any-agent>-reading:`, so the
  same file works unchanged on every instance. Regex bodies and the
  operator-facing block messages stay in the instance language because
  that is the language of the prose being judged and of the operator
  reading the block; every comment and
  docstring is English ASCII per the framework code rule.

DGN-1507 -- TWO MORE DEFECTS, MEASURED 2026-09-16
  (1) _strip_quoted's inline-quote regexes excluded newlines
      (`[^"\n]{0,300}`), so a quote wrapped across a line break was never
      stripped and its contents reached the CLAIM/SUMMARY matchers. Measured:
      the SAME sentence passed or blocked depending only on where the
      paragraph happened to wrap. That is a mechanical bug, not a judgement --
      fixed by dropping the `\n` exclusion (quotes still stop at the next
      quote character, they just may now span lines).
  (2) A document that DISCUSSES this gate is structurally full of MENTIONS of
      the claim shape ("owner decided" as a description of what gets caught,
      not an assertion that the owner decided). CLAIM/SUMMARY cannot tell use
      from mention with a 40-char proximity regex, so a gate-report about a
      false positive routinely reproduces the false-positive shape and gets
      blocked by the very defect it is reporting. The `attrib-mention:`
      marker (below) is the fix: same species as the quote-strip exemption
      above, but it costs more to write than the bypass flag, so it resists
      casual abuse better than a blanket exemption would.

  HONEST LIMIT (say it here, not just in the ticket): this gate detects claim
  SHAPE, not claim TRUTH. Its real failure mode is not "no source cited", it
  is "judged from a quotation" -- a summary can carry every required field
  and still misread the quote it is attached to (DGN-1323's original
  incident had both a time marker and an utterance word). Nothing below
  changes that. Lowering the false-positive rate lowers the cost of citing
  correctly; it does not make correct citation happen. Do not read a falling
  block count as the problem being closed.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from claim_source_table import RULES  # noqa: E402

# An assertion that the owner settled something.
# DGN-1542: the gap is LAZY -- pair the owner word with the NEAREST decision
# word, not the farthest one 40 chars away. Greedy pairing skipped over a
# negated stem and landed on a decision word in the NEXT sentence ("오너
# 확정은 없다. 승인이 ..." paired 오너 with 승인), so the mood of the wrong
# clause got judged. Match existence is unchanged; only the pairing is.
CLAIM = re.compile(r"(형님|오너|owner)[^\n]{0,40}?(확정|결정|판정|축자|승인|지시)")

# The specific shape that caused DGN-1323: a summary asserting the owner
# ANSWERED / DECIDED. A verbatim quote nearby does not license this sentence --
# the quote is raw material, the "he answered" is the agent's judgement.
SUMMARY = re.compile(
    r"(형님|오너|owner)[가이는]?\s*(이미\s*)?"
    r"(답했|답하셨|정했|정하셨|확정했|확정하셨|말했으니|말씀하셨으니)"
)

# Explicit field separation: the seam is marked.
SEPARATED = re.compile(r"owner-verbatim\s*:", re.I)
# FLEET GENERALISATION (2026-09-08): the original single-instance version
# hardcoded this to its own agent slug, so a document written by any other
# agent could never satisfy the seam requirement and had to bypass the gate.
# `<agent>-reading:` accepts any agent slug (e.g. peer-reading, self-reading,
# agent-reading). This widens PASS, never BLOCK.
READING = re.compile(
    r"(\b[a-z][\w-]*-reading\b|내\s*읽기|나는\s*이것을)\s*:?", re.I)

TIME = re.compile(r"\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}")
UTTER = re.compile(r"발화|원문|축자|said|verbatim|대화 기록|세션에서")

# DGN-1507 design item 2. Marks the paragraph right after it as a DESCRIPTION
# of claim form (what CLAIM/SUMMARY catch), not an assertion -- e.g. a
# gate-report explaining what got blocked. Same species as the quote-strip
# exemption in _strip_quoted: it removes text from pass-1 detection, it does
# not disable the gate. Deliberately a whole marker line (not a one-char
# sigil) so it costs more to type than the HANDOFF_ATTRIB_OK bypass -- cheap
# escapes get used reflexively, this one does not.
ATTRIB_MENTION = re.compile(r"attrib-mention\s*:", re.I)

# DGN-1542 -- MOOD IS READ FROM THE CLAUSE, NOT FROM WORD PRESENCE.
# Measured 2026-09-17/18, four false positives in one lane, all the same root:
#   "그건 별도 오너 승인 건이다"          a FUTURE requirement, read as done
#   "그건 릴리스 오너 승인 게이트다"     names the gate that WILL decide
#   "오너 승인이 필요하다" 계열            deontic, read as done
#   "... 오너 미확정 PLACEHOLDER ..."     negation bound to the very stem --
#                                        the gate printed "claims 오너 미확정"
# The last one is the sharpest: the sentence DISCLOSED the absence of owner
# approval and the source-demanding gate blocked that very disclosure. A gate
# that cannot pass an honest disclosure of absence has a wrong predicate.
# So the DEFINITION is narrowed (a claim asserts a COMPLETED owner act;
# negation voids it, deontic/prospective mood voids it), instead of growing a
# suppression list -- the head session ruled that direction on 2026-09-17 for
# the claim lint (DGN-1525) and it holds here. Every void below is a CLOSED
# GRAMMATICAL CLASS (negation morphemes, deontic morphology, the nominal
# pending-slot frame), never a specimen list. Module level with enumerable
# names so the parity checker can compare bodies across copies.
#
# (1) bound negation prefix on the decision stem: 미확정 / 未승인. The token
#     itself denotes absence; checked against the single char before the stem.
NEG_PREFIX = re.compile(r"[미未]$")
# (2) clause-level negation / hedging morphemes: -지 않-, 없-, 아직, and
#     preverbal 안/못 (standalone word, so A안/B안 the proposal noun does not
#     void -- a space-preceded bare 안 still can; honest limit, widens PASS).
NEG_CLAUSE = re.compile(r"않|없|아직|(?:^|[\s(\[*_~`\"'>-])[안못]\s")
# (3) deontic / prospective mood: -어야 하-/한다/함/할/해/되-, 필요, 요건,
#     예정. The act is required or scheduled, not done.
DEONTIC_CLAUSE = re.compile(r"야\s*[하한할함해되된될돼]|필요|요건|예정")
# (4) the decision word as MODIFIER of a pending-slot noun directly after it:
#     "승인 건(이다)" / "승인 게이트다" / "확정 대상" / "승인 절차" / "승인
#     전". The act is nominalized as a thing that has not happened yet, not
#     asserted as done. 사항 is deliberately NOT in the set: "확정 사항" is
#     tense-ambiguous in Korean and usually means an already-settled item --
#     voiding it would drop a true positive class (locked in the tests).
PENDING_SLOT = re.compile(
    r"^\s*(?:건|게이트|대상|절차|전)(?=[이에은는을를의다로가]|[^가-힣]|$)")

# Clause boundary for the mood checks: sentence enders AND commas. A
# negation/deontic morpheme binds within its own phrase, and a comma marks
# the phrase edge -- measured on the real corpus: "(형님 08-03 C1 재확정,
# phasing 없음)" is a completed-act citation whose 없음 negates PHASING, not
# the reconfirmation; without the comma boundary that true positive was
# silently voided.
_CLAUSE_END = re.compile(r"[.!?。;:,，]")


def _clause_of(text: str, pos: int) -> str:
    """The comma/sentence-bounded segment containing pos, within its line.
    Mood is judged PER SEGMENT so a requirement or negation in one phrase
    cannot shield -- or void -- a claim in another (DGN-1542)."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    line = text[start:end]
    rel = pos - start
    cstart = 0
    for sm in _CLAUSE_END.finditer(line):
        if sm.start() >= rel:
            return line[cstart:sm.start()]
        cstart = sm.end()
    return line[cstart:]


def _claim_voided(text: str, m) -> bool:
    """DGN-1542: a CLAIM match stands only if its clause asserts a COMPLETED
    owner act. True = voided (negated / deontic / pending-slot nominal)."""
    kw = m.start(2)
    if NEG_PREFIX.search(text[max(0, kw - 1):kw]):
        return True
    if PENDING_SLOT.search(text[m.end(2):m.end(2) + 12]):
        return True
    clause = _clause_of(text, kw)
    return bool(NEG_CLAUSE.search(clause) or DEONTIC_CLAUSE.search(clause))


# CONFESS (DGN-1331 sec 2). Measured twice on 2026-09-07 that CLAIM also fires
# on RETROSPECTIVE self-accusation: "I inherited your reading as the owner's
# decision" confesses a past error, it does not assert an owner decision.
# Assertion vs confession is the real axis. Without this, every retraction and
# every error admission counted as a claim. Module level with an enumerable name
# and a comparable body, so the parity checker can compare it.
CONFESS = re.compile(
    r"승계했|착각했|틀렸|오류|잘못|실수|안 했|못 했|하지 않았|어겼|물린다|철회")


def _strip_quoted(text: str) -> str:
    """Drop blockquotes, code fences and indented blocks.

    WHY (2026-09-07, same day as the gate): the first message that DISCUSSED the
    incident was blocked by this gate on both sides -- because a retrospective
    quotes the bad sentence. Both senders reached for the escape hatch. A gate
    that fires on every retrospective teaches habitual bypass, and a gate that is
    habitually bypassed is a dead gate (measured in this estate: a prior gate,
    dead for 25 days).

    A claim inside a quote is a CITATION, not an assertion. Only unquoted prose
    can attribute a decision to the owner.

    DGN-1331: this function is pass 1 ONLY. Source detection must never run on
    its output -- the citations it is looking for are exactly what this removes.

    DGN-1507 design item 2: an `attrib-mention:` marker line and the
    paragraph immediately following it (until the next blank line) are
    dropped here too -- a description of claim FORM, not a claim.
    """
    out = []
    in_fence = False
    in_mention = False
    for line in text.splitlines():
        st = line.lstrip()
        if st.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if ATTRIB_MENTION.search(st):
            in_mention = True
            continue
        if in_mention:
            if not st:
                in_mention = False
            else:
                continue
        if st.startswith(">"):
            continue
        if line.startswith("    ") or line.startswith("\t"):
            continue
        out.append(line)
    text = "\n".join(out)
    # Inline quotes are citations too. Found the hard way (2026-09-07, third
    # repeat of the same shape): block-only stripping passed 5 synthetic
    # fixtures while the REAL blocked document stayed blocked -- its claim sat in
    # an inline quote. Strip "..." '...' `...` and the CJK quote marks.
    #
    # DGN-1507 cause 2: the character classes below used to exclude `\n`
    # (`[^"\n]{0,300}`), so a quote wrapped across a line break was never
    # stripped and the same sentence passed or blocked depending only on
    # where the paragraph happened to wrap (measured 2026-09-16). Dropping
    # the `\n` exclusion lets a quote span lines; it still stops at the next
    # matching quote character, so this widens PASS, never BLOCK.
    text = re.sub(r'"[^"]{0,300}"', " ", text)
    text = re.sub(r"'[^']{0,300}'", " ", text)
    text = re.sub(r"`[^`]{0,300}`", " ", text)
    text = re.sub(r"[“”‘’「」『』]"
                  r"[^“”‘’「」『』]{0,300}"
                  r"[“”‘’「」『』]", " ", text)
    return text


def _line_of(text: str, pos: int) -> str:
    """The single line containing pos -- confession is judged per line, not per doc."""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:] if end == -1 else text[start:end]


def pair_violations(stripped: str, raw: str):
    """DGN-1331 two-pass check. Returns [(rule, matched_claim_text), ...].

    stripped -> claim detection (pass 1). raw -> source detection (pass 2).
    Never swap them and never pass one text for both: the citations that satisfy
    `required` live in the regions `stripped` has already deleted.

    A rule fires only when ALL THREE hold:
      claim type present, canonical source ABSENT, insufficient source PRESENT.
    Canonical source present -> PASS for that rule even if an insufficient source
    is also cited (citing a run log next to the plist is not an error).
    """
    hits = []
    for rule in RULES:
        m = rule.claim.search(stripped)
        if not m:
            continue
        if rule.required.search(raw):
            continue
        if not rule.forbidden.search(raw):
            continue
        hits.append((rule, m.group(0).strip()))
    return hits


def main() -> int:
    raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
    body = _strip_quoted(raw)

    if SUMMARY.search(body) and not (SEPARATED.search(body) and READING.search(body)):
        sys.stderr.write(
            "차단: 오너가 '답했다/정했다'로 요약했는데 인용과 판정의 이음매가 표시되지 않았다\n"
        )
        sys.stderr.write("  주장: %s\n" % SUMMARY.search(body).group(0).strip())
        sys.stderr.write(
            "  필요: 'owner-verbatim:' (형님이 실제로 한 말) + "
            "'<agent>-reading:' (내가 그것을 무엇으로 읽는지) 두 필드 분리\n"
        )
        sys.stderr.write("  사유: DGN-1323 -- 인용은 참이어도 요약은 내 판정이다\n")
        sys.stderr.write(
            "  우회: HANDOFF_ATTRIB_OK=<사유> (false-positive/urgent-correction/"
            "gate-report/other -- 빈 값이면 other 로 기록됨. =1 아님, DGN-1507)\n"
        )
        return 0

    # DGN-1331: claim type vs source kind. Two passes -- claim on stripped,
    # source on raw.
    pairs = pair_violations(body, raw)
    if pairs:
        sys.stderr.write("차단: 주장 유형에 맞는 정본 출처가 인용에 없다\n")
        for rule, text in pairs[:3]:
            sys.stderr.write("  주장: %s  [유형: %s]\n" % (text, rule.claim_label))
            sys.stderr.write("  필요: %s -- 이 출처를 열고 인용할 것\n" % rule.canonical)
            sys.stderr.write("  사유: 인용된 출처가 %s 뿐이다 (DGN-1331 -- 출처가 아니라 "
                             "출처와 주장유형의 정합이 없다)\n" % rule.insufficient)
        sys.stderr.write(
            "  우회: HANDOFF_ATTRIB_OK=<사유> (false-positive/urgent-correction/"
            "gate-report/other -- 빈 값이면 other 로 기록됨. =1 아님, DGN-1507)\n"
        )
        return 0

    hits = [m.group(0).strip() for m in CLAIM.finditer(body)
            if not _claim_voided(body, m)
            and not CONFESS.search(_line_of(body, m.start()))]
    if not hits:
        return 1
    if TIME.search(body) and UTTER.search(body):
        return 1

    sys.stderr.write("차단: 오너 확정을 주장하는데 출처 표지가 없다\n")
    for h in hits[:3]:
        sys.stderr.write("  주장: %s\n" % h)
    sys.stderr.write("  필요: 언제 한 발화인지(시각/날짜) + '발화/축자/원문' 표기\n")
    sys.stderr.write("  우회: HANDOFF_ATTRIB_OK=1 (사유가 있을 때만)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
