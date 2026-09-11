#!/usr/bin/env python3
"""Claim-type -> canonical-source table (DGN-1331). Pure data + lookup. No I/O.

WHY THIS EXISTS
  DGN-993 requires a source for every causal claim. It does not look at WHICH
  source. Measured 2026-09-07 (4 recurrences in one day, across two instances):
  every one of them CITED a source and every one cited the WRONG KIND of source.

    case 1  one worktree, grep 0 hits    -> "the framework has no such code"
            (actual: it exists on the dev branch; the grepped checkout was main)
    case 2  run log at 03:30/11:30/19:05 -> "the cron fires 3x a day"
            (actual: the plist declares WatchPaths and no StartInterval at all
             -- purely event driven)
    case 3  declared-vs-registered diff  -> "undecidable, ask a human"
            (actual: already declared in plists.defer and a preserve comment)
    case 4  `grep -rln` filename list    -> "A calls B"
            (actual: one comment line, zero call sites; the line was never read)

  So the missing half of DGN-993 is not "cite a source", it is
  "the claim type decides which source is canonical".

  Column three of the table (the INSUFFICIENT source) is half the predicate, not
  decoration: the block fires on claim-type AND canonical-source-absent AND
  insufficient-source-present. Requiring only column two would block every honest
  document that has not yet cited anything, and a gate that fires on everything
  is bypassed into a dead gate (measured: a prior gate, dead for 25 days).

STRUCTURE REQUIREMENT (DGN-1331 sec 2, locked)
  Every predicate is MODULE LEVEL, with an enumerable name and a comparable
  regex body. The parity comparison unit is the (role, pattern body) pair.

  Name-level comparison alone is known-bad: it reported 3 phantom diffs between
  two attrib_gate copies (SEPARATED+READING vs FIELDS are the same two roles in
  different packaging) while missing the one real behavioural diff. See
  routines/lib/attrib_gate_parity.py.

TWO PASSES (DGN-1331 sec 3, locked)
  claim detection   -> _strip_quoted(body)   (a claim inside a quote is a citation)
  source  detection -> raw body              (citations LIVE in fences/indents/backticks)
  Measured on 12 outgoing messages of one day: 9 carried citations in raw, only 3
  survived stripping. Running both predicates over one text would have blocked 6
  sendable documents. The two predicates address complementary sets of the text.
  This module only supplies the pairs; attrib_gate.py runs the two passes.

FLEET NOTE (ported to agents/.template 2026-09-08)
  The regex bodies below are written in the instance language (Korean) because
  that is the language the prose being judged is written in. That is data, not
  authored prose: a detector for Korean sentences cannot be spelled in ASCII.
  All comments and docstrings are English ASCII per the framework code rule.
  On an instance whose working language is not Korean the Korean alternatives
  simply never match and the English alternatives still do -- the module is
  inert, never broken.

  Nothing here reads the filesystem, an instance path, or a ticket ledger.
  The one former instance-ism (a literal `worklog/DGN-` prefix in
  INTENT_REQUIRED) is generalised to any ticket-id shape, so an instance whose
  ledger uses a different prefix is not punished for citing it.
"""
import re

# --------------------------------------------------------------------------- #
# Role 1 -- "X does not exist"
#   canonical    all branches / all instances
#   insufficient a grep in one worktree
# --------------------------------------------------------------------------- #
EXISTENCE_CLAIM = re.compile(
    r"(코드|파일|함수|구현|정의|술어|규칙|패턴|필드|컬럼|스크립트|엔트리|항목|기능)"
    r"[^\n]{0,24}?(가|이|은|는|도)?\s*"
    r"(없다|없음|없습니다|존재하지\s*않(는다|습니다)|부재다|미존재)"
    r"|(does not exist|doesn't exist|is not present|not found anywhere"
    r"|no such (file|function|code|entry))"
)
EXISTENCE_REQUIRED = re.compile(
    r"(전\s*브랜치|모든\s*브랜치|all branches|branch\s+-a|--all\b|for-each-ref"
    r"|ls-remote|전\s*인스턴스|모든\s*인스턴스|all instances|각\s*인스턴스)"
)
EXISTENCE_FORBIDDEN = re.compile(
    # \b on rg: measured 2026-09-07 that a bare `rg\s+-` matches inside an
    # instance persona name ending in "-rg" in ordinary prose and blocked an
    # unrelated document.
    # `grep ... 0 hits` is case 1's own shape (a bare, flagless grep cited as
    # proof of absence).
    r"(grep\s+-[a-zA-Z]*r|\brg\s+-|ripgrep|grep[^\n]{0,16}0\s*건"
    r"|작업트리\s*1개|한\s*체크아웃|this worktree)"
)

