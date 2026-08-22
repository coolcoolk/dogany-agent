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
  FP2 (telegram.md Tier-2 blockquote): `> ` blockquote lines are the
      official Tier-2 option-layout format.  Gate previously counted them
      as wall-text or skeleton violations.  Tier-2 blocks are now excluded
      from prose metrics and from the SKELETON trigger.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

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

    FP2 fix: these lines are the sanctioned Tier-2 format (telegram.md);
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
    rule_id: str          # WALL|SKELETON|JARGON|DONE_CLAIM|DEPRECATED_TERM|SOT_UNREAD|MOMENTUM|BACKREF
    severity: str         # always SEVERITY_DETECT in Stage 1
    reason: str
    metrics: dict = field(default_factory=dict)  # length/structure indicators
    excerpt: str = ""     # short indicator only, never full message text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_message(
    text: Optional[str],
    had_action: bool = True,
    sot_read: bool = True,
    deprecated_terms: Optional[list] = None,
    turn_texts: Optional[List[str]] = None,
    content: Optional[dict] = None,
    is_scheduled_reminder: bool = False,
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
