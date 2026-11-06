#!/usr/bin/env python3
"""L1 send-time output gate -- detect-only check library (DGN-821, DGN-896).

HARNESS-AGNOSTIC pure check library. Zero Claude-Code / harness imports:
input is the outgoing message text, output is a list of Violation records.
Any harness (Claude Code Stop hook, other runtimes) wires this through its
own thin adapter; this file must stay portable verbatim.

Stage 1 (this build): detect-only / canary.  The adapter NEVER blocks --
it logs findings for calibration.  Block enforcement is Stage 2 (canonical,
bridge terminal-hold, owner-gated).

Checks active in Stage 1 (five categories, all DETECT-ONLY):

  WALL: outgoing message is a wall of prose.  Prose character count and
      consecutive prose line count are computed AFTER masking code fences,
      tables, and blockquotes.  Thresholds are intentionally generous
      (calibration canary, not a blocker).  Constants are exposed as
      module-level attributes for easy calibration.  Content-pluggable:
      thresholds come from domain content dict (DGN-896).

  SKELETON: a decision-turn message (has [[OPTIONS]] or a long numbered
      list) lacks a top conclusion paragraph (first visible prose block
      before the decision list is empty or very short).  STRUCTURE-based,
      not substring containment.  Tier-2 blockquote option-lists are
      excluded from triggering this check (they ARE the conclusion layout).

  JARGON: message directed at the user exposes file paths, shell
      predicates, or function-name tokens -- likely internal jargon leaking
      into user-facing text.  Stage 1 = candidate-log only (no judgment,
      no block); calibration determines the threshold.  DISABLED 2026-08-13.

  DONE_CLAIM: outgoing message contains a completion-claim marker (Korean
      verb stems like "완료했", "반영했", etc.) while the current turn had
      zero tool_use actions.  Stage 1 = candidate-log only -- HIGH false-
      positive rate expected (cross-turn reporting turns look identical).
      The adapter supplies had_action=True/False; check_message defaults
      had_action=True so existing callers are unaffected (DGN-831).
      Content-pluggable: marker list comes from domain content dict (DGN-896).

  DEPRECATED_TERM: outgoing message reuses a retired estate term (e.g.
      "domain agent", "main agent" as org-role labels).  The canonical
      deprecated-term list lives in the ESTATE-TAXONOMY SoT (single source);
      the adapter parses it and passes it in via deprecated_terms.  The lib
      stays portable (no file reads).  High precision / low cost -- pure text
      substring match, no transcript scan.  Directly regression-guards the
      DGN-815 failure (crew-role assertion reused a deprecated term from hot
      memory instead of consulting SoT).  Known FP: meta-discussion that
      quotes the deprecated term while explaining the deprecation itself.

  SOT_UNREAD: outgoing message asserts on an operational-structure topic
      (Stage-1 armed tier = ORG/ROSTER only, the DGN-815 incident domain)
      while the current turn read zero sot/ docs.  The adapter supplies
      sot_read=True/False; check_message defaults sot_read=True so existing
      callers never trigger it.  HIGH false-positive rate expected (design
      discussion ABOUT org structure looks identical to asserting a fact) --
      Stage-1 candidate-log only.  Armed narrowly (org-roster markers) on
      purpose; broader operational-structure tiers are Stage-2 expansion.

  MOMENTUM: outgoing message emits a stop/defer/batch/nag phrase without a
      real blocking signal (DGN-896, 5th detector, detect-only).  Fires when
      the message contains a no-signal phrase AND no real-signal phrase.
      Critical exception: scheduled-reminder utterances (timeblock alerts) are
      unconditionally exempt -- they are owner-configured alerts, not agent-
      initiated stop points.  Content-pluggable: phrase lists come from domain
      content dict (DGN-896).

  BACKREF: final message defers substantive content to the progress log
      ("위에 답한 대로") instead of restating the point.  Progress narration is
      collapsed in the UI, so a backref leaves the final answer without the
      summary (DGN-699 interim/final separation).  Narrow markers only
      (DGN-904 fix4 FP lesson): require a "said/explained" verb + a "as/like"
      tail, so plain references ("위에 표에서") do NOT fire.  Detect-only
      canary; block promotion deferred to post-calibration.  (DGN-913)

  IDRILL_PROSE_Q: a message that arms an idrill keyboard ([[IDRILL:<id>]]
      marker) ALSO asks in prose for the value the keyboard collects
      ("몇 개 하셨어요?").  The owner then sees buttons AND a text question.
      This is the one rule with a NORMALIZER (normalize_idrill_prose_question):
      the bridge send seat removes the duplicate question sentence and counts
      it; the keyboard and every other sentence are left byte-identical.
      check_message only DETECTS it (Stop-hook canary).  Block promotion is
      decided only after the bridge counter is stable (DGN-983, DGN-1527).

  FENCE_CJK_GRID: a plain code fence holds a multi-column grid whose cells
      carry CJK text (a Korean label column, space- or pipe-aligned).  The
      text layer routes such tables out of fences (Tier 1 = values only);
      CJK never aligns in a monospace block.  Code fences with a language
      info string are skipped.  Detect-only canary (DGN-1645).

  FENCE_PROSE / BROKEN_QUOTE / SELF_BLAME (FDGN-595): reader-only examples
      in fences, interrupted/oversized quotes, and standalone blame filler.
      Detection stays pure here; the Stop adapter requests a rewrite.

  OWNER_PROCEDURE: an owner-directed Korean imperative plus an app/site
      surface in the same sentence. Closed patterns, quoted examples masked.
      Record-first (DGN-2032): log hits and whether an exhausted-means line
      cites a source; a citation is NOT proof of tool exhaustion or permission
      to give a procedure. Stays record-only here.

  OWNER_ASK (DGN-2032 rev2): the BLOCKING form, keyed on means tried, not on
      phrasing.  owner_ask_verdict reads an ask sentence (source/surface +
      owner-directed verb) against the turn's tool blob: search-only turn,
      or a named source never opened, or >1 ask after real attempts ->
      block; fetch/browser used + one ask -> pass; tool calls invisible or
      no fetch tool visible -> record only.  Not part of check_message: the
      Stop adapter routines/owner-ask-stop.py calls it (template-wired).
      Kit-declared readable facts (DGN-2165) block before the web-means
      branches; localized nouns/requests and read hints come from the kit
      conf and i18n tables through the adapter.

  EXIT_HATCH_OPTION: an authored OPTIONS menu contains one of four exact
      stop/defer labels. Stage 1 records hits only, via the existing canary;
      no new Stop block. The bridge separately drops these labels and
      suppresses a reduced menu below two options (DGN-2033).

  TERM_LEAK: a model message uses a known mistranslation of a layered
      dictionary term ("다리" for bridge).  The dictionary is DATA, read by
      routines/lib/term_dict.py from the instance's framework / kit / module
      layer files (never injected into model context); this lib only
      matches.  find_term_leaks returns every hit with its sentence;
      apply_term_rewrites replaces ONLY hits of entries the loader marked
      safe-to-rewrite.  Count-first: the bridge send seat logs every hit and
      rewrites the safe ones; nothing is ever blocked (DGN-1500).

Content-pluggable architecture (DGN-896):
  check_message() accepts an optional `content` parameter (dict).  When
  None, the dev-domain defaults are used (backward-compatible -- all 114+
  existing callers without the content param continue to work unchanged).
  The adapter reads AGENT_ROLE from config/agent.conf and passes the
  corresponding domain content dict from output_gate_content.py.

Design bias: CONSERVATIVE -- start lenient, calibrate tighter from logs.
False-positive is the real danger: a wrongly flagged good message wastes
calibration signal.  Checks are opt-in by severity; every path that would
produce a spurious flag stays un-armed in Stage 1.

FP fixes from Fable grill (2026-08-10, DGN-821 REOPEN):
  FP1 (Korean inflection): C1 OPTIONS-structure judgment replaced by
      STRUCTURE-based detection ("numbered list + descriptions present")
      instead of label-substring containment.  Korean morphology (josa /
      verbal inflection) means verbatim label text never appears in prose
      -> substring containment would block every Korean OPTIONS message.
  FP2 (rules/cold.framework.text.md Tier-2 blockquote): `> ` blockquote lines are the
      official Tier-2 option-layout format.  Gate previously counted them
      as wall-text or skeleton violations.  Tier-2 blocks are now excluded
      from prose metrics and from the SKELETON trigger.
"""
import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from owner_term import owner_terms  # noqa: E402
except ImportError:  # fail open: the gate must still load without the term module
    def owner_terms(_cwd):
        return ()

_OWNER_WORDS = "(?:%s)" % ("|".join(
    re.escape(term) for term in owner_terms(os.getcwd())) or "(?!)")

# ---------------------------------------------------------------------------
# Severity constants
# ---------------------------------------------------------------------------
SEVERITY_DETECT = "detect"   # Stage-1 log only; never blocks

# ---------------------------------------------------------------------------
# Wall-text thresholds (calibration constants -- exposed for easy tuning).
# These are the DEV-DOMAIN defaults; content-pluggable via content param.
# ---------------------------------------------------------------------------
# Maximum prose character count (excluding masked lines) before WALL fires.
WALL_CHAR_LIMIT: int = 1200
# Maximum consecutive prose lines (no blank, no masked line) before WALL fires.
WALL_LINE_LIMIT: int = 22
# Joint condition flag (DGN-821 2026-08-16 tuning batch).
# When True: WALL fires only when BOTH char threshold AND line threshold are
# exceeded simultaneously.  When False (legacy): WALL fires when EITHER
# threshold is exceeded.
#
# Calibration rationale: canary observed WALL at 1469 chars / 7 lines and
# 1264 chars / 5 lines -- char-only breaches on structured reports that were
# NOT real walls (a genuine prose wall is long AND has many consecutive lines).
# Joint condition eliminates the char-only FP class while keeping true walls.
#
# Default True: use joint condition.  Set to False in content dict to revert
# to legacy OR behavior for a specific domain.
WALL_JOINT_CONDITION: bool = True

# ---------------------------------------------------------------------------
# Skeleton: minimum conclusion characters before the decision block.
# Messages with a top prose section this short trigger SKELETON detection.
# Set generously: a single short Korean phrase (e.g. "처리됐습니다.") must pass.
# These are the DEV-DOMAIN defaults; content-pluggable via content param.
# ---------------------------------------------------------------------------
SKELETON_MIN_CONCLUSION_CHARS: int = 4

# Minimum numbered OPTIONS run length to trigger SKELETON.
# Matches bridge exempt threshold (single-confirm buttons = len 1, exempt).
SKELETON_MIN_OPTION_RUN: int = 2

# ---------------------------------------------------------------------------
# DONE_CLAIM: completion-claim markers (Korean verb stems, lenient set).
# These are the DEV-DOMAIN defaults; content-pluggable via content param.
# Calibration: add/remove stems here; substring match covers inflections.
# FP note (cross-turn): reporting turns ("완료했습니다" for prior-turn work)
#   were a Stage-1 known FP -- addressed in the adapter via cross-turn
#   had_action window expansion (DGN-821 calibration 2026-08-15).
# FP note (inflection): non-assertive conjugations (interrogative/conditional/
#   embedded) e.g. "커밋했는지", "완료하면" must NOT trigger -- guarded by
#   DONE_CLAIM_NON_ASSERTIVE_RE below (DGN-821 calibration 2026-08-15).
# ---------------------------------------------------------------------------
DONE_CLAIM_MARKERS: list = [
    "완료했",   # 완료했습니다, 완료했어요, 완료했다
    "반영했",   # 반영했습니다, 반영했어요
    "보냈",     # 보냈습니다, 보냈어요
    "머지했",   # 머지했습니다
    "전송했",   # 전송했습니다
    "처리했",   # 처리했습니다
    "적용했",   # 적용했습니다
    "끝냈",     # 끝냈습니다
    "커밋했",   # 커밋했습니다
]

# ---------------------------------------------------------------------------
# Content-plug helpers (DGN-896).
# Extract per-check values from the content dict, falling back to module
# defaults so all existing callers continue to work with content=None.
# ---------------------------------------------------------------------------

def _wall_char_limit(content: Optional[dict]) -> int:
    if content:
        return int(content.get("wall_char_limit", WALL_CHAR_LIMIT))
    return WALL_CHAR_LIMIT


def _wall_line_limit(content: Optional[dict]) -> int:
    if content:
        return int(content.get("wall_line_limit", WALL_LINE_LIMIT))
    return WALL_LINE_LIMIT


def _wall_exempt_markers(content: Optional[dict]) -> list:
    if content:
        return list(content.get("wall_exempt_markers", []))
    return []


def _wall_joint_condition(content: Optional[dict]) -> bool:
    """Return True to use joint (AND) condition for WALL; False for legacy OR.

    DGN-821 2026-08-16 tuning batch: default True (joint).
    Content dicts may override to False to revert to legacy OR behavior.
    """
    if content:
        return bool(content.get("wall_joint_condition", WALL_JOINT_CONDITION))
    return WALL_JOINT_CONDITION


def _done_claim_markers(content: Optional[dict]) -> list:
    if content:
        return list(content.get("done_claim_markers", DONE_CLAIM_MARKERS))
    return DONE_CLAIM_MARKERS


def _skeleton_min_conclusion_chars(content: Optional[dict]) -> int:
    if content:
        return int(content.get("skeleton_min_conclusion_chars",
                               SKELETON_MIN_CONCLUSION_CHARS))
    return SKELETON_MIN_CONCLUSION_CHARS


def _skeleton_min_option_run(content: Optional[dict]) -> int:
    if content:
        return int(content.get("skeleton_min_option_run",
                               SKELETON_MIN_OPTION_RUN))
    return SKELETON_MIN_OPTION_RUN


def _momentum_no_signal_phrases(content: Optional[dict]) -> list:
    if content:
        return list(content.get("momentum_no_signal_phrases", []))
    return []


def _momentum_real_signal_phrases(content: Optional[dict]) -> list:
    if content:
        return list(content.get("momentum_real_signal_phrases", []))
    return []


def _momentum_scheduled_reminder_markers(content: Optional[dict]) -> list:
    if content:
        return list(content.get("momentum_scheduled_reminder_markers", []))
    return []

# Non-assertive (interrogative / conditional / embedded) conjugation suffixes
# that immediately follow a marker stem.  When matched, the marker is a STATE
# QUESTION or CONDITIONAL -- NOT a completion CLAIM -- and must be suppressed.
# Design bias: conservative (favour false-negative over false-positive) -- any
# suffix ambiguity -> keep the check silent rather than block a valid message.
# Suffixes covered (DGN-821 2026-08-15 calibration verdicts):
#   는지   -- embedded question ("커밋했는지 확인해요")
#   는가   -- formal question  ("완료했는가")
#   는데   -- contextual connector, not assertion ("처리했는데 오류가")
#   다면   -- hypothetical     ("완료했다면")
#   으면/면 -- conditional     ("완료하면", "됐으면")
#   어야/아야 -- obligative    ("보내야", "처리해야")
#   을지/ㄹ지 -- uncertainty   ("완료할지", "끝낼지")
#   다가   -- interrupted action ("하다가")
#   던     -- retrospective   ("했던")
#   는지도  -- included via 는지 prefix match
# Korean morphology note: stems end with 했/냈/봤/etc.; non-assertive forms
# immediately follow with no intervening space in most conjugations.  The
# pattern anchors at the character position right after the matched stem.
DONE_CLAIM_NON_ASSERTIVE_RE = re.compile(
    r"(?:는지|는가|는데|다면|으면|어야|아야|을지|ㄹ지|다가|던|면(?!서))"
)

# ---------------------------------------------------------------------------
# DEPRECATED_TERM: default empty.  The canonical list is the ESTATE-TAXONOMY
# SoT; the adapter parses it and passes it into check_message(deprecated_terms=).
# A hardcoded copy here would fork the single source, so we keep the default
# empty -- with no list supplied the check simply never fires (safe).
# ---------------------------------------------------------------------------
DEFAULT_DEPRECATED_TERMS: list = []

# ---------------------------------------------------------------------------
# SOT_UNREAD: Stage-1 armed marker tier = ORG/ROSTER only (the DGN-815 incident
# domain).  A message asserting org/roster structure without a sot/ read this
# turn is a candidate.  Broader operational-structure markers (deploy, kit,
# lifekit, devkit) are deliberately NOT armed in Stage 1 -- they would flood
# the canary with design-discussion FPs before calibration.  Substring match.
#
# KNOWN Stage-1 CJK-substring FPs (no word boundary for CJK; documented for
# Stage-2 marker re-tuning, grill 2026-08-11): "리더" fires inside 리더보드 /
# 카드리더기 / 테크리더; "크루" fires inside 크루즈.  These are expected
# calibration noise -- Stage-2 entry MUST re-tune this list before any block.
# "standalone" was DROPPED (fired on "standalone 스크립트" -- pure dev noise,
# not org-specific).
# ---------------------------------------------------------------------------
SOT_ORG_MARKERS: list = [
    "크루",          # crew
    "로스터",        # roster
    "리더",          # leader (Korean)
    "스태프",        # staff (Korean)
    "dev-crew",
    "life-crew",
]

# ---------------------------------------------------------------------------
# Jargon: regex patterns that suggest internal terms leaking to user output.
# ---------------------------------------------------------------------------
# Absolute file path (starts with / or ~/).
_JARGON_PATH_RE = re.compile(r"(?:^|(?<=\s))(?:/[\w./\-_]+|~/[\w./\-_]+)")
# Python/shell function-call-ish: genuine code token followed IMMEDIATELY by
# "(" with NO intervening space and NOT fused with Hangul characters.
#
# Tightened (DGN-821 2026-08-16 tuning batch):
#   OLD: r"\b[a-z_]{4,}\w*\s*\("  -- \w matches Unicode (including Hangul),
#        and \s* allowed a space before "(", causing two classes of false positives:
#        (a) Space-paren: "doctrine (" / "baseline (" -- prose word + paren-aside.
#        (b) Hangul-fused: "folding합니다(" / "fresh입니다(" -- Korean conjugation
#            appended to an English stem; \w* consumed the Hangul syllables.
#
#   NEW: r"\b[a-z_]{4,}[a-zA-Z0-9_]*\("
#        - [a-zA-Z0-9_]* replaces \w*: only ASCII alphanumeric/underscore chars
#          are consumed, so the match STOPS before any Hangul character.
#          If Hangul follows the ASCII token before "(", the match fails
#          because \( cannot match the Hangul syllable.
#        - No \s*: a space between token and "(" no longer matches.
#          Genuine function calls have no space before "(".
#
# False-positive corpus (canary 2026-08-16, ALL must NOT match):
#   "folding합니다("  -- Hangul between token end and "(" -> no match
#   "doctrine ("      -- space before "(" -> no match
#   "fresh입니다("    -- Hangul fused -> no match
#   "baseline ("      -- space before "(" -> no match
#   "path:/new와"     -- path-RE handles paths; this has Hangul -> no func match
#
# True-positive corpus (ALL must still match):
#   "preflight("    -- clean ASCII token, no space, >=4 chars -> matches
#   "run_adapter("  -- underscore token -> matches
#   "check_message("-> matches
#   "self_restart(" -> matches
_JARGON_FUNC_RE = re.compile(r"\b[a-z_]{4,}[a-zA-Z0-9_]*\(")
# Shell predicate: -is-ancestor / --flag style OR double-dash flag chains.
_JARGON_PRED_RE = re.compile(r"--[a-z][\w\-]{3,}")

# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------
OPTIONS_MARKER = "[[OPTIONS]]"
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_OPTION_LINE_RE = re.compile(r"^\s*(\d+)[.、)）]\s*(\S.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|")

# DGN-913: BACKREF -- final message defers substantive content to the progress
# log ("위에 답한 대로") instead of restating the point.  Progress narration is
# collapsed in the UI, so a backref leaves the final answer without the summary
# (DGN-699 interim/final separation).  Narrow markers only (DGN-904 fix4 FP
# lesson): require a "said/explained" verb + a "as/like" tail, so plain
# references ("위에 표에서") do NOT fire.
_BACKREF_RE = re.compile(
    r"위에서?\s*(?:답한|말한|설명한|정리한|드린)\s*(?:대로|것|바|내용)"
    r"|앞서\s*(?:답한|말한|설명한|드린)\s*(?:대로|것|바)"
    r"|\babove\b,?\s+(?:as|per)\s+(?:i|we)\b",
    re.IGNORECASE,
)


