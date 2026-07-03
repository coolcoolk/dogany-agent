#!/usr/bin/env python3
"""Skill-feedback gate: two-mode hook so skill feedback deterministically
enters a propose-edit loop.

Mode A  --record  (PostToolUse, matcher "Skill"): after a Skill runs, write a
  marker file <project_root>/.claude/.last-skill.json =
  {"skill": <name>, "ts": <epoch int>, "session_id": <id>, "injected": 0},
  overwritten each time. Meta skills (skill-creator, memory-search) are NOT
  recorded: the propose-edit procedure itself invokes them, and stamping them
  would overwrite the marker mid-feedback-loop with the wrong skill.

Mode B  --inject  (UserPromptSubmit): read the marker; if it is missing,
  corrupt, stale (older than the window, default 1800s, env override
  DOGANY_SKILL_FEEDBACK_WINDOW), from another session (cron / subagent
  poisoning guard), or already injected MAX_INJECTIONS times, exit silently
  with no output. Otherwise emit an additionalContext note (same JSON/stdout
  format as the memory recall hook) reminding the model to run the
  propose-edit loop IF the current message is feedback about that skill;
  semantic judgment stays with the model. Each injection increments the
  marker's counter so the note appears at most MAX_INJECTIONS times per
  skill run instead of on every message in the window.

Fail-open contract (mirrors token-gate.py / card-followup.py): ANY exception
exits 0 with no output. The hook must never block or materially delay a turn.
"""
import json
import os
import sys
import time

MARKER_REL = os.path.join(".claude", ".last-skill.json")
DEFAULT_WINDOW_SEC = 1800
MAX_INJECTIONS = 2
MAX_SKILL_NAME = 100

# Meta skills invoked BY the propose-edit procedure (or routinely before
# answering). Recording them would clobber the marker mid-loop (grill M2).
RECORD_DENYLIST = {
    "skill-creator",
    "dogany-skill-creator",
    "memory-search",
    "dogany-memory-search",
}


def _window_sec():
    try:
        v = int(os.environ.get("DOGANY_SKILL_FEEDBACK_WINDOW", ""))
        if v > 0:
            return v
    except Exception:
        pass
    return DEFAULT_WINDOW_SEC


def _marker_path(cwd):
    return os.path.join(cwd or os.getcwd(), MARKER_REL)


def _write_marker(path, payload):
    """Atomic write: temp file + os.replace, so readers never see a torn file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _record(data):
    """PostToolUse (matcher Skill): stamp the marker with skill name + epoch."""
    if data.get("hook_event_name") not in (None, "PostToolUse"):
        return
    if data.get("tool_name") != "Skill":
        return

    tool_input = data.get("tool_input") or {}
    skill = tool_input.get("skill") or ""
    if not isinstance(skill, str) or not skill.strip():
        return
    skill = skill.strip()[:MAX_SKILL_NAME]
    if skill in RECORD_DENYLIST:
        return

    cwd = data.get("cwd") or os.getcwd()
    path = _marker_path(cwd)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    payload = {
        "skill": skill,
        "ts": int(time.time()),
        "session_id": data.get("session_id") or "",
        "injected": 0,
    }
    _write_marker(path, payload)


NOTE = (
    "[skill-feedback gate] Skill '{skill}' ran {mins} min ago. If the user's "
    "current message is feedback on that skill's behavior (complaint, "
    "correction, improvement request): do NOT hot-patch or silently adjust; "
    "(1) propose a concrete skill edit and wait for approval; (2) read the "
    "skill-creator skill (dogany-skill-creator in product instances) before "
    "editing; (3) edit, verify, then log one line to memory. If it is a "
    "dogany-* framework skill OR a bundled lifekit skill (diet-log, "
    "workout-log, appointment-log, relationship, task-update), do not edit it "
    "in place: copy it under a new non-framework name, edit the copy, and "
    "note the improvement as an upstream product suggestion. If the root "
    "cause is in service/product code (bridge, runtime, memory engine, "
    "lifekit core), do not edit locally: say so and draft a product issue "
    "report. If this message is not feedback about the skill, ignore this "
    "note silently."
)


def _inject(data):
    """UserPromptSubmit: emit the note if the marker is fresh, else silent."""
    cwd = data.get("cwd") or os.getcwd()
    path = _marker_path(cwd)
    try:
        with open(path, "r") as fh:
            marker = json.load(fh)
    except Exception:
        return  # missing / corrupt -> silent

    if not isinstance(marker, dict):
        return
    skill = marker.get("skill")
    ts = marker.get("ts")
    if not isinstance(skill, str) or not skill.strip():
        return
    try:
        ts = int(ts)
    except Exception:
        return

    age = int(time.time()) - ts
    if age < 0 or age > _window_sec():
        return  # stale -> silent

    # Session guard (grill M3): only inject into the session that ran the
    # skill. Kills cron-headless and subagent marker poisoning.
    marker_sid = marker.get("session_id") or ""
    my_sid = data.get("session_id") or ""
    if not marker_sid or not my_sid or marker_sid != my_sid:
        return

    # Injection cap (grill M1): at most MAX_INJECTIONS notes per skill run.
    try:
        injected = int(marker.get("injected", 0))
    except Exception:
        injected = 0
    if injected >= MAX_INJECTIONS:
        return
    marker["injected"] = injected + 1
    _write_marker(path, marker)

    mins = max(0, age // 60)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": NOTE.format(
                skill=skill.strip()[:MAX_SKILL_NAME], mins=mins
            ),
        }
    }
    print(json.dumps(out), flush=True)


def main():
    try:
        mode = ""
        for arg in sys.argv[1:]:
            if arg in ("--record", "--inject"):
                mode = arg
        if not mode:
            sys.exit(0)  # no explicit mode -> no-op (wiring-typo guard)
        try:
            data = json.load(sys.stdin)
        except Exception:
            sys.exit(0)  # fail open: bad input -> silent no-op
        if not isinstance(data, dict):
            sys.exit(0)

        if mode == "--record":
            _record(data)
        else:
            _inject(data)
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException:
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)


if __name__ == "__main__":
    main()
