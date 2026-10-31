#!/usr/bin/env python3
"""PostToolUse hook: record a deliberate subagent stop at the moment it happens.

WHY THIS EXISTS
The workbench's live section answers two questions for the owner: what is
running, and what actually died.  A launch that never completes and whose
jsonl goes silent is reported as presumed-dead -- correct for a crash, wrong
for a stop the session itself ordered.  status-footer.py drops an agent from
that section as soon as a stop record exists for it (see its Rev 14 block),
but a record nobody writes changes nothing, so this hook is the writer.

WHO ACTUALLY STOPS A SUBAGENT
The stop is performed by the HARNESS, not by this repo: the runtime exposes a
stop tool that takes the agent id and terminates the background task.  Nothing
in this repo can intercept that call, and the runtime emits no terminal task
notification afterwards (measured: the stopped agent's id appears in the main
transcript at launch and at stop, and never again -- which is exactly why the
presumed-dead state was absorbing).  The one integration point the repo DOES
own is the PostToolUse hook the runtime fires after the tool returns: it runs
at stop time, in the stopping session, with the stopped id in hand.  That is a
write-time record, not a scanner/poller/sweeper looking for stops after the
fact (standing decision 2026-09-03).

CONTRACT
  stdin  : PostToolUse payload {tool_name, tool_input, tool_response, ...}
  effect : status-footer.py's stop record for tool_input.task_id
  stdout : nothing
  exit   : always 0 -- a hook must never break or delay the session

CONSERVATIVE DIRECTION (do not relax without re-reading this)
A record is written ONLY when the tool response confirms the stop succeeded.
Anything else -- wrong tool, missing id, unparsable payload, failed stop, a
response shape this does not recognise -- writes nothing, which leaves the
agent on the pre-existing live/presumed-dead classification.  That is the safe
failure direction: a missing record costs one stale alarm row, while a record
written for an agent that is still alive would SUPPRESS a real death alarm.

The response shape is harness-owned and therefore the one fragile input here.
Measured sample (the stop that produced this ticket), tool_response:
    {"message": "Successfully stopped task: <id> (<description>)",
     "task_id": "<id>", "task_type": "local_agent", "command": "<description>"}
Both the echoed id and the success wording are required; a future harness that
renames either degrades to "no record", never to a false stop.
"""
import importlib.util
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
FOOTER_PY = os.path.join(SCRIPT_DIR, "status-footer.py")

# The tool the harness uses to stop one background subagent.
STOP_TOOL = "TaskStop"

# Success marker in the harness response ("Successfully stopped task: ...").
SUCCESS_MARK = "successfully stopped"


def _response_text(resp):
    """Flatten a tool_response of unknown shape to one searchable string."""
    if isinstance(resp, str):
        return resp
    try:
        return json.dumps(resp, ensure_ascii=False)
    except Exception:
        return ""


def _stopped_id(data):
    """Return the agent id whose stop this payload CONFIRMS, else None."""
    if data.get("tool_name") != STOP_TOOL:
        return None
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    agent_id = tool_input.get("task_id")
    if not isinstance(agent_id, str) or not agent_id:
        return None
    text = _response_text(data.get("tool_response"))
    if agent_id not in text:
        return None            # response does not even name the id -> unknown
    if SUCCESS_MARK not in text.lower():
        return None            # not a confirmed stop -> leave the alarm alone
    return agent_id


def _load_recorder():
    """Import status-footer.py's stop-record writer.

    The writer is NOT reimplemented here: the record's path, id grammar and
    JSON shape have exactly one owner (status-footer.py), and a second copy
    of that contract is the classic way the two halves silently drift apart.
    A sibling that predates the writer simply has no such attribute, and this
    hook then does nothing -- the same fail-open direction as everything else
    in this file.
    """
    spec = importlib.util.spec_from_file_location("status_footer", FOOTER_PY)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "_record_agent_stop", None)


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return
        agent_id = _stopped_id(data)
        if not agent_id:
            return
        recorder = _load_recorder()
        if recorder is None:
            return
        # No reason text: the hook observes THAT the stop happened, never why.
        # The explicit --agent-stopped CLI is where a caller that knows the
        # reason states it; inventing one here would be fiction in the record.
        recorder(agent_id)
    except Exception:
        return                 # fail open: never break or delay a turn


if __name__ == "__main__":
    main()
    sys.exit(0)
