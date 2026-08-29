#!/usr/bin/env python3
"""PreToolUse done-claim gate: blocks status->done transitions on worklog tickets
that lack verification evidence (DGN-898).

Built and hardened live on the Metal dev-crew instance (DGN-1009), carried to
the canonical template by DGN-1079-group2-carryback. decomp-gate.py /
decomp-ledger.py (DGN-1009 item 3, the work-set declare/carry ledger) were
NOT carried in the same pass: that mechanism is a forcing point for a "Lane
routing / default-parallel-autonomy" doctrine that lives only in the Metal
instance's own persona docs, not in this template's persona file or
rules/hot.framework.md -- shipping the gate without the doctrine it enforces would be
exactly the false-forcing-point defect DGN-1006 targets.

Design (re: 재확정 + 2.0 rider, 2026-08-20):
  done-claim gate = formal presence gate, NOT a truth oracle.
  The gate verifies that an evidence LINE exists in the resulting ticket content.
  It cannot verify that the evidence is accurate or that the claimed verification
  actually happened.  A dishonest or mistaken agent can still write a passing line.
  This is an intentional limit, identical in spirit to DGN-935 write-gate: a
  structural forcing point that prevents accidental/lazy omission, not a content
  auditor.  Truth-level verification remains the human owner's responsibility.

Mechanism:
  Trigger: Write, Edit or MultiEdit whose file_path matches
    worklog/<TICKET_FILE_GLOB>.
    TICKET_FILE_GLOB is instance-configurable (portability rider):
    env TICKET_FILE_GLOB= wins, else config/agent.conf `TICKET_FILE_GLOB=`,
    else falls back to the original hardcoded "DGN-*.md" (this template's
    own default, zero regression when unset). Case-insensitive match against
    the basename.
  Detect done-transition:
    - Write:  tool_input["content"] contains a "status: done" line
    - Edit:   tool_input["new_string"] introduces "status: done"
    - MultiEdit: any tool_input["edits"][i]["new_string"] introduces
      "status: done" (DGN-1009 hole: the tool reached the hook via the
      Edit|Write|MultiEdit matcher but the script silently ignored it)
  Evidence marker check (case-insensitive, any of the following on their own line,
  or as a phrase in the content):
    verify:      -- explicit verification record
    evidence:    -- evidence field
    e2e:         -- end-to-end test record (DGN-898 UX/e2e rider)
    tier1        -- Tier-1 self-lint pass record
    test:        -- test result record
    self-verify ok  -- free-form self-verify confirmation phrase
    test.*pass      -- test pass phrase (regex)
  If done-transition detected AND no evidence marker present -> DENY.
  All other calls -> ALLOW unconditionally.

Fail-open on any error: a gate malfunction must never block the agent.

I/O contract mirrors tmp-artifact-gate.py / cc-memory-write-guard.py:
  - reads PreToolUse JSON payload from stdin
  - emits deny via hookSpecificOutput/permissionDecision JSON to stdout
  - always exits 0 (permissionDecision field signals deny, not the exit code)

--scan mode (DGN-1009 extension, additive -- hook invocation passes no argv and
is untouched):
  python3 done-claim-gate.py --scan [--since YYYY-MM-DD] [--repo DIR]
  Retroactive backstop for the paths the PreToolUse hook structurally cannot
  see.  Measured leak that motivated it: DGN-1003 / DGN-1004 reached
  status: done on 2026-08-21 (gate live since 2026-08-20) with zero evidence
  markers -- the flips arrived via paths outside this session's Write/Edit
  tools.  The scan walks worklog/DGN-*.md files whose git history changed
  since --since (default GATE_LIVE_DATE), and flags any file whose
  frontmatter status is done, whose done-line introduction (git pickaxe) is
  on/after --since, and whose content has no evidence marker.  Exit 1 on
  findings, 0 clean.

KNOWN LIMITS -- what this machinery CANNOT catch (DGN-1009 item 3):
  1. Presence gate, not a truth oracle (both modes): any line matching an
     evidence marker satisfies it, including a fabricated or mistaken one.
     Truth-level verification stays with the human owner.
  2. Hook mode only sees Write/Edit tool calls in sessions where the hook is
     wired (.claude/settings.local.json is per-instance and gitignored:
     worktree/junior sessions launched as separate processes, plain Bash
     writes (sed/echo/git merge), and other machines are all invisible).
  3. Scan mode dates the done transition by the newest git-pickaxe commit for
     the literal string "status: done"; a body-prose occurrence of that string
     appearing/disappearing can mis-date the transition.  Transitions strictly
     before --since are deliberately out of scope (legacy done tickets are not
     re-litigated).
  4. Scan mode enforces nothing until wired to a scheduled runner (proposal:
     daily gate-scan alongside ticket-hygiene.sh --gates-only); unwired it
     catches nothing.
  5. Product T-NNN tickets are NOT covered by either mode (worklog/<glob>
     only) -- unchanged from DGN-898.
  6. TICKET_FILE_GLOB is a presence-gate widener, not a smarter classifier:
     a broad pattern (e.g. "*.md" for instances whose tickets are not
     DGN-*-named) makes every direct worklog/*.md file a done-transition
     candidate, not just tickets. False-positive risk is bounded by the
     unrelated triggers this gate already requires (a literal "status: done"
     line AND, for --scan, YAML frontmatter status: done) -- non-ticket
     worklog files (pickup notes, reports/ subdir, decisions/ subdir) are
     filtered out unless they happen to carry that exact text. Instances
     adopting a wide glob should confirm their non-ticket worklog/*.md files
     never contain a literal "status: done" line.
"""
import fnmatch
import json
import os
import re
import sys

