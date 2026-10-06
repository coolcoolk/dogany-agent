#!/usr/bin/env python3
"""Background-job owner protocol hooks (DGN-1691 phase 2).

A background Bash / Monitor / Workflow job launched by the owner session
follows the dispatched-sibling protocol: an owner-language display name, a
batched START push, an END push with time and elapsed, a workbench row.  The
mechanics live in routines/lib/bg_job_notice.py and on the dispatch rails
(routines/dispatch-detached.sh); this file is only the two hook entry points.

  --gate      PreToolUse (Bash|Monitor|Workflow).  A BACKGROUND launch whose
              `description` is not an owner-renderable name in the instance
              language is denied -- the dispatch rail's --display-name
              refusal, armed at the one point where the name can still be
              corrected.  Foreground Bash is never touched.
  --launched  PostToolUse (Bash|Monitor|Workflow|TaskStop).  The launch ack
              queues the START notice at once (the tool_response carries the
              task id: measured {"backgroundTaskId"} for Bash, auto-moved
              included, {"taskId"} for Monitor).  A confirmed TaskStop marks
              the job self-stopped so its END stays quiet.  The END itself is
              sent by the Stop hook (status-footer.py), where the task
              notification is visible.

WHY PostToolUse FOR START: it fires once, right after the harness acks the
launch, with the id in a structured field -- the notice goes out within the
dispatch join window of the launch.  The Stop hook runs only at turn end
(a launch early in a long turn would be announced minutes late, possibly
after it finished) and re-scans the whole transcript every time; it stays as
the catch-up path for a missed hook, deduplicated by the shared sent-state.

Owner session only (same guard as the workbench: status-footer.py
_is_owner_session); a subagent context (payload carries agent_id), a
dispatched worker, a cron session are all no-ops.  Always exit 0; the gate
speaks only through the PreToolUse permissionDecision JSON.
"""
import importlib.util
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))

import bg_job_notice  # noqa: E402

FOOTER_PY = os.path.join(SCRIPT_DIR, "status-footer.py")
GATE_SWITCH_PY = os.path.join(SCRIPT_DIR, "gate-switch.py")
GATE_SLUG = "bg-job-name"
STOP_TOOL = "TaskStop"
STOP_SUCCESS = "successfully stopped"


def _load(path, name):
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _is_owner(data):
    """The workbench's own ownership guard, never a second copy of it."""
    if data.get("agent_id"):
        return False
    footer = _load(FOOTER_PY, "status_footer")
    check = getattr(footer, "_is_owner_session", None)
    return bool(check and check(data.get("transcript_path") or ""))


def _deny(reason):
    """Deny, or the central switch's off-notice when switched off (DGN-1231);
    a broken switch never disarms the gate."""
    gs = _load(GATE_SWITCH_PY, "gate_switch")
    if gs is not None:
        try:
            if not gs.is_enabled(GATE_SLUG, os.getcwd()):
                print(json.dumps(gs.off_notice(GATE_SLUG, reason, os.getcwd()),
                                 ensure_ascii=False))
                return
        except Exception:
            pass
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))


def gate(data):
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    if tool not in bg_job_notice.BG_TOOLS or not isinstance(inp, dict):
        return
    if not bg_job_notice.is_background_launch(tool, inp):
        return
    if not _is_owner(data):
        return
    lang = bg_job_notice.owner_lang()
    problem = bg_job_notice.name_problem(bg_job_notice.launch_name(inp), lang)
    if problem is None:
        return
    _deny(
        "Background job name gate (DGN-1691): this background %s launch "
        "will be announced to the owner (start and end push, workbench row) "
        "under its `description`, and %s. Re-issue the same call with "
        "`description` written for the owner in the instance language "
        "(AGENT_LANG=%s): what the job does, in plain words -- no run ids, "
        "hashes, paths or exit codes. Same rule as dispatch --display-name."
        % (tool, problem, lang))


def launched(data):
    tool = data.get("tool_name", "")
    resp = data.get("tool_response")
    if tool == STOP_TOOL:
        inp = data.get("tool_input") or {}
        tid = inp.get("task_id") if isinstance(inp, dict) else None
        text = resp if isinstance(resp, str) else json.dumps(
            resp, ensure_ascii=False)
        if tid and tid in text and STOP_SUCCESS in text.lower() \
                and _is_owner(data):
            bg_job_notice.mark_self_stopped(tid)
        return
    if tool not in bg_job_notice.BG_TOOLS:
        return
    tid = bg_job_notice.launched_task_id(resp)
    if not tid or not _is_owner(data):
        return
    bg_job_notice.record_start(
        tid, tool, bg_job_notice.launch_name(data.get("tool_input")),
        bg_job_notice.launch_start_epoch(resp),
        slot=bg_job_notice.model_slot(tool, data.get("tool_input")))


def main(argv):
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return 0
        if "--gate" in argv:
            gate(data)
        elif "--launched" in argv:
            launched(data)
    except Exception:
        pass  # fail open: never break or delay a turn
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
