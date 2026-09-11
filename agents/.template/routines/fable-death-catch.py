#!/usr/bin/env python3
"""PostToolUse hook: catch a Fable heavy-dispatch death on the usage limit and
turn it into a Fable->Opus fallback (DGN-606 runtime-death path).

The PreToolUse usage-gate cannot predict a Fable-specific weekly-cap exhaustion:
account-wide 5h/7d utilization does not break out Fable's quota, so a Fable
dispatch can pass the gate and then die at RUNTIME on "You've hit your limit".

This hook observes the dispatch RESULT. When a Fable-requested Agent/Task
dispatch comes back carrying the usage-limit death signature, it:
  1. writes .claude/.fable-exhausted {"expiry": <7d reset iso>} so the
     usage-gate redirects every SUBSEQUENT Fable dispatch to Opus pre-flight
     (first death is the only death, until the window resets); and
  2. injects a directive telling the orchestrator to re-fire the identical
     dispatch now with model: opus.

Only fires on Fable-requested dispatches with the death signature. Everything
else passes through untouched. Fail-open on any error: never break the agent.

NOTE (coverage): this catches SYNCHRONOUS dispatch deaths (result in
tool_response) and any awaited-agent result routed through PostToolUse. A purely
backgrounded agent whose death arrives only as a later task-notification is not a
tool result here; that path is covered by dispatch-detached.sh's own exit-path
writer (write_fable_exhausted_marker, DGN-1158). The pre-flight redirect in
usage-gate.py is the mechanical guarantee once the flag exists.

Flag contract (single marker, one schema): JSON {"expiry": iso8601} at
<workspace root>/.claude/.fable-exhausted. Same file, same schema and same
expiry source as dispatch-detached.sh's write_fable_exhausted_marker(); readers
are usage-gate.py fable_exhausted() and dispatch-detached.sh fable_exhausted().
Never fork the contract.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HEAVY_TOOLS = {"Agent", "Task"}
FABLE_ALIASES = {"fable"}
FABLE_FLAG_NAME = ".fable-exhausted"
CACHE_NAME = ".usage-cache.json"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Workspace root anchor for the flag and the usage cache, matching
# usage-gate.py's STATE_DIR and dispatch-detached.sh's $WORKSPACE. NEVER
# derive these from the payload cwd: subagents and worktree shells report
# differing cwds and a cwd-derived path would split the marker across
# .claude dirs, silently un-wiring the readers (DGN-835 path anchor).
WORKSPACE_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
STATE_DIR = os.path.join(WORKSPACE_ROOT, ".claude")

# Strong, harness-specific death signature. Kept narrow so a subagent that
# merely discusses "limits" in its output never trips it.
#
# DGN-1350: must stay in LOCKSTEP with RE_FABLE_CREDITS_DEATH in the sibling
# writer routines/dispatch-detached.sh. The two drifted apart and each missed
# wordings the other caught: this one lacked "requires usage credits" (the
# older cap message), the bash one lacked "reached your ... limit" (the
# 2026-09-08 rewording, which killed two detached dispatches with no marker
# written). Union of every wording observed so far; the paired test asserts
# both writers accept the same corpus.
RE_DEATH = re.compile(
    r"hit your (?:usage )?limit"
    r"|usage limit reached"
    r"|weekly limit reached"
    r"|reached your .*limit"
    r"|requires usage credits",
    re.IGNORECASE,
)


def requested_model(tool_input):
    model = tool_input.get("model")
    if not isinstance(model, str):
        return ""
    return model.strip().lower()


def response_text(resp):
    """Flatten a tool_response of unknown shape to a searchable string."""
    if isinstance(resp, str):
        return resp
    try:
        return json.dumps(resp, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(resp)


def reset_expiry():
    """Expiry for the flag = the 7d window reset from the usage cache (the Fable
    cap resets on the same weekly boundary). Fallback: now + 3 days so the flag
    self-clears and Fable is re-probed even if the cache is missing."""
    cache = os.path.join(STATE_DIR, CACHE_NAME)
    try:
        with open(cache, encoding="utf-8") as f:
            resets = json.load(f).get("seven_day_resets_at")
        if resets:
            # validate it parses; if not, fall through to default
            datetime.fromisoformat(str(resets).replace("Z", "+00:00"))
            return str(resets)
    except (OSError, ValueError, AttributeError):
        pass
    return (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()


DIRECTIVE = (
    "Fable heavy dispatch died on the Fable weekly usage cap. Recorded "
    "fable-exhausted (resets at the 7d window boundary); the usage-gate will "
    "now redirect further Fable dispatches to Opus automatically until reset. "
    "Your NEXT action: re-fire the IDENTICAL dispatch now with model: opus "
    "(AGENT.md Fable->Opus fallback). Do not retry with Fable this window."
)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open

    try:
        if data.get("hook_event_name") not in (None, "PostToolUse"):
            sys.exit(0)
        if data.get("tool_name", "") not in HEAVY_TOOLS:
            sys.exit(0)
        tool_input = data.get("tool_input") or {}
        if requested_model(tool_input) not in FABLE_ALIASES:
            sys.exit(0)  # only Fable deaths get the Fable->Opus fallback

        text = response_text(data.get("tool_response"))
        if not RE_DEATH.search(text):
            sys.exit(0)  # not a usage-limit death -> nothing to do

        flag_path = os.path.join(STATE_DIR, FABLE_FLAG_NAME)
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = flag_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "expiry": reset_expiry(),
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "source": "fable-death-catch",
                }, f)
            os.replace(tmp, flag_path)
        except OSError:
            pass  # flag write failed; still inject the directive below

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": DIRECTIVE,
            }
        }))
        sys.exit(0)
    except Exception:
        sys.exit(0)  # fail open


if __name__ == "__main__":
    main()
