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
  Escape hatch lives in the CALLER (HANDOFF_ATTRIB_OK=1) and is logged there.
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
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from claim_source_table import RULES  # noqa: E402

# An assertion that the owner settled something.
CLAIM = re.compile(r"(형님|오너|owner)[^\n]{0,40}(확정|결정|판정|축자|승인|지시)")

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
    """
    out = []
    in_fence = False
    for line in text.splitlines():
        st = line.lstrip()
        if st.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
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
    text = re.sub(r'"[^"\n]{0,300}"', " ", text)
    text = re.sub(r"'[^'\n]{0,300}'", " ", text)
    text = re.sub(r"`[^`\n]{0,300}`", " ", text)
    text = re.sub(r"[“”‘’「」『』]"
                  r"[^“”‘’「」『』\n]{0,300}"
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
        sys.stderr.write("  우회: HANDOFF_ATTRIB_OK=1 (사유가 있을 때만)\n")
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
        sys.stderr.write("  우회: HANDOFF_ATTRIB_OK=1 (사유가 있을 때만)\n")
        return 0

    hits = [m.group(0).strip() for m in CLAIM.finditer(body)
            if not CONFESS.search(_line_of(body, m.start()))]
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