# Only intercept these tools.
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# Regex: matches "status: done" as a line (leading whitespace allowed, value
# may have trailing whitespace).  Case-insensitive.
_RE_STATUS_DONE = re.compile(r"^\s*status\s*:\s*done\s*$", re.IGNORECASE | re.MULTILINE)

# Evidence markers accepted (case-insensitive).  These are checked against the
# FULL resulting content (for Write) or the new_string fragment (for Edit, since
# that is where new evidence would appear -- or the on-disk file already has it).
#
# Line-anchored markers (must appear at start of a line, with optional leading
# space, followed by colon and at least one non-whitespace character):
_RE_LINE_MARKERS = re.compile(
    r"^\s*(verify|evidence|e2e|test)\s*:\s*\S",
    re.IGNORECASE | re.MULTILINE,
)

# Phrase markers (may appear anywhere in the content):
_RE_PHRASE_MARKERS = re.compile(
    r"(?:self[-\s]verify\s+ok|tier1\b|test.*pass)",
    re.IGNORECASE,
)

DENY_REASON = (
    "done 전이엔 검증 증거가 필요합니다(DGN-898): "
    "verify:/evidence:/e2e:/test: 라인 또는 self-grill 결과를 "
    "티켓에 남기세요."
)


TICKET_GLOB_KEY = "TICKET_FILE_GLOB"
DEFAULT_TICKET_GLOB = "DGN-*.md"


