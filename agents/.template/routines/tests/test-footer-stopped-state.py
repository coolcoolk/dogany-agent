#!/usr/bin/env python3
# test-lane: fast
"""Tests for Rev 14: a deliberately stopped subagent leaves the live section.

Background (the defect this locks down): before Rev 14 a launched-but-never-
completed subagent had exactly two fates -- live, or presumed-dead -- and
presumed-dead was ABSORBING.  The only exit from the live section is a
completion event in the main transcript, and an agent killed on purpose never
emits one, so it sat on the owner's pinned board under "presumed dead"
indefinitely.  A0 below proves the absorbing property still holds for a real
silent death (that is the alarm, and it must NOT be weakened); every other
case proves a RECORDED stop is excluded from the board instead.

ASSERTIONS EXPLICITLY INVERTED (owner correction 2026-09-11): the first cut of
this suite asserted that a stopped agent stays VISIBLE for a 30-minute
retention window (A1/A2/A3 checked for a "stopped" label on the board).  The
owner ruled against showing it at all: a stop is something the session itself
ordered, so it is the session's own housekeeping, and the board is the OWNER's
screen -- it carries what is running and what actually died, nothing else.  A
retention window of 30 minutes or 3 is still noise with a timer on it.  Those
checks now assert ABSENCE, and A3 additionally pins that no retention constant
exists to tune.  Coverage is not reduced: what the stopped-visible checks used
to prove (reason passthrough, the record surviving) is now proven against the
record on disk, which is where it still lives -- the owner's VIEW changed, the
data did not.

Run:  /usr/bin/python3 routines/tests/test-footer-stopped-state.py
Exit: 0 all pass, 1 any fail.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
FOOTER_PY = os.path.join(os.path.dirname(HERE), "status-footer.py")

WORK = tempfile.mkdtemp(prefix="footer-stopped-test.")
STOPS = os.path.join(WORK, "stops")
# Set BEFORE the module is loaded so _stops_dir() never touches the real tree.
os.environ["DOGANY_SUBAGENT_STOPS_DIR"] = STOPS

_spec = importlib.util.spec_from_file_location("status_footer", FOOTER_PY)
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)

PASS = 0
FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        print("  PASS: %s" % desc)
        PASS += 1
    else:
        print("  FAIL: %s" % desc)
        FAIL += 1


# ---------------------------------------------------------------------------
# Mock session layout (mirrors test-footer-liveness.py conventions)
# ---------------------------------------------------------------------------
AID = "b1111111111111111"    # the agent that gets stopped
AID2 = "b2222222222222222"   # the agent that genuinely dies

ENC = "-Users-mock-proj-stopped"
SESS = "mock-sess-stopped-0001"
PROJ = os.path.join(WORK, ".claude", "projects", ENC)
SUB = os.path.join(PROJ, SESS, "subagents")
TRANSCRIPT = os.path.join(PROJ, SESS + ".jsonl")
os.makedirs(SUB, exist_ok=True)

REAL_HOME = os.environ.get("HOME")
os.environ["HOME"] = WORK


def write_meta(aid, description):
    with open(os.path.join(SUB, "agent-%s.meta.json" % aid), "w") as fh:
        json.dump({"agentType": "general-purpose",
                   "description": description,
                   "toolUseId": "toolu_x"}, fh)


def touch_jsonl(aid, age_secs=0):
    p = os.path.join(SUB, "agent-%s.jsonl" % aid)
    with open(p, "w") as fh:
        fh.write('{"type":"assistant","message":{"content":'
                 '[{"type":"text","text":"working"}]}}\n')
    t = time.time() - age_secs
    os.utime(p, (t, t))


def launch(aid):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [
            {"type": "text",
             "text": "Async agent launched successfully.\n"
                     "agentId: %s (internal ID)" % aid}]}]}})


def completion(aid):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [
            {"type": "text",
             "text": "<task-notification><task-id>%s</task-id>"
                     "<status>completed</status></task-notification>" % aid}]}]}})


def write_transcript(lines):
    with open(TRANSCRIPT, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def clear_stops():
    for name in os.listdir(STOPS) if os.path.isdir(STOPS) else []:
        os.remove(os.path.join(STOPS, name))


def age_stop(aid, age_secs):
    """Rewrite an existing stop record with ts = now - age_secs."""
    p = sf._stop_record_path(aid)
    with open(p) as fh:
        rec = json.load(fh)
    rec["ts"] = int(time.time() - age_secs)
    with open(p, "w") as fh:
        json.dump(rec, fh, ensure_ascii=False)


write_meta(AID, "Triage 13 failures")
write_meta(AID2, "Unrelated long job")

STALE = sf.LIVE_STALE_SECS + 60

# ---------------------------------------------------------------------------
# A0 -- REGRESSION: a genuine silent death is still presumed-dead, and stays
# ---------------------------------------------------------------------------
print("\n--- A0 regression: unrecorded silence -> presumed-dead, unweakened ---")
clear_stops()
touch_jsonl(AID2, age_secs=STALE)
write_transcript([launch(AID2)])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("genuine silent death still surfaces", len(descs) == 1)
check("genuine silent death still carries the presumed-dead label",
      any(sf.STALLED_LABEL in d for d in descs))
# The alarm must not be time-limited: absorbing is CORRECT for a real death.
touch_jsonl(AID2, age_secs=86400 * 30)
descs = sf._collect_active_subagents(TRANSCRIPT)
check("30-day-old genuine death still alarms (alarm not weakened)",
      len(descs) == 1 and sf.STALLED_LABEL in descs[0])
print("  descs: %r" % descs)

# ---------------------------------------------------------------------------
# A1 -- a stopped agent leaves the owner's view entirely
# EXPLICITLY INVERTED (owner correction 2026-09-11): used to assert the row
# was visible with a "stopped" label instead of the presumed-dead one.
# ---------------------------------------------------------------------------
print("\n--- A1: recorded stop -> absent from the live section ---")
clear_stops()
touch_jsonl(AID, age_secs=STALE)
write_transcript([launch(AID)])
check("stop record written by the stopper",
      sf._record_agent_stop(AID, "committed into a live repo"))
descs = sf._collect_active_subagents(TRANSCRIPT)
check("stopped agent does not appear in the live section at all", descs == [])
check("stopped agent is NOT presumed-dead",
      not any(sf.STALLED_LABEL in d for d in descs))
# The owner's VIEW loses the row; the record does NOT disappear -- it is the
# session's own bookkeeping, and the reason still round-trips through it.
rec = sf._stop_record(AID)
check("the stop record survives on disk", rec is not None)
check("stop reason is carried through the record",
      rec is not None and rec["reason"] == "committed into a live repo")
check("the record is a real file under the stops dir",
      os.path.isfile(sf._stop_record_path(AID)))

# ---------------------------------------------------------------------------
# A2 -- a stop with no reason behaves identically (nothing to render either way)
# EXPLICITLY INVERTED (owner correction 2026-09-11): used to assert a bare
# label rendered on the board when the stop carried no reason.
# ---------------------------------------------------------------------------
print("\n--- A2: stop with no reason -> also absent ---")
clear_stops()
sf._record_agent_stop(AID, "")
check("reasonless stop is absent too",
      sf._collect_active_subagents(TRANSCRIPT) == [])
rec = sf._stop_record(AID)
check("reasonless record still exists with an empty reason",
      rec is not None and rec["reason"] == "")

# ---------------------------------------------------------------------------
# A3 -- exclusion is immediate and permanent; there is NO retention window
# EXPLICITLY INVERTED (owner correction 2026-09-11): used to assert the row
# stayed visible for STOPPED_RETAIN_SECS and only then cleared.  The constant
# was deleted rather than zeroed -- a zero-valued knob still invites tuning,
# and this is a decision, not a setting -- so its ABSENCE is pinned here.
# ---------------------------------------------------------------------------
print("\n--- A3: no retention window exists ---")
check("no retention constant to tune",
      not hasattr(sf, "STOPPED_RETAIN_SECS"))
clear_stops()
sf._record_agent_stop(AID, "stopped on purpose")
check("absent the instant the record is written (0s old)",
      sf._collect_active_subagents(TRANSCRIPT) == [])
age_stop(AID, 1800 + 60)
check("still absent past the window that used to apply",
      sf._collect_active_subagents(TRANSCRIPT) == [])
age_stop(AID, 86400 * 30)
descs = sf._collect_active_subagents(TRANSCRIPT)
check("still absent 30 days later", descs == [])
check("and an old stop never decays back into presumed-dead",
      not any(sf.STALLED_LABEL in d for d in descs))

# ---------------------------------------------------------------------------
# A4 -- live wins over a stale stop record (stopped, then resumed)
# ---------------------------------------------------------------------------
print("\n--- A4: a resumed agent reverts to plain live ---")
clear_stops()
sf._record_agent_stop(AID, "stopped on purpose")
touch_jsonl(AID, age_secs=5)          # jsonl moving again -> proof of life
descs = sf._collect_active_subagents(TRANSCRIPT)
check("resumed agent shows as live", len(descs) == 1)
check("resumed agent carries the plain description, no suffix",
      descs == ["Triage 13 failures"])
check("resumed agent carries no presumed-dead label",
      not any(sf.STALLED_LABEL in d for d in descs))
print("  descs: %r" % descs)

# ---------------------------------------------------------------------------
# A5 -- a completed agent is unaffected by a stop record
# ---------------------------------------------------------------------------
print("\n--- A5: completion still outranks everything ---")
clear_stops()
sf._record_agent_stop(AID, "stopped on purpose")
touch_jsonl(AID, age_secs=STALE)
write_transcript([launch(AID), completion(AID)])
check("completed agent stays out of the live section",
      sf._collect_active_subagents(TRANSCRIPT) == [])

# ---------------------------------------------------------------------------
# A6 -- mixed board: the stop is removed, the real death is left alone
# EXPLICITLY INVERTED (owner correction 2026-09-11): used to assert both rows
# surfaced with distinguishable labels.  The stopped one is now simply gone,
# which is the whole point -- the alarm is not competing with housekeeping.
# ---------------------------------------------------------------------------
print("\n--- A6: only the genuine death is left on the board ---")
clear_stops()
touch_jsonl(AID, age_secs=STALE)
touch_jsonl(AID2, age_secs=STALE)
write_transcript([launch(AID), launch(AID2)])
sf._record_agent_stop(AID, "committed into a live repo")
descs = sf._collect_active_subagents(TRANSCRIPT)
check("exactly one row survives", len(descs) == 1)
check("the survivor is the genuine death, alarmed",
      "Unrelated long job" in descs[0] and sf.STALLED_LABEL in descs[0])
check("the stopped agent is nowhere on the board",
      not any("Triage 13 failures" in d for d in descs))
print("  descs: %r" % descs)

# ---------------------------------------------------------------------------
# A7 -- a broken stop record must not suppress the death alarm
# ---------------------------------------------------------------------------
print("\n--- A7: malformed stop record degrades to presumed-dead ---")
clear_stops()
write_transcript([launch(AID)])
os.makedirs(STOPS, exist_ok=True)
with open(os.path.join(STOPS, "%s.json" % AID), "w") as fh:
    fh.write("{ this is not json")
descs = sf._collect_active_subagents(TRANSCRIPT)
check("malformed record -> alarm still fires",
      len(descs) == 1 and sf.STALLED_LABEL in descs[0])
with open(os.path.join(STOPS, "%s.json" % AID), "w") as fh:
    json.dump({"reason": "no timestamp here"}, fh)
descs = sf._collect_active_subagents(TRANSCRIPT)
check("record with no timestamp -> alarm still fires",
      len(descs) == 1 and sf.STALLED_LABEL in descs[0])

# ---------------------------------------------------------------------------
# A8 -- id validation: no path traversal from a transcript- or argv-sourced id
# ---------------------------------------------------------------------------
print("\n--- A8: agent id validation ---")
check("traversal id rejected by the path builder",
      sf._stop_record_path("../../etc/passwd") is None)
check("empty id rejected", sf._stop_record_path("") is None)
check("non-hex id rejected", sf._stop_record_path("not-hex!") is None)
check("writer refuses a bad id", not sf._record_agent_stop("../x", "nope"))
check("reader returns None for a bad id", sf._stop_record("../x") is None)

# ---------------------------------------------------------------------------
# A9 -- the CLI is the stopper's entry point
# ---------------------------------------------------------------------------
print("\n--- A9: --agent-stopped CLI ---")
clear_stops()
env = dict(os.environ)
env["DOGANY_SUBAGENT_STOPS_DIR"] = STOPS
r = subprocess.run(["/usr/bin/python3", FOOTER_PY, "--agent-stopped", AID,
                    "--reason", "committed into a live repo"],
                   env=env, capture_output=True, text=True)
check("CLI exits 0 on a valid stop", r.returncode == 0)
rec = sf._stop_record(AID)
check("CLI wrote a readable record", rec is not None)
check("CLI persisted the reason",
      rec is not None and rec["reason"] == "committed into a live repo")
r = subprocess.run(["/usr/bin/python3", FOOTER_PY, "--agent-stopped", "bad!"],
                   env=env, capture_output=True, text=True)
check("CLI exits non-zero on an invalid id", r.returncode != 0)
r = subprocess.run(["/usr/bin/python3", FOOTER_PY, "--agent-stopped"],
                   env=env, capture_output=True, text=True)
check("CLI exits non-zero with no id", r.returncode != 0)

# ---------------------------------------------------------------------------
# A10 -- the stop state adds NO owner-facing copy at all
# EXPLICITLY INVERTED (owner correction 2026-09-11): used to assert a
# STOPPED_LABEL constant existed under a WORDING PENDING OWNER CONFIRMATION
# marker.  The owner removed the row instead of wording it, so the check now
# pins that no such label exists -- reintroducing one would silently put
# unapproved text on the owner's screen, which is exactly what the marker
# convention protects against.
# ---------------------------------------------------------------------------
print("\n--- A10: the stop state ships no owner-facing copy ---")
with open(FOOTER_PY, encoding="utf-8") as fh:
    src = fh.read()
check("no stopped label constant exists in the module",
      not hasattr(sf, "STOPPED_LABEL") and not hasattr(sf, "STOPPED_LABEL_FMT"))
check("no stopped label constant is declared in the source",
      "STOPPED_LABEL" not in src)

# ---------------------------------------------------------------------------
# A11 -- the WRITER is wired: the stop hook records at stop time
# ---------------------------------------------------------------------------
print("\n--- A11: subagent-stop PostToolUse hook ---")
HOOK_PY = os.path.join(os.path.dirname(HERE), "subagent-stop-record.py")
SETTINGS = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                        ".claude", "settings.json")


def hook_payload(tool_name, task_id, response):
    return json.dumps({"hook_event_name": "PostToolUse",
                       "tool_name": tool_name,
                       "tool_input": {"task_id": task_id},
                       "tool_response": response})


def run_hook(payload):
    return subprocess.run(["/usr/bin/python3", HOOK_PY], input=payload,
                          env=env, capture_output=True, text=True)


# Measured harness response shape for a successful stop (the stop that
# produced this ticket), replayed verbatim.
OK_RESPONSE = json.dumps({
    "message": "Successfully stopped task: %s (Triage 13 failures)" % AID,
    "task_id": AID, "task_type": "local_agent",
    "command": "Triage 13 failures"})

clear_stops()
write_transcript([launch(AID)])
touch_jsonl(AID, age_secs=STALE)
r = run_hook(hook_payload("TaskStop", AID, OK_RESPONSE))
check("hook exits 0", r.returncode == 0)
check("hook wrote the stop record", sf._stop_record(AID) is not None)
check("and the agent is off the board end-to-end",
      sf._collect_active_subagents(TRANSCRIPT) == [])

# Everything that is not a CONFIRMED stop must write nothing: a record for a
# live agent would suppress a real death alarm later.
clear_stops()
r = run_hook(hook_payload("Bash", AID, OK_RESPONSE))
check("another tool writes nothing",
      r.returncode == 0 and sf._stop_record(AID) is None)
r = run_hook(hook_payload("TaskStop", AID, "Failed to stop task: %s" % AID))
check("a failed stop writes nothing",
      r.returncode == 0 and sf._stop_record(AID) is None)
r = run_hook(hook_payload("TaskStop", AID, "Successfully stopped task: other"))
check("a response naming another id writes nothing",
      r.returncode == 0 and sf._stop_record(AID) is None)
# A traversal id that the response DOES confirm still has to die at the
# writer's id grammar -- the payload is harness input, never a path.
r = run_hook(hook_payload("TaskStop", "../x",
                          "Successfully stopped task: ../x"))
check("a confirmed but non-hex id writes nothing", r.returncode == 0)
check("no traversal file appeared",
      (not os.path.isdir(STOPS)) or os.listdir(STOPS) == [])
r = run_hook("{ not json at all")
check("garbage stdin fails open (exit 0, no record)",
      r.returncode == 0 and sf._stop_record(AID) is None)

# The hook is only a writer if the harness actually runs it: pin the wiring.
with open(SETTINGS, encoding="utf-8") as fh:
    settings = json.load(fh)
post = settings.get("hooks", {}).get("PostToolUse", [])
wired = [e for e in post
         if "TaskStop" in (e.get("matcher") or "")
         and any("subagent-stop-record.py" in (h.get("command") or "")
                 for h in e.get("hooks", []))]
check("hook is registered as a PostToolUse entry on the stop tool",
      len(wired) == 1)

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
os.environ["HOME"] = REAL_HOME if REAL_HOME is not None else ""
import shutil
shutil.rmtree(WORK, ignore_errors=True)

print("\n===========================")
print("Results: %d passed, %d failed" % (PASS, FAIL))
print("===========================\n")
raise SystemExit(1 if FAIL else 0)
