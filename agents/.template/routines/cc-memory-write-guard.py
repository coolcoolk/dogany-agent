#!/usr/bin/env python3
"""PreToolUse guard: blocks hand-writes into engine-owned and CC shadow memory paths.

Denies Edit/Write/MultiEdit calls whose target path falls under either:
  (a) the Claude Code shadow memory store:  .../.claude/projects/*/memory/
  (b) the Dogany engine memory store:       any path segment /memories/ belonging
      to a Dogany instance root (anchored on the /memories/ path component).

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

DENY_MSG = (
    "Engine-owned memory: do not hand-write. "
    "Route via memories/inbox.md append or the engine consolidation path (RULES). "
    "The CC shadow store (.claude/projects/*/memory/) and the Dogany memory-engine "
    "store (memories/) are both write-protected; only memory.py and the "
    "consolidation cron may write there."
)


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

        if _is_blocked(file_path.strip()):
            _deny(DENY_MSG)

    except Exception:
        pass  # fail open on any unexpected error

    sys.exit(0)


if __name__ == "__main__":
    main()