def _is_blockquote(line: str) -> bool:
    """True for Tier-2 option-layout blockquote lines ("> ...").

    FP2 fix: these lines are the sanctioned Tier-2 format (rules/cold.framework.text.md);
    masking them prevents false wall/skeleton flags on correct messages.
    """
    return line.lstrip().startswith(">")


def _visible_prose_lines(text: str) -> List[str]:
    """Return lines that count as prose for wall/skeleton metrics.

    Masked (excluded) line types:
      - fenced code-block content and delimiter lines
      - table rows (|...)
      - blockquote lines ("> ...") -- FP2 Tier-2 fix
      - the [[OPTIONS]] marker line itself
      - blank lines (not counted as prose)
    """
    out: List[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _is_blockquote(line):
            continue
        if _TABLE_ROW_RE.match(line):
            continue
        if line.strip() == OPTIONS_MARKER:
            continue
        if not line.strip():
            continue
        out.append(line)
    return out


def _option_entries(text: str) -> List[Tuple[int, str]]:
    """(number, label_text) for numbered list lines in visible prose."""
    entries = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _is_blockquote(line):
            continue
        m = _OPTION_LINE_RE.match(line)
        if m:
            entries.append((int(m.group(1)), m.group(2).strip()))
    return entries


def _last_run(entries: List[Tuple[int, str]]) -> List[Tuple[int, str]]:
    """Last contiguous 1..N numbered run (port of bridge options.py logic)."""
    runs: List[List[Tuple[int, str]]] = []
    cur: List[Tuple[int, str]] = []
    for num, label in entries:
        if num == 1:
            if cur:
                runs.append(cur)
            cur = [(num, label)]
        elif cur and num == cur[-1][0] + 1:
            cur.append((num, label))
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs[-1] if runs else []


# ---------------------------------------------------------------------------
# DGN-914 Phase 1: multi-question prompt prefilter
# ---------------------------------------------------------------------------
# Structural heuristic: cheap, no LLM, no semantic coverage judgment.
# Two signals trigger the flag:
#   (A) Question-mark count: >= 2 '?' OR '？' in the prompt text.
#   (B) Enumerated sub-questions: a numbered list pattern (1. / 2. / ...) each
#       line ending with a question mark -- at least 2 such entries.
# Signal B catches "please answer: 1. X 2. Y" without any '?'.
# Design bias: conservative FP rate -- we log raw data for hit turns only;
# a FP wastes a log line but never blocks.  A FN simply loses a data point;
# Phase 2 batch judge is tolerant of FN input gaps.
#
# Rhetorical '?' note: single '?' ("알겠지?" in a statement) does not trigger
# signal A (requires >=2).  Two rhetorical '?' in one message is a known edge-
# case; the Phase 2 batch judge is the precision gate, not this prefilter.

_ENUM_QUESTION_RE = re.compile(
    r"^\s*\d+[.、)）]\s*.+[?？]\s*$",
    re.MULTILINE,
)


def is_multi_question_prompt(prompt: str) -> bool:
    """Return True if the prompt structurally looks like a multi-question turn.

    Phase 1 prefilter (DGN-914): cheap structural flag ONLY.  Not a semantic
    coverage judgment.  Never raises; returns False on None / empty input.

    Signal A: two or more '?' or '？' characters anywhere in the prompt.
    Signal B: two or more numbered-list lines each ending with '?' or '？'
              (catches "1. X? 2. Y?" style without relying on signal A alone).

    Args:
        prompt: The raw user prompt text for the current turn.

    Returns:
        True  -> prefilter hit; adapter logs raw (prompt, answer) canary line.
        False -> single/non-question turn; NO raw logging (privacy boundary).
    """
    if not prompt or not prompt.strip():
        return False
    # Signal A: total question-mark count >= 2.
    qmarks = prompt.count("?") + prompt.count("？")
    if qmarks >= 2:
        return True
    # Signal B: at least 2 numbered lines ending with a question mark.
    enum_matches = _ENUM_QUESTION_RE.findall(prompt)
    if len(enum_matches) >= 2:
        return True
    return False


# ---------------------------------------------------------------------------
# Violation record
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    rule_id: str          # WALL|SKELETON|JARGON|DONE_CLAIM|DEPRECATED_TERM|SOT_UNREAD|MOMENTUM|BACKREF|IDLE_PROMISE|IDRILL_PROSE_Q|TERM_LEAK|FENCE_CJK_GRID|OWNER_PROCEDURE|EXIT_HATCH_OPTION
    severity: str         # always SEVERITY_DETECT in Stage 1
    reason: str
    metrics: dict = field(default_factory=dict)  # length/structure indicators
    excerpt: str = ""     # short indicator only, never full message text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# --- IDLE_PROMISE (DGN-1081) ------------------------------------------- #
# Rule source: 2026-07-07 owner-confirmed tone rule ("no future tense for
# work you are not actually doing").  It lived as prose for over a year with
# NO enforcement machine and was violated repeatedly (owner escalation
# 2026-08-25 06:5x).  This is the machine.
#
# What makes it CHECKABLE at Stop time: the turn is ENDING.  A commitment
# with an IMMEDIACY marker ("지금/이제/바로 ...하겠습니다") is therefore
# self-contradictory by construction -- the message promises to act *now*
# while the turn closes with no further action possible.
#
# Deliberately NARROW: a bare "하겠습니다" is legitimate (owner-gated work,
# next-session work, delegated work).  Only immediacy + commitment fires.
# Broad banned-word matching is the approach that FAILED on 2026-08-14
# (SKELETON block revert) and again in DGN-1042/1014 -- do not repeat it.

# DGN-1081 rev3 (2026-08-25, owner caught a THIRD miss: "2.0 이어갑니다").
# The enumerated-verb list is the wrong shape -- it loses a race it cannot win,
# because Korean forms an action promise productively (any stem + 겠/ㄹ 게).
# rev1 missed "밀겠", rev2 missed "이어갑니다".  Replaced with the PRODUCTIVE
# form itself: <stem>겠<ending> / <stem>ㅂ니다 on a motion-completion verb.
IDLE_PROMISE_COMMIT_RE = re.compile(
    r'([가-힣]겠(습니다|어요|습니당|음|다)|할게요|하죠|'
    r'(이어|들어|나아|해)?갑니다|겁니다|시작합니다|진행합니다)'
)
IDLE_PROMISE_NOW_RE = re.compile(r'(지금|이제|바로|먼저|당장|이번엔|우선|계속|이어서|마저)')


def _configured_helper_term():
    conf = os.path.join(os.environ.get("PROJECT_ROOT", os.getcwd()), "config", "agent.conf")
    try:
        with open(conf, encoding="utf-8") as fh:
            for raw in fh:
                if raw.lstrip().startswith("DASHBOARD_SIBLING_TERM="):
                    value = raw.split("=", 1)[1].strip().strip('"\'')
                    if value:
                        return value
    except OSError:
        pass
    return "subagent"


_HELPER_TERM_RE = re.escape(_configured_helper_term())
# DGN-1081 rev2 (2026-08-25, owner caught a miss): a CONTINUATION marker is the
# same failure as an immediacy marker.  "계속 밀겠습니다" promises to resume work
# in a turn that is closing -- structurally unkeepable, identical to "지금 ...".
# The original set only covered "start now"; this adds "keep going".
# Exempts: the promise is explicitly about a future session / owner gate /
# already-running delegate -- those are honest future tense.
# DGN-1168: a [[OPTIONS]] marker is the same "hands the decision to the
# owner" shape as the owner-gate exemptions above -- a numbered item ending
# in a commit verb names a CANDIDATE action for the owner to pick, not a
# self-commitment the turn is closing on.
IDLE_PROMISE_EXEMPT_RE = re.compile(
    r'(아침에|내일|다음 세션|' + _OWNER_WORDS + r' (확인|승인|판단|결정)|오너 게이트|'
    r'돌고 있|가동 중|' + _HELPER_TERM_RE + r'(?:이|가)? (돌|작업)|대기|\[\[OPTIONS\]\])'
)


# --- IDLE_PROMISE block layer (DGN-1279) -------------------------------- #
# The DETECT rule above stays deliberately broad: DGN-1081 rev1/rev2/rev3 each
# lost a race to Korean's productive promise form, so narrowing it would give
# back three owner-caught misses.  Blocking, however, needs precision, and the
# measurement says the broad rule has none: 52 fires replayed over the
# 2026-09-03..04 transcripts judged 38 false / 10 true / 4 undecidable
# (79.2% FP).  So the block verdict is computed as a SEPARATE, strictly
# narrower layer on top of the unchanged detection.
#
# Three conditions, all required:
#   (1) the promise clause names an action whose fulfilment leaves a
#       machine-visible tool signature (IDLE_PROMISE_ACTIONS -- a whitelist,
#       not a heuristic; anything not on it can never block),
#   (2) that clause is UNCONDITIONAL -- no contingency marker, no owner gate,
#       and the turn does not close on a question to the owner.  The largest
#       FP class by far (22 of 38) was honest contingent future tense
#       ("결과 오면 회수해서 올리겠습니다"), which the next turn kept,
#   (3) the action's tool signature is ABSENT from the current turn.
#
# Why a whitelist and not a general contrast: matching an arbitrary Korean
# verb phrase to a tool call is semantic work a regex cannot do.  What IS
# sound is a closed set of promises that name one artifact with one command.
# v1 carries exactly one entry -- dispatch -- because that is the shape the
# owner escalated on verbatim twice ("발주했냐" 2026-09-03, "안돌아갔지?"
# 2026-09-04) and the shape that carried 5 of the 10 measured true positives.
# Everything else stays detect-only; recall is traded away on purpose.
#
# SUPERSEDED in part by DGN-1308 (carrier inversion, below): the whitelist
# still decides WHICH signature a named shape needs, but a promise outside
# it no longer passes silently.

# Contingency: the promise is conditioned on an event that has not happened.
# Deliberately over-broad ("[가-힣]면" also catches nouns ending in 면).  It is
# scoped to the promise CLAUSE only, and over-exempting costs recall, never
# precision -- the safe direction for a gate that blocks.
IDLE_PROMISE_CONTINGENT_RE = re.compile(
    r'([가-힣]면|[가-힣]거든|는 대로|은 대로|ㄴ 대로|후에|뒤에|다음에|이후|'
    r'돌아오|끝나|회수되|풀리|나오면|승인|확인받)'
)
# The turn hands a decision to the owner: any promise in it is owner-gated.
IDLE_PROMISE_OWNER_Q_RE = re.compile(
    r'(까요|습니까|ㅂ니까|십시오|주십시오|여쭙)[.?!]?\s*$'
)
# --- v2 shape extension (DGN-1279 rev2, 2026-09-08) --------------------- #
# Same discipline, three more shapes measured before enabling.  Replay of the
# 2026-08-20..09-07 dev-instance transcripts (936 deduplicated IDLE_PROMISE fires,
# files/reports/idle-promise-v2.md carries the command and the per-fire
# judgments):
#
#   shape     matches  sig-present  block-fires  true  false
#   ticket        17            1           10     10      0
#   merge         15            0            3      3      0
#   dispatch      21            3           15      -      -   (v1, unchanged)
#   commit         0            0            0      -      -   NOT SHIPPED
#   handoff        2            0            0      -      -   NOT SHIPPED
#   restart       20           13            5      0      5   REJECTED
#
# ticket / merge ship blocking (0 false positives on replay).  commit and
# handoff have a sound signature but ZERO measured block fires, so there is
# no replay to call clean -- they are documented in the report and left out
# rather than shipped on a guess.  restart is REJECTED on measurement: all 5
# of its fires were false, and two of them were the agent QUOTING restart
# copy ("재시작합니다" inside a release note it was reading aloud), which no
# tool signature can separate from a commitment.  measurement
# ("재겠습니다/측정하겠습니다") is REJECTED for the reason the shape rule
# demands: a measurement can be a grep, a script, a log read or an owner
# question, so no tool signature settles whether it happened.

# Whitelist: (kind, promise-clause pattern, tool-signature pattern).
#
# Signature side is deliberately GENEROUS -- any turn command that touches
# the artifact counts as kept.  Over-crediting costs recall only; it can
# never manufacture a block.
IDLE_PROMISE_ACTIONS = (
    (
        "dispatch",
        re.compile(r'발주(하겠|합니다|해야겠|하러|로 돌아가|를? ?(넣|붙이|띄우|올리)겠)'),
        re.compile(r'(?m)^CMD .*dispatch-detached\.sh'),
    ),
    (
        # "티켓 파겠습니다 / 티켓 열고 ... / 별도 티켓으로 끊겠습니다".
        # The object marker window is one particle wide on purpose: the loose
        # first cut ("티켓" within 14 chars of any opening verb) matched
        # "티켓 상태를 조회해서 세션을 엽니다", which promises nothing.
        "ticket",
        re.compile(r'티켓(으로|로|을|를)?\s*(하나|한 ?건|한 ?장)?\s*(새로\s*)?'
                   r'(파겠|파고|팝니다|신설(하겠|합니다)|열겠|열고|엽니다|'
                   r'생성(하겠|합니다)|만들겠|만들고|끊겠|끊고)'),
        re.compile(r'(?m)^(CMD|TOOL) .*worklog/'),
    ),
    (
        # "머지하고 정리하겠습니다 / 머지하겠습니다".  "머지까지" and
        # "머지부터" are plan narration, not a commitment, and are excluded:
        # the one false positive the loose cut produced was "...검증해서
        # 머지까지" in a sentence listing the remaining steps.
        "merge",
        re.compile(r'머지(하겠|합니다|해두겠|해 두겠|하고|해서|해놓겠)'),
        re.compile(r'(?m)^CMD .*(\bgit\b.*\bmerge\b|loop-merge\.sh|'
                   r'promote-to-main\.sh)'),
    ),
)

# --- block-layer-only narrowings (DGN-1279 rev2) ------------------------ #
# Both are computed on the TAIL and only ever REMOVE blocks.  Neither is
# allowed near _check_idle_promise: detection breadth stays untouched.
#
# The bridge grammar is `[[OPTIONS: a | b]]`, not the bare `[[OPTIONS]]`
# token IDLE_PROMISE_EXEMPT_RE carries, so a real owner-decision turn never
# hit that exemption.  Measured: 1 of the ticket shape's false positives was
# exactly this -- "...티켓 열겠습니다.\n[[OPTIONS: 개명 나중에 | 지금 강행 |
# 보류]]" (2026-08-27 19:18 KST).  A turn that hands the owner a choice is
# owner-gated by construction.
IDLE_PROMISE_OPTIONS_RE = re.compile(r'\[\[OPTIONS')

# Explicit QUEUEING: the turn already said, in the owner's own reading, that
# the action waits on something in flight.  That IS remedy (b) of the block
# message ("say honestly it is not done and what it waits on"), so blocking
# it punishes the honest form.  3 of the ticket shape's 5 first-cut false
# positives were this -- "린트 작업 끝나면 이어서 하겠습니다",
# "지금 도는 둘 끝나면 바로 붙이겠습니다", "리셋 후 작업 목록에 함께 넣습니다".
#
# Deliberately NOT the broad IDLE_PROMISE_CONTINGENT_RE: that one matches
# any "[가-힣]면", which the tail carries constantly for unrelated reasons
# ("...했으면서", "실패하면 즉시 보고하게 돼 있습니다"), and running it over
# the whole tail threw away true positives on replay.  This list is the
# narrow set of queueing phrases only.  "다음 순번" is excluded on purpose:
# "티켓 열고 발주 시스템 다음 순번에 넣겠습니다" queues the DISPATCH, while
# the ticket itself is promised now (measured true positive).
IDLE_PROMISE_DEFERRAL_RE = re.compile(
    r'(끝나면|끝나고|끝난 ?(뒤|후)|마치면|마친 ?(뒤|후)|돌아오면|'
    r'회수(되면|하면)|리셋 ?(후|되면)|창이 ?(풀리|열리)|'
    r'이어서 ?(하|붙이|가|진행)|나중에|잠시 ?(후|뒤)|조금 ?(뒤|후)|'
    r'기다렸다가|오는 즉시|착지 ?(하면|되면|알림))')


def _promise_clause(tail: str, pattern) -> Optional[str]:
    """Return the sentence of `tail` that carries the promise, or None.

    Contingency has to be judged on the promise clause, not the whole tail:
    a neighbouring sentence routinely carries an unrelated "면" ("게이트를
    그대로 쓰면 통과 못 합니다") that would wrongly exempt a real violation.
    """
    for chunk in re.split(r'(?<=[.!?])\s+|\n', tail):
        if pattern.search(chunk):
            return chunk
    return None


def _promise_action_contrast(tail: str, turn_tool_blob: Optional[str]):
    """(kind, clause, done) for the whitelisted promise in `tail`.

    The blob is the adapter's channel-tagged turn summary: `CMD <shell
    command, heredoc bodies stripped>` and `TOOL <name> <path>`.  Action
    signatures match CMD lines ONLY -- a command name appearing inside a
    file the turn wrote is a mention, not an invocation.

    done is True/False when the tool blob was supplied, None when it was not
    (adapter could not read the turn -- fail-open, never block).

    With more than one shape on the whitelist (rev2) a tail can name two of
    them.  A shape whose signature IS present wins, so a turn that kept one
    of its promises is reported as kept rather than blocked on the other --
    the same safe direction the signature patterns are written in.
    """
    first = None
    for kind, promise_re, tool_re in IDLE_PROMISE_ACTIONS:
        clause = _promise_clause(tail, promise_re)
        if clause is None:
            continue
        if turn_tool_blob is None:
            return kind, clause, None
        if tool_re.search(turn_tool_blob):
            return kind, clause, True
        if first is None:
            first = (kind, clause, False)
    return first if first is not None else (None, None, None)


# --- carrier inversion (DGN-1308, owner go 2026-10-02 16:12) ------------ #
# rev2 still answered "is this promise one of the three shapes we know?"
# and let every other promise through: "재고 올리겠습니다", "22:50에
# 걸겠습니다", "<peer agent>에게 미리 해 두라고 하겠습니다", "따로 이어서
# 하겠습니다" all passed BY CONSTRUCTION, and the owner -- seeing the gate
# alive -- believed it was fixed.  No detection manufactured trust.
#
# The default is inverted.  Every commitment sentence in the final reply is
# checked, whatever its verb.  It passes only on a CARRIER: a tool call in
# THIS turn through which the promise can physically be kept after the turn
# closes.  After a Stop there are exactly four such paths -- (i) detached
# work (dispatch, background job), (ii) a schedule (wakeup / cron /
# monitor), (iii) a durable surface (commit, ticket file, handoff to another
# agent), (iv) the owner's next prompt.  (i)-(iii) are visible in the turn's
# tool_use blocks; (iv) is the owner-trigger exemption.  A promise with no
# carrier cannot be kept by any path.
#
# The carrier list is an enumeration too, but its direction is reversed:
# an unlisted carrier now produces a VISIBLE block instead of a silent pass.
#
# Evidence is read from tool_use structure only (the adapter's blob), never
# from prose.  Signatures stay generous -- over-crediting costs recall, and
# carrier misattribution ("committed X, promised Y") is an accepted residual
# -- except for send and fix promises, which DGN-2328 matches (below).
#
#   kind        signature in the turn blob                      armed
#   dispatch    CMD ... dispatch-detached.sh                      yes
#   background  BG <tool> (run_in_background Bash/Agent), Workflow yes
#   schedule    ScheduleWakeup / CronCreate / Monitor / crontab   yes
#   handoff     CMD ... handoff-send.sh, SendMessage              yes
#   commit      CMD git ... commit|merge|cherry-pick              no
# A ticket write records intent only; it is not a carrier (DGN-2164).
#
# "armed" = the carrier will re-invoke the agent by itself.  A promise that
# is CONDITIONAL on an event ("결과 오면 ...", "끝나는 대로 ...") passes
# only on an armed carrier: a commit made this turn does not deliver a
# result that has not arrived yet.
IDLE_PROMISE_CARRIERS = (
    ("dispatch", re.compile(r'(?m)^CMD .*dispatch-detached\.sh'), True),
    ("background", re.compile(r'(?m)^(BG |TOOL Workflow\b)'), True),
    ("schedule", re.compile(
        r'(?m)^(TOOL (ScheduleWakeup|CronCreate|Monitor)\b|'
        r'CMD .*\bcrontab\b(?!\s+-l)|CMD .*\blaunchctl\s+(load|bootstrap)\b)'),
     True),
    ("handoff", re.compile(r'(?m)^(CMD .*handoff-send\.sh|TOOL SendMessage\b)'),
     True),
    ("commit", re.compile(
        r'(?m)^CMD .*\bgit\b[^|;&\n]*\b(commit|merge|cherry-pick)(?![-\w])'),
     False),

)

# Exemptions -- an explicit, short list.  Everything else is checked.
#   E1 code fences, blockquote lines, quoted spans: a mention, not a promise
#      (the restart-copy class of rev2: "재시작합니다" read aloud from a
#      release note).
#   E2 owner trigger: the promise waits on something only the owner can do
#      ("누르시면 ...", "승인 주시면 ...", "착수하실 거면 ...").
#      A request to the owner in the immediately preceding sentence also
#      supplies that trigger; it does not exempt later commitments.
#   E3 the reply hands the owner a decision ([[OPTIONS ...]]) or its
#      closing lines put a question to the owner ("확인 들어갈까요?") --
#      any promise beside the question waits on the answer.
#   E4 the detection layer's IDLE_PROMISE_EXEMPT_RE (next session / owner
#      gate / named running delegate), applied per sentence.
IDLE_PROMISE_OWNER_TRIGGER_RE = re.compile(
    r'([가-힣]시면|[가-힣]시는 대로|주시면|주실 때|실 ?거면|시려면|실 경우|'
    r'시겠다면|시다면|' + _OWNER_WORDS + r'(이|께서)|' + _OWNER_WORDS + r' (말씀|답)|오너가|'
    r'원하시|말씀(하|주)시|승인|확인받|허락)'
)
# Sentence-final request forms only: a reported request or an unrelated
# owner condition in the preceding sentence must not exempt a commitment.
IDLE_PROMISE_OWNER_REQUEST_RE = re.compile(r'(주세요|주시면)\s*[.!]?\s*$')
IDLE_PROMISE_QUESTION_RE = re.compile(r'(\?|까요|습니까|ㅂ니까)\s*$')
# Conditional on an EVENT (not the owner).  Narrower than
# IDLE_PROMISE_CONTINGENT_RE on purpose: "[가-힣]면" alone also matches
# nouns ("화면", "측면").  The rev2 DEFERRAL entries "이어서 하..." and
# "나중에" are NOT here: they name no event at all -- "따로 이어서
# 하겠습니다" is the 2026-10-02 16:0x miss itself.
IDLE_PROMISE_EVENT_COND_RE = re.compile(
    r'([가-힣]면(?=[\s,.)])(?<!화면|측면|표면|전면|정면|단면|양면|방면|국면|장면)|'
    r'거든|[가-힣] 때|는 대로|은 대로|ㄴ 대로|'
    r'(후|뒤|다음)에|이후|고 ?나서|끝나|돌아오|회수되|풀리|도착|착지|완료되|'
    r'잠시 ?(후|뒤)|조금 ?(뒤|후)|기다렸다가|오는 즉시|리셋 ?(후|되면)|'
    r'창이 ?(풀리|열리))'
)
# A commitment form that commits to nothing.  The detection regex is kept
# broad (DGN-1081 rev3); the block layer only blocks on a sentence that
# still carries a commitment after these are removed:
#   - negated or refused ("안 여쭙겠습니다", "넘겨짚지 않겠습니다",
#     "판단 못 하겠습니다") -- there is no work to carry;
#   - epistemic/modal 겠 ("모르겠습니다", "좋겠습니다", "알겠습니다");
#   - 겁니다 without a first-person subject: "만든 겁니다" is a copula,
#     "통과할 겁니다 / 잡혔을 겁니다" are predictions; only "제가 ... 할
#     겁니다" commits the speaker;
#   - a stance, not work: "2.7 후보로 두겠습니다", "그대로 두겠습니다";
#   - bare motion "갑니다" ("짚고 갑니다", "올라갑니다") and "들어갑니다"
#     ("2.5에 들어갑니다" = gets included) -- narration; the rev3 miss form
#     "이어갑니다" stays a commitment.
_IP_NONCOMMIT_RE = re.compile(
    r'(않겠|말겠|'
    r'(알|모르|좋|같|싶|있|없|맞|되|않|어렵|쉽|크|많|적|괜찮|필요하|가능하|'
    r'아니|그렇|나|이)겠|(그대로|로|으로) ?두겠)'
)
_IP_NEGATED_RE = re.compile(r'(?:^|\s)(안|못)(\s[가-힣]*|하|되|가|해)$')
_IP_FIRST_PERSON_RE = re.compile(r'(제가|저는|저도|제 쪽|내가)')
# DGN-2328: a keep-as-is statement reports the present state, it does not
# promise work ("I will keep it as is" after the owner picked keep-as-is was
# blocked and forced a pointless tool call).  Anchored to the commit form
# it precedes, so a second commitment in the same sentence still counts,
# and the keep verb is required: "I will do that" / "I will proceed this way"
# commit to doing the work as agreed and stay promises.
# "Back to the original" is excluded: promising a revert still promises work.
_IP_KEEP_AS_IS_RE = re.compile(
    r'(그대로|현재대로|지금대로|이대로|현행대로|현 ?상태(로|대로)?)\s*'
    r'(유지(해)?|놔|놓아|남겨)\s*$')


def _ip_commits(sentence: str) -> bool:
    """True when the sentence carries a real commitment form (block layer)."""
    for m in IDLE_PROMISE_COMMIT_RE.finditer(sentence):
        form, a = m.group(0), m.start()
        if _IP_NEGATED_RE.search(sentence[max(0, a - 6):a + 1]):
            continue  # "안 들어갑니다", "못 하겠습니다"
        if _IP_KEEP_AS_IS_RE.search(sentence[:a]):
            continue  # Keeping the current state promises no pending work.
        if "겠" in form:
            head = sentence[max(0, a - 6):a + 2]
            if _IP_NONCOMMIT_RE.search(head):
                continue
        elif form == "겁니다":
            prev = sentence[:a].rstrip()[-1:]
            if not prev or not ("가" <= prev <= "힣") or \
                    (ord(prev) - 0xAC00) % 28 != 8:  # final consonant ㄹ
                continue
            if not _IP_FIRST_PERSON_RE.search(sentence[:a]):
                continue
        elif form in ("갑니다", "들어갑니다"):
            continue  # "2.5에 들어갑니다" = will be included
        return True
    return False


# A DEFERRED, separate item ("이 검사 추가는 따로 이어서 하겠습니다",
# "다음 버전에 넣겠습니다") names work the turn did NOT do, so the turn's
# commit or background job -- evidence for the work it DID do -- cannot
# carry it. Only a carrier that queues new work does: a dispatch, schedule
# or handoff; a background job must be bound to this ticket.  This is the 2026-10-02 16:0x miss:
# that turn committed and pushed a fix, then deferred the E2E check.
IDLE_PROMISE_DEFERRED_RE = re.compile(
    r'(따로|별도로?|나중에|추후|다음 ?(번|기회|차례|버전|릴리스)|후속으로)'
)
IDLE_PROMISE_QUEUE_CARRIERS = ("dispatch", "schedule", "handoff")

# --- evidence must match the promise (DGN-2328) ------------------------- #
# DGN-1308 let an unconditional promise pass on ANY carrier anywhere in the
# turn.  2026-10-27 18:50: "I will send it again" passed because the same turn
# had sent an unrelated handoff earlier -- the evidence predated the promise
# and was for something else.  For a promise whose object the tool blob can
# see, the evidence now has to FIT and come AFTER the promise context:
#
#   object  promise verb             fitting evidence
#   send    send / transmit / forward / reply   dispatch, handoff/SendMessage, git push
#   fix     fix / modify / patch / correct      dispatch, commit
#
# Promise context = the last earlier message of the turn that talks about
# that object (any tense: "Sending failed", "I will send it to the reviewer").  Only
# a fitting call after it counts.  With no earlier mention the turn start
# opens the context -- except for a REDO promise ("I will send it again"): a
# redo follows an attempt, so a send that no message of the turn has talked
# about since is that attempt, not the redo.
# A Latin-script recipient name must appear on a handoff line; a
# Hangul persona name cannot be matched to a slug and stays generous.
# Promises with no such object keep the DGN-1308 any-carrier residual.
_IP_OBJECT_KINDS = (
    ("send",
     re.compile(r'(보내|보낼|전송|발송|전달|회신)'),
     re.compile(r'(보내|보냈|보낸|보낼|보냄|전송|발송|전달|회신|핸드오프|'
                r'handoff)', re.I),
     re.compile(r'^(CMD .*dispatch-detached\.sh|CMD .*handoff-send\.sh|'
                r'TOOL SendMessage\b|CMD .*\bgit\b[^|;&\n]*\bpush\b)')),
    ("fix",
     re.compile(r'(고치|고칠|수정(하|해|할)|패치(하|해|할)|바로잡)'),
     re.compile(r'(고치|고쳤|고친|고칠|수정|패치|바로잡)'),
     re.compile(r'^(CMD .*dispatch-detached\.sh|'
                r'CMD .*\bgit\b[^|;&\n]*\b(commit|merge|cherry-pick)(?![-\w]))')),
)
_IP_REDO_RE = re.compile(r'(다시|재전송|재발송|재시도|재수정|한 ?번 더)')
_IP_RECIPIENT_RE = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s?(에게|한테|께)')
_IP_ADDRESSED_RE = re.compile(r'^(CMD .*handoff-send\.sh|TOOL SendMessage\b)')
_IP_OBJECT_LABEL = {
    "send": "a dispatch, handoff or push to that recipient",
    "fix": "a dispatch or commit",
}


def _ip_object_kinds(sentence: str) -> List[str]:
    return [k for k, verb, _m, _f in _IP_OBJECT_KINDS if verb.search(sentence)]


def _ip_events(turn_tool_blob: str, turn_events) -> list:
    """Ordered [(kind, payload)] of the turn; blob-only callers get the tool
    lines with no earlier messages (all of them precede the final reply)."""
    if turn_events is not None:
        return list(turn_events)
    return [("tool", ln) for ln in (turn_tool_blob or "").splitlines()]


def _ip_object_kept(sentence: str, kind: str, events: list) -> bool:
    """A fitting call after the promise context (see the table above)."""
    _k, _verb, mention, fit = next(o for o in _IP_OBJECT_KINDS if o[0] == kind)
    start = None
    for i, (ek, payload) in enumerate(events):
        if ek == "text" and mention.search(
                "\n".join(_promise_prose_lines(payload))):
            start = i + 1
    if start is None:
        start = len(events) if _IP_REDO_RE.search(sentence) else 0
    recipients = {m.group(1).lower() for m in _IP_RECIPIENT_RE.finditer(sentence)}
    for ek, payload in events[start:]:
        if ek != "tool" or not fit.search(payload):
            continue
        if kind == "send" and recipients and _IP_ADDRESSED_RE.search(payload) \
                and not any(r in payload.lower() for r in recipients):
            continue  # a handoff to someone else
        return True
    return False
# "끝나면 A 합칩니다. 그다음 B 하겠습니다." -- B continues A's condition.
_IP_CHAIN_RE = re.compile(r'^(그\s?다음|그\s?뒤|그\s?후|그러고\s?나서|이어서)')
_IP_FENCE_RE = re.compile(r'```.*?(```|\Z)', re.DOTALL)
_IP_QUOTED_RE = re.compile(r'"[^"\n]*"|“[^”\n]*”|「[^」\n]*」|`[^`\n]*`')
_IP_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+|\n')

# Scope of the check: the last N non-empty prose lines of the final reply.
# The final reply's closing is where an unkeepable promise lives; body
# lines are plan narration and status reports ("끝나면 합칩니다").
# Measured on the 2026-10-02 replay (see DGN-1308): all three misses sit in
# the last two lines.  None = the whole reply.
IDLE_PROMISE_SCOPE_LINES = 2


def _promise_prose_lines(text: str) -> List[str]:
    """Prose lines of the reply with the E1 mention forms removed."""
    body = _IP_FENCE_RE.sub("\n", text or "")
    out = []
    for ln in body.splitlines():
        if not ln.strip() or _is_blockquote(ln):
            continue
        if ln.strip().startswith("[[") or ln.strip().startswith("|"):
            continue  # bridge marker line / table row
        ln = _IP_QUOTED_RE.sub(" ", ln)
        out.append(ln.replace("**", "").replace("__", ""))  # emphasis only
    return out


# Lookback for an armed carrier started in an EARLIER turn that is still in
# flight ("종결 알림 오면 회수하겠습니다" three turns after the dispatch).
# Replay 2026-09-02..10-02: 283 of 627 strict-mode fires were exactly this
# class.  Evidence is still tool_use only -- the adapter scans earlier
# turns' tool calls and keeps the kinds whose window has not run out.  It
# only ever satisfies a CONDITIONAL promise; an unconditional one still
# needs a carrier in this turn.  0 for a kind = this-turn only.
IDLE_PROMISE_ARMED_LOOKBACK_S = {
    "dispatch": 6 * 3600,
    "handoff": 6 * 3600,
    "schedule": 6 * 3600,
    "background": 3600,
}


def idle_promise_armed_kinds(blob: Optional[str]) -> List[str]:
    """Armed carrier kinds present in a tool blob (adapter lookback helper)."""
    if not blob:
        return []
    return [k for k, sig, armed in IDLE_PROMISE_CARRIERS
            if armed and sig.search(blob)]


def _turn_carriers(turn_tool_blob: str):
    """(kinds present, armed?) for the turn blob."""
    kinds = [k for k, sig, _a in IDLE_PROMISE_CARRIERS
             if sig.search(turn_tool_blob)]
    armed = any(a for k, _s, a in IDLE_PROMISE_CARRIERS if k in kinds)
    return kinds, armed


def _shapes_of(sentence: str):
    """[(kind, tool signature)] for every rev2 shape the sentence names."""
    return [(kind, tool_re) for kind, promise_re, tool_re in IDLE_PROMISE_ACTIONS
            if promise_re.search(sentence)]


# FDGN-579: recurring behavior requires persistence, not an ordinary carrier.
_IP_RECUR_KO = re.compile(r'(\uc55e\uc73c\ub85c|\ub2e4\uc74c\ubd80\ud130|\ub9e4\ubc88|\ud56d\uc0c1|\ub2e4\uc2dc\ub294|\ub54c\ub9c8\ub2e4)')
_IP_RECUR_EN = re.compile(r'\b(from now on|going forward|next time|always|never|every time)\b', re.I)
_IP_RECUR_COMMIT_KO = re.compile(r'(?:[\uac00-\ud7a3]+\uaca0(?:\uc2b5\ub2c8\ub2e4|\uc5b4\uc694|\ub2e4)|[\uac00-\ud7a3]+[\uc744\u3139]?\s?\uac8c\uc694|\ud560\uac8c\uc694)')
_IP_RECUR_COMMIT_EN = re.compile(r"\b(?:I|we)\s+(?:(?:will|shall|won['\u2019]t)\b|['\u2019]ll\b)|\b(?:I|we)['\u2019]ll\b", re.I)
_IP_CURRENT_ONLY = re.compile(r'(\uc774\ubc88\s*(\ud134|\ub2f5\ubcc0|\ud55c\s*\ubc88)\ub9cc|\uc9c0\uae08\ub9cc|\b(?:only (?:this turn|this time|for now)|for this turn only)\b)', re.I)
_IP_ATTRIBUTED = re.compile(r'^\s*(?:\uc608\uc2dc|\uc608\ubb38|\uc778\uc6a9|example|quotation|prediction)\s*:', re.I)
_IP_SAVE_QUESTION = re.compile(r'(\uc2a4\ud0ac\ub85c?\s*(?:\uc800\uc7a5|\ub0a8\uaca8|\ub9cc\ub4e4).*?(?:\uae4c\uc694|\?)|(?:shall|should|may|can)\s+I\s+(?:save|store|persist|make).*?\bskill\b.*?\?)', re.I)


def recurring_promises(text):
    """All prose commitments, including negative forms and body positions."""
    out = []
    for line in _promise_prose_lines(text):
        line = re.sub(r"(?<![A-Za-z])'[^'\n]+'(?![A-Za-z])", ' ', line)
        if _IP_ATTRIBUTED.search(line):
            continue
        for sentence in _IP_SENTENCE_SPLIT_RE.split(line):
            # An unrelated later question cannot waive a separate commitment.
            if _IP_SAVE_QUESTION.search(sentence) or _IP_CURRENT_ONLY.search(sentence):
                continue
            ko = _IP_RECUR_KO.search(sentence) and _IP_RECUR_COMMIT_KO.search(sentence)
            en = _IP_RECUR_EN.search(sentence) and _IP_RECUR_COMMIT_EN.search(sentence)
            if (ko or en) and not IDLE_PROMISE_QUESTION_RE.search(sentence.strip()):
                out.append(sentence.strip())
    return out


def recurring_behavior_unkept(text, persisted=()):
    # Mapping is explicitly reviewed by the editor; no semantic paraphrase claim.
    normalize = lambda s: re.sub(r'\s+', ' ', s).strip(' .!*_').casefold()
    kept = {normalize(s) for s in persisted}
    return [s for s in recurring_promises(text) if normalize(s) not in kept]


def recurring_behavior_block_reason(text, persisted=()):
    misses = recurring_behavior_unkept(text, persisted)
    if not misses:
        return None
    return ('IDLE_PROMISE block (FDGN-579 recurring behavior). No applied, '
            'authorized skill update for this behavior in this turn: %r. '
            'Persist the instruction with skill-layer.py update-begin/update-check '
            'and report its actual scope, or propose the concrete behavior and '
            'ask whether to save it as a skill with save/this-turn-only options. '
            'Wait for the answer. Otherwise describe this turn only. A ticket, '
            'dispatch, session memory or unrelated commit does not persist behavior. '
            'The owner never sees this note: do not mention it, a check, gate, '
            'hook, block or rule in the reply.' % misses[0][:200])


def idle_promise_unkept(
    text: Optional[str],
    turn_tool_blob: Optional[str] = None,
    armed_earlier: bool = False,
    persisted_behaviors=(),
    turn_events=None,
) -> List[Tuple[str, str, str]]:
    """Every unkept commitment in the final reply: [(kind, need, sentence)].

    kind  -- a rev2 shape ("dispatch"/"ticket"/"merge") when the sentence
             names one, else "carrier".
    need  -- "signature" (the shape's own command is required), "carrier"
             (any carrier) or "armed" (conditional: an armed carrier).
    [] on no block, including every fail-open path (blob None).

    armed_earlier -- the adapter found an armed carrier from an earlier turn
    still inside its IDLE_PROMISE_ARMED_LOOKBACK_S window.  Satisfies a
    conditional promise only.
    """
    recurring = recurring_behavior_unkept(text or '', persisted_behaviors)
    if recurring:
        return [('recurring', 'persistence', s) for s in recurring]
    if turn_tool_blob is None or not text:
        return []  # cannot contrast -> never block
    lines = _promise_prose_lines(text)
    if IDLE_PROMISE_SCOPE_LINES:
        lines = lines[-IDLE_PROMISE_SCOPE_LINES:]
    if not lines:
        return []
    region = "\n".join(lines)
    if IDLE_PROMISE_OPTIONS_RE.search(text or ""):
        return []  # E3: the reply hands the owner a decision list
    if IDLE_PROMISE_OWNER_Q_RE.search(region.strip()):
        return []  # E3: closes on a question to the owner
    sentences = [c.strip() for c in _IP_SENTENCE_SPLIT_RE.split(region)
                 if c and c.strip()]
    if any(IDLE_PROMISE_QUESTION_RE.search(c.rstrip("*_ ")) for c in sentences):
        return []  # E3: the closing lines put a question to the owner
    kinds, armed = _turn_carriers(turn_tool_blob)
    armed = armed or bool(armed_earlier)
    unkept = []
    prev_cond = False
    for i, sent in enumerate(sentences):
        conditional = bool(IDLE_PROMISE_EVENT_COND_RE.search(sent)) or (
            prev_cond and bool(_IP_CHAIN_RE.search(sent)))
        prev_cond = conditional
        if recurring_promises(sent):
            continue  # checked against persisted evidence above
        if not _ip_commits(sent):
            continue
        if IDLE_PROMISE_EXEMPT_RE.search(sent):
            continue  # E4
        if IDLE_PROMISE_OWNER_TRIGGER_RE.search(sent):
            continue  # E2
        if i and IDLE_PROMISE_OWNER_REQUEST_RE.search(sentences[i - 1]):
            continue  # E2: the owner's response triggers this commitment
        shapes = _shapes_of(sent)
        # DGN-2164: writing a ticket is tracking, not starting its fix. A
        # carrier for another ticket cannot keep this ticket's promise.
        targets = set(re.findall(r"\b[A-Z][A-Z0-9]*-\d+\b", sent))
        if not targets:
            targets = set(re.findall(r"worklog/([A-Z][A-Z0-9]*-\d+)", turn_tool_blob))
        ticket_only = (shapes and all(k == "ticket" for k, _ in shapes)
                       and not any(_ip_commits(promise_re.sub("", sent))
                                   for kind, promise_re, _ in IDLE_PROMISE_ACTIONS if kind == "ticket"))
        ticket_bound = bool(targets and not ticket_only)
        if ticket_bound:
            bound = set(armed_earlier.get("ticket_ids", [])) if conditional and isinstance(armed_earlier, dict) else set()
            for line in turn_tool_blob.splitlines():
                if any(k in ("dispatch", "background", "schedule") and sig.search(line)
                       for k, sig, _ in IDLE_PROMISE_CARRIERS):
                    bound.update(re.findall(r"\b[A-Z][A-Z0-9]*-\d+\b", line))
            if not targets.issubset(bound):
                unkept.append(("carrier", "ticket_bound", sent))
                continue
        if any(tool_re.search(turn_tool_blob) for _k, tool_re in shapes):
            continue  # a named action itself ran (kept shape wins, rev2)
        shape = shapes[0][0] if shapes else None
        if conditional:
            if not armed:
                unkept.append((shape or "carrier", "armed", sent))
        elif shape is not None:
            # A named shape keeps its rev2 contract: its own signature.
            unkept.append((shape, "signature", sent))
        elif IDLE_PROMISE_DEFERRED_RE.search(sent):
            if not any(k in IDLE_PROMISE_QUEUE_CARRIERS for k in kinds) and not (targets and "background" in kinds):
                unkept.append(("carrier", "queue", sent))
        elif _ip_object_kinds(sent) and not ticket_bound:
            # DGN-2328: the evidence has to fit the object and follow the
            # promise context; any carrier anywhere no longer does.  A
            # carrier bound to the promise's ticket id (above) already fits.
            events = _ip_events(turn_tool_blob, turn_events)
            for obj in _ip_object_kinds(sent):
                if not _ip_object_kept(sent, obj, events):
                    unkept.append(("carrier", "fit:" + obj, sent))
                    break
        elif not kinds:
            unkept.append(("carrier", "carrier", sent))
    return unkept


# DGN-2328 (2026-10-27 21:23): a block made the agent tell the owner about
# the gate in its next reply.  The owner never sees this feedback, so the
# rewritten reply must read as if it never arrived; and a sentence that
# promised nothing pending gets a rewrite, not a manufactured tool call.
IDLE_PROMISE_REMEDY_TAIL = (
    "  If the sentence promises no pending work -- it states how things stay "
    "or reports work already done -- rewrite it as a plain present- or "
    "past-tense statement; do not run a tool just to satisfy this note.\n"
    "Do not restate the promise. The owner never sees this note: write the "
    "reply as if it never arrived -- do not mention it, a check, gate, hook, "
    "block or rule, and do not apologise for or explain the rewrite.")


def idle_promise_block_verdict(
    text: Optional[str],
    turn_tool_blob: Optional[str] = None,
    armed_earlier: bool = False,
    persisted_behaviors=(),
    turn_events=None,
) -> Optional[Tuple[str, str]]:
    """(shape kind, model-facing block reason) for an unkeepable promise.

    DGN-1308: the default is inverted -- see the carrier section above.
    None -- i.e. no block -- when every commitment in the reply's closing
    lines is exempt or carried, and on every fail-open path (turn_tool_blob
    None: the adapter could not read the turn).

    The kind is returned so the adapter can consult that shape's own kill
    switch ("carrier" for a promise outside the rev2 shapes); callers that
    only need the reason use idle_promise_block_reason.
    """
    recurring_reason = recurring_behavior_block_reason(text, persisted_behaviors)
    if recurring_reason:
        return 'recurring', recurring_reason
    unkept = idle_promise_unkept(text, turn_tool_blob, armed_earlier,
                                 persisted_behaviors, turn_events)
    if not unkept:
        return None
    kind, need, _sent = unkept[0]
    need_txt = {
        "fit:%s" % obj: ("nothing in this turn after the promise came up "
                         "does it -- only %s made after that point keeps it; "
                         "an earlier or unrelated call does not" % label)
        for obj, label in _IP_OBJECT_LABEL.items()}
    need_txt.update({
        "ticket_bound": "the ticket has no dispatch, background job or wakeup bound to its id; a ticket write does not start a fix",
        "signature": "the %s command itself did not run in this turn" % kind,
        "carrier": ("this turn left no evidence the work started (no commit, "
                    "dispatch, handoff, background job or "
                    "scheduled wakeup/cron)"),
        "queue": ("it defers a separate item, and nothing in this turn queues "
                  "it (no dispatch, handoff or schedule -- a "
                  "commit or job for other work does not carry it)"),
        "armed": ("it is conditional on an event, but nothing armed in this "
                  "turn will bring that event back (no dispatch, background "
                  "job, handoff, wakeup, cron or monitor)"),
    })
    clauses = "\n".join("  - %r: %s" % (s[:160], need_txt[n])
                        for _k, n, s in unkept[:3])
    return kind, (
        "IDLE_PROMISE block (DGN-1308). Your reply commits to a %s in its "
        "closing lines and the turn is ending, so nothing can keep the "
        "promise:\n%s\n"
        "Do one of these now, in this turn:\n"
        "  (a) do it now -- start the work with a tool call (or finish it) "
        "and report the concrete result, or\n"
        "  (b) write the tracked item and say plainly it is NOT STARTED and where "
        "it is tracked.\n"
        "%s" % ("piece of work" if kind == "carrier" else kind, clauses,
                IDLE_PROMISE_REMEDY_TAIL)
    )


def idle_promise_block_reason(
    text: Optional[str],
    turn_tool_blob: Optional[str] = None,
) -> Optional[str]:
    """Reason-only view of idle_promise_block_verdict (v1 call signature)."""
    verdict = idle_promise_block_verdict(text, turn_tool_blob)
    return None if verdict is None else verdict[1]


# --- IDRILL_PROSE_Q (DGN-983 / DGN-1527) -------------------------------- #
# A turn that arms an idrill keyboard sometimes ALSO asks in prose for the
# value the keyboard collects.  Measured by the pack author on headless runs
# (not re-run here, no live leak exists in local transcripts): 1/3 leaked
# before the SKILL was tightened, 0/5 after.  The marker itself was attached
# 8/8.  The leaked form was "몇 개 하셨는지 아래에서 탭해 주세요": a count
# question welded to a pointer at the buttons.
#
# Landing order (DGN-1527, the gate's own "measure before block" rule):
#   stage 1 (this build)  normalize + count.  The bridge removes the duplicate
#                         question sentence and appends one counter line per
#                         removal to the output-gate canary log.
#   stage 2 (not built)   block promotion, decided only once that count is
#                         stable.  A count that falls toward 0 means the
#                         prompt layer learned; a flat count means the
#                         machine is carrying it.
#
# Why remove and not rewrite: the keyboard message already carries the
# pack-declared prompt copy (arm step_text["1"]), so the removed sentence is
# a true duplicate and the framework authors no owner-facing copy (DGN-983
# point 2: copy needs owner confirmation).  Why not a silent drop: every
# removal is counted WITH its sentence, so "did the leak stop or did the gate
# eat it" stays answerable (DGN-1527).
#
# Predicate replay 2026-09-30 (local transcripts; no live leak exists there):
#   74 real texts carrying a standalone marker (consumer stdout, harness runs)
#     -> 0 fires (first cut: 1, a dumped arm JSON line -> JSON guard added);
#   8855 unique assistant messages of four live agent sessions,
#     predicate forced on as if a keyboard rode along -> 3 fires, all genuine
#     count questions in dev chat that no keyboard accompanies (first cut: 28;
#     25 were bare request stems, "...니까요", or quoted copy -> narrowed).
#
# Scope is deliberately narrow; recall is traded for precision because a
# false positive deletes an owner-facing sentence:
#   - only a message that carries an idrill marker (the bridge additionally
#     requires that the arm will actually render a keyboard);
#   - only prose: code fences, blockquotes, table rows and marker lines are
#     never touched;
#   - sentence-level: a sentence is removed only when it asks for a count
#     AND reads as a question or a request for input.  A plain pointer at the
#     buttons ("아래에서 반복수 눌러주세요") is NOT a question and stays.

IDRILL_PROSE_Q_RULE_ID = "IDRILL_PROSE_Q"

# Mirror of bridge/formatting.IDRILL_MARKER_RE (this lib stays portable and
# cannot import the bridge).  Parity is pinned by the bridge test
# test_dgn983_idrill_prose_gate.py.
IDRILL_MARKER_LINE_RE = re.compile(r"^\[\[IDRILL:([0-9a-f]{8})\]\]$")

# "how many <unit>": the count ask itself.  "몇 번째 / 몇 세트째" asks WHICH,
# not how many, and is excluded.
_IDRILL_COUNT_ASK_RE = re.compile(
    r"몇\s*(?:개|회|번(?!\s*째)|세트(?!\s*째)|렙|rep|키로|킬로|kg)"
    r"|\bhow\s+many\b",
    re.IGNORECASE,
)
# The count as a noun, for asks that carry no "몇" ("반복수 알려주세요").
_IDRILL_COUNT_NOUN_RE = re.compile(
    r"반복\s*(?:수|횟수)|횟수|개수|렙\s*수|\breps?\b|\brepetitions?\b",
    re.IGNORECASE,
)
# The sentence is a question.  "...니까요" is a because-ending, not a question.
_IDRILL_QUESTION_END_RE = re.compile(
    r"(?:[?？]|나요|(?<!니)까요|습니까|셨어요|했어요|셨나요|했나요|셨죠|했죠)"
    r"[\s.!~)\"']*$"
)
# The sentence requests input (by typing or by tapping).  The verb must be in
# a REQUEST form: a bare stem also matches the agent's own statements and
# offers ("적어요", "알려 드릴게요", "말씀하신", "입력받는").
_IDRILL_REQUEST_RE = re.compile(
    r"(?:알려|말해|말씀해|적어|써|입력해|보내|남겨|골라|선택해|탭해|눌러|찍어)"
    r"\s*(?:주(?:세요|시겠|실래|십시오|셔요|시면)|줘|달라)"
    r"|\b(?:tell\s+me|enter|type|let\s+me\s+know|tap|pick)\b",
    re.IGNORECASE,
)
# A pointer at the keyboard.  Only consulted for the noun form: "반복수를
# 아래 버튼으로 입력해 주세요" points at the buttons and is not a duplicate ask.
_IDRILL_BUTTON_POINTER_RE = re.compile(
    r"아래|밑에|버튼|키보드|탭|눌러|\b(?:below|button|keyboard|tap)\b",
    re.IGNORECASE,
)
_IDRILL_QMARK_END_RE = re.compile(r"[?？][\s.!~)\"']*$")
_IDRILL_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?？。])(\s+)")
_IDRILL_OTHER_MARKER_RE = re.compile(r"^\s*(?:\[\[|[a-z_]+::)")
# A JSON-shaped line (a dumped arm file, a tool payload) is not prose.
_IDRILL_JSONISH_RE = re.compile(r'"\s*:\s*[\[{"\d]')
# Quoted spans are mentions (a quoted label, quoted copy), not the ask itself.
_IDRILL_QUOTED_RE = re.compile(r'"[^"]*"|“[^”]*”|`[^`]*`|\'[^\']*\'')


