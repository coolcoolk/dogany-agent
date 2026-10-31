#!/usr/bin/env python3
"""PreToolUse gate on the in-session delegation path (DGN-1414).

WHY. Discipline already says detached dispatch (routines/dispatch-detached.sh)
is the DEFAULT and in-session Task/Agent is the EXCEPTION (short AND result
needed this turn) -- but the cheap path had no gate, so the cheap path got
chosen. Measured 2026-09-12: the same task was spawned in-session on sonnet
(nobody checked) and separately through dispatch-detached.sh, which REFUSED
sonnet on the coverage bottleneck axis. The in-session sibling died on the
owner's very next ordinary message -- one real work loss. Prose reminders do
not fix a rule nobody reads at the decision point; the fix is wired into the
tool call itself.

FIRES on tool names Agent and Task only. Any other tool name is a silent
pass-through (exit 0, no output) -- same silent-below-threshold shape as
usage-gate.py.

DENY unless BOTH hold:
  1. tool_input.model is present and non-empty after stripping whitespace.
     Missing/blank -- "unset forbidden" (rules/hot.framework.md Delegation) --
     no silent model inherit.
  2. The prompt text (tool_input.prompt) contains a line,
     anchored to the start of a line,
     matching `IN-SESSION: <reason>` where <reason> is at least 10 characters
     after stripping surrounding whitespace. This is the explicit declaration
     of why the detached path was not used.

No bypass: no environment-variable escape, no --force, no allowlist. Matches
the deny mechanism of routines/usage-gate.py (emit() at usage-gate.py:566-574)
and routines/skill-landing-gate.py (_deny_or_notice at skill-landing-gate.py:
100-118) -- hookSpecificOutput/permissionDecision JSON on stdout, exit 0 always
(the decision field, not the exit code, signals deny to the harness).

Fails open on any error reading/parsing stdin: a gate malfunction must never
break the agent (same contract as every sibling PreToolUse hook here).
"""
import json
import sys

GATED_TOOLS = {"Agent", "Task"}

MIN_REASON_LEN = 10
IN_SESSION_PREFIX = "IN-SESSION:"

# OWNER-COPY-PENDING: access-choice wording requires owner review before deployment.
DETACHED_HOWTO = (
    "routines/dispatch-detached.sh --task <prompt-file> --model "
    "<fable|opus|sonnet|haiku> --bottleneck <coverage|accuracy|judgment> "
    "--bottleneck-why \"<task evidence>\" "
    "--label <label> [--ticket DGN-NNN] (--read-only | --worktree <slug>)"
)

# OWNER-COPY-PENDING: live-write routing wording requires owner review.
# DGN-1458: live workspace writes (an instance dir, its database -- paths
# that are no git checkout) used to have no detached lane, which pushed them
# back in-session. They have one now; the deny text names it.
LIVE_WRITE_LANE = (
    "Live workspace writes (paths that are no git checkout) are NOT a reason "
    "to stay in-session: dispatch a read-only planner with --read-only "
    "--live-target <path> (it writes a plan, never the live path), then "
    "apply the plan with routines/dispatch-detached.sh --apply-plan <runid> "
    "(dry run) and --apply-plan <runid> --apply."
)

DEATH_FACT = (
    "An in-session subagent is bound to this session and dies on the "
    "owner's very next ordinary message (not /stop only) -- unavoidable by "
    "principle. Measured 2026-09-12: exactly this happened -- one real "
    "work loss."
)


def _reason_text(tool_input):
    prompt = tool_input.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def _model_missing(tool_input):
    model = tool_input.get("model")
    return not (isinstance(model, str) and model.strip())


def _in_session_declared(text):
    return any(
        line.startswith(IN_SESSION_PREFIX)
        and len(line[len(IN_SESSION_PREFIX):].strip()) >= MIN_REASON_LEN
        for line in text.splitlines()
    )


def emit(decision, reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open: never break the agent on bad input

    tool_name = data.get("tool_name", "")
    if tool_name not in GATED_TOOLS:
        sys.exit(0)  # not Agent/Task -- silent pass-through

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        sys.exit(0)

    if _model_missing(tool_input):
        emit("deny", (
            "In-session dispatch gate (DGN-1414): 'model' is missing or "
            "empty on this " + tool_name + " call. Unset model = silent "
            "inherit, which is forbidden (rules/hot.framework.md "
            "Delegation: 'unset forbidden -- state model + why, no silent "
            "inherit'). Set an explicit model, or better, use the detached "
            "path which forces this same declaration: " + DETACHED_HOWTO +
            ". " + DEATH_FACT
        ))
        sys.exit(0)

    text = _reason_text(tool_input)
    if not _in_session_declared(text):
        emit("deny", (
            "In-session dispatch gate (DGN-1414): no 'IN-SESSION: <reason>' "
            "declaration line found in the prompt (or the reason is under "
            + str(MIN_REASON_LEN) + " characters). Detached dispatch "
            "(routines/dispatch-detached.sh) is the DEFAULT; in-session "
            "Task/Agent is the EXCEPTION and requires stating why the "
            "detached path was not used -- add a line matching exactly "
            "'IN-SESSION: <at least " + str(MIN_REASON_LEN) + " characters "
            "of reason>' to the prompt. If this does not need to run "
            "in-session, dispatch it instead: " + DETACHED_HOWTO + ". " +
            LIVE_WRITE_LANE + " " + DEATH_FACT
        ))
        sys.exit(0)

    sys.exit(0)  # both conditions satisfied -- allow


if __name__ == "__main__":
    main()
