#!/usr/bin/env python3
"""PreToolUse write-gate: blocks locked/spec artifacts from landing in files/tmp/.

Mechanism (DGN-935 -- "재편: 사전 게이트 중심"):
  Trigger: Write or Edit tool call.
  Condition: file_path is under a files/tmp/ directory AND basename matches a
             locked/spec signature glob (case-insensitive).
  Action on match: DENY with re-routing instruction.
  All other paths, and pure-scratch tmp writes, pass through unconditionally.

Signature globs (basename only, case-insensitive):
  *locked*  *-LOCKED*  *spec*  *contract*  *doctrine*

Fail-open on any error: a gate malfunction must never block the agent.

I/O contract mirrors cc-memory-write-guard.py (the harness memory-write-block hook):
  - reads PreToolUse JSON payload from stdin
  - emits deny via hookSpecificOutput/permissionDecision JSON to stdout
  - always exits 0 (permissionDecision field signals deny, not the exit code)
"""
import fnmatch
import json
import os
import sys

# Basename glob patterns that indicate a locked / canonical artifact.
# Case-insensitive match (lowercased before compare).
_SIGNATURE_GLOBS = (
    "*locked*",
    "*-locked*",
    "*spec*",
    "*contract*",
    "*doctrine*",
)

DENY_REASON = (
    "정본은 worklog/sot로 저장하세요. tmp는 스크래치 전용입니다(정본 유실 방지, DGN-935)."
)

# Only intercept these tools (same set as cc-memory-write-guard.py).
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}


def _is_under_tmp(path: str) -> bool:
    """Return True if the normalized path contains a files/tmp/ segment."""
    # Normalize once; match against path component boundaries to avoid false
    # positives like myfiles/tmpdir/ (must be 'files/tmp' as consecutive segments).
    normalized = os.path.normpath(path)
    # Walk all segment combinations: look for .../files/tmp/...
    parts = normalized.replace("\\", "/").split("/")
    for i in range(len(parts) - 1):
        if parts[i] == "files" and parts[i + 1] == "tmp":
            return True
    return False


def _matches_signature(basename: str) -> bool:
    """Return True if basename matches any locked/spec signature glob."""
    lower = basename.lower()
    for pattern in _SIGNATURE_GLOBS:
        if fnmatch.fnmatch(lower, pattern):
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


# --- central switch (DGN-1231) ---------------------------------------------

_GATE_SLUG = "tmp-artifact"
_GATE_SWITCH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "gate-switch.py")


def _load_gate_switch():
    """Central gate on/off switch (DGN-1231).  None -> switch machinery
    absent/broken: the gate stays armed (fail toward protection)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gate_switch", _GATE_SWITCH_PATH)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _deny_or_notice(reason: str, cwd=None) -> None:
    """Consulted only at the would-deny moment: switched off -> emit a
    non-blocking off-notice instead of the deny (DGN-1231 visibility)."""
    gs = _load_gate_switch()
    if gs is not None:
        try:
            if not gs.is_enabled(_GATE_SLUG, cwd):
                print(json.dumps(gs.off_notice(_GATE_SLUG, reason, cwd),
                                 ensure_ascii=False))
                return
        except Exception:
            pass  # broken switch never disarms the gate
    _deny(reason)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open: bad / missing input never blocks

    try:
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input") or {}

        if tool_name not in _WRITE_TOOLS:
            sys.exit(0)  # not a write tool -- pass through

        # Both Edit and Write (and MultiEdit) use "file_path" at the top level.
        file_path = tool_input.get("file_path") or ""
        if not isinstance(file_path, str) or not file_path.strip():
            sys.exit(0)  # no usable path -> fail open

        file_path = file_path.strip()

        # Gate: only deny on positive (tmp + signature) match.
        if _is_under_tmp(file_path) and _matches_signature(os.path.basename(file_path)):
            _deny_or_notice(DENY_REASON, data.get("cwd"))

    except Exception:
        pass  # fail open on any unexpected error

    sys.exit(0)


if __name__ == "__main__":
    main()