def is_idrill_count_question(sentence: str) -> bool:
    """True when one prose sentence asks for the count an idrill keyboard
    collects.  Pure predicate, no marker condition (the caller owns that)."""
    s = _IDRILL_QUOTED_RE.sub(" ", sentence or "").strip()
    if not s:
        return False
    if _IDRILL_COUNT_ASK_RE.search(s):
        return bool(_IDRILL_QUESTION_END_RE.search(s)
                    or _IDRILL_REQUEST_RE.search(s))
    if _IDRILL_COUNT_NOUN_RE.search(s):
        # Without "몇" a bare 셨어요/했어요 ending reads as a statement just as
        # often ("반복수가 늘어나셨어요"), so the noun form needs a literal "?".
        if _IDRILL_QMARK_END_RE.search(s):
            return True
        return bool(_IDRILL_REQUEST_RE.search(s)
                    and not _IDRILL_BUTTON_POINTER_RE.search(s))
    return False


def has_idrill_marker(text: Optional[str]) -> bool:
    """True when `text` carries a standalone [[IDRILL:<id>]] line."""
    if not text or "[[IDRILL:" not in text:
        return False
    return any(IDRILL_MARKER_LINE_RE.match(ln.strip())
               for ln in text.split("\n"))


def normalize_idrill_prose_question(
    text: Optional[str],
    keyboard_armed: Optional[bool] = None,
) -> Tuple[str, List[str]]:
    """Remove prose count-questions from a message that arms a keyboard.

    Returns (text, removed_sentences).  keyboard_armed=None derives the
    condition from a marker line in `text`; the bridge passes True after it
    has stripped the marker and confirmed the arm renders.  When nothing is
    removed the input is returned unchanged (same object).  Never raises on
    str input.  Masked lines (fences, blockquotes, tables, markers) and
    every non-matching sentence are kept byte-identical.
    """
    if not text:
        return text or "", []
    armed = has_idrill_marker(text) if keyboard_armed is None \
        else bool(keyboard_armed)
    if not armed:
        return text, []
    lines = text.split("\n")
    out: List[Optional[str]] = []
    removed: List[str] = []
    in_fence = False
    for ln in lines:
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            out.append(ln)
            continue
        if (in_fence or not ln.strip() or _is_blockquote(ln)
                or _TABLE_ROW_RE.match(ln)
                or _IDRILL_OTHER_MARKER_RE.match(ln)
                or _IDRILL_JSONISH_RE.search(ln)):
            out.append(ln)
            continue
        parts = _IDRILL_SENTENCE_SPLIT_RE.split(ln)
        # parts alternates [sentence, sep, sentence, sep, ..., sentence]
        sentences = parts[0::2]
        seps = parts[1::2]
        hit = [is_idrill_count_question(s) for s in sentences]
        if not any(hit):
            out.append(ln)
            continue
        removed.extend(s.strip() for s, h in zip(sentences, hit) if h)
        lead = ln[:len(ln) - len(ln.lstrip())]
        kept: List[str] = []
        for i, (s, h) in enumerate(zip(sentences, hit)):
            if h:
                continue
            if kept:
                kept.append(seps[i - 1] if i - 1 < len(seps) else " ")
            kept.append(s.strip() if not kept else s)
        joined = "".join(kept).strip()
        out.append((lead + joined) if joined else None)
    if not removed:
        return text, []
    # Drop emptied lines; a blank line that the drop leaves touching another
    # blank line (or the top of the message) goes with it.
    result: List[str] = []
    after_drop = False
    for ln in out:
        if ln is None:
            after_drop = True
            continue
        if after_drop and not ln.strip() and (not result
                                              or not result[-1].strip()):
            after_drop = False
            continue
        after_drop = False
        result.append(ln)
    return "\n".join(result).rstrip(), removed