# --------------------------------------------------------------------------- #
# Role 2 -- "X runs on this cadence"
#   canonical    the schedule declaration (plist / timer / crontab)
#   insufficient a run log
# --------------------------------------------------------------------------- #
CADENCE_CLAIM = re.compile(
    r"(하루|매일|매시간|매분|주마다|\d+\s*(분|시간|일)\s*마다)"
    r"[^\n]{0,24}(\d+\s*회|돈다|돌고|실행된다|발화|주기)"
    r"|(주기가|주기로|간격으로)[^\n]{0,16}(돈다|돌고|실행)"
    r"|(cron|launchd|스케줄)[^\n]{0,24}\d+\s*회"
    r"|runs every\s+\d*\s*(minute|hour|day)"
)
CADENCE_REQUIRED = re.compile(
    r"(plist|StartCalendarInterval|StartInterval|WatchPaths|launchd|launchctl"
    r"|crontab|systemd|OnCalendar|\.timer\b|스케줄\s*선언)"
)
CADENCE_FORBIDDEN = re.compile(
    r"(\.log\b|로그\s*(에|를|에서|의|만)|실행\s*로그|handoff\s*로그|log file|log line)"
)

# --------------------------------------------------------------------------- #
# Role 3 -- "A calls B"
#   canonical    the body of that line (comment or not)
#   insufficient a filename match
# --------------------------------------------------------------------------- #
CALLS_CLAIM = re.compile(
    r"[\w./-]+\.(sh|py|js|ts|rb)\s*(가|이|은|는|를|을)?[^\n]{0,40}?"
    r"(부른다|부르고|호출한다|호출하고|실행한다|를\s*쓴다|를\s*사용한다)"
    r"|\b[\w./-]+\.(sh|py|js|ts|rb)\s+(calls|invokes|executes|runs)\s+[\w./-]+"
)
CALLS_REQUIRED = re.compile(
    r"(\d+\s*(행|번째\s*줄)|줄\s*본문|해당\s*줄|본문\s*확인|주석\s*(여부|아님|1줄)"
    r"|[\w./-]+\.(sh|py|js|ts|rb):\d+|line\s*\d+)"
)
CALLS_FORBIDDEN = re.compile(
    # No \b after l: `grep -rln` (case 4's actual evidence) has 'n' after the 'l'
    # and a word boundary there never matches.
    r"(grep\s+-[a-zA-Z]*l|파일명\s*(매치|목록)|filename match|파일\s*목록)"
)

# --------------------------------------------------------------------------- #
# Role 4 -- "I cannot tell whether this is intentional"
#   canonical    the declaration surface (defer / preserve / a ticket)
#   insufficient a set-difference result
# --------------------------------------------------------------------------- #
INTENT_CLAIM = re.compile(
    r"(의도인지|의도적인지|의도적\s*인지)[^\n]{0,20}(모른다|모르겠|알\s*수\s*없|불명|불확실)"
    r"|(판정\s*불가|판단\s*불가|판별\s*불가)"
    r"|(cannot tell (if|whether)|intentional or not)"
)
INTENT_REQUIRED = re.compile(
    # FLEET GENERALISATION (2026-09-08): the original single-instance version
    # hardcoded `worklog/DGN-`. An instance whose ledger uses another
    # prefix (T-, or no worklog dir at all) would have cited a real
    # declaration surface and still been blocked. Column two is now
    # prefix-agnostic and also accepts the bare word "ticket" in either
    # language. This widens PASS, never BLOCK.
    r"(\.dogany-preserve|preserve|defer|plists\.defer"
    r"|worklog/[A-Za-z]+-\d+|worklog/|티켓|\bticket\b"
    r"|선언\s*표면|주석에\s*선언|declared)"
)
INTENT_FORBIDDEN = re.compile(
    r"(차집합|set difference|comm\s+-|diff\s*결과|선언\s*vs\s*등재"
    r"|declared\s*vs\s*registered)"
)

