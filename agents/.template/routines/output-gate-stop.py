#!/usr/bin/env python3
"""Thin Claude Code Stop-hook adapter -- L1 output gate (DGN-821).

ALL check logic lives in routines/lib/output_gate.py (harness-agnostic).
This file is plumbing only: parse the Stop-hook stdin JSON, extract the
LAST assistant message text from the transcript, call check_message,
and LOG any findings to the canary log.

Enforcement model (current -- ALL rules are detect-only / log-only):
  - EVERY rule (WALL, SKELETON, JARGON, DONE_CLAIM, SOT_UNREAD, MOMENTUM,
    DEPRECATED_TERM) is detect-only in the current build.  The adapter NEVER
    issues a block decision; it always exits 0 with no stdout.
  - SKELETON block was implemented 2026-08-14 and REVERTED the same day
    (see "SKELETON BLOCK REVERT" in DGN-821 worklog) after a pre-deploy
    3-lens review caught two design bugs: B1 (checked-in test regression
    against the detect-only contract) and B2 (split-turn false-block on
    bridge.md separation-contract turns where the conclusion is in an earlier
    message and [[OPTIONS]] is in the final message).  Re-enabling SKELETON
    block requires B1/B2 lock + green suite first.
  - All findings are written to the canary log for owner calibration.  The
    adapter NEVER emits JSON to stdout regardless of what is detected.
  - Lane: interactive only (settings.local.json).  Loop/cron replies are not
    covered here; those are reviewed on their own rails.
  - Fail-open: any error (bad stdin, missing transcript, import failure)
    exits 0 silently.  This hook must never wedge a turn.

_SKELETON_BLOCK_REASON is kept as a module constant for the future
re-enablement of SKELETON block after the B1/B2 design fixes are locked.
It is NOT currently used -- included here so the constant is available
without code changes when SKELETON block is re-enabled.

Log path: <BOT_DATA_DIR>/logs/output-gate-canary.log
  (resolved from config/agent.conf BOT_DATA_DIR; falls back to
   .telegram_bot/logs relative to cwd; absolute path if env-set).

Log format per finding:
  TIMESTAMP  RULE_ID  metric_key=val ...  [excerpt_if_any]

Transcript reading pattern mirrors routines/status-footer.py (transcript_path
from stdin JSON -> read JSONL -> last assistant message text blocks).
"""
import json
import os
import sys
import time

# Single conf parser (DGN-916): quote/whitespace normalization lives in
# routines/lib/conf_reader.py only.  Guarded import keeps the Stop-hook
# fail-open contract even if the lib file is ever missing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from conf_reader import conf_get
except Exception:
    def conf_get(key, conf_path=None):
        return ""

# ---------------------------------------------------------------------------
# Log path resolution
# ---------------------------------------------------------------------------
_LOG_FILENAME = "output-gate-canary.log"

# Model-facing regeneration feedback for a SKELETON block (NOT user copy).
# DGN-896 / DGN-874 Part 4: English only (RULES Code=English/ASCII).
_SKELETON_BLOCK_REASON = (
    "L0 response skeleton violation: this is a decision-turn message "
    "([[OPTIONS]] present) but the top conclusion paragraph is missing. "
    "Put the conclusion/answer in 1-2 lines at the top, grounds/context "
    "in the middle, and the decision list at the bottom. Rewrite. "
    "(output-gate SKELETON, DGN-821)"
)


def _resolve_log_path(cwd: str) -> str:
    """Resolve the canary log path.

    Priority:
      1. env DOGANY_OUTPUT_GATE_LOG (test override)
      2. BOT_DATA_DIR from config/agent.conf + /logs/<filename>
      3. .telegram_bot/logs/<filename> relative to cwd
    """
    env_path = os.environ.get("DOGANY_OUTPUT_GATE_LOG")
    if env_path:
        return env_path

    # Try config/agent.conf for BOT_DATA_DIR (single conf parser, DGN-916).
    val = conf_get("BOT_DATA_DIR", os.path.join(cwd, "config", "agent.conf"))
    if val:
        return os.path.join(val, "logs", _LOG_FILENAME)

    # Fallback: .telegram_bot/logs relative to cwd.
    return os.path.join(cwd, ".telegram_bot", "logs", _LOG_FILENAME)