def _check_idrill_prose_question(text: str) -> Optional[Violation]:
    """IDRILL_PROSE_Q detect view for check_message (Stop-hook canary)."""
    _, removed = normalize_idrill_prose_question(text)
    if not removed:
        return None
    return Violation(
        rule_id=IDRILL_PROSE_Q_RULE_ID,
        severity=SEVERITY_DETECT,
        reason=("message arms an idrill keyboard and also asks for the same "
                "input in prose (DGN-983)"),
        metrics={"sentences": len(removed)},
        excerpt=removed[0][:60],
    )


# --- Reply containers and filler (FDGN-595) ----------------------------- #
FORMAT_RULE_IDS = frozenset(("FENCE_PROSE", "BROKEN_QUOTE", "SELF_BLAME"))
_FORMAT_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})([^`]*)$")
_FORMAT_QUOTE_RE = re.compile(r"^ {0,3}>(!?)[ \t]?(.*)$")
_FORMAT_COPY_RE = re.compile(
    r"(?:\b(?:copy|paste|save|relay|forward|send)\b.{0,70}"
    r"\b(?:verbatim|following|below|file|message|text|to)\b|"
    r"\b(?:file contents|relay text|message to (?:send|relay|forward))\b|"
    r"(?:\ubcf5\uc0ac|\ubd99\uc5ec\s*\ub123|\uadf8\ub300\ub85c\s*(?:\ubcf4\ub0b4|\uc804\ub2ec|\uc800\uc7a5))"
    r".{0,50}(?:\ud14d\uc2a4\ud2b8|\ub0b4\uc6a9|\uba54\uc2dc\uc9c0|\ud30c\uc77c|\uc544\ub798|\ub2e4\uc74c)|"
    r"(?:\uc544\ub798|\ub2e4\uc74c).{0,50}(?:\uadf8\ub300\ub85c\s*)?"
    r"(?:\ubcf5\uc0ac|\ubd99\uc5ec\s*\ub123|\uc804\ub2ec|\uc800\uc7a5))", re.I)
_FORMAT_CODE_RE = re.compile(
    r"^(?:[#$] |#!|//|/\*|\* |-- |[{}\[\]();]+\s*[,;]?\s*$|"
    r"(?:sudo\s+)?(?:bash|sh|zsh|git|python[23]?|node|npm|npx|pnpm|yarn|"
    r"uv|pip[3]?|rg|grep|sed|awk|cat|echo|printf|cd|ls|cp|mv|rm|mkdir|"
    r"chmod|curl|wget|make|docker|kubectl|export|source|set|test|true|false)\b|"
    r"(?:\./|/|~/)[^\s]+(?:\s|$)|"
    r"(?:def|class|import|from|return|raise|assert|print|if|elif|else|for|while|"
    r"try|except|with|function|const|let|var|async|await|SELECT|INSERT|UPDATE|"
    r"CREATE|FROM|WHERE)\b|"
    r"[A-Za-z_][\w.\[\]'\"]*\s*(?:=|\+=|:=|\(|\{)|"
    r"[\"'][\w.-]+[\"']\s*:|"
    r"[\w.-]+\s*:\s*(?:[\d\[\]{]|true\b|false\b|null\b)|"
    r"(?:-\s+)?(?!(?:why|decision|example|sample|option|choice|confirmed)\b)[A-Za-z_][\w.-]*:\s+|"
    r"\[[\w.-]+\]$)", re.I)
_FORMAT_PROSE_RE = re.compile(
    r"[\uac00-\ud7a3]|"
    r"^(?:why|decision|example|sample|option|choice|confirmed)\s*[:\-]|"
    r"\b[A-Za-z]+\s+[A-Za-z]+\b.*[.!?]$", re.I)
_FORMAT_BLAME_RE = re.compile(
    r"(?:^|[.!?,]\s+)[\s*_-]*(?:"
    r"\uc81c\s*(?:\uc2e4\uc218|\uc798\ubabb)\uc785\ub2c8\ub2e4|"
    r"\uc6d0\uc778\uc740\s*\uc800\uc785\ub2c8\ub2e4|"
    r"\uc8c4\uc1a1\ud569\ub2c8\ub2e4|(?:that was |it was )?my\s+(?:mistake|fault)|"
    r"(?:I(?: am|'m)\s+)?sorry)"
    r"(?:[.!?]+[*_]*(?=\s|$)|[*_]*(?=$))", re.I)
_FORMAT_ROW_RE = re.compile(
    r"^(?:[-\u2022]\s+|\u2514\s*|[|\u2502\u2503]\s*|[+-]\s+|"
    r"\S[^\n]{0,40}?[\u2580-\u259f]{2,}|[\u2580-\u259f]{2,}|"
    r"[\u2500\u2501\u2014-]{3,}\s*$)")


def _format_fences(text: str):
    """Yield exact fence spans, including unclosed fences and long delimiters."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _FORMAT_FENCE_RE.match(lines[i])
        if not match:
            i += 1
            continue
        start, delim = i, match[1]
        close = re.compile(r"^\s*" + re.escape(delim[0]) +
                           r"{" + str(len(delim)) + r",}\s*$")
        i += 1
        body = []
        while i < len(lines) and not close.match(lines[i]):
            body.append(lines[i])
            i += 1
        yield start, min(i + 1, len(lines)), body
        i += 1


def _format_row(line: str) -> bool:
    """True for a non-prose list, card, table, amount, or divider row."""
    return bool(_FORMAT_ROW_RE.match(line.strip()))


def _format_finished(line: str) -> bool:
    stripped = line.strip()
    return (_format_row(stripped)
            or bool(re.search(r"[.!?\u3002][\"'\u201d\u2019*_)]*$", stripped)))


def reply_format_violations(text: Optional[str]) -> List[Violation]:
    """Conservative container checks; copy intent is local to each fence.

    Language tags alone cannot launder a prose example as code. Conversely,
    command/config syntax and explicit file/relay copy material stay intact.
    Fences are masked before inspecting quotes and standalone filler.
    """
    if not text:
        return []
    lines = text.splitlines()
    visible = list(lines)
    found = {}
    for start, end, body in _format_fences(text):
        visible[start:end] = [lines[start]] + [''] * (end - start - 1)
        # Only the immediately preceding paragraph can declare copy intent.
        context = []
        j = start - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        while (j >= 0 and lines[j].strip() and len(context) < 3
               and not _FORMAT_FENCE_RE.match(lines[j])):
            context.insert(0, lines[j])
            j -= 1
        if _FORMAT_COPY_RE.search(' '.join(context)):
            continue
        try:
            if isinstance(json.loads('\n'.join(body)), (dict, list)):
                continue
        except (ValueError, TypeError):
            pass
        example = bool(re.search(
            r"\b(?:example|sample|candidate|decision)\b|"
            r"\uc608\uc2dc|\ud6c4\ubcf4|\uc2dc\uc548", ' '.join(context), re.I))
        # Do not promote the existing detect-only grid rule by accident.
        # A data grid without sentence/example labels is not prose evidence.
        rows = [line for line in body if line.strip()
                and not _GRID_RULE_LINE_RE.match(line)]
        if (not example and rows
                and all(len(_grid_cells(line)) >= 2 for line in rows)
                and not any(re.match(r"^[A-Za-z\uac00-\ud7a3][A-Za-z\uac00-\ud7a3 ]{0,30}:", line.strip())
                            or re.search(r"[.!?]$", line.strip()) for line in rows)):
            continue
        heredoc = None
        for line in body:
            stripped = line.strip()
            if heredoc:
                if stripped == heredoc:
                    heredoc = None
                continue
            machine = _FORMAT_CODE_RE.search(stripped)
            if machine:
                delimiter = re.search(r"<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?", stripped)
                if delimiter:
                    heredoc = delimiter[1]
            if (stripped and not machine
                    and (_FORMAT_PROSE_RE.search(stripped)
                         or (example and re.search(r"[A-Za-z]", stripped)))):
                found.setdefault('FENCE_PROSE', stripped)
                break

    i = 0
    while i < len(visible):
        match = _FORMAT_QUOTE_RE.match(visible[i])
        if not match:
            i += 1
            continue
        start, fold = i, bool(match[1])
        while i < len(visible) and _FORMAT_QUOTE_RE.match(visible[i]):
            i += 1
        bodies = [_FORMAT_QUOTE_RE.match(line)[2].strip()
                  for line in visible[start:i]]
        if i - start > 3 and not fold and not all(_format_row(body)
                                                  for body in bodies):
            found.setdefault('BROKEN_QUOTE', visible[start])
        # A quote ending mid-sentence must not spill into unquoted prose or
        # resume after an unquoted blank. Complete, separate quotes are legal.
        last = _FORMAT_QUOTE_RE.match(visible[i - 1])[2].strip()
        j = i
        while j < len(visible) and not visible[j].strip():
            j += 1
        if last and not _format_finished(last) and j < len(visible):
            next_line = visible[j].strip()
            continuation = (j == i or _FORMAT_QUOTE_RE.match(visible[j])
                            or re.match(r"[a-z]", next_line)
                            or re.search(r"[,;:]$|\b(?:and|or|because|continues)$|"
                                         r"(?:\uace0|\uba70|\ub294\ub370|\ud574\uc11c|\ud558\uba74)$", last))
            if continuation and not re.match(
                    r"^(?:#{1,6} |[-*] |\d+[.)] |\[\[|send_file::|```|~~~)",
                    next_line):
                found.setdefault('BROKEN_QUOTE', visible[start])

    for line in visible:
        # Quoted text and inline quoted examples describe filler, not own it.
        if not _FORMAT_QUOTE_RE.match(line) and _FORMAT_BLAME_RE.search(_IP_QUOTED_RE.sub(' ', line)):
            found.setdefault('SELF_BLAME', line.strip())
    reasons = {
        'FENCE_PROSE': 'Reader-only prose or an example is inside a code fence.',
        'BROKEN_QUOTE': 'A quote is interrupted mid-thought or exceeds three lines without >! folding.',
        'SELF_BLAME': 'Remove standalone self-blame or apology filler.',
    }
    return [Violation(rule_id=rule, severity=SEVERITY_DETECT,
                      reason=reasons[rule], excerpt=excerpt[:100])
            for rule, excerpt in found.items()]


