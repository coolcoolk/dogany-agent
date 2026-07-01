#!/usr/bin/env python3
"""PreToolUse backstop for token-heavy actions.

Blocks the deep-research skill and the Workflow tool unless a fresh
approval sentinel exists in <cwd>/.claude/.token-gate-approved.
Layer-2 enforcement; the constitution rule is the primary gate.
Mechanism-based detection only (no natural-language matching).
On approval the sentinel is consumed (one-shot). Fails open on any error.
"""
import json
import os
import sys
import time

SENTINEL_NAME = ".token-gate-approved"
SENTINEL_TTL_SEC = 600  # approval valid for 10 minutes
EXPENSIVE_SKILLS = {"deep-research"}
EXPENSIVE_TOOLS = {"Workflow"}


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open: never break the agent on bad input

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    skill = (
        tool_input.get("skill")
        or tool_input.get("name")
        or tool_input.get("skill_name")
        or ""
    )

    is_expensive = tool_name in EXPENSIVE_TOOLS or (
        tool_name == "Skill" and skill in EXPENSIVE_SKILLS
    )
    if not is_expensive:
        sys.exit(0)  # pass through

    sentinel = os.path.join(cwd, ".claude", SENTINEL_NAME)
    if os.path.exists(sentinel):
        age = time.time() - os.path.getmtime(sentinel)
        if age <= SENTINEL_TTL_SEC:
            try:
                os.remove(sentinel)  # one-shot consume
            except OSError:
                pass
            sys.exit(0)  # approved -> allow

    label = skill or tool_name
    reason = (
        "Token-heavy action '" + label + "' blocked by approval gate. "
        "STOP and ask the user for approval first (state your reasoning "
        "and warn about high token cost). After the user approves, run: "
        "touch '" + sentinel + "' then retry. If the user already "
        "explicitly requested this action, create the sentinel and retry."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
