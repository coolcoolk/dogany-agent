#!/usr/bin/env python3
"""PreToolUse guard: blocks hand-writes into engine-owned and CC shadow memory paths.

Denies Edit/Write/MultiEdit calls whose target path falls under any of:
  (a) the Claude Code shadow memory store:  .../.claude/projects/*/memory/
  (b) the Dogany engine memory store:       any path segment /memories/ belonging
      to a Dogany instance root (anchored on the /memories/ path component).
  (c) the Why net store:                    <bot data dir>/state/why-net/
      (own deny message; writer is routines/why-net.py only).  Any data dir
      name: DOGANY_BOT_DATA_DIR need not be .telegram_bot (grill-3 fix 11).
  (d) Claude Code session transcripts:      .../.claude/projects/**.jsonl
      (own deny message; the Why net owner-turn resolver reads them, so a
      hand-written transcript would be a forged owner turn -- v2.7 T1 grill
      fix 1).

All other paths pass through unconditionally.
Fails open on any error (bad input, missing field, exception): the tool pipeline
must never be blocked by a guard malfunction.
Mirrors the I/O contract of token-gate.py:
  - reads PreToolUse JSON payload from stdin
  - emits deny via hookSpecificOutput/permissionDecision JSON to stdout
  - always exits 0 (the decision field, not the exit code, signals deny)
"""
import json
import os
import re
import sys


# Anchored segment patterns (both sides of the segment boundary are required).
# Pattern (a): CC shadow store -- /.claude/projects/<slug>/memory/ (or /memory at end)
_RE_CC_SHADOW = re.compile(r"[/\\]\.claude[/\\]projects[/\\][^/\\]+[/\\]memory([/\\]|$)")
# Pattern (b): Dogany engine store -- /memories/ path segment (not just a substring)
_RE_ENGINE_MEM = re.compile(r"[/\\]memories([/\\]|$)")
# Pattern (c): Why net store -- <bot data dir>/state/why-net/ (v2.7 T1 spec 1).
# The data dir is $DOGANY_BOT_DATA_DIR or <root>/.telegram_bot; any name
# matches (grill-3 fix 11: a renamed data dir was unguarded).
# Gap on record: Bash redirection stays open (same as memories/); the
# nightly `why-net derive --audit` hash-chain check is its detector.
_RE_WHY_NET = re.compile(r"[/\\]state[/\\]why-net([/\\]|$)")
# Pattern (d): Claude Code transcripts -- .claude/projects/<dir>/...jsonl.
_RE_TRANSCRIPT = re.compile(r"[/\\]\.claude[/\\]projects[/\\].+\.jsonl$", re.IGNORECASE)

DENY_MSG = (
    "Engine-owned memory: do not hand-write. "
    "Route via memories/inbox.md append or the engine consolidation path (RULES). "
    "The CC shadow store (.claude/projects/*/memory/) and the Dogany memory-engine "
    "store (memories/) are both write-protected; only memory.py and the "
    "consolidation cron may write there."
)

WHY_NET_DENY_MSG = (
    "Why net store: do not hand-write. "
    "state/why-net/ is append-only and written only by routines/why-net.py "
    "(propose / confirm / attach / revise / pivot / retire / interpret)."
)

TRANSCRIPT_DENY_MSG = (
    "Claude Code session transcript: do not hand-write. "
    ".claude/projects/**.jsonl is written by the runtime only; the Why net "
    "owner-turn resolver reads it as the record of what the owner said."
)


def _is_transcript(path: str) -> bool:
    """Return True if path is a Claude Code session transcript."""
    return any(_RE_TRANSCRIPT.search(c) for c in (os.path.normpath(path), path))


def _is_why_net(path: str) -> bool:
    """Return True if path falls under the Why net store."""
    return any(_RE_WHY_NET.search(c) for c in (os.path.normpath(path), path))


def _is_blocked(path: str) -> bool:
    """Return True if path falls under a protected memory directory."""
    normalized = os.path.normpath(path)
    # Use the normalized path but also the original (normpath strips trailing sep)
    for candidate in (normalized, path):
        if _RE_CC_SHADOW.search(candidate):
            return True
        if _RE_ENGINE_MEM.search(candidate):
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


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open: bad input -> allow

    try:
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input") or {}

        # Defensive check: only run for write-type tools.
        if tool_name not in ("Edit", "Write", "MultiEdit"):
            sys.exit(0)

        # Extract file path. Edit and Write both use "file_path".
        # MultiEdit uses the same "file_path" field at the top level.
        file_path = tool_input.get("file_path") or ""
        if not isinstance(file_path, str) or not file_path.strip():
            sys.exit(0)  # no path -> allow (cannot determine target, fail open)

        if _is_why_net(file_path.strip()):
            _deny(WHY_NET_DENY_MSG)
        elif _is_transcript(file_path.strip()):
            _deny(TRANSCRIPT_DENY_MSG)
        elif _is_blocked(file_path.strip()):
            _deny(DENY_MSG)

    except Exception:
        pass  # fail open on any unexpected error

    sys.exit(0)


if __name__ == "__main__":
    main()