def reply_format_block_reason(text: Optional[str]) -> Optional[str]:
    hits = reply_format_violations(text)
    if not hits:
        return None
    return ('Rewrite the outgoing reply: ' + ' '.join(v.reason for v in hits) +
            ' Use plain prose and show examples exactly as they appear on screen. '
            'Reserve fences for commands, config, file contents, or explicit relay text. '
            'Keep needed quotes contiguous; use >! only for skippable detail. '
            'State the correction or result directly. This feedback is for you only; '
            'never mention this check or its refusal to the owner.')


# --- FENCE_CJK_GRID (DGN-1645) ------------------------------------------- #
# Rule source: rules/cold.framework.text.md Tables -- "Tier 1: values /
# numbers / status only, no natural-language column" and "CJK text never
# aligns in a code block -- never attempt a multi-column grid in a code
# block."  The rule was hot-injected for weeks (DGN-038, DGN-085 done) and was
# still broken twice in a row on 2026-09-23 (Korean label column, space-
# aligned, inside a fence).  The bridge's DGN-1889 reshaping covers markdown
# pipe tables in PROSE only; a fence is passed through verbatim, so nothing
# saw this shape.  This is the detector for it.
#
# A fence is flagged when it holds at least FENCE_CJK_GRID_MIN_ROWS grid rows:
# a line that splits into 2+ non-empty cells (on `|`, a box-drawing bar, or a
# run of 2+ spaces after the indent) with CJK in at least one cell.  Code
# fences (an info string naming a language) are skipped: a CJK comment after
# aligned code is not a table.  Detect-only, like every other rule here.
FENCE_CJK_GRID_RULE_ID = "FENCE_CJK_GRID"
FENCE_CJK_GRID_MIN_ROWS = 2
_FENCE_OPEN_RE = re.compile(r"^\s*(```|~~~)\s*([^\s`]*)")
_FENCE_PLAIN_INFO = ("", "text", "txt", "plain", "plaintext")
_GRID_CJK_RE = re.compile(
    r"[\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u4e00-\u9fff\uac00-\ud7a3]")
_GRID_CELL_SPLIT_RE = re.compile(r"\s*[|\u2502\u2503]\s*| {2,}|\t+")
_GRID_RULE_LINE_RE = re.compile(r"^[\s|:\-+=\u2500-\u257f]*$")


def _grid_cells(line: str) -> List[str]:
    """Non-empty cells of one fence line (indent stripped first)."""
    return [c for c in _GRID_CELL_SPLIT_RE.split(line.strip()) if c.strip()]


def fence_cjk_grids(text: Optional[str]) -> List[dict]:
    """Every plain fence that holds a CJK multi-column grid.

    Returns [{"rows": n, "first": <first grid row>}] per flagged fence.  An
    unterminated fence is still scanned: the reader sees it as a block.
    """
    if not text:
        return []
    found: List[dict] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        delim, info = m.group(1), m.group(2).lower()
        body: List[str] = []
        i += 1
        while i < len(lines) and not lines[i].strip().startswith(delim):
            body.append(lines[i])
            i += 1
        i += 1  # closing delimiter (or end of text)
        if info not in _FENCE_PLAIN_INFO:
            continue
        rows = [ln for ln in body
                if not _GRID_RULE_LINE_RE.match(ln)
                and len(_grid_cells(ln)) >= 2
                and _GRID_CJK_RE.search(ln)]
        if len(rows) >= FENCE_CJK_GRID_MIN_ROWS:
            found.append({"rows": len(rows), "first": rows[0].strip()})
    return found


def _check_fence_cjk_grid(text: str) -> Optional[Violation]:
    """FENCE_CJK_GRID detect view for check_message (Stop-hook canary)."""
    grids = fence_cjk_grids(text)
    if not grids:
        return None
    return Violation(
        rule_id=FENCE_CJK_GRID_RULE_ID,
        severity=SEVERITY_DETECT,
        reason=("multi-column grid with a CJK column inside a code block; "
                "Tier 1 fences are values-only (DGN-1645)"),
        metrics={"fences": len(grids),
                 "rows": sum(g["rows"] for g in grids)},
        excerpt=grids[0]["first"][:60],
    )


# --- WHAT_SOLO_PICK (DGN-2049) ------------------------------------------- #
# Canon (rules/hot.framework.md Problem solving, How vs What split): a WHAT
# choice -- anything the user sees or owns as output (wording, look,
# emoji/glyphs, layout, names, tone) -- goes out as 2-7 rendered candidates
# + one recommendation and the USER picks.  Incident 2026-10-13: the owner
# called three emoji candidates ugly; the agent read that as "don't make me
# choose" and decided alone, twice (ruler emoji + "ro galgeyo" = "I'll go
# with it", then the same with a target emoji).
#
# Fires on a first-person decision ("<X> + ro + galgeyo / jeonghaesseoyo /
# halgeyo" -- I'll go with / I decided on / I'll do X)
# whose object X is a What item, when the current turn shows no candidate
# list and the user's own prompt this turn neither picked nor delegated.
# "Preceding" means since the user's last message: a list from an EARLIER
# turn does not count -- in the incident the candidates were one turn back
# and the owner had rejected them.
#
# X is a What item only on a positive signal: X carries an emoji/symbol,
# X is a quoted span (a name/wording), X names a What noun (emoji, wording,
# title ...), or X is a demonstrative/number pick ("this one", "no. 2")
# inside a sentence that names a What noun.  A How noun in X (method,
# order, tool ...) vetoes.  Everything else -- a tool name ("go with
# pytest"), a plan ("go in this order") -- is How and never fires: How is
# decided, not asked.
# Masked before matching: code fences, inline code, blockquotes (quoting
# the user) and quoted spans (a quoted decision phrase is talk ABOUT it).
#
# Ladder: the lib only detects; the Stop adapter records every hit to the
# canary and blocks only in mode `block` (record-first, DGN-1279 precedent).

WHAT_SOLO_PICK_RULE_ID = "WHAT_SOLO_PICK"

_WSP_QUOTE_MARK = "\ue000"
_WSP_FENCE_RE = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)", re.DOTALL)
_WSP_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_WSP_QUOTED_RE = re.compile(
    "\"[^\"\\n]{1,80}\"|\u201c[^\u201d\\n]{1,80}\u201d|"
    "\u2018[^\u2019\\n]{1,80}\u2019|'[^'\\n]{1,80}'|"
    "\u300c[^\u300d\\n]{1,80}\u300d|\u300e[^\u300f\\n]{1,80}\u300f")
_WSP_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\uff1f\u3002])\s+|\n")
_WSP_DECIDE_RE = re.compile(
    r"(?P<obj>[^\s,.!?:;()\[\]]{1,24})\s*(?:으로|로)\s*"
    r"(?P<verb>갈게요|가겠습니다|갑니다|갈게|할게요|하겠습니다|할게|"
    r"정했어요|정했습니다|정했다|정할게요|결정했어요|결정했습니다|"
    r"확정했어요|확정했습니다|확정할게요|쓸게요|쓰겠습니다|"
    r"바꿀게요|바꾸겠습니다|바꿨어요|바꿨습니다)"
    r"(?=[\s.!~,)\]]|$)")
_WSP_WHAT_NOUN_RE = re.compile(
    r"이모지|이모티콘|아이콘|글리프|기호|문구|워딩|표현|말투|어투|톤|"
    r"이름|명칭|네이밍|제목|타이틀|라벨|레이블|색상|색깔|컬러|배색|"
    r"레이아웃|디자인|폰트|글꼴|모양|로고|썸네일|호칭|닉네임|슬로건|"
    r"\b(?:emoji|icon|glyph|wording|title|label|colou?r|layout|font|"
    r"tone|logo)s?\b", re.I)
_WSP_HOW_NOUN_RE = re.compile(
    r"방식|방법|순서|도구|툴|구현|구조|경로|스크립트|브랜치|테스트|커밋|"
    r"파이프라인|명령|커맨드|워크플로|절차|단계|라이브러리|패키지|설정")
_WSP_PICK_REF_RE = re.compile(
    r"^(?:이걸|그걸|저걸|이거|그거|저거|이것|그것|저것|이쪽|그쪽|"
    r"\d{1,2}번|[A-Za-z]안|첫\s?번째|두\s?번째|세\s?번째)$")
_WSP_CANDIDATE_LINE_RE = re.compile(
    r"^\s*(?:>\s*)?(?:\d{1,2}[.\u3001)\uff09]|[\u2460-\u2468])\s*\S")
_WSP_USER_PICK_RE = re.compile(
    r"^\s*\d{1,2}\s*번?\s*[.!~]*\s*$|\d{1,2}\s*번\s*(?:으로|로|이|가|요|$)|"
    r"(?:이걸|그걸|저걸|이거|그거|저거)\s*(?:로|으로)|"
    r"(?:으로|로)\s*(?:해|가|하자|가자|할게|갈게|정해|해줘|바꿔)")
_WSP_USER_DELEGATE_RE = re.compile(
    r"알아서|맡길게|맡겨|네가\s?정해|니가\s?정해|골라\s?줘|정해\s?줘|"
    r"아무거나|추천대로|추천한\s?걸로|ㅇㅇ|ㅇㅋ|오케이|\bok\b|\bgo\b|"
    r"(?<!안)(?<!안\s)(?:좋아|좋네|좋다)(?![^.!?~\n]{0,12}(?:않|없))|"
    r"(?:^|\s)그래(?:요)?(?=[\s.!~]|$)", re.I)


def _wsp_symbolic(s: str) -> bool:
    """True when s carries an emoji / pictograph / symbol glyph."""
    import unicodedata
    for ch in s:
        cp = ord(ch)
        if 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF \
                or 0x2B00 <= cp <= 0x2BFF:
            return True
        if unicodedata.category(ch) == "So":
            return True
    return False


def _wsp_mask(text: str) -> List[str]:
    """Visible prose lines, fences/inline code/blockquotes dropped and
    quoted spans replaced by one marker char (a quoted name stays a
    candidate object; a quoted decision phrase loses its verb)."""
    t = _WSP_FENCE_RE.sub("\n", text or "")
    t = _WSP_INLINE_CODE_RE.sub(" ", t)
    t = _WSP_QUOTED_RE.sub(_WSP_QUOTE_MARK, t)
    return [ln for ln in t.split("\n")
            if ln.strip() and not _is_blockquote(ln)]


def what_candidates_shown(texts) -> bool:
    """True when any of texts renders a candidate list: an [[OPTIONS]]
    menu or 2+ numbered lines (plain or blockquote Tier-2 layout)."""
    for t in texts or ():
        if not t:
            continue
        if OPTIONS_MARKER in t:
            return True
        body = _WSP_FENCE_RE.sub("\n", t)
        n = sum(1 for ln in body.split("\n")
                if _WSP_CANDIDATE_LINE_RE.match(ln))
        if n >= 2:
            return True
    return False


def _wsp_user_picked(user_prompt: str, obj: str) -> bool:
    """The user's own prompt this turn picked or delegated the choice."""
    p = (user_prompt or "").strip()
    if not p:
        return False
    if _WSP_USER_PICK_RE.search(p) or _WSP_USER_DELEGATE_RE.search(p):
        return True
    core = obj.replace(_WSP_QUOTE_MARK, "").strip()
    return bool(core) and core in p


def what_solo_pick(
    text: Optional[str],
    turn_texts: Optional[List[str]] = None,
    user_prompt: Optional[str] = None,
) -> Optional[dict]:
    """First WHAT decision the agent made alone in `text`, or None.

    Returns {"phrase", "object", "signal"} -- signal is emoji | quoted |
    noun | pick-ref.  turn_texts = every assistant message of the current
    turn (None -> `text` alone); user_prompt = the user's message that
    opened the turn (None/empty -> the pick/delegate exemption cannot be
    read, so the hit still records; the adapter never blocks on it).
    """
    if not text or not text.strip():
        return None
    if what_candidates_shown(turn_texts if turn_texts else [text]):
        return None
    for line in _wsp_mask(text):
        for sent in _WSP_SENTENCE_SPLIT_RE.split(line):
            for m in _WSP_DECIDE_RE.finditer(sent):
                obj = m.group("obj")
                if _WSP_HOW_NOUN_RE.search(obj):
                    continue
                signal = ""
                if _wsp_symbolic(obj):
                    signal = "emoji"
                elif _WSP_QUOTE_MARK in obj:
                    signal = "quoted"
                elif _WSP_WHAT_NOUN_RE.search(obj):
                    signal = "noun"
                elif _WSP_PICK_REF_RE.match(obj):
                    prefix = sent[:m.start()]
                    if _WSP_WHAT_NOUN_RE.search(prefix) \
                            and not _WSP_HOW_NOUN_RE.search(prefix):
                        signal = "pick-ref"
                if not signal:
                    continue
                if _wsp_user_picked(user_prompt, obj):
                    continue
                return {"phrase": m.group(0).strip()[:60],
                        "object": obj.replace(_WSP_QUOTE_MARK, '"..."'),
                        "signal": signal}
    return None


def what_solo_pick_block_reason(hit: dict) -> str:
    """Model-facing Stop feedback for a WHAT_SOLO_PICK block (not user copy)."""
    return (
        "WHAT_SOLO_PICK: you decided a WHAT choice alone (\"%s\") -- "
        "wording / look / emoji / layout / name is the user's pick, not "
        "yours. Rewrite: show 2-7 concrete candidates rendered exactly as "
        "they will appear, mark one as the recommendation, and let the user "
        "pick. If the user rejected earlier candidates, these must be NEW "
        "ones. (How choices -- method, order, tooling -- stay yours; rules/"
        "hot.framework.md Problem solving, DGN-2049)" % hit.get("phrase", "")
    )


