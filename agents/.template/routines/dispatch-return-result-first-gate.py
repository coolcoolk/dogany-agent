#!/usr/bin/env python3
"""PreToolUse latch for dispatch-return turns (DGN-1687).

The bridge creates .telegram_bot/dispatch-return-context.json only for an
injected [dispatch-return] turn.  It changes ``visible`` only after its first
assistant text has been delivered to the owner.  Until then this gate permits
only narrow result-reading tools and denies work-capable or long-running
tools.  A normal owner turn has no context record and is untouched.

DGN-1715: the CLI evaluates this hook right after the model's text block,
without waiting for the bridge to push that text to the owner.  The bridge
marks ``delivering`` as soon as it holds the text; before denying, the gate
waits a short grace for that mark and then for the delivery verdict, so a
result line already on its way is never counted as missing.

DGN-1715 r2: a denial is recorded (lib/dispatch_return_forcing.py) and the
deny reason tells the model that a turn which cannot continue its follow-up
must END on a one-line proposal plus an [[OPTIONS]] marker, never a bare
"next conversation" promise; the Stop seat enforces it.  A later tool that
passes the open latch clears the record (the follow-up resumed).

DGN-1738: the context file is instance-wide.  The gate acts only when the
hook payload's session_id equals the record's session_id; a detached worker
(different session) or a payload without session_id is allowed untouched.
"""
import json
import os
import re
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
import dispatch_return_forcing as forcing  # noqa: E402

SLUG = "dispatch-return-result-first"
FILENAME = "dispatch-return-context.json"
MAX_AGE_SECONDS = 3600
READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "read_file", "search_files"}
POLL_SECONDS = 0.05


def _seconds(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


# DGN-1715: how long a would-be denial waits for the bridge.  GRACE covers
# the bridge reading the text block; DELIVERY covers the owner push itself
# and stays under the 10s hook timeout (a timed-out hook fails open).
GRACE_SECONDS = _seconds("DISPATCH_RETURN_GRACE_SECONDS", 1.5)
DELIVERY_WAIT_SECONDS = _seconds("DISPATCH_RETURN_DELIVERY_WAIT_SECONDS", 7.0)

DENY_REASON = (
    "Dispatch-return result-first gate (DGN-1687): this injected dispatch "
    "return has not yet sent an owner-visible result line. Send the concise "
    "result first; then follow-up work may begin. Only result-reading tools "
    "such as Read and `git show` are available until delivery succeeds. "
    + forcing.PROPOSAL_INSTRUCTION
)


def _load_switch():
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gate_switch_for_dispatch_return", os.path.join(SCRIPT_DIR, "gate-switch.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_context_path = forcing.context_path


def _latch_state(cwd):
    """Return None (open), "delivering" (push in flight) or "closed"."""
    try:
        with open(_context_path(cwd), encoding="utf-8") as fh:
            row = json.load(fh)
        if not isinstance(row, dict) or row.get("schema") != 1:
            return None
        if not _awaiting_row(row):
            return None
        return "delivering" if row.get("delivering") is True else "closed"
    except Exception:
        return None


def _awaiting_result(cwd):
    """Wait out an in-flight delivery; True only if the latch stays closed."""
    state = _latch_state(cwd)
    if state is None:
        return False
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        limit = DELIVERY_WAIT_SECONDS if state == "delivering" else GRACE_SECONDS
        if elapsed >= limit:
            return True
        time.sleep(POLL_SECONDS)
        state = _latch_state(cwd)
        if state is None:
            return False


def _awaiting_row(row):
    try:
        # DGN-1688: intentional self-cancel recovery is quiet and never
        # creates this context in the bridge.  Keep the exemption defensive
        # for a stale or externally-created context record as well.
        if row.get("cancel_by") == "self":
            return False
        if not isinstance(row.get("turn_id"), str) or not row["turn_id"]:
            return False
        if row.get("visible") is True:
            return False
        ts = row.get("ts")
        return isinstance(ts, (int, float)) and time.time() - ts <= MAX_AGE_SECONDS
    except Exception:
        return False


def _safe_bash(command):
    """Allow only simple git inspection commands needed to read the result."""
    if not isinstance(command, str) or any(x in command for x in ("\n", ";", "&&", "||", "|", ">", "<", "`", "$")):
        return False
    if "--output" in command:
        return False
    return bool(re.match(r"^\s*git\s+(?:show|status|diff|log)(?:\s|$)", command))


def _allowed(data):
    tool = data.get("tool_name")
    if tool in READ_ONLY_TOOLS:
        return True
    if tool == "Bash":
        inp = data.get("tool_input") or {}
        return _safe_bash(inp.get("command")) if isinstance(inp, dict) else False
    return False


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}), flush=True)


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return
        cwd = str(data.get("cwd") or os.getcwd())
        if _allowed(data):
            return
        # DGN-1738: the record is instance-wide; act only on the session
        # that owns the dispatch-return turn (detached workers pass).
        row = forcing.read_row(_context_path(cwd))
        if row is None or not forcing.owns_row(row, data):
            return
        if not _awaiting_result(cwd):
            # The latch is open (or there is none): a denied follow-up that
            # now runs is resumed work, not a stalled turn.
            row = forcing.read_row(_context_path(cwd))
            if row is not None:
                forcing.clear_denial(_context_path(cwd), row["turn_id"])
            return
        switch = _load_switch()
        if switch is not None and not switch.is_enabled(SLUG, cwd):
            print(json.dumps(switch.off_notice(SLUG, DENY_REASON, cwd)), flush=True)
            return
        row = forcing.read_row(_context_path(cwd))
        if row is not None:
            forcing.record_denial(_context_path(cwd), row["turn_id"])
        _deny(DENY_REASON)
    except Exception:
        return


if __name__ == "__main__":
    main()