def _log_findings(log_path: str, violations: list) -> None:
    """Append one line per violation to the canary log."""
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as fh:
            for v in violations:
                metrics_str = " ".join(
                    "%s=%s" % (k, val) for k, val in v.metrics.items()
                )
                excerpt_str = ("[%s]" % v.excerpt) if v.excerpt else ""
                fh.write(
                    "%s  %s  %s  %s\n"
                    % (ts, v.rule_id, metrics_str, excerpt_str)
                )
    except Exception:
        pass  # fail-open: log write failure never surfaces


# ---------------------------------------------------------------------------
# Deprecated-term source (ESTATE-TAXONOMY SoT, single source of truth)
# ---------------------------------------------------------------------------
_DEP_BEGIN = "DEPRECATED-TERMS-BEGIN"
_DEP_END = "DEPRECATED-TERMS-END"


def _sot_dir_candidates(cwd: str) -> list:
    """Candidate sot/ directories (workspace has no sot symlink; it is shared).

    DGN-896 portability: no instance-specific hardcoded paths.  Priority order:
      1. DOGANY_SOT_DIR env var (explicit override; test/CI use this)
      2. cwd/sot (canonical workspace-local symlink or directory)
      3. cwd/../../sot (worktree layout: agents/<slug>/ -> agents/../sot/)
      4. DOGANY_ROOT env var + /sot (installation root, if set)
    """
    candidates = [
        os.path.join(cwd, "sot"),
        os.path.join(cwd, "..", "..", "sot"),
    ]
    # Env-var carve-outs (DGN-896): explicit env overrides expand first.
    env_sot = os.environ.get("DOGANY_SOT_DIR")
    if env_sot:
        candidates.insert(0, env_sot)
    dogany_root = os.environ.get("DOGANY_ROOT")
    if dogany_root:
        candidates.append(os.path.join(dogany_root, "sot"))
    return candidates


def _load_deprecated_terms(cwd: str) -> list:
    """Parse the machine-readable deprecated-term block from ESTATE-TAXONOMY.

    Block format (between _DEP_BEGIN and _DEP_END marker lines):
        term => replacement (scope note)
    Only the text BEFORE "=>" is taken as the term.  Fails open to [] on any
    trouble (missing file, no block) so the DEPRECATED_TERM check simply stays
    silent rather than wedging the hook.
    """
    for d in _sot_dir_candidates(cwd):
        path = os.path.join(d, "ESTATE-TAXONOMY.md")
        if not os.path.isfile(path):
            continue
        try:
            terms: list = []
            in_block = False
            with open(path, "r", errors="replace") as fh:
                for line in fh:
                    if _DEP_BEGIN in line:
                        in_block = True
                        continue
                    if _DEP_END in line:
                        break
                    if not in_block:
                        continue
                    # Defensive stop: if the END marker was ever deleted, a
                    # markdown heading marks the next section -- do not absorb
                    # the rest of the doc's "=>" lines as terms.
                    if line.lstrip().startswith("#"):
                        break
                    if "=>" not in line:
                        continue
                    term = line.split("=>", 1)[0].strip()
                    # Strip list bullets / stray markup.
                    term = term.lstrip("-*# ").strip().strip('"').strip("'")
                    if term:
                        terms.append(term)
            return terms
        except Exception:
            return []
    return []


# ---------------------------------------------------------------------------
# Transcript reading (same pattern as status-footer.py)
# ---------------------------------------------------------------------------

def _is_genuine_user_boundary(etype: str, is_injected: bool, msg: dict) -> bool:
    """Return True if this entry qualifies as a genuine user-prompt boundary.

    A genuine boundary: type=user, not injected, carries a text block,
    and does NOT carry a tool_result block (tool_result entries are tool
    feedback, not user prompts).
    """
    if etype != "user" or is_injected:
        return False
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return True
    if isinstance(content, list):
        has_text = any(
            isinstance(b, dict) and b.get("type") == "text"
            for b in content
        )
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        )
        if has_text and not has_tool_result:
            return True
    return False


def _entries_have_tool_use(entries: list, start: int, end: int) -> bool:
    """Return True if any assistant tool_use block exists in entries[start:end]."""
    for etype, _is_injected, msg in entries[start:end]:
        if etype != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
    return False