def _check_what_solo_pick(
    text: str,
    turn_texts: Optional[List[str]] = None,
    user_prompt: Optional[str] = None,
) -> Optional[Violation]:
    """WHAT_SOLO_PICK detect view (the Stop adapter records it)."""
    hit = what_solo_pick(text, turn_texts, user_prompt)
    if not hit:
        return None
    return Violation(
        rule_id=WHAT_SOLO_PICK_RULE_ID,
        severity=SEVERITY_DETECT,
        reason=("first-person WHAT decision with no candidate list this "
                "turn and no pick from the user (DGN-2049)"),
        metrics={"signal": hit["signal"]},
        excerpt=hit["phrase"],
    )


# --- TERM_LEAK (DGN-1500) ------------------------------------------------ #
# Layered term dictionary check.  The rules come from term_dict.load() (the
# adapter reads the files; this lib stays free of file IO).  A rule is:
#   {id, layer, owner, canonical, forms, rewrite, context_any, except}
# Only forms listed in the dictionary are caught -- a mistranslation the
# model invents fresh is not (the ticket's stated limit; the list grows from
# the canary log).
#
# Matching is whole-word: a form never matches inside a longer word (the
# Korean "ladder" contains the Korean "bridge"-mistranslation as a suffix),
# a Korean form may carry trailing particles, and an ASCII form matches case-
# insensitively on word boundaries.  Never matched: inline code, blockquotes
# (owner quotes), quoted spans, marker lines, JSON-shaped lines and URLs --
# quoting or citing a term is not using it.  Code FENCES are matched but
# never rewritten: the forms are Korean mistranslations, which no log or
# command legitimately contains (the 2026-09-15 incident itself sat in a
# fenced status table), while a rewrite there could alter a literal.
# context_any cues are looked up in the hit's own sentence (quotes masked);
# a hit inside one of the rule's `except` phrases is skipped.  A rewrite-safe
# hit is still left alone (counted only) when its sentence already contains
# the canonical form -- "A, not B" would otherwise become "A, not A".

TERM_LEAK_RULE_ID = "TERM_LEAK"

# Particles / copula endings a Korean noun may carry (up to two stacked).
# Plain "다" is deliberately absent: it turns a noun form into a verb stem.
_TERM_PARTICLES = (
    "이라는", "이라고", "이에요", "입니다", "에서는", "에게는", "으로는",
    "에서", "에게", "으로", "까지", "부터", "처럼", "보다", "이랑", "이란",
    "라는", "라고", "예요", "이다", "이야", "하고", "들",
    "은", "는", "이", "가", "을", "를", "의", "에", "께", "로", "와", "과",
    "도", "만", "랑", "란", "야",
)
_TERM_PARTICLE_ALT = "|".join(sorted(_TERM_PARTICLES, key=len, reverse=True))
_TERM_PARTICLE_RE = re.compile(_TERM_PARTICLE_ALT)
# (consonant, vowel). Rieul behaves as a consonant except for (eu)ro.
_TERM_PARTICLE_PAIRS = (
    ("\uc744", "\ub97c"), ("\uc740", "\ub294"), ("\uc774", "\uac00"), ("\uacfc", "\uc640"),
    ("\uc774\ub791", "\ub791"), ("\uc774\ub780", "\ub780"), ("\uc774\ub77c\ub294", "\ub77c\ub294"),
    ("\uc774\ub77c\uace0", "\ub77c\uace0"), ("\uc774\uc5d0\uc694", "\uc608\uc694"), ("\uc774\ub2e4", "\ub2e4"),
    ("\uc774\uc57c", "\uc57c"),
    ("\uc73c\ub85c", "\ub85c"), ("\uc73c\ub85c\ub294", "\ub85c\ub294"),
)


def _term_repair_particles(tail: str, particle_class: str) -> str:
    out = []
    for match in _TERM_PARTICLE_RE.finditer(tail):
        particle = match.group()
        for consonant, vowel in _TERM_PARTICLE_PAIRS:
            if particle in (consonant, vowel):
                use_vowel = (particle_class == "vowel" or
                             (particle_class == "rieul" and consonant.startswith("\uc73c\ub85c")))
                particle = vowel if use_vowel else consonant
                break
        out.append(particle)
        # A second stacked particle agrees with the particle just emitted.
        ending = (ord(particle[-1]) - 0xAC00) % 28
        particle_class = "vowel" if ending == 0 else "rieul" if ending == 8 else "consonant"
    return "".join(out)


_TERM_WORDCH = r"0-9A-Za-z_가-힣"
_TERM_URL_RE = re.compile(r"https?://\S+")
_TERM_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_TERM_QUOTED_RE = re.compile(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|「[^」\n]*」')
_TERM_MARKER_LINE_RE = re.compile(r"^\s*(?:\[\[|[a-z_]+::)")
_TERM_SENTENCE_END_RE = re.compile(r"[.!?？。]+(?=\s|$)")
_term_re_cache: dict = {}


def _term_form_re(form: str):
    rx = _term_re_cache.get(form)
    if rx is None:
        body = re.escape(form).replace(r"\ ", r"\s+")
        if re.search(r"[가-힣]$", form):
            tail = r"(?:(?:%s){0,2})(?![%s])" % (_TERM_PARTICLE_ALT, _TERM_WORDCH)
        else:
            tail = r"(?![%s])" % _TERM_WORDCH
        rx = re.compile(r"(?<![%s])(%s)%s" % (_TERM_WORDCH, body, tail),
                        re.IGNORECASE)
        _term_re_cache[form] = rx
    return rx


def _term_masked_spans(text: str):
    """(masked, fenced) character ranges of `text` (see above).

    masked = never matched; fenced = matched, never rewritten.
    """
    spans: List[Tuple[int, int]] = []
    fenced: List[Tuple[int, int]] = []
    pos = 0
    in_fence = False
    for ln in text.split("\n"):
        start, end = pos, pos + len(ln)
        pos = end + 1
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            spans.append((start, end))
            continue
        if in_fence:
            fenced.append((start, end))
        if (_is_blockquote(ln) or _TERM_MARKER_LINE_RE.match(ln)
                or _IDRILL_JSONISH_RE.search(ln)):
            spans.append((start, end))
            continue
        for rx in (_TERM_URL_RE, _TERM_INLINE_CODE_RE, _TERM_QUOTED_RE):
            for m in rx.finditer(ln):
                spans.append((start + m.start(), start + m.end()))
    return spans, fenced


def _term_in_spans(a: int, b: int, spans) -> bool:
    return any(s < b and a < e for s, e in spans)


def _term_sentence(text: str, a: int, b: int) -> str:
    """The sentence (within its line) that holds text[a:b]."""
    ls = text.rfind("\n", 0, a) + 1
    le = text.find("\n", b)
    le = len(text) if le < 0 else le
    line = text[ls:le]
    s0, s1 = 0, len(line)
    for m in _TERM_SENTENCE_END_RE.finditer(line):
        if m.end() <= a - ls:
            s0 = m.end()
        elif m.start() >= b - ls:
            s1 = m.end()
            break
    return line[s0:s1].strip()


def find_term_leaks(text: Optional[str], rules) -> List[dict]:
    """Every dictionary hit in `text`, in text order.

    Each hit: {id, layer, owner, form, match, start, end, sentence,
    rewrite, canonical, fenced}.  `match` is the matched form only
    (particles are not part of it); `rewrite` is already False for a hit
    inside a code fence or in a sentence that also holds the canonical.  Overlapping hits keep the earliest, longest one.
    Never raises on str input with well-formed rules.
    """
    if not text or not rules:
        return []
    spans, fenced = _term_masked_spans(text)
    visible = None
    hits: List[dict] = []
    for rule in rules:
        cues = [c.casefold() for c in (rule.get("context_any") or ())]
        excepted: List[Tuple[int, int]] = []
        for phrase in rule.get("except") or ():
            excepted.extend(m.span() for m in re.finditer(
                re.escape(phrase), text, re.IGNORECASE))
        for form in rule.get("forms") or ():
            for m in _term_form_re(form).finditer(text):
                a, b = m.span(1)
                if _term_in_spans(a, b, spans):
                    continue
                if any(s0 <= a and b <= e0 for s0, e0 in excepted):
                    continue
                if cues:
                    if visible is None:
                        chars = list(text)
                        for s0, e0 in spans:
                            for i in range(s0, min(e0, len(chars))):
                                chars[i] = " "
                        visible = "".join(chars)
                    ctx = _term_sentence(visible, a, b).casefold()
                    if not any(c in ctx for c in cues):
                        continue
                in_fence = _term_in_spans(a, b, fenced)
                sentence = _term_sentence(text, a, b)
                canonical = rule.get("canonical", "")
                hits.append({
                    "id": rule["id"], "layer": rule["layer"],
                    "owner": rule["owner"], "form": form,
                    "match": m.group(1), "start": a, "end": b,
                    "sentence": sentence,
                    "rewrite": (bool(rule.get("rewrite")) and not in_fence
                                and not (canonical and canonical.casefold() in sentence.casefold())),
                    "canonical": canonical,
                    "particle_end": m.end(),
                    "particle_class": rule.get("particle_class"),
                    "fenced": in_fence,
                })
    hits.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))
    out: List[dict] = []
    for h in hits:
        if out and h["start"] < out[-1]["end"]:
            continue
        out.append(h)
    return out


def apply_term_rewrites(text: str, hits) -> str:
    """`text` with every rewrite-eligible hit replaced by its canonical form.

    Kit Latin terms with a pronunciation class also repair attached particles.
    Everything else (unsafe hits and surrounding prose) is kept
    byte-identical; no eligible hit -> the input object is returned.
    """
    todo = [h for h in hits if h.get("rewrite") and h.get("canonical")]
    if not todo:
        return text
    out = []
    pos = 0
    for h in sorted(todo, key=lambda h: h["start"]):
        out.append(text[pos:h["start"]])
        out.append(h["canonical"])
        pos = h["end"]
        if h.get("particle_class"):
            end = h.get("particle_end", pos)
            out.append(_term_repair_particles(text[pos:end], h["particle_class"]))
            pos = end
    out.append(text[pos:])
    return "".join(out)


def _check_idle_promise(
    text: str,
    had_action: bool = True,
    turn_tool_blob: Optional[str] = None,
) -> Optional[Violation]:
    """IDLE_PROMISE: turn ends on an immediate-action promise.

    Fires when the LAST ~2 lines carry an immediacy marker AND a commitment
    verb, and no exemption applies.  had_action does NOT suppress it: the
    failure mode is "did some tool calls, then promised MORE and stopped".
    """
    if not text:
        return None
    tail = "\n".join([ln for ln in text.strip().splitlines() if ln.strip()][-2:])
    if not tail:
        return None
    if IDLE_PROMISE_EXEMPT_RE.search(tail):
        return None
    commit = IDLE_PROMISE_COMMIT_RE.search(tail)
    if not commit:
        return None
    # rev3: immediacy is NO LONGER required.  Requiring commit AND immediacy is
    # what let all three caught misses through -- "2.0 이어갑니다" carries no
    # immediacy token yet is exactly the failure.  The honest-future-tense cases
    # the AND-guard was protecting (owner gate / next session / running delegate)
    # are ALREADY covered by IDLE_PROMISE_EXEMPT_RE above, so the guard was
    # redundant defense paid for in misses.  Immediacy now only raises
    # confidence in the metric; it does not gate the fire.
    now = IDLE_PROMISE_NOW_RE.search(tail)
    # DGN-1279: record the ACTION CONTRAST alongside had_action.  had_action
    # is "any tool ran in this or the prior turn" -- it says nothing about the
    # promised action, which is why 20/20 canary rows carried had_action=True
    # while three of that day's promises went unkept.  action/action_done name
    # the promised artifact and whether its tool signature ran in THIS turn.
    kind, matched, done = _promise_action_contrast(tail, turn_tool_blob)
    return Violation(
        rule_id="IDLE_PROMISE",
        severity=SEVERITY_DETECT,
        reason=("turn ends promising immediate action; the turn is closing so "
                "the promise cannot be kept in it (2026-07-07 tone rule)"),
        metrics={"had_action": had_action, "tail_len": len(tail),
                 "immediacy": bool(now),
                 "action": kind or "-",
                 "action_done": "-" if done is None else done},
        excerpt=(commit.group(0) + "|" + (now.group(0) if now else "-")),
    )

# DGN-2033: exact whole-label set, parity-pinned against bridge.options.
# Bridge releases ship without routines; keep this detector harness-agnostic.
EXIT_HATCH_LABELS = frozenset({
    "\uc77c\ub2e8 \uc5ec\uae30\uae4c\uc9c0", "\ub098\uc911\uc5d0", "\uadf8\ub9cc", "\ub2e4\uc74c\uc5d0",
})


def _check_exit_hatch_options(text: str) -> Optional[Violation]:
    """Record only authored labels, never prose, quotations or code examples.

    Source priority mirrors the bridge: labeled marker, bare-marker trailing
    block, last numbered 1..N run. No marker means no canary observation.
    """
    lines = []
    fenced = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            lines.append("")
        else:
            lines.append("" if fenced or line.lstrip().startswith(">") else line.strip())
    labeled = []
    bare = None
    has_marker = False
    numbered = re.compile(r"^(\d+)[.\u3001)\uff09]\s*(.+)")
    runs, run = [], []
    for i, line in enumerate(lines):
        m = re.fullmatch(r"\[\[OPTIONS:(.*)\]\]", line)
        if m:
            has_marker = True
            labels = [v.strip() for v in m[1].split("|") if v.strip()]
            if labels:
                labeled = labels
        elif line == OPTIONS_MARKER:
            has_marker = True
            bare = i
        n = numbered.match(line)
        if n:
            if int(n[1]) == 1:
                if run:
                    runs.append(run)
                run = [n[2].strip()]
            elif run and int(n[1]) == len(run) + 1:
                run.append(n[2].strip())
            else:
                if len(run) >= 2:
                    runs.append(run)
                run = []
    if run:
        runs.append(run)
    labels = labeled
    if not labels and bare is not None:
        for line in lines[bare + 1:]:
            if not line or line.startswith(("[[OPTIONS", "[[IDRILL:", "send_file::", "link_preview::")):
                break
            n = numbered.match(line)
            labels.append(n[2].strip() if n else line)
    if not labels and has_marker and runs:
        labels = runs[-1]
    hits = []
    for label in labels:
        # Same display wrappers as bridge.strip_markdown_label, then the
        # trailing recommendation decoration. Never strip lexical suffixes.
        plain = re.sub(r"^#{1,6}\s+", "", label.lstrip())
        for pattern in (r"\*\*(.+?)\*\*", r"(?<![0-9A-Za-z])__(.+?)__(?![0-9A-Za-z])",
                        r"\*(.+?)\*", r"(?<![0-9A-Za-z])_(.+?)_(?![0-9A-Za-z])", r"`(.+?)`"):
            plain = re.sub(pattern, r"\1", plain, flags=re.S)
        plain = " ".join(plain.split())
        plain = re.sub(r"\s*\((?:rec|\ucd94\ucc9c)\)$", "", plain).strip()
        if plain in EXIT_HATCH_LABELS:
            hits.append(plain)
    if not hits:
        return None
    return Violation(
        rule_id="EXIT_HATCH_OPTION", severity=SEVERITY_DETECT,
        reason="Exit labels are not owner decisions; omit the exit button.",
        metrics={"hits": len(hits), "labels": len(labels), "stage": "record"},
        excerpt=" | ".join(hits),
    )


# --- OWNER_PROCEDURE (DGN-2032): record before any block promotion --------- #
OWNER_PROCEDURE_RULE_ID = "OWNER_PROCEDURE"
# Closed surface grammar: app/site nouns or a domain with these TLDs.
_OWNER_SURFACE_RE = re.compile(
    r"(?:\uc571|\uc0ac\uc774\ud2b8|\ud648\ud398\uc774\uc9c0)(?:\uc5d0\uc11c|\uc73c\ub85c|\uc5d0|\uc744|\ub97c|\uc758|\uc740|\ub294|$)"
    r"|(?<![\w.-])(?:[a-z0-9-]+\.)+(?:com|net|org|kr|io|co|app)(?![a-z0-9.-])",
    re.IGNORECASE,
)
_OWNER_IMPERATIVE_RE = re.compile(
    r"(?:(?:\uac80\uc0c9|\ud655\uc778)\ud574[ \t]*\ubcf4\uc138\uc694|\uc5f4\uc5b4[ \t]*\ubcf4\uc138\uc694|\ubd10[ \t]*\uc8fc\uc138\uc694"
    r"|\ud558\uc2dc\uba74[ \t]*\ub429\ub2c8\ub2e4|\uc9c1\uc811[ \t]*(?:\ud55c[ \t]*\ubc88[ \t]*)?"
    r"(?:\ubcf4\uc154\uc57c|\ud655\uc778\ud558\uc154\uc57c)[ \t]*\uc815\ud655\ud569\ub2c8\ub2e4)(?!\w)"
)
_OWNER_QUOTE_RE = re.compile(r"`[^`]*`|\"[^\"]*\"|'[^'\n]*'|\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019")
_OWNER_CITATION_RE = re.compile(
    r"^(?:\ud655\uc778\ud55c \uc218\ub2e8|\uc2dc\ub3c4\ud55c \uc218\ub2e8|Exhausted means):[^\n]*"
    r"\[[^\]\n]+\]\((?:https?://|/)[^\s)]+\)", re.IGNORECASE,
)


def _check_owner_procedure(text: str) -> Optional[Violation]:
    """Count closed procedure shapes; no domain overrides or blanket exemption.

    Same sentence only; ellipses are retained for the real KIPRIS incident.
    Fences, blockquotes, inline code and quoted examples never count. The
    citation metric is syntactic evidence for calibration, never a verified
    claim that all available tools were exhausted.
    """
    lines = _visible_prose_lines(text)
    cited = any(_OWNER_CITATION_RE.search(line.strip()) for line in lines)
    hits = []
    for line in lines:
        if line.lstrip().startswith("[[OPTIONS"):
            continue
        prose = _OWNER_QUOTE_RE.sub("", line)
        for sentence in re.split(r"(?<!\.)[.!?](?!\.)[ \t]+", prose):
            if _OWNER_SURFACE_RE.search(sentence):
                hits.extend(m.group(0) for m in _OWNER_IMPERATIVE_RE.finditer(sentence))
    if not hits:
        return None
    return Violation(
        rule_id=OWNER_PROCEDURE_RULE_ID,
        severity=SEVERITY_DETECT,
        reason="owner-directed app/site procedure; exhaust own means and ask "
               "only for one owner-private value (record-first)",
        metrics={"hits": len(hits), "exhausted_means_cited": int(cited)},
        excerpt=hits[0][:60],
    )


