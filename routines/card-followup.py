#!/usr/bin/env python3
"""PostToolUse hook: enforce the status card after a RECORD succeeds.

Keyed on the RECORD CLI (lifekit.sh meal-add / workout-add), NOT on the skill,
so it fires even when the agent hand-rolls a raw Bash call instead of invoking
the diet-log / workout-log skill.

Fires only on a SUCCESSFUL record:
  - tool is Bash and its command runs `lifekit.sh meal-add` or `workout-add`
  - the tool did not error (exit 0), stderr carries no failure
  - stdout is well-formed for that record type:
      meal-add    ->  ^\\d+\\t.+\\t\\d        (id  name  kcal)
      workout-add ->  ^\\d+\\t.+             (id  type ...)
On a match it injects a HARD, high-salience additionalContext order telling the
model that its NEXT action, before answering anything else in this message, is
to render the status card into files/outbox/ and emit its `send_file::` line.

Does NOT fire on:
  - failed record calls (non-zero exit / stderr failure / malformed stdout)
  - usage/help calls
  - read-only calls (meal-find, meal-day, agg-day, agg-week, workout-find, etc.)

Dedupe: at most one card order per turn, tracked by a per-session sentinel file
keyed on session_id (removed at each SessionStart is not required; the sentinel
carries the session id so a new turn in the same session still dedupes only
within that turn -- see below).

Fail-open: any bad/unexpected stdin -> exit 0 with no output, mirroring the
token-gate.py contract. The hook must never break the agent.
"""
import json
import os
import re
import sys
import tempfile

# Well-formed record stdout (first line).
RE_MEAL = re.compile(r"^\d+\t.+\t\d")     # id \t name \t kcal
RE_WORKOUT = re.compile(r"^\d+\t.+")       # id \t type ...

# stderr substrings that signal a real failure (not the benign auto-register
# warning workout-add prints, e.g. "미등록 분류 자동 등록").
STDERR_FAIL_MARKERS = ("Traceback", "Error", "error:", "usage:", "Usage:")


def _extract_record_kind(command):
    """Return 'meal' or 'workout' if the command is a lifekit RECORD add call,
    else None. Read-only / mutate-other subcommands return None.
    """
    if "lifekit.sh" not in command and "lifekit.py" not in command:
        return None
    # meal-add / workout-add must appear as a token (not meal-find etc.).
    # Use word-ish boundaries: the subcommand is preceded by whitespace and
    # followed by whitespace (it always takes args) or end.
    if re.search(r"(?:^|\s)meal-add(?:\s|$)", command):
        return "meal"
    if re.search(r"(?:^|\s)workout-add(?:\s|$)", command):
        return "workout"
    return None


def _stdout_well_formed(kind, stdout):
    first_line = (stdout or "").lstrip("\n").split("\n", 1)[0]
    if kind == "meal":
        return bool(RE_MEAL.match(first_line))
    if kind == "workout":
        return bool(RE_WORKOUT.match(first_line))
    return False


def _stderr_ok(stderr):
    s = stderr or ""
    for marker in STDERR_FAIL_MARKERS:
        if marker in s:
            return False
    return True


def _dedupe_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")
    return os.path.join(tempfile.gettempdir(), "card-followup-%s.turn" % safe)


ORDER = (
    "A {kind} record just succeeded. Your NEXT action, before answering "
    "anything else in this message, is to render the status card and send it: "
    "run the diet-log card renderer (.claude/skills/diet-log/card.py) so it "
    "reads today's lifekit.db and outputs into files/outbox/, then emit its "
    "`send_file:: <absolute path>` line. This is MANDATORY and cannot be "
    "deferred to a later turn. After the card is emitted, continue and answer "
    "the rest of the user's message. If matplotlib is unavailable and the "
    "renderer exits with code 3, report the numbers as text instead."
)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open: never break the agent on bad input

    try:
        if data.get("hook_event_name") not in (None, "PostToolUse"):
            sys.exit(0)
        if data.get("tool_name") != "Bash":
            sys.exit(0)

        tool_input = data.get("tool_input") or {}
        command = tool_input.get("command") or ""
        kind = _extract_record_kind(command)
        if not kind:
            sys.exit(0)  # not a record add -> no injection

        resp = data.get("tool_response") or {}
        # PostToolUse only fires on success, but guard explicitly anyway.
        if resp.get("interrupted"):
            sys.exit(0)
        stdout = resp.get("stdout") or ""
        stderr = resp.get("stderr") or ""
        if not _stderr_ok(stderr):
            sys.exit(0)
        if not _stdout_well_formed(kind, stdout):
            sys.exit(0)

        # Dedupe: one card order per turn. A "turn" is a single hook process
        # lifetime is too short; we key on session_id + the model's current
        # tool_use batch is not exposed, so we dedupe within a short window by
        # writing a sentinel and only injecting when it is fresh-per-turn.
        # Practical rule: one injection per (session_id, prompt_uuid) if
        # available; else fall back to session_id. This collapses a compound
        # turn that records twice into a single card order.
        session_id = data.get("session_id") or ""
        turn_key = data.get("prompt_uuid") or data.get("uuid") or ""
        sentinel = _dedupe_path(session_id)
        already = ""
        try:
            with open(sentinel, "r") as fh:
                already = fh.read().strip()
        except OSError:
            already = ""
        # If we have a turn_key and it matches the last one, we already injected
        # for this turn -> skip. If no turn_key is available, dedupe on the
        # session for a short mtime window so back-to-back records in one turn
        # only order one card.
        if turn_key:
            if already == turn_key:
                sys.exit(0)
        else:
            try:
                import time
                if os.path.exists(sentinel):
                    age = time.time() - os.path.getmtime(sentinel)
                    if age <= 30:  # same-turn window
                        sys.exit(0)
            except OSError:
                pass
        try:
            with open(sentinel, "w") as fh:
                fh.write(turn_key)
        except OSError:
            pass

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": ORDER.format(kind=kind),
            }
        }))
        sys.exit(0)
    except Exception:
        sys.exit(0)  # fail open on any unexpected error


if __name__ == "__main__":
    main()