def _had_tool_use_this_turn(path: str) -> bool:
    """Return True if ANY tool_use ran in the CURRENT or IMMEDIATELY PRIOR turn.

    Cross-turn window (DGN-821 calibration 2026-08-15):
      - "Current turn" = entries after the last genuine user prompt boundary.
      - "Immediately prior turn" = entries between the second-to-last and the
        last genuine user prompt boundaries.
      - had_action is True if EITHER window contains a tool_use OR an assistant
        entry that signals a background/subagent task completion (tool_use in
        a sidechain entry counts, since the adapter folds subagent transcripts).

    Window rationale: cross-turn DONE_CLAIM FPs arise when the agent does real
    work in turn N (tool_use present), then sends a pure-text status report in
    turn N+1 ("완료했습니다"; had_action=False for turn N+1 alone).  Including
    the immediately prior turn in the window suppresses this class of FP while
    keeping the detector armed for genuine "no work at all" violations.  Only
    one prior turn is included -- extending further would suppress true violations
    (two-turn-old work is not a credible basis for a current completion claim).

    Fail-open to True (I/O/parse error, no boundary found -> "assume action
    present" so DONE_CLAIM does NOT fire spuriously on adapter errors).

    Why the whole current turn, not just the last message group: a turn with
    tool calls has MULTIPLE assistant message groups -- the tool_use blocks
    live in earlier groups and the closing summary is the last group.  Scanning
    only the last group wrongly reports had_action=False (DGN-821 calibration
    root-cause fix).
    """
    if not path or not os.path.isfile(path):
        return True  # fail-open: assume action present

    try:
        entries: list = []  # (etype, is_injected, msg_dict)
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype not in ("assistant", "user"):
                    continue
                is_injected = bool(
                    entry.get("isMeta") or entry.get("isSidechain")
                )
                entries.append((etype, is_injected, entry.get("message") or {}))
    except Exception:
        return True  # fail-open

    if not entries:
        return True  # fail-open

    # Collect genuine user-prompt boundary indices (last two are enough).
    boundaries: list = []
    for idx in range(len(entries) - 1, -1, -1):
        etype, is_injected, msg = entries[idx]
        if _is_genuine_user_boundary(etype, is_injected, msg):
            boundaries.append(idx)
            if len(boundaries) == 2:
                break

    if not boundaries:
        return True  # fail-open: no clear turn boundary

    last_boundary = boundaries[0]

    # Check current turn: entries after the last genuine user prompt.
    if _entries_have_tool_use(entries, last_boundary + 1, len(entries)):
        return True

    # Check immediately prior turn: entries between the second-to-last
    # and last genuine user prompt boundaries (cross-turn window).
    if len(boundaries) >= 2:
        prior_boundary = boundaries[1]
        if _entries_have_tool_use(entries, prior_boundary + 1, last_boundary):
            return True

    return False


def _sot_read_this_turn(path: str) -> bool:
    """Return True if any sot/ doc was consulted in the CURRENT turn.

    "Current turn" = entries after the last GENUINE user prompt (a type=user
    entry whose content is a plain string or contains a text block -- NOT a
    tool_result-only entry).  Within that window, any assistant tool_use block
    whose serialized input references "sot/" (Read file_path, Grep path, Bash
    command, etc.) counts as a consultation.

    Fails open to True (any I/O or parse error, or no user boundary found ->
    "assume sot was read" so SOT_UNREAD does NOT fire spuriously on adapter
    errors -- same conservative bias as _had_tool_use_this_turn).
    """
    if not path or not os.path.isfile(path):
        return True  # fail-open

    try:
        entries: list = []  # (etype, is_injected, msg_dict)
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype not in ("assistant", "user"):
                    continue
                # isMeta = harness-injected (skill body, hook context, etc.);
                # isSidechain = subagent record folded into the same file.
                # Neither is a genuine user PROMPT and must not be treated as a
                # turn boundary (Major-1: skill-injected user entries were
                # swallowing the current turn's earlier sot/ reads).
                is_injected = bool(
                    entry.get("isMeta") or entry.get("isSidechain")
                )
                entries.append((etype, is_injected, entry.get("message") or {}))
    except Exception:
        return True  # fail-open

    if not entries:
        return True  # fail-open

    # Find last genuine user prompt boundary.  A genuine boundary is a real
    # user PROMPT: type user, NOT injected, content a plain string or a list
    # that carries a text block and NO tool_result block (tool_result-bearing
    # user entries are tool feedback, not prompts).
    boundary = -1
    for idx in range(len(entries) - 1, -1, -1):
        etype, is_injected, msg = entries[idx]
        if etype != "user" or is_injected:
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            boundary = idx
            break
        if isinstance(content, list):
            has_text = any(
                isinstance(b, dict) and b.get("type") == "text"
                for b in content
            )
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
            if has_text and not has_tool_result:
                boundary = idx
                break

    if boundary < 0:
        return True  # fail-open: no clear turn boundary

    # Scan assistant tool_use after the boundary for a sot/ reference.
    for etype, is_injected, msg in entries[boundary + 1:]:
        if etype != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            try:
                blob = json.dumps(block.get("input") or {}).lower()
            except Exception:
                continue
            if "sot/" in blob:
                return True
    return False