# --- OWNER_ASK means gate (DGN-2032 rev2): keyed on means TRIED ------------ #
# Owner ruling 2026-10-14 14:12: a well-timed ask is GOOD; the defect is
# coming back after one shallow cycle without trying the means the agent
# has. Incident (2026-10-14 14:07): 2 WebSearch + a PDF grep, then a capture-and-
# send ask plus a call-the-customer-centre ask -- the insurer's own page,
# cited in the reply, was never fetched. So the verdict reads the turn's
# TOOL CALLS, never the reply's exhausted-means citation line.
#
# Ask = one visible sentence that names a source/surface (app, site, page,
# policy document, screen, customer centre, phone number, domain) AND
# directs the owner to check / send / ask (closed verb list). Quoted text,
# fences, blockquotes and OPTIONS lines never count (same masking as
# OWNER_PROCEDURE).
_OWNER_ASK_SURFACE_RE = re.compile(
    _OWNER_SURFACE_RE.pattern
    + r"|\uace0\uac1d\uc13c\ud130|\ucf5c\uc13c\ud130|\uc0c1\ub2f4\uc13c\ud130"
      r"|\uc99d\uad8c|\ud654\uba74|\ucea1\ucc98|\uc2a4\ud06c\ub9b0\uc0f7"
      r"|\ud398\uc774\uc9c0|\ub9c1\ud06c|\uba54\uc77c\ud568"
      r"|(?<!\d)\d{2,4}-\d{3,4}(?:-\d{4})?(?!\d)",
    re.IGNORECASE,
)
_OWNER_ASK_VERB_RE = re.compile(
    _OWNER_IMPERATIVE_RE.pattern
    # send / tell / photograph / capture / upload / share / check / find + please
    + r"|(?:\ubcf4\ub0b4|\uc54c\ub824|\ucc0d\uc5b4|\ucea1\ucc98\ud574|\uc62c\ub824"
      r"|\uacf5\uc720\ud574|\ud655\uc778\ud574|\ucc3e\uc544)[ \t]*"
      r"\uc8fc(?:\uc2dc\uba74|\uc138\uc694|\uc2ed\uc2dc\uc624)"
    # ask / look up / go in / check / query + 'you may try' / 'just try'
    + r"|(?:\ubb3c\uc5b4|\ucc3e\uc544|\ub4e4\uc5b4\uac00|\ud655\uc778\ud574"
      r"|\uc870\ud68c\ud574)[ \t]*\ubcf4(?:\uc154\ub3c4|\uc2dc\uba74)[ \t]*"
      r"(?:\ub429\ub2c8\ub2e4|\ub3fc\uc694)"
    # inquire / check / query + 'you may' / 'just do'
    + r"|(?:\ubb38\uc758|\ud655\uc778|\uc870\ud68c)\ud558(?:\uc154\ub3c4|\uc2dc\uba74)"
      r"[ \t]*(?:\ub429\ub2c8\ub2e4|\ub3fc\uc694)"
)
# Means classes from the adapter's tool blob ("TOOL <name> <path>" / "CMD
# <shell>" lines, output-gate-stop._tool_blob_lines). A browser or fetch of
# ANY page counts as tried: the gate checks that the means were used, not
# which URL (that is judgment).
_MEANS_FETCH_TOOL_RE = re.compile(
    r"^TOOL (?:WebFetch|mcp__\S*(?:browser|playwright|puppeteer|chrome"
    r"|fetch|navigate)\S*)\b", re.IGNORECASE | re.MULTILINE)
_MEANS_FETCH_CMD_RE = re.compile(
    r"^CMD .*(?:\bagent-browser\b|\b(?:curl|wget)\b[^\n]*https?://)",
    re.MULTILINE)
_MEANS_SEARCH_RE = re.compile(
    r"^TOOL (?:WebSearch|mcp__\S*search\S*)\b", re.IGNORECASE | re.MULTILINE)
