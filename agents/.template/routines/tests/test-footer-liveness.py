#!/usr/bin/env python3
"""Tests for DGN-540: session-background-agent liveness fixes.

Covers the two confirmed blindness mechanisms:

  Case A -- resume-after-completion: a SendMessage-resumed agent was dropped
  from the dashboard because the harness returns a JSON tool_result body
  (not a fresh "Async agent launched" line) and the old _scan_transcript
  did not recognise it as a new launch.  After the fix _RESUME_RE detects
  the resume body and updates last_launch[id] past the completion index.

  Case B -- long-tool-call silence: LIVE_STALE_SECS=45 dropped a live agent
  whose jsonl had been silent for 3+ minutes during a long tool call.  After
  the fix LIVE_STALE_SECS=1200 (20 min) survives typical long calls while the
  transcript completion-event detection still guards against ghost agents.

Run:  /usr/bin/python3 routines/tests/test-footer-liveness.py
Exit: 0 all pass, 1 any fail.
"""

import importlib.util
import json
import os
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
FOOTER_PY = os.path.join(os.path.dirname(HERE), "status-footer.py")

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
# Mock session layout (mirrors test-footer-desc.py conventions)
# ---------------------------------------------------------------------------
AID = "b1111111111111111"   # primary agent under test
AID2 = "b2222222222222222"  # second agent for multi-agent tests

WORK = tempfile.mkdtemp(prefix="footer-liveness-test.")
ENC = "-Users-mock-proj-liveness"
SESS = "mock-sess-liveness-0001"
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
    """Write or update a subagent jsonl with mtime = now - age_secs."""
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
             "text": "Async agent launched successfully.\nagentId: %s (internal ID)" % aid}]}]}})


def task_notification_completed(aid):
    """type=user string-content completion notice (matches real harness format)."""
    return json.dumps({"type": "user", "message": {
        "content": (
            "<task-notification>"
            "<task-id>%s</task-id>"
            "<status>completed</status>"
            '<summary>Agent "%s" came to rest</summary>'
            "</task-notification>" % (aid, aid))}})


def sendmessage_resume_result(aid):
    """type=user tool_result returned by the harness on SendMessage resume.

    The JSON body is embedded as escaped text inside the outer transcript
    JSON, matching the live session pattern (verified line 414 of the
    DGN-540 evidence transcript):
      {"success":true,"message":"Agent \\"<id>\\" had no active task;
       resumed from transcript in the background..."}
    """
    inner = json.dumps({
        "success": True,
        "message": (
            'Agent "%s" had no active task; resumed from transcript in the '
            "background with your message." % aid
        )
    })
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text": inner}]}]}})


def write_transcript(lines):
    with open(TRANSCRIPT, "w") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
write_meta(AID, "DGN-540 resume test agent")
write_meta(AID2, "secondary agent")

print("\n=== status-footer.py DGN-540 liveness tests ===\n")

# ---------------------------------------------------------------------------
# Case A: resume-after-completion (DGN-540 Case A)
# ---------------------------------------------------------------------------

# --- A1: launch -> completion -> resume -> agent must appear active ----------
print("--- A1: launch->completion->resume -> active (DGN-540 Case A) ---")
touch_jsonl(AID, age_secs=60)  # fresh within 1200s, stale under old 45s
write_transcript([
    launch(AID),
    task_notification_completed(AID),
    sendmessage_resume_result(AID),
])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("agent is active after resume", len(descs) >= 1 and any(AID[:8] in d or "DGN-540" in d for d in descs))
print("  descs: %r" % descs)

# --- A2: verify last_launch updated past completion -------------------------
print("\n--- A2: _scan_transcript: last_launch > last_compl after resume ---")
ll, lc, _ = sf._scan_transcript(TRANSCRIPT)
check("last_launch[AID] exists", AID in ll)
check("last_compl[AID] exists", AID in lc)
if AID in ll and AID in lc:
    check("last_launch > last_compl (resume flipped state)",
          ll[AID] > lc[AID])