def _current_turn_assistant_texts(path: str) -> list:
    """Return ordered list of assistant message texts in the current turn.

    "Current turn" = entries after the last genuine user prompt boundary,
    using the same _is_genuine_user_boundary predicate as _had_tool_use_this_turn
    and _sot_read_this_turn.  Reuses existing turn-boundary machinery (DGN-874 B2).

    Each distinct assistant message (grouped by message.id) produces one entry
    in the returned list.  Messages with no text content are excluded.
    Returns [] on any I/O or parse trouble (fail-open: caller falls back to
    single-message SKELETON scan).
    """
    if not path or not os.path.isfile(path):
        return []

    try:
        entries: list = []  # (etype, is_injected, msg_dict)
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype not in ("assistant", "user"):
                    continue
                is_injected = bool(
                    entry.get("isMeta") or entry.get("isSidechain")
                )
                entries.append((etype, is_injected, entry.get("message") or {}))
    except Exception:
        return []

    if not entries:
        return []

    # Find the last genuine user prompt boundary.
    boundary = -1
    for idx in range(len(entries) - 1, -1, -1):
        etype, is_injected, msg = entries[idx]
        if _is_genuine_user_boundary(etype, is_injected, msg):
            boundary = idx
            break

    # Collect assistant messages after the boundary, grouped by message.id.
    # Each group (same id) is joined into one text; distinct ids become
    # distinct entries in the returned list (preserving message order).
    result: list = []
    cur_id = None
    cur_parts: list = []

    def _flush():
        if cur_parts:
            result.append("\n".join(cur_parts))

    for etype, _is_inj, msg in entries[boundary + 1:]:
        if etype != "assistant":
            # Non-assistant entry (tool_result user entry) inside the turn:
            # keep accumulating -- do not reset grouping (the message group
            # continues across tool feedback entries).
            continue
        mid = msg.get("id")
        texts: list = []
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                ):
                    t = block.get("text") or ""
                    if t.strip():
                        texts.append(t)
        elif isinstance(content, str) and content.strip():
            texts.append(content)

        if mid is not None and cur_id is not None and mid != cur_id:
            # New message id -> flush current group.
            _flush()
            cur_parts = []
        if mid is not None:
            cur_id = mid
        cur_parts.extend(texts)

    _flush()
    return result