# Names that make "fetch/browser available" true when the adapter sees them
# offered (deferred-tool list) or used anywhere in the session.
MEANS_FETCH_TOOL_NAME_RE = re.compile(
    r"^(?:WebFetch|mcp__\S*(?:browser|playwright|puppeteer|chrome|fetch"
    r"|navigate)\S*)$", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\((https?://[^\s)]+)\)")
_URL_HOST_RE = re.compile(r"https?://([A-Za-z0-9.-]+)")
_BARE_HOST_RE = re.compile(
    r"(?<![\w.@/-])((?:[a-z0-9-]+\.)+(?:com|net|org|kr|io|co|app))(?![a-z0-9.-])",
    re.IGNORECASE)
_HOST_SUFFIX = {"com", "net", "org", "kr", "io", "co", "app", "or", "go",
                "ne", "ac", "re", "jp", "uk", "www"}


def _host_label(host: str) -> str:
    """Registrable name of a host: www.kipris.or.kr -> kipris."""
    parts = [p for p in host.lower().split(".") if p]
    while parts and parts[-1] in _HOST_SUFFIX:
        parts.pop()
    return parts[-1] if parts else ""


def owner_ask_opened(turn_tool_blob: str) -> set:
    """Host labels the turn actually fetched or browsed (fetch-class TOOL
    lines carry the url; fetch-class CMD lines carry it in the command)."""
    opened = set()
    for line in turn_tool_blob.splitlines():
        if (_MEANS_FETCH_TOOL_RE.match(line)
                or _MEANS_FETCH_CMD_RE.match(line)):
            opened.update(_host_label(h) for h in _URL_HOST_RE.findall(line))
    opened.discard("")
    return opened


def _named_sources(asks: List[str], text: str) -> dict:
    """{label: url-or-host} for sources an ask sentence itself names: a
    bare domain in the ask, or a linked source whose registrable name
    (>= 4 chars, e.g. "kipris") appears in the ask.  Measured
    2026-10-14: widening to domains anywhere in the reply's prose blocked
    a reply whose domain was the topic, not a source (FP), so it stays
    per ask sentence."""
    named = {}
    links = _MD_LINK_RE.findall(text)
    for ask in asks:
        low = ask.lower()
        for host in _BARE_HOST_RE.findall(ask):
            if _host_label(host):
                named[_host_label(host)] = host
        for _label_text, url in links:
            hosts = _URL_HOST_RE.findall(url)
            label = _host_label(hosts[0]) if hosts else ""
            if len(label) >= 4 and label in low:
                named.setdefault(label, url)
    return named


def owner_ask_sentences(text: Optional[str], surface_re=None, verb_re=None,
                        private_re=None) -> List[str]:
    """Visible owner requests; optional phrase tables also classify kit facts.

    Private-value/status clauses may be masked, but never exempt the whole
    sentence: a separate request for the collection must still be checked.
    Default patterns preserve the source-search classifier's contract.
    """
    if not text:
        return []
    surface_re = surface_re or _OWNER_ASK_SURFACE_RE
    verb_re = verb_re or _OWNER_ASK_VERB_RE
    asks = []
    for line in _visible_prose_lines(text):
        if line.lstrip().startswith("[[OPTIONS"):
            continue
        prose = _OWNER_QUOTE_RE.sub("", line)
        for sentence in re.split(r"(?<!\.)[.!?](?!\.)[ \t]+", prose):
            candidate = private_re.sub(" ", sentence) if private_re else sentence
            if surface_re.search(candidate) and verb_re.search(candidate):
                asks.append(sentence.strip())
    return asks


def _kit_fact_ask_verdict(text, turn_tool_blob, readable_facts):
    """Declared kit facts precede all web-means branches, including blind.

    The adapter supplies localized phrase tables and kit read hints. This
    portable library neither reads files nor knows domain nouns/commands.
    """
    hits, hints, kinds = [], [], []
    for fact in readable_facts or ():
        patterns = {
            key: re.compile("|".join("(?:%s)" % p[key] for p in fact["phrases"]),
                            re.IGNORECASE)
            for key in ("surface", "request", "private")}
        asks = owner_ask_sentences(text, patterns["surface"],
                                   patterns["request"], patterns["private"])
        if asks:
            hits.extend(a for a in asks if a not in hits)
            if fact["kind"] not in kinds:
                kinds.append(fact["kind"])
                hints.append("%s: %s" % (fact["kind"], fact["read_hint"]))
    if not hits:
        return None
    search, fetch = (owner_ask_means(turn_tool_blob)
                     if turn_tool_blob is not None else (-1, -1))
    return {
        "state": "kit_readable_fact", "block": True, "asks": len(hits),
        "search": search, "fetch": fetch, "excerpt": hits[0][:60],
        "fact_kinds": kinds,
        "reason": (
            "OWNER_ASK block (DGN-2165): the active kit declares these facts "
            "readable. Read them yourself before replying:\n%s\n"
            "Use the kit context and read hint, including an empty result. "
            "If a read fails, say that it could not be read; do not ask the "
            "owner to supply, list, check or send it instead. Named-item "
            "done/defer questions and specific unknown private facts are "
            "allowed. Web search/fetch counts do not waive this rule. "
            "Rewrite the reply; this gate fires once per turn."
            % "\n".join(hints)),
    }


def owner_ask_means(turn_tool_blob: str) -> Tuple[int, int]:
    """(search calls, fetch/browser calls) in the turn's tool blob."""
    fetch = (len(_MEANS_FETCH_TOOL_RE.findall(turn_tool_blob))
             + len(_MEANS_FETCH_CMD_RE.findall(turn_tool_blob)))
    return len(_MEANS_SEARCH_RE.findall(turn_tool_blob)), fetch


def owner_ask_verdict(
    text: Optional[str],
    turn_tool_blob: Optional[str],
    fetch_available: Optional[bool],
    readable_facts: Optional[list] = None,
) -> Optional[dict]:
    """None when the reply asks nothing of the owner; else a record dict.

    state (block=True for kit_readable_fact, shallow, unopened and multi):
      kit_readable_fact asks the owner to supply a fact the active kit
                declares readable -> retry with its read hint, regardless
                of tool visibility, read failure or web-means counts.
      shallow   asks; the turn searched but never fetched/browsed; the
                agent had fetch/browser tools -> try the source first.
      unopened  the ask itself sends the owner to a source (a named domain,
                or a linked source whose name the ask repeats) that this
                turn never fetched/browsed, with or without other searches
                or fetches -> open that one.
      multi     means were tried (fetch/browser ran) but the reply makes
                more than one ask -> narrow to ONE owner-private value.
      tried     fetch/browser ran and the reply makes one ask -> pass.
      no_search asks without any web search this turn (clarifying or
                owner-intent asks live here) -> record only.
      no_tool   searched only, but no fetch/browser tool was visible to
                the agent -> record only (cannot claim it had the means).
      blind     tool calls not visible (blob None) -> record only.
    """
    fact_verdict = _kit_fact_ask_verdict(text, turn_tool_blob, readable_facts)
    if fact_verdict:
        return fact_verdict
    asks = owner_ask_sentences(text)
    if not asks:
        return None
    rec = {"asks": len(asks), "search": -1, "fetch": -1, "block": False,
           "excerpt": asks[0][:60], "reason": None}
    if turn_tool_blob is None:
        rec["state"] = "blind"
        return rec
    search, fetch = owner_ask_means(turn_tool_blob)
    rec.update(search=search, fetch=fetch)
    opened = owner_ask_opened(turn_tool_blob)
    unopened = {k: v for k, v in _named_sources(asks, text or "").items()
                if k not in opened}
    rec["unopened"] = sorted(unopened.values())
    if fetch:  # means visibly used: availability is proven
        rec["state"] = ("unopened" if unopened
                        else "multi" if len(asks) > 1 else "tried")
    elif search or unopened:
        if fetch_available is not True:
            rec["state"] = "no_tool"
        else:
            rec["state"] = "shallow" if search else "unopened"
    else:
        rec["state"] = "no_search"
    if rec["state"] in ("shallow", "unopened", "multi"):
        rec["block"] = True
        rec["reason"] = _owner_ask_reason(rec, asks, text or "")
    return rec


def _owner_ask_reason(rec: dict, asks: List[str], text: str) -> str:
    quoted = "\n".join("  - %r" % a[:160] for a in asks[:3])
    if rec["state"] == "multi":
        return (
            "OWNER_ASK block (DGN-2032). You did try a source this turn "
            "(fetch/browser), good. But the reply hands the owner %d asks:\n"
            "%s\n"
            "After real attempts, one ask is right: say what you tried, "
            "what you did not find, and ask for ONE specific value only "
            "the owner can see (e.g. the plan name on their policy). Drop "
            "the alternative procedures. This gate fires once per turn."
            % (len(asks), quoted))
    if rec["state"] == "unopened":
        return (
            "OWNER_ASK block (DGN-2032): try the source first. Your reply "
            "sends the owner to a source you did not open this turn:\n%s\n"
            "Not opened: %s. Open it with WebFetch or the browser (agent-browser for a "
            "search form) and do the lookup yourself. Then report: what you "
            "tried, what you found or that you did not find it, and at most "
            "ONE specific value only the owner can see. It fires once per "
            "turn." % (quoted, ", ".join(rec["unopened"])))
    urls = list(dict.fromkeys(u for _t, u in _MD_LINK_RE.findall(text)))[:3]
    named = ("\nSources your own reply names: %s" % ", ".join(urls)
             if urls else "")
    return (
        "OWNER_ASK block (DGN-2032): try the source first. Your reply sends "
        "the owner to check/send/ask something:\n%s\n"
        "but this turn only searched (%d search call(s), no WebFetch or "
        "browser call). You have fetch/browser tools. Open the source your "
        "reply names or your search surfaced -- the official product, "
        "quote or signup page -- with WebFetch or the browser, and read "
        "it.%s\n"
        "Then report: what you tried, what you found or that you did not "
        "find it, and at most ONE specific value only the owner can see. "
        "A cited-means line in the reply does not count; this gate reads "
        "tool calls. "
        "It fires once per turn."
        % (quoted, rec["search"], named))


def check_message(
    text: Optional[str],
    had_action: bool = True,
    sot_read: bool = True,
    deprecated_terms: Optional[list] = None,
    turn_texts: Optional[List[str]] = None,
    content: Optional[dict] = None,
    is_scheduled_reminder: bool = False,
    turn_tool_blob: Optional[str] = None,
) -> List[Violation]:
    """Detect Stage-1 signals in one outgoing message.

    Args:
        text: The outgoing assistant message text (last message of the turn).
        had_action: Whether the current turn contained at least one tool_use
            block.  Defaults to True so existing callers (no had_action arg)
            are treated as "action present" -- backward-compatible and safe.
            Pass False to enable DONE_CLAIM detection for turns with no actions.
        sot_read: Whether the current turn read at least one sot/ doc.  Defaults
            to True so existing callers never trigger SOT_UNREAD -- backward-
            compatible.  Pass False (adapter found no sot/ read this turn) to
            enable SOT_UNREAD detection.
        deprecated_terms: Canonical retired-term list from the ESTATE-TAXONOMY
            SoT (parsed by the adapter).  None/empty -> DEPRECATED_TERM never
            fires.  Keeping the source in the SoT (not hardcoded here) preserves
            one-writer-per-state.
        turn_tool_blob: Serialized tool_use names+inputs of the CURRENT turn
            (from the adapter).  None -> the promised-action contrast is not
            computable; IDLE_PROMISE still detects, but nothing can block
            (DGN-1279 fail-open).
        turn_texts: Ordered list of ALL assistant message texts in the current
            turn (from the adapter's turn-boundary scan).  When supplied, the
            SKELETON check evaluates the WHOLE TURN -- enabling correct detection
            of split-contract turns (conclusion in msg1, [[OPTIONS]] in msg2).
            When absent, SKELETON falls back to scanning `text` alone so existing
            callers without turn_texts are backward-compatible.  (DGN-874 B2)
        content: Per-domain content dict from output_gate_content.py (DGN-896).
            None -> dev-domain defaults are used.  Backward-compatible: all
            existing callers without this param are unaffected.
        is_scheduled_reminder: When True, the message is a timeblock-scheduled
            alert utterance -- unconditionally exempt from MOMENTUM gating.
            The adapter sets this flag based on domain-specific heuristics or
            metadata.  Defaults to False (safe: unknown messages are not exempt).

    Returns a (possibly empty) list of Violation records.  Never blocks,
    never raises on any str input.  All violations carry severity=DETECT.
    """
    if not text or not text.strip():
        return []

    violations: List[Violation] = []

    # --- WALL check ------------------------------------------------------- #
    char_limit = _wall_char_limit(content)
    line_limit = _wall_line_limit(content)
    exempt_markers = _wall_exempt_markers(content)

    # Render-surface exemption: if any exempt marker appears in the message,
    # skip the WALL check entirely (lifekit structured outputs).
    wall_exempt = any(m in text for m in exempt_markers) if exempt_markers else False

    if not wall_exempt:
        prose_lines = _visible_prose_lines(text)
        prose_chars = sum(len(ln) for ln in prose_lines)
        # Consecutive prose line count (reset on blank-equivalent; blanks
        # already excluded by _visible_prose_lines, so ALL lines in
        # prose_lines are non-blank prose -- check consecutive runs).
        max_consecutive = _max_consecutive_lines(text)

        # Joint condition (DGN-821 2026-08-16 tuning batch):
        # A genuine wall is BOTH long (char threshold) AND dense (line threshold).
        # With joint=True (default): fire only when BOTH conditions hold.
        # With joint=False (legacy): fire when EITHER condition holds.
        # Rationale: canary saw char-only breaches (1469c/7L, 1264c/5L) that
        # were NOT real walls -- structured reports that are wide but not dense.
        joint = _wall_joint_condition(content)
        if joint:
            _wall_fires = (
                prose_chars > char_limit and max_consecutive > line_limit
            )
        else:
            _wall_fires = (
                prose_chars > char_limit or max_consecutive > line_limit
            )

        if _wall_fires:
            violations.append(Violation(
                rule_id="WALL",
                severity=SEVERITY_DETECT,
                reason="prose volume exceeds threshold (joint char+line condition)",
                metrics={
                    "prose_chars": prose_chars,
                    "char_limit": char_limit,
                    "max_consecutive_lines": max_consecutive,
                    "line_limit": line_limit,
                    "joint_condition": joint,
                },
            ))

    # --- BACKREF check (DGN-913) ------------------------------------------ #
    # Final message points at the collapsed progress log instead of restating
    # the substantive point -> the summary is invisible to the reader (DGN-699).
    # Detect-only canary; block promotion deferred until FP/FN calibration.
    _backref_m = _BACKREF_RE.search(text)
    if _backref_m:
        violations.append(Violation(
            rule_id="BACKREF",
            severity=SEVERITY_DETECT,
            reason="final message defers to progress-log content instead of "
                   "restating the point (DGN-699 interim/final separation)",
            metrics={"match": _backref_m.group(0).strip()[:40]},
        ))

    # --- SKELETON check ---------------------------------------------------- #
    # DGN-874 B2: when the adapter supplies turn_texts (all assistant messages
    # in the current turn), evaluate SKELETON over the whole turn.  Fall back
    # to single-message scan when turn_texts is absent (backward-compat).
    skel_min_chars = _skeleton_min_conclusion_chars(content)
    skel_min_run = _skeleton_min_option_run(content)
    if turn_texts is not None and len(turn_texts) > 1:
        skel = _check_skeleton_turn(
            turn_texts,
            min_conclusion_chars=skel_min_chars,
            min_option_run=skel_min_run,
        )
    else:
        skel = _check_skeleton(
            text,
            min_conclusion_chars=skel_min_chars,
            min_option_run=skel_min_run,
        )
    if skel:
        violations.append(skel)

    # --- JARGON check (DISABLED 2026-08-13, DGN-821 calibration) ----------- #
    # Canary 2-day data: 30/46 events were JARGON, ~all false positives -- the
    # word-then-paren heuristic (_JARGON_FUNC_RE) misfires on bilingual prose
    # asides ("프레시입니다 (...)", "point(", "doctrine("). Zero block value.
    # Detector code kept for a possible narrow redefinition (code-token/path
    # only) later; not wired into the active check.
    # jargon = _check_jargon(text)
    # if jargon:
    #     violations.append(jargon)

    # --- DONE_CLAIM check ------------------------------------------------- #
    markers = _done_claim_markers(content)
    done = _check_done_claim(text, had_action, markers=markers)
    if done:
        violations.append(done)

    # --- DEPRECATED_TERM check -------------------------------------------- #
    dep = _check_deprecated_term(text, deprecated_terms)
    if dep:
        violations.append(dep)

    # --- SOT_UNREAD check ------------------------------------------------- #
    sot = _check_sot_unread(text, sot_read)
    if sot:
        violations.append(sot)

    # --- MOMENTUM check --------------------------------------------------- #
    # 5th detector (DGN-896): detect stop/defer/nag utterance without a real
    # signal.  Detect-only; never blocks in Stage 1.
    # Scheduled-reminder exemption: if is_scheduled_reminder=True (adapter
    # detected a timeblock alert utterance) OR if the message contains a
    # scheduled-reminder marker from the content dict, skip this check.
    momentum = _check_momentum(
        text,
        content=content,
        is_scheduled_reminder=is_scheduled_reminder,
    )
    if momentum:
        violations.append(momentum)

    # --- IDLE_PROMISE check (DGN-1081) ---------------------------------- #
    _ip = _check_idle_promise(text, had_action, turn_tool_blob)
    if _ip is not None:
        violations.append(_ip)

    # --- IDRILL_PROSE_Q check (DGN-983 / DGN-1527) ---------------------- #
    # Detect-only here; the bridge send seat is what normalizes and counts.
    _iq = _check_idrill_prose_question(text)
    if _iq is not None:
        violations.append(_iq)

    # --- FENCE_CJK_GRID check (DGN-1645) -------------------------------- #
    _fg = _check_fence_cjk_grid(text)
    if _fg is not None:
        violations.append(_fg)

    # New classes enter the existing detect-only canary ladder first.
    owner_procedure = _check_owner_procedure(text)
    if owner_procedure is not None:
        violations.append(owner_procedure)

    exit_hatch = _check_exit_hatch_options(text)
    if exit_hatch is not None:
        violations.append(exit_hatch)

    violations.extend(reply_format_violations(text))
    return violations


# ---------------------------------------------------------------------------
# Internal check helpers
# ---------------------------------------------------------------------------

def _max_consecutive_lines(text: str) -> int:
    """Max run of consecutive prose lines (no intervening blank/masked line).

    Blank lines or masked lines (fences, blockquotes, table rows) act as
    breaks.  This measures the longest unbroken prose streak.
    """
    max_run = 0
    cur_run = 0
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            cur_run = 0
            continue
        if in_fence:
            cur_run = 0
            continue
        if _is_blockquote(line) or _TABLE_ROW_RE.match(line):
            cur_run = 0
            continue
        if line.strip() == OPTIONS_MARKER:
            cur_run = 0
            continue
        if not line.strip():
            max_run = max(max_run, cur_run)
            cur_run = 0
        else:
            cur_run += 1
    max_run = max(max_run, cur_run)
    return max_run


def _check_skeleton(
    text: str,
    min_conclusion_chars: int = SKELETON_MIN_CONCLUSION_CHARS,
    min_option_run: int = SKELETON_MIN_OPTION_RUN,
) -> Optional[Violation]:
    """SKELETON: decision-turn message lacks a top conclusion paragraph.

    Trigger condition (STRUCTURE-based, not substring):
      - message contains [[OPTIONS]] marker (outside fences/blockquotes)
        AND the last numbered run has >= min_option_run entries
    AND
      - the prose ABOVE the decision block is shorter than
        min_conclusion_chars characters.

    Design note: numbered-list-only messages (no [[OPTIONS]] marker) are
    NOT triggering SKELETON -- procedure/summary lists look identical and
    produce too many FPs.  Only messages with an explicit [[OPTIONS]] marker
    are classified as decision turns.

    FP2 fix: if the decision block itself is entirely in blockquotes (Tier-2
    format), skip this check -- the blockquote IS the structured reply.

    Args:
        text: Message text to evaluate.
        min_conclusion_chars: Minimum chars in top prose before the decision
            block.  Content-pluggable (DGN-896); defaults to module constant.
        min_option_run: Minimum numbered run to trigger.  Content-pluggable.
    """
    # Check whether OPTIONS marker is present in visible prose.
    has_options_marker = False
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _is_blockquote(line):
            continue
        if line.strip() == OPTIONS_MARKER:
            has_options_marker = True
            break

    if not has_options_marker:
        return None

    entries = _option_entries(text)
    run = _last_run(entries)

    # Single-option confirm buttons are exempt.
    if len(run) < min_option_run:
        return None

    # FP2: if the message is predominantly blockquote (Tier-2 layout),
    # the blockquote IS the structured answer -- skip SKELETON.
    total_lines = [ln for ln in text.split("\n") if ln.strip()]
    bq_lines = [ln for ln in total_lines if _is_blockquote(ln)]
    if total_lines and len(bq_lines) / len(total_lines) > 0.4:
        return None

    # Find the line index of the decision block start.
    decision_start_line = _decision_block_start(text, run, has_options_marker)
    if decision_start_line is None:
        return None

    # Count conclusion chars ABOVE the decision block.
    # Use _conclusion_prose_lines so blockquote text ("> 결론: X") is
    # counted as valid conclusion content -- DGN-874 B2 blockquote allowance.
    lines = text.split("\n")
    pre_lines = lines[:decision_start_line]
    pre_prose = _conclusion_prose_lines("\n".join(pre_lines))
    pre_chars = sum(len(ln) for ln in pre_prose)

    if pre_chars < min_conclusion_chars:
        return Violation(
            rule_id="SKELETON",
            severity=SEVERITY_DETECT,
            reason="decision-turn message lacks a top conclusion paragraph",
            metrics={
                "conclusion_chars": pre_chars,
                "min_required": min_conclusion_chars,
                "trigger": "OPTIONS" if has_options_marker else "long-list",
            },
        )
    return None


def _decision_block_start(
    text: str,
    run: List[Tuple[int, str]],
    has_options_marker: bool,
) -> Optional[int]:
    """Return the line index where the decision block begins.

    For [[OPTIONS]] messages: the line of the first entry in the last
    numbered run (run[0]).  For long-list-only: same.
    Returns None if the run is empty or the position is indeterminate.
    """
    if not run:
        return None
    first_num = run[0][0]
    first_label = run[0][1]
    in_fence = False
    for idx, line in enumerate(text.split("\n")):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _is_blockquote(line):
            continue
        m = _OPTION_LINE_RE.match(line)
        if m and int(m.group(1)) == first_num and first_label in m.group(2):
            return idx
    return None


def _conclusion_prose_lines(text: str) -> List[str]:
    """Return lines that count as conclusion content for the SKELETON check.

    Unlike _visible_prose_lines, blockquote lines ARE included here: a
    conclusion written as "> 결론: X" at the top of a turn is valid conclusion
    content.  Only code fences, table rows, the [[OPTIONS]] marker, and blank
    lines are excluded.

    This is intentionally separate from _visible_prose_lines so the FP2 fix
    (blockquote masking for WALL/prose-metrics) is NOT disturbed -- we only
    unlock blockquote text for conclusion-char counting in the SKELETON check.
    (DGN-874 B2 blockquote-conclusion allowance)
    """
    out: List[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _TABLE_ROW_RE.match(line):
            continue
        if line.strip() == OPTIONS_MARKER:
            continue
        if not line.strip():
            continue
        # Blockquote lines ARE kept (stripped of leading "> ") so that
        # "> 결론: X" contributes its text to the conclusion char count.
        if _is_blockquote(line):
            # Strip the leading "> " prefix to get the actual text.
            out.append(line.lstrip().lstrip(">").lstrip())
        else:
            out.append(line)
    return out


def _check_skeleton_turn(
    turn_texts: List[str],
    min_conclusion_chars: int = SKELETON_MIN_CONCLUSION_CHARS,
    min_option_run: int = SKELETON_MIN_OPTION_RUN,
) -> Optional[Violation]:
    """SKELETON check over the full turn (DGN-874 B2 -- turn-unit scan).

    Evaluates conclusion position across ALL assistant messages in the
    current turn (supplied in chronological order by the adapter).

    Pass condition: within the turn's assistant message sequence, conclusion
    content exists BEFORE the decision block (first [[OPTIONS]] + numbered
    run).  A split turn where msg1 = conclusion + table/image and msg2 =
    [[OPTIONS]] only is a PASS (conclusion sits at the turn top, before the
    decision block which starts in msg2).

    Fail condition: the decision block appears before any conclusion content
    -- i.e. the first message that contains the [[OPTIONS]] marker has no
    conclusion content preceding it, and no earlier message contains
    conclusion content either.

    Algorithm:
      1. Find which message index contains the [[OPTIONS]] marker.
      2. Collect all conclusion-eligible chars from messages BEFORE that
         message (entire earlier messages count as pre-decision conclusion).
      3. Within the [[OPTIONS]] message itself, count conclusion chars above
         the decision block start line (same as single-message logic).
      4. Total pre-decision conclusion chars < min_conclusion_chars
         -> SKELETON violation.

    Args:
        turn_texts: Ordered list of all assistant texts in the current turn.
        min_conclusion_chars: Content-pluggable threshold (DGN-896).
        min_option_run: Content-pluggable minimum run threshold.
    """
    if not turn_texts:
        return None

    # Step 1: find the message index that contains [[OPTIONS]] (outside fences).
    options_msg_idx: Optional[int] = None
    for idx, msg_text in enumerate(turn_texts):
        in_fence = False
        for line in msg_text.split("\n"):
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if _is_blockquote(line):
                continue
            if line.strip() == OPTIONS_MARKER:
                options_msg_idx = idx
                break
        if options_msg_idx is not None:
            break

    if options_msg_idx is None:
        return None  # no [[OPTIONS]] in turn -> no SKELETON

    # Validate the run in the [[OPTIONS]] message.
    options_text = turn_texts[options_msg_idx]
    entries = _option_entries(options_text)
    run = _last_run(entries)
    if len(run) < min_option_run:
        return None  # single-confirm button: exempt

    # FP2: if the [[OPTIONS]] message is predominantly blockquote (Tier-2
    # layout), skip SKELETON for this turn (the blockquote IS the answer).
    total_lines = [ln for ln in options_text.split("\n") if ln.strip()]
    bq_lines = [ln for ln in total_lines if _is_blockquote(ln)]
    if total_lines and len(bq_lines) / len(total_lines) > 0.4:
        return None

    # Step 2: conclusion chars from messages ENTIRELY BEFORE the [[OPTIONS]]
    # message.  Use _conclusion_prose_lines (blockquote text counts).
    pre_decision_chars = 0
    for prior_text in turn_texts[:options_msg_idx]:
        pre_decision_chars += sum(
            len(ln) for ln in _conclusion_prose_lines(prior_text)
        )

    # Step 3: conclusion chars from within the [[OPTIONS]] message itself,
    # above its decision block start line.
    decision_start_line = _decision_block_start(options_text, run, True)
    if decision_start_line is not None:
        options_lines = options_text.split("\n")
        pre_in_msg = options_lines[:decision_start_line]
        pre_decision_chars += sum(
            len(ln) for ln in _conclusion_prose_lines("\n".join(pre_in_msg))
        )

    if pre_decision_chars < min_conclusion_chars:
        return Violation(
            rule_id="SKELETON",
            severity=SEVERITY_DETECT,
            reason="decision turn lacks a top conclusion paragraph (turn-unit scan)",
            metrics={
                "conclusion_chars": pre_decision_chars,
                "min_required": min_conclusion_chars,
                "trigger": "OPTIONS",
                "turn_message_count": len(turn_texts),
                "options_in_message": options_msg_idx,
            },
        )
    return None


def _check_jargon(text: str) -> Optional[Violation]:
    """JARGON: detect candidate internal terms in visible prose.

    Stage 1 = log candidates only; no threshold, no block.  Fires whenever
    at least one pattern matches in non-masked content.
    """
    # Only check visible prose (not code blocks, blockquotes, tables).
    prose = "\n".join(_visible_prose_lines(text))
    if not prose:
        return None

    candidates: List[str] = []
    for m in _JARGON_PATH_RE.finditer(prose):
        candidates.append("path:" + m.group()[:40])
    for m in _JARGON_FUNC_RE.finditer(prose):
        candidates.append("func:" + m.group()[:30])
    for m in _JARGON_PRED_RE.finditer(prose):
        candidates.append("flag:" + m.group()[:30])

    if not candidates:
        return None

    return Violation(
        rule_id="JARGON",
        severity=SEVERITY_DETECT,
        reason="candidate internal terms detected in prose",
        metrics={"candidate_count": len(candidates)},
        excerpt="; ".join(candidates[:5]),
    )


def _marker_is_assertive(prose: str, marker: str) -> bool:
    """Return True if the marker occurrence in prose is a completion CLAIM.

    A marker occurrence is NOT a claim when it is immediately followed by a
    non-assertive conjugation suffix (interrogative, conditional, embedded).
    All occurrences of the marker must be non-assertive for the marker to be
    suppressed -- if at least one occurrence is assertive, the marker counts.

    Conservative design: any doubt -> treat as assertive (keep detection).
    """
    start = 0
    found_assertive = False
    while True:
        pos = prose.find(marker, start)
        if pos < 0:
            break
        after = prose[pos + len(marker):]
        if DONE_CLAIM_NON_ASSERTIVE_RE.match(after):
            # This occurrence is non-assertive -- check remaining occurrences.
            start = pos + 1
            continue
        # Found an occurrence NOT followed by a non-assertive suffix.
        found_assertive = True
        break
    return found_assertive


def _check_done_claim(
    text: str,
    had_action: bool,
    markers: Optional[list] = None,
) -> Optional[Violation]:
    """DONE_CLAIM: completion-claim marker present but no tool_use this turn.

    Fires when ALL of:
      - had_action is False (adapter found zero tool_use in this turn OR the
        immediately preceding turn window -- see adapter cross-turn extension)
      - visible prose contains at least one marker stem that is NOT
        immediately followed by a non-assertive conjugation suffix
        (interrogative/conditional/embedded forms excluded -- DGN-821
        calibration 2026-08-15; see DONE_CLAIM_NON_ASSERTIVE_RE).

    Stage 1 = log candidates only; no block.

    Design bias: conservative (favour false-negative over false-positive).
    A marker followed exclusively by non-assertive suffixes is suppressed;
    a marker where at least one occurrence is assertive is kept.

    Args:
        text: Message text to evaluate.
        had_action: Whether a tool_use action was present this turn/window.
        markers: Content-pluggable marker list (DGN-896).  None -> dev defaults.
    """
    if had_action:
        # Action present in this turn or a closely preceding turn -> no
        # basis for a claim violation.
        return None

    # Check visible prose (not code blocks, blockquotes, tables).
    prose = "\n".join(_visible_prose_lines(text))
    if not prose:
        return None

    active_markers = markers if markers is not None else DONE_CLAIM_MARKERS
    matched: List[str] = []
    for marker in active_markers:
        if marker not in prose:
            continue
        if _marker_is_assertive(prose, marker):
            matched.append(marker)

    if not matched:
        return None

    return Violation(
        rule_id="DONE_CLAIM",
        severity=SEVERITY_DETECT,
        reason="completion-claim marker present with no tool_use action (candidate; cross-turn report FP guarded by adapter window)",
        metrics={"marker_count": len(matched), "had_action": False},
        excerpt="; ".join(matched[:5]),
    )


def _check_momentum(
    text: str,
    content: Optional[dict] = None,
    is_scheduled_reminder: bool = False,
) -> Optional[Violation]:
    """MOMENTUM: stop/defer/nag utterance without a real blocking signal.

    5th detector (DGN-896).  Fires when the message contains a no-signal
    phrase AND no real-signal phrase.  Detect-only; never blocks in Stage 1.

    Scheduled-reminder exception (CRITICAL, lifekit-specific):
    A scheduled-reminder/timeblock-alert utterance is unconditionally exempt.
    The exemption is checked in two ways:
      1. is_scheduled_reminder=True: the adapter (or a domain predicate) has
         already identified this message as a scheduled alert.
      2. A scheduled-reminder marker from the content dict appears in the
         message text (e.g. "[REMINDER]", "[알림]", "timeblock_alert").
    Either condition is sufficient to skip MOMENTUM gating.

    For the dev domain, no-signal phrases are empty (content dict has []),
    so the check never fires -- the exception mechanism exists but is inert.
    This is intentional: dev instances have no timeblock reminders and do not
    nag users about sleep/wellness.  The lifekit domain arms the full list.

    Design bias: conservative -- no-signal list is explicit; unknown phrases
    never trigger.  A message with BOTH a no-signal AND a real-signal phrase
    is treated as having a real signal (passes).  Multiple no-signal phrases
    with no real signal -> one violation (not one per phrase).

    Args:
        text: Message text to evaluate.
        content: Domain content dict (DGN-896).  None -> dev defaults (inert).
        is_scheduled_reminder: True if the adapter/caller has identified this
            as a scheduled alert -- unconditionally exempt.
    """
    no_signal_phrases = _momentum_no_signal_phrases(content)
    if not no_signal_phrases:
        # Dev domain (or unknown): nothing armed -- momentum check is inert.
        return None

    # Scheduled-reminder exemption step 1: caller flag.
    if is_scheduled_reminder:
        return None

    # Scheduled-reminder exemption step 2: content-dict marker in text.
    reminder_markers = _momentum_scheduled_reminder_markers(content)
    if reminder_markers and any(m in text for m in reminder_markers):
        return None

    # Check whether any no-signal phrase appears in visible prose.
    prose = "\n".join(_visible_prose_lines(text))
    if not prose:
        return None

    matched_no_signal = [p for p in no_signal_phrases if p in prose]
    if not matched_no_signal:
        return None  # no stop/nag phrase present -> pass

    # Check whether any real-signal phrase is also present.
    # Real signal suppresses the violation (the stop/nag IS justified).
    real_signal_phrases = _momentum_real_signal_phrases(content)
    if real_signal_phrases and any(p in prose for p in real_signal_phrases):
        return None  # real signal present -> justified stop -> pass

    return Violation(
        rule_id="MOMENTUM",
        severity=SEVERITY_DETECT,
        reason="stop/defer/nag phrase emitted without a real blocking signal",
        metrics={"no_signal_count": len(matched_no_signal)},
        excerpt="; ".join(matched_no_signal[:3]),
    )


def _check_deprecated_term(
    text: str, deprecated_terms: Optional[list]
) -> Optional[Violation]:
    """DEPRECATED_TERM: outgoing prose reuses a retired estate term.

    The term list is the ESTATE-TAXONOMY SoT (adapter-supplied).  Match is a
    case-insensitive substring over VISIBLE PROSE only (code fences, tables,
    blockquotes masked) so quoting a deprecated term inside a code/example
    block does not fire.  Stage 1 = candidate-log only.

    Known FP (documented): a message explaining the deprecation itself must
    name the term ("'domain agent'는 폐기어") and will be flagged -- expected
    Stage-1 candidate, resolved by owner labeling before any Stage-2 block.
    """
    terms = deprecated_terms if deprecated_terms else DEFAULT_DEPRECATED_TERMS
    if not terms:
        return None

    prose = "\n".join(_visible_prose_lines(text))
    if not prose:
        return None

    matched: List[str] = []
    for term in terms:
        # str() guard: SoT text is always str, but never trust the input --
        # a non-str term must not raise (docstring "never raises").
        t = (str(term).strip() if term else "")
        if not t:
            continue
        # ASCII word-boundary match: "main agent" must NOT match inside
        # "domain agent" (do-main agent).  CJK chars are not [A-Za-z], so
        # the guards are inert for CJK-adjacent terms.  Trailing "s?" catches
        # the plural reuse form ("domain agents") without re-opening the
        # substring FP (the leading guard still blocks "do-main agent").
        # Case-insensitive.
        pat = r"(?<![A-Za-z])" + re.escape(t) + r"s?(?![A-Za-z])"
        if re.search(pat, prose, re.IGNORECASE):
            matched.append(t)

    if not matched:
        return None

    return Violation(
        rule_id="DEPRECATED_TERM",
        severity=SEVERITY_DETECT,
        reason="retired estate term reused in user-facing prose (SoT deprecated-term match)",
        metrics={"term_count": len(matched)},
        excerpt="; ".join(matched[:5]),
    )


def _check_sot_unread(text: str, sot_read: bool) -> Optional[Violation]:
    """SOT_UNREAD: org/roster assertion with no sot/ read this turn.

    Fires when ALL of:
      - sot_read is False (adapter found zero sot/ doc reads this turn)
      - visible prose contains at least one SOT_ORG_MARKERS token

    Stage 1 = candidate-log only, armed on the ORG/ROSTER tier only (the
    DGN-815 incident domain).  HIGH FP expected: design discussion about org
    structure is indistinguishable from asserting an org fact.  That is the
    calibration signal -- owner labeling in Stage 2 separates real misses from
    discussion turns before any block.
    """
    if sot_read:
        # A sot/ doc was consulted this turn -> no basis for the flag.
        return None

    prose = "\n".join(_visible_prose_lines(text))
    if not prose:
        return None
    prose_low = prose.lower()

    matched: List[str] = []
    for marker in SOT_ORG_MARKERS:
        if marker.lower() in prose_low:
            matched.append(marker)

    if not matched:
        return None

    return Violation(
        rule_id="SOT_UNREAD",
        severity=SEVERITY_DETECT,
        reason="org/roster assertion with no sot/ read this turn (candidate; may be design-discussion FP)",
        metrics={"marker_count": len(matched), "sot_read": False},
        excerpt="; ".join(matched[:5]),
    )
