#!/usr/bin/env python3
"""Unit tests for the live-label resolution in routines/status-footer.py (DGN-211).

Focus: the description shown in the footer must follow a mid-flight redirect.
The meta.json "description" is frozen at spawn; a SendMessage-resume carries a
fresh 5-10 word `summary` recap.  The footer must prefer the latest post-launch
recap over the frozen description, and fall back to the description when no
recap exists.

These are function-level tests: they call _collect_active_subagents /
_resolve_description / _sendmessage_recap directly against a mock transcript,
so they do NOT depend on the Stop-hook ownership gate (main()).

Run:  /usr/bin/python3 routines/tests/test-footer-desc.py
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
# Mock session layout.  transcript_path drives both the liveness parse and the
# subagents-dir derivation (which uses expanduser("~")); override HOME so
# meta.json resolution lands in the temp tree.
# ---------------------------------------------------------------------------
AID = "a52066ec0968591c8"          # active agent under test
OTHER = "aaa0000111000000"         # a second, never-resumed agent

WORK = tempfile.mkdtemp(prefix="footer-desc-test.")
ENC = "-Users-mock-proj"
SESS = "mock-sess-0001"
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


def fresh_jsonl(aid):
    p = os.path.join(SUB, "agent-%s.jsonl" % aid)
    with open(p, "w") as fh:
        fh.write('{"type":"assistant","message":{"content":['
                 '{"type":"text","text":"working"}]}}\n')
    now = time.time()
    os.utime(p, (now, now))


def launch(aid):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [
            {"type": "text",
             "text": "Async agent launched successfully.\nagentId: %s (internal ID)" % aid}]}]}})


def sendmessage(aid, summary):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "SendMessage",
         "input": {"to": aid, "summary": summary,
                   "message": "long redirect body ..."}}]}})


def write_transcript(lines):
    with open(TRANSCRIPT, "w") as fh:
        fh.write("\n".join(lines) + "\n")


SPAWN_DESC = "productune v0.6 PM structure research"
REDIRECT = "Redirect: research main, not v0.6"

write_meta(AID, SPAWN_DESC)
write_meta(OTHER, "GCal grill")
fresh_jsonl(AID)
fresh_jsonl(OTHER)

print("\n=== status-footer.py DGN-211 label-resolution tests ===\n")

# --- scenario 1: mid-flight redirect -> label follows the SendMessage recap --
print("--- 1: launch then SendMessage-resume -> recap wins over frozen meta ---")
write_transcript([launch(AID), sendmessage(AID, REDIRECT)])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("agent is active", AID in [d for d in descs] or len(descs) == 1)
check("label is the resume recap, not the spawn description",
      descs == [REDIRECT])
check("frozen spawn description NOT shown", SPAWN_DESC not in descs)
print("  descs: %r" % descs)

# --- scenario 2: no resume -> falls back to frozen meta description ----------
print("\n--- 2: launch only, no resume -> meta description (no regression) ---")
write_transcript([launch(AID)])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("label falls back to spawn description", descs == [SPAWN_DESC])
print("  descs: %r" % descs)

# --- scenario 3: recap predates a later relaunch -> ignored ------------------
print("\n--- 3: SendMessage then a newer launch -> stale recap ignored ---")
write_transcript([launch(AID), sendmessage(AID, REDIRECT), launch(AID)])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("recap older than current launch is ignored", descs == [SPAWN_DESC])
print("  descs: %r" % descs)

# --- scenario 4: recap targets a DIFFERENT agent -> no cross-contamination ---
print("\n--- 4: recap for agent B must not relabel agent A ---")
write_transcript([launch(AID), launch(OTHER), sendmessage(OTHER, REDIRECT)])
descs = sf._collect_active_subagents(TRANSCRIPT)
check("both agents active", len(descs) == 2)
check("agent A keeps its spawn description", SPAWN_DESC in descs)
check("agent B carries the recap", REDIRECT in descs)
print("  descs: %r" % descs)

# --- scenario 5: _sendmessage_recap parsing -------------------------------
print("\n--- 5: _sendmessage_recap extracts (to, summary) ---")
entry = json.loads(sendmessage(AID, REDIRECT))
to_id, summary = sf._sendmessage_recap(entry)
check("to id parsed", to_id == AID)
check("summary parsed", summary == REDIRECT)
to_id2, _ = sf._sendmessage_recap(json.loads(launch(AID)))
check("non-SendMessage entry -> (None, None)", to_id2 is None)

# --- scenario 6: _resolve_description precedence rules --------------------
print("\n--- 6: _resolve_description precedence ---")
check("post-launch recap wins",
      sf._resolve_description(AID, 1, (2, REDIRECT), SUB) == REDIRECT)
check("pre-launch recap ignored -> meta",
      sf._resolve_description(AID, 5, (2, REDIRECT), SUB) == SPAWN_DESC)
check("no recap -> meta",
      sf._resolve_description(AID, 1, None, SUB) == SPAWN_DESC)
check("blank recap -> meta",
      sf._resolve_description(AID, 1, (2, "   "), SUB) == SPAWN_DESC)
check("no meta, no recap -> short id",
      sf._resolve_description("deadbeef00", 1, None, None) == "deadbeef")

# --- scenario 7: _collect_ledger_running (DGN-540) ----------------------
print("\n--- 7: _collect_ledger_running parses ledger running rows ---")
import shutil as _shutil

LEDGER_WORK = tempfile.mkdtemp(prefix="footer-ledger-test.")
PRODUCT_DIR = os.path.join(LEDGER_WORK, "product")
os.makedirs(PRODUCT_DIR)
LEDGER_PATH = os.path.join(PRODUCT_DIR, "auto-loop-ledger.md")

ORIG_ROOT = sf._ROOT_DIR
sf._ROOT_DIR = LEDGER_WORK

# happy-path: one running row, one done row
with open(LEDGER_PATH, "w") as fh:
    fh.write("# header comment\n")
    fh.write("| branch | item | state | attempts | backoff_until | last_ts | flags | note |\n")
    fh.write("| auto/dgn-540 | DGN-540 fix live blind | running | 1 | - | 2026-07-24 | propagation-followup | |\n")
    fh.write("| auto/dgn-539 | DGN-539 inbox wave | done | 2 | - | 2026-07-23 | | |\n")
results = sf._collect_ledger_running()
check("running row returned", results == ["junior: DGN-540 fix live blind"])
check("done row excluded", "junior: DGN-539 inbox wave" not in results)

# item missing -> falls back to branch
with open(LEDGER_PATH, "w") as fh:
    fh.write("| auto/dgn-541 | - | running | 1 | - | 2026-07-24 | | |\n")
results = sf._collect_ledger_running()
check("item '-' -> branch fallback", results == ["junior: auto/dgn-541"])

# header separator row skipped (too few parts after split)
with open(LEDGER_PATH, "w") as fh:
    fh.write("| --- | --- | --- |\n")
    fh.write("| auto/dgn-542 | DGN-542 task | running | 1 | - | 2026-07-24 | | |\n")
results = sf._collect_ledger_running()
check("separator row skipped", len(results) == 1)

# missing ledger file -> fail-open (empty list)
os.remove(LEDGER_PATH)
results = sf._collect_ledger_running()
check("missing ledger -> empty list (fail-open)", results == [])

sf._ROOT_DIR = ORIG_ROOT
_shutil.rmtree(LEDGER_WORK, ignore_errors=True)

# ---------------------------------------------------------------------------
os.environ["HOME"] = REAL_HOME if REAL_HOME is not None else ""
import shutil
shutil.rmtree(WORK, ignore_errors=True)

print("\n===========================")
print("Results: %d passed, %d failed" % (PASS, FAIL))
print("===========================\n")
raise SystemExit(1 if FAIL else 0)