def _current_turn_user_prompt(path: str) -> str:
    """Return the text of the last genuine user prompt in the transcript.

    Finds the last genuine user-prompt boundary (same predicate as the other
    turn-boundary functions) and extracts its text content.  Text blocks in
    a content list are joined; plain-string content returned as-is.

    Returns "" on any I/O / parse trouble (fail-open: the caller treats "" as
    a non-hit, so no raw logging occurs on errors -- safe default).
    """
    if not path or not os.path.isfile(path):
        return ""

    try:
        entries: list = []  # (etype, is_injected, msg_dict)
        with open(path, "r", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except Exception:
                    continue
                etype = entry.get("type")
                if etype not in ("assistant", "user"):
                    continue
                is_injected = bool(
                    entry.get("isMeta") or entry.get("isSidechain")
                )
                entries.append((etype, is_injected, entry.get("message") or {}))
    except Exception:
        return ""

    if not entries:
        return ""

    # Walk backwards to find the last genuine user prompt.
    for etype, is_injected, msg in reversed(entries):
        if not _is_genuine_user_boundary(etype, is_injected, msg):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text") or ""
                    if t.strip():
                        parts.append(t)
            return "\n".join(parts)
    return ""


# DGN-914 Phase 1: raw canary log filename.
_RAW_CANARY_FILENAME = "output-gate-canary-raw.log"


def _log_raw_canary(log_path: str, prompt: str, answer: str) -> None:
    """Append ONE raw canary line for a multi-question hit turn.

    Privacy boundary (DGN-914, owner-approved): called ONLY when the
    prefilter flags the prompt as a multi-question turn.  Never called for
    single/non-question turns.  One line per turn; format:

      TIMESTAMP  MULTI_Q  prompt=<first 120 chars>  answer=<first 120 chars>

    Truncation prevents the log from becoming a full conversation dump while
    preserving enough signal for Phase 2 batch judge calibration.  Full text
    is available in the Claude transcript if the batch judge needs it.

    Fail-open: any write error is silently swallowed (never wedge a turn).
    """
    try:
        raw_dir = os.path.dirname(log_path)
        os.makedirs(raw_dir, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        # Truncate to 120 chars and make single-line (replace newlines).
        def _clip(s: str, n: int = 120) -> str:
            s = s.replace("\n", " ").replace("\r", " ")
            return s[:n] + ("..." if len(s) > n else "")

        raw_log = os.path.join(raw_dir, _RAW_CANARY_FILENAME)
        with open(raw_log, "a", encoding="utf-8") as fh:
            fh.write(
                "%s  MULTI_Q  prompt=%s  answer=%s\n"
                % (ts, _clip(prompt), _clip(answer))
            )
    except Exception:
        pass  # fail-open: log write failure never surfaces


def _last_assistant_text(path: str) -> str:
    """Concatenate text blocks of the LAST assistant message from JSONL.

    The transcript is JSONL; one API assistant message may span several
    consecutive type=assistant lines sharing message.id.  Group trailing
    consecutive assistant entries (split on a non-assistant entry or an id
    change) and return the last group's text joined by newlines.
    I/O or parse trouble -> "" (fail-open).
    """
    if not path or not os.path.isfile(path):
        return ""
    last_parts: list = []
    cur_parts: list = []
    cur_id = None
    try:
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                # Cheap pre-filter for non-assistant lines.
                if (
                    '"type":"assistant"' not in raw
                    and '"type": "assistant"' not in raw
                ):
                    if cur_parts:
                        last_parts = cur_parts
                    cur_parts = []
                    cur_id = None
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                if entry.get("type") != "assistant":
                    if cur_parts:
                        last_parts = cur_parts
                    cur_parts = []
                    cur_id = None
                    continue
                msg = entry.get("message") or {}
                mid = msg.get("id")
                texts: list = []
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "text"
                        ):
                            t = block.get("text") or ""
                            if t.strip():
                                texts.append(t)
                elif isinstance(content, str) and content.strip():
                    texts.append(content)
                if cur_id is not None and mid is not None and mid != cur_id:
                    if cur_parts:
                        last_parts = cur_parts
                    cur_parts = []
                if mid is not None:
                    cur_id = mid
                cur_parts.extend(texts)
    except Exception:
        return ""
    parts = cur_parts if cur_parts else last_parts
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Domain-content injection (DGN-896)
# ---------------------------------------------------------------------------

def _read_agent_role(cwd: str) -> str:
    """Read AGENT_ROLE from config/agent.conf.

    Returns the value (lowercase, stripped) or "dev" if absent/unreadable.
    Fail-open to "dev" so the dev-domain defaults are always used on any
    error -- the adapter must never wedge a turn.
    """
    val = conf_get("AGENT_ROLE", os.path.join(cwd, "config", "agent.conf"))
    return val.lower() if val else "dev"


def _load_active_domain_content(cwd: str) -> dict:
    """Load and return the active domain content dict for check_message().

    Reads AGENT_ROLE from config/agent.conf and returns the matching entry
    from output_gate_content.DOMAIN_CONTENT.  Fails open to the dev-domain
    content dict on any import or lookup error.

    Import is deferred (same pattern as check_message import) so a missing
    output_gate_content.py does not wedge the hook.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "output_gate_content",
            os.path.join(here, "lib", "output_gate_content.py"),
        )
        if spec is None or spec.loader is None:
            return {}
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        domain = _read_agent_role(cwd)
        get_fn = getattr(mod, "get_domain_content", None)
        if callable(get_fn):
            return get_fn(domain)
        # Fallback: direct dict access.
        domain_content = getattr(mod, "DOMAIN_CONTENT", {})
        return domain_content.get(domain, domain_content.get("dev", {}))
    except Exception:
        return {}  # fail-open: no content -> lib uses dev defaults


def _detect_scheduled_reminder(text: str, domain_content: dict) -> bool:
    """Return True if the message appears to be a scheduled-reminder alert.

    Checks the message for any scheduled-reminder markers defined in the
    active domain content dict.  For the dev domain the list is empty so
    this always returns False (inert -- no false exemptions in dev).

    This is the adapter-side predicate the spec calls for: the lib receives
    is_scheduled_reminder=True and unconditionally skips MOMENTUM gating.
    """
    markers = domain_content.get("momentum_scheduled_reminder_markers", [])
    if not markers:
        return False
    return any(m in text for m in markers)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        try:
            data = json.load(sys.stdin)
        except Exception:
            sys.exit(0)  # fail-open: bad input never blocks
        if not isinstance(data, dict):
            sys.exit(0)

        # Gate MAIN-session output only; never fire on SubagentStop.
        if data.get("hook_event_name") not in (None, "Stop"):
            sys.exit(0)

        cwd = str(data.get("cwd") or os.getcwd())
        transcript_path = data.get("transcript_path") or ""
        text = _last_assistant_text(transcript_path)
        if not text:
            sys.exit(0)

        # Scan for tool_use in the current turn (for DONE_CLAIM check).
        had_action = _had_tool_use_this_turn(transcript_path)
        # Scan for a sot/ read in the current turn (for SOT_UNREAD check).
        sot_read = _sot_read_this_turn(transcript_path)
        # Parse the canonical deprecated-term list from ESTATE-TAXONOMY SoT.
        deprecated_terms = _load_deprecated_terms(cwd)
        # DGN-874 B2: collect all assistant message texts in the current turn
        # for turn-unit SKELETON scan.  Reuses existing boundary machinery.
        turn_texts = _current_turn_assistant_texts(transcript_path)

        # DGN-896: load active domain content dict for content-pluggable checks.
        domain_content = _load_active_domain_content(cwd)
        # DGN-896: detect scheduled-reminder utterance (MOMENTUM exception).
        is_scheduled_reminder = _detect_scheduled_reminder(text, domain_content)

        # Import the portable library (deferred so import failure is fail-open).
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, os.path.join(here, "lib"))
        from output_gate import check_message, is_multi_question_prompt  # noqa: PLC0415

        violations = check_message(
            text,
            had_action=had_action,
            sot_read=sot_read,
            deprecated_terms=deprecated_terms,
            turn_texts=turn_texts if len(turn_texts) > 1 else None,
            content=domain_content if domain_content else None,
            is_scheduled_reminder=is_scheduled_reminder,
        )

        # DGN-914 Phase 1: multi-question prefilter + hit-turn raw logging.
        # Extract the user prompt for the current turn and check the prefilter.
        # Raw (prompt, answer) is logged ONLY on a prefilter hit (privacy boundary).
        # Single/non-question turns: NO raw logging.  Fail-open: any error in
        # the prefilter path is silently suppressed (never wedge the hook).
        try:
            user_prompt = _current_turn_user_prompt(transcript_path)
            if user_prompt and is_multi_question_prompt(user_prompt):
                log_path_for_raw = _resolve_log_path(cwd)
                _log_raw_canary(log_path_for_raw, user_prompt, text)
        except Exception:
            pass  # fail-open: prefilter never blocks

        if not violations:
            sys.exit(0)

        log_path = _resolve_log_path(cwd)
        _log_findings(log_path, violations)
        # All rules are detect-only in the current build (DGN-821).
        # No block decision is emitted; violations are logged to the canary
        # only.  Re-enabling any block requires design fixes + green suite
        # first (see header docstring and DGN-821 / DGN-874 for details).
        sys.exit(0)

    except SystemExit:
        raise
    except BaseException:
        # Catch-all fail-open: any uncaught exception exits 0 silently.
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)


if __name__ == "__main__":
    main()