# --- A3: double-resume: last resume idx wins, still active ------------------
print("\n--- A3: launch->compl->resume->resume -> still active ---")
touch_jsonl(AID, age_secs=30)
write_transcript([
    launch(AID),
    task_notification_completed(AID),
    sendmessage_resume_result(AID),   # first resume
    sendmessage_resume_result(AID),   # second resume (e.g. redirect)
])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("agent still active after double resume", len(descs) >= 1)
ll, lc, _ = sf._scan_transcript(TRANSCRIPT)
if AID in ll and AID in lc:
    check("last_launch still > last_compl after double resume", ll[AID] > lc[AID])
print("  descs: %r" % descs)

# --- A4: completion after resume -> agent leaves list -----------------------
print("\n--- A4: launch->compl->resume->compl -> NOT active ---")
touch_jsonl(AID, age_secs=30)
write_transcript([
    launch(AID),
    task_notification_completed(AID),   # first completion
    sendmessage_resume_result(AID),      # resume
    task_notification_completed(AID),   # completion after resume
])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("agent leaves list after second completion",
      not any(AID[:8] in d or "DGN-540" in d for d in descs))
ll, lc, _ = sf._scan_transcript(TRANSCRIPT)
if AID in ll and AID in lc:
    check("last_compl > last_launch after second completion", lc[AID] > ll[AID])
print("  descs: %r" % descs)

# --- A5: resume for agent B must not relabel agent A -----------------------
print("\n--- A5: resume for agent B does not affect agent A ---")
touch_jsonl(AID, age_secs=30)
touch_jsonl(AID2, age_secs=30)
write_transcript([
    launch(AID),
    launch(AID2),
    task_notification_completed(AID2),
    sendmessage_resume_result(AID2),  # resume only B
])
descs = sf._collect_active_subagents(TRANSCRIPT)
ll, lc, _ = sf._scan_transcript(TRANSCRIPT)
check("AID still active (untouched)", AID in ll and AID not in lc)
check("AID2 active again after resume", AID2 in ll and ll.get(AID2, -1) > lc.get(AID2, -1))
print("  descs: %r" % descs)

# ---------------------------------------------------------------------------
# Case B: long-tool-call silence (DGN-540 Case B)
# ---------------------------------------------------------------------------

# --- B1: jsonl 3-min stale but launch-without-completion -> active ----------
print("\n--- B1: launch-without-completion, jsonl 3-min stale -> active (Case B) ---")
touch_jsonl(AID, age_secs=180)   # 3 min stale -- dropped by old 45s gate
write_transcript([launch(AID)])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("agent shown despite 3-min jsonl silence", len(descs) >= 1)
check("LIVE_STALE_SECS is >= 600", sf.LIVE_STALE_SECS >= 600)
print("  descs: %r" % descs)
print("  LIVE_STALE_SECS =", sf.LIVE_STALE_SECS)

# --- B2: jsonl very stale (beyond new ceiling) -> not active ---------------
print("\n--- B2: jsonl beyond ceiling -> not active ---")
ceiling = sf.LIVE_STALE_SECS
touch_jsonl(AID, age_secs=ceiling + 60)
write_transcript([launch(AID)])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("agent absent when jsonl exceeds LIVE_STALE_SECS ceiling", len(descs) == 0)
print("  descs: %r" % descs)

# --- B3: completed agent not rescued by high ceiling -----------------------
print("\n--- B3: completion notice still removes agent despite high ceiling ---")
touch_jsonl(AID, age_secs=10)   # fresh
write_transcript([
    launch(AID),
    task_notification_completed(AID),
])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("completed agent not shown even with fresh jsonl", len(descs) == 0)
print("  descs: %r" % descs)

# ---------------------------------------------------------------------------
# Sanity: plain launch without completion -> active (regression guard)
# ---------------------------------------------------------------------------
print("\n--- sanity: plain launch -> active (no regression) ---")
touch_jsonl(AID, age_secs=5)
write_transcript([launch(AID)])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("active on plain launch", len(descs) >= 1)
print("  descs: %r" % descs)

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