# --------------------------------------------------------------------------- #
# Role 5 -- "X is not running"
#   canonical    a machine-wide sweep + the fan-out registry
#   insufficient one instance's scope
# --------------------------------------------------------------------------- #
NOT_RUNNING_CLAIM = re.compile(
    r"(안\s*돈다|안\s*돌고|안\s*돌아간다|돌지\s*않(는다|고)|실행되지\s*않(는다|고)"
    r"|작동하지\s*않(는다|고)|동작하지\s*않(는다|고)|죽어\s*있다|안\s*뜬다)"
    r"|(is not running|isn't running|never runs|no longer runs|stopped running)"
)
NOT_RUNNING_REQUIRED = re.compile(
    r"(launchctl\s+list|systemctl\s+list|머신\s*전수|전수\s*(확인|조사|점검|스캔)"
    r"|팬아웃|fan-?out|명부|registry|MANAGED-TARGETS|모든\s*인스턴스|all instances)"
)
NOT_RUNNING_FORBIDDEN = re.compile(
    # Measured 2026-09-07: a bare "my instance" appears in ordinary prose and
    # blocked an unrelated document. Column 3 names a SCOPE DECLARATION, so
    # require that shape.
    r"(이|내|한)\s*인스턴스(에서)?만|인스턴스\s*스코프|this instance only"
    r"|로컬만|내\s*워크스페이스에서만"
    r"|(이|내)\s*인스턴스에서(만)?\s*(확인|조회|점검|봤|스캔|grep)"
)


class ClaimSourceRule:
    """One row of the DGN-1331 table. Immutable by convention."""

    __slots__ = ("role", "claim_label", "canonical", "insufficient",
                 "claim", "required", "forbidden",
                 "claim_name", "required_name", "forbidden_name")

    def __init__(self, role, claim_label, canonical, insufficient,
                 claim, required, forbidden,
                 claim_name, required_name, forbidden_name):
        self.role = role
        self.claim_label = claim_label
        self.canonical = canonical
        self.insufficient = insufficient
        self.claim = claim
        self.required = required
        self.forbidden = forbidden
        self.claim_name = claim_name
        self.required_name = required_name
        self.forbidden_name = forbidden_name

    def __repr__(self):                                   # pragma: no cover
        return "<ClaimSourceRule %s>" % self.role


# The table itself. Row order is the ticket's order and is part of the record.
# The three label columns are operator-facing strings printed in the block
# message, so they stay in the instance language alongside the predicates.
RULES = (
    ClaimSourceRule(
        "existence", "X 가 없다",
        "전 브랜치 / 전 인스턴스", "작업트리 1개 grep",
        EXISTENCE_CLAIM, EXISTENCE_REQUIRED, EXISTENCE_FORBIDDEN,
        "EXISTENCE_CLAIM", "EXISTENCE_REQUIRED", "EXISTENCE_FORBIDDEN"),
    ClaimSourceRule(
        "cadence", "X 가 이 주기로 돈다",
        "스케줄 선언(plist)", "실행 로그",
        CADENCE_CLAIM, CADENCE_REQUIRED, CADENCE_FORBIDDEN,
        "CADENCE_CLAIM", "CADENCE_REQUIRED", "CADENCE_FORBIDDEN"),
    ClaimSourceRule(
        "calls", "A 가 B 를 부른다",
        "그 줄 본문(주석 여부 포함)", "파일명 매치",
        CALLS_CLAIM, CALLS_REQUIRED, CALLS_FORBIDDEN,
        "CALLS_CLAIM", "CALLS_REQUIRED", "CALLS_FORBIDDEN"),
    ClaimSourceRule(
        "intent_unknown", "이게 의도인지 모른다",
        "선언 표면(defer/preserve/티켓)", "차집합 결과",
        INTENT_CLAIM, INTENT_REQUIRED, INTENT_FORBIDDEN,
        "INTENT_CLAIM", "INTENT_REQUIRED", "INTENT_FORBIDDEN"),
    ClaimSourceRule(
        "not_running", "X 가 안 돈다",
        "머신 전수 + 팬아웃 명부", "인스턴스 스코프",
        NOT_RUNNING_CLAIM, NOT_RUNNING_REQUIRED, NOT_RUNNING_FORBIDDEN,
        "NOT_RUNNING_CLAIM", "NOT_RUNNING_REQUIRED", "NOT_RUNNING_FORBIDDEN"),
)

ROLES = tuple(r.role for r in RULES)


def lookup(role):
    """Return the rule for `role`, or None. The whole public surface."""
    for rule in RULES:
        if rule.role == role:
            return rule
    return None


def inventory():
    """(role, slot, constant-name, pattern-body) tuples -- the parity unit.

    Pattern BODY, not name: name-only comparison is the known-bad method
    (DGN-1331 sec 2).
    """
    out = []
    for rule in RULES:
        out.append((rule.role, "claim", rule.claim_name, rule.claim.pattern))
        out.append((rule.role, "required", rule.required_name,
                    rule.required.pattern))
        out.append((rule.role, "forbidden", rule.forbidden_name,
                    rule.forbidden.pattern))
    return tuple(out)