def _ticket_glob(repo: str = None) -> str:
    """Instance ticket-filename glob (portability rider).

    Resolution order: env TICKET_FILE_GLOB= (wins) -> config/agent.conf
    `TICKET_FILE_GLOB=` (checked at `repo`, then cwd, then the script's own
    ../config -- same candidate order as usage-gate.py's read_conf) ->
    hardcoded DEFAULT_TICKET_GLOB. Missing/unset config on any instance
    reproduces the original DGN-*.md-only behavior exactly -- no regression.
    """
    env = os.environ.get(TICKET_GLOB_KEY, "").strip()
    if env:
        return env
    candidates = []
    if repo:
        candidates.append(os.path.join(repo, "config", "agent.conf"))
    candidates.append(os.path.join(os.getcwd(), "config", "agent.conf"))
    candidates.append(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config", "agent.conf"))
    prefix = TICKET_GLOB_KEY + "="
    seen = set()
    for path in candidates:
        norm = os.path.normpath(path)
        if norm in seen:
            continue
        seen.add(norm)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(prefix):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            return val
        except OSError:
            continue
    return DEFAULT_TICKET_GLOB


def _is_worklog_ticket(file_path: str, repo: str = None) -> bool:
    """Return True if file_path is a worklog/<TICKET_FILE_GLOB> ticket."""
    normalized = os.path.normpath(file_path)
    # Match the last two path segments: .../worklog/<glob>
    parts = normalized.replace("\\", "/").split("/")
    if len(parts) < 2:
        return False
    parent = parts[-2]
    basename = parts[-1]
    if parent != "worklog":
        return False
    return fnmatch.fnmatch(basename.lower(), _ticket_glob(repo).lower())


def _has_status_done(text: str) -> bool:
    """Return True if text contains a 'status: done' line."""
    return bool(_RE_STATUS_DONE.search(text))


def _has_evidence(text: str) -> bool:
    """Return True if text contains at least one recognised evidence marker."""
    if _RE_LINE_MARKERS.search(text):
        return True
    if _RE_PHRASE_MARKERS.search(text):
        return True
    return False


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


# Date the PreToolUse hook went live on this instance (DGN-898 canary).
# Scan mode does not re-litigate done transitions older than this.
# Instance-configurable since DGN-1009 (the hardcoded value below is simply
# this template's own placeholder go-live date; each instance that wires the
# hook should set its own via config/agent.conf or env once it goes live):
# resolution mirrors _ticket_glob -- env GATE_LIVE_DATE= wins, else
# config/agent.conf `GATE_LIVE_DATE=`, else this default (no regression when
# unset).  Value format YYYY-MM-DD; an invalid value fails loudly in
# time.strptime, same as an invalid --since argument.
GATE_LIVE_DATE_KEY = "GATE_LIVE_DATE"
GATE_LIVE_DATE = "2026-08-20"


def _gate_live_date(repo: str = None) -> str:
    """Instance gate go-live date (DGN-1009 portability rider).

    Resolution order: env GATE_LIVE_DATE= (wins) -> config/agent.conf
    `GATE_LIVE_DATE=` (checked at `repo`, then cwd, then the script's own
    ../config -- same candidate order as _ticket_glob / usage-gate.py's
    read_conf) -> hardcoded GATE_LIVE_DATE.  Missing/unset config on any
    instance reproduces the original behavior exactly -- no regression.
    """
    env = os.environ.get(GATE_LIVE_DATE_KEY, "").strip()
    if env:
        return env
    candidates = []
    if repo:
        candidates.append(os.path.join(repo, "config", "agent.conf"))
    candidates.append(os.path.join(os.getcwd(), "config", "agent.conf"))
    candidates.append(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config", "agent.conf"))
    prefix = GATE_LIVE_DATE_KEY + "="
    seen = set()
    for path in candidates:
        norm = os.path.normpath(path)
        if norm in seen:
            continue
        seen.add(norm)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(prefix):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            return val
        except OSError:
            continue
    return GATE_LIVE_DATE


def _frontmatter_status(text: str) -> str:
    """Return the status value from YAML frontmatter, '' if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^status\s*:\s*(\S+)", line)
        if m:
            return m.group(1).strip()
    return ""


def _scan(repo: str, since: str) -> int:
    """Retroactive done-without-evidence scan.  Returns exit code."""
    import subprocess
    import time

    def git(args):
        return subprocess.run(["git"] + args, capture_output=True, text=True,
                              cwd=repo).stdout

    since_ct = time.mktime(time.strptime(since, "%Y-%m-%d"))

    # Candidates: worklog/DGN-*.md touched by any commit since the date, plus
    # files with uncommitted local changes.
    touched = set()
    log = git(["log", f"--since={since}", "--name-only", "--format="])
    for line in log.splitlines():
        line = line.strip()
        if line and _is_worklog_ticket(line, repo):
            touched.add(line)
    porcelain = git(["status", "--porcelain", "--", "worklog/"])
    for line in porcelain.splitlines():
        path = line[3:].strip().strip('"')
        if path and _is_worklog_ticket(path, repo):
            touched.add(path)

    findings = []
    checked = 0
    for rel in sorted(touched):
        path = os.path.join(repo, rel)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue  # deleted/renamed since -- nothing to judge
        if _frontmatter_status(content).lower() != "done":
            continue
        checked += 1
        if _has_evidence(content):
            continue
        # No evidence: date the done transition via pickaxe (newest commit
        # changing the occurrence count of the literal done line).
        hits = git(["log", "--format=%ct|%h", "-S", "status: done", "--", rel])
        transition_ct, transition_sha = None, "uncommitted"
        first = hits.splitlines()[0].strip() if hits.strip() else ""
        if first:
            ct_s, transition_sha = first.split("|", 1)
            transition_ct = int(ct_s)
        if transition_ct is not None and transition_ct < since_ct:
            continue  # legacy transition, out of scope
        when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(transition_ct))
                if transition_ct else "uncommitted (working tree)")
        findings.append(f"[NO-EVIDENCE] {rel}: status done since {when} "
                        f"({transition_sha}) with no evidence marker "
                        f"(verify:/evidence:/e2e:/test:/tier1/self-verify ok/"
                        f"test..pass)")

    print(f"done-claim scan: since={since}, candidates={len(touched)}, "
          f"done-status checked={checked}")
    if findings:
        for f in findings:
            print(f)
        print(f"RESULT: {len(findings)} finding(s)")
        return 1
    print("RESULT: clean")
    return 0


def main():
    if "--scan" in sys.argv:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--scan", action="store_true")
        # --since default resolves per-instance AFTER parsing (needs --repo);
        # explicit --since always wins.
        ap.add_argument("--since", default=None)
        ap.add_argument("--repo", default=os.getcwd())
        args = ap.parse_args()
        repo = os.path.abspath(args.repo)
        since = args.since or _gate_live_date(repo)
        sys.exit(_scan(repo, since))

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open: bad / missing input never blocks

    try:
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input") or {}
        cwd = data.get("cwd") or os.getcwd()

        if tool_name not in _WRITE_TOOLS:
            sys.exit(0)

        file_path = tool_input.get("file_path") or ""
        if not isinstance(file_path, str) or not file_path.strip():
            sys.exit(0)

        file_path = file_path.strip()

        if not _is_worklog_ticket(file_path, cwd):
            sys.exit(0)  # not a worklog ticket -> pass through

        if tool_name == "Write":
            content = tool_input.get("content") or ""
            if not _has_status_done(content):
                sys.exit(0)  # not a done transition -> allow
            if _has_evidence(content):
                sys.exit(0)  # evidence present -> allow
            _deny(DENY_REASON)

        elif tool_name == "Edit":
            new_string = tool_input.get("new_string") or ""
            if not _has_status_done(new_string):
                sys.exit(0)  # new_string does not introduce status:done -> allow
            # done transition detected in the edit fragment.
            # Check evidence in new_string (where the agent would add the evidence
            # line in the same edit), AND fall back to the on-disk file in case
            # evidence was written in a prior edit.
            if _has_evidence(new_string):
                sys.exit(0)  # evidence in the new fragment -> allow
            # Check on-disk file for pre-existing evidence.
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                    on_disk = fh.read()
                if _has_evidence(on_disk):
                    sys.exit(0)  # evidence already on disk -> allow
            except OSError:
                sys.exit(0)  # cannot read file -> fail open
            _deny(DENY_REASON)

        elif tool_name == "MultiEdit":
            # DGN-1009: MultiEdit shares file_path at top level but carries no
            # content/new_string; the payload is tool_input["edits"] =
            # [{old_string, new_string}, ...].  Before this branch existed the
            # tool reached the hook (matcher Edit|Write|MultiEdit) and the
            # script silently ignored it -- an evidence-free done flip via
            # MultiEdit was ALLOWed (measured control run, DGN-1009 14:44).
            edits = tool_input.get("edits")
            if not isinstance(edits, list):
                sys.exit(0)  # malformed payload -> fail open (file contract)
            new_strings = []
            for entry in edits:
                if not isinstance(entry, dict):
                    sys.exit(0)  # malformed entry -> fail open (file contract)
                ns = entry.get("new_string")
                if isinstance(ns, str):
                    new_strings.append(ns)
            if not any(_has_status_done(ns) for ns in new_strings):
                sys.exit(0)  # no entry introduces status:done -> allow
            # BATCH-LEVEL judgment, deliberately NOT per-entry: the evidence
            # line may ride a DIFFERENT entry of the same MultiEdit call than
            # the one flipping status (agents commonly batch the status flip
            # and the verify:/evidence: line as separate edits).  Judging each
            # entry in isolation would false-positive on that legitimate
            # pattern.  Deny only when NO new_string in the whole batch
            # carries evidence AND the on-disk file has none either.
            if any(_has_evidence(ns) for ns in new_strings):
                sys.exit(0)  # evidence elsewhere in the same batch -> allow
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                    on_disk = fh.read()
                if _has_evidence(on_disk):
                    sys.exit(0)  # evidence already on disk -> allow
            except OSError:
                sys.exit(0)  # cannot read file -> fail open
            _deny(DENY_REASON)

    except Exception:
        pass  # fail open on any unexpected error

    sys.exit(0)


if __name__ == "__main__":
    main()
