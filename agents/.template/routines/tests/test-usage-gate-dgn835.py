#!/usr/bin/env python3
"""DGN-835 usage-gate over-blocking relief -- unit + E2E tests.

Function-level (module import, STATE_DIR redirected to a temp dir):
  - relief_check pass/deny matrix (5-condition predicate, 7d-only scope,
    opt-in DISPATCH_COST table: unregistered plans denied -- FATAL-1)
  - relief ledger: time-bucket accumulation, bucket rotation, herd cutoff,
    accumulation across distinct fetched_at (N1 keying)
  - exempt_dispatch allowlist matrix (subagent x model x cap)
  - sentinel integer-count semantics (touch=1, echo N=N, mtime preserved)
  - fetch cache additive key (old-shape cache read compat)

E2E (subprocess against a sandboxed copy of the gate + stub claude-usage.sh):
  - deny message anchored at the WORKSPACE .claude (not payload cwd)
  - exempt baseline-editor+sonnet passes silently over threshold
  - fable model voids the exemption
  - relief allow then ledger-driven deny on the same snapshot (herd control)
  - 8-way parallel herd on one snapshot: flock-atomic ledger grants exactly
    the cap-fitting number (MAJOR-B)
  - 8-way cache-MISS herd, distinct fetched_at per hook: time-bucket ledger
    key still bounds it to the cap-fitting number (N1 -- was 8/8 before)

Run:  /usr/bin/python3 routines/tests/test-usage-gate-dgn835.py
Exit: 0 all pass, 1 any fail.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
GATE_PY = os.path.join(ROUTINES, "usage-gate.py")

_spec = importlib.util.spec_from_file_location("usage_gate", GATE_PY)
ug = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ug)

PASS = 0
FAIL = 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        print("  PASS: %s" % desc)
        PASS += 1
    else:
        print("  FAIL: %s%s" % (desc, (" -- " + detail) if detail else ""))
        FAIL += 1


def iso_in(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def usage_snap(fh=5.0, sd=96.2, reset_in=3 * 3600, fetched_at=None):
    return {
        "five_hour_util": fh,
        "seven_day_util": sd,
        "seven_day_resets_at": iso_in(reset_in),
        "five_hour_resets_at": iso_in(1800),
        "fetched_at": fetched_at if fetched_at is not None else time.time(),
    }


# ---------------------------------------------------------------------------
# sandbox STATE_DIR
# ---------------------------------------------------------------------------
TMP = tempfile.mkdtemp(prefix="usage-gate-835-")
ug.STATE_DIR = os.path.join(TMP, ".claude")

THR_5H, THR_7D = ug.PLAN_THRESHOLDS["max_20x"]  # 92 / 96


def clear_state():
    shutil.rmtree(ug.STATE_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# relief_check matrix
# ---------------------------------------------------------------------------
print("== relief_check (DGN-835 A1) ==")

clear_state()
r = ug.relief_check("max_20x", THR_5H, THR_7D, 5.0, 96.2, usage_snap())
check("relief passes: sd=96.2, reset 3h, C=0.5 (96.7 <= 97)", bool(r))
check("relief reason names the relief", r is not None and "relief" in r.lower())

clear_state()
r = ug.relief_check("max_20x", THR_5H, THR_7D, 93.0, 96.2, usage_snap(fh=93.0))
check("no relief when 5h window hot (fh >= thr_5h)", r is None)

clear_state()
r = ug.relief_check("max_20x", THR_5H, THR_7D, 5.0, 95.0, usage_snap(sd=95.0))
check("no relief below 7d threshold (not in blocked band)", r is None)

clear_state()
r = ug.relief_check(
    "max_20x", THR_5H, THR_7D, 5.0, 96.2, usage_snap(reset_in=30 * 3600)
)
check("no relief when reset beyond 24h horizon", r is None)

clear_state()
snap = usage_snap()
snap["seven_day_resets_at"] = ""
r = ug.relief_check("max_20x", THR_5H, THR_7D, 5.0, 96.2, snap)
check("no relief without a parseable 7d reset time", r is None)

clear_state()
snap = usage_snap()
snap["seven_day_resets_at"] = iso_in(-60)  # reset already passed
r = ug.relief_check("max_20x", THR_5H, THR_7D, 5.0, 96.2, snap)
check("no relief when reset already passed (R <= 0)", r is None)

clear_state()
snap = usage_snap()
del snap["fetched_at"]
r = ug.relief_check("max_20x", THR_5H, THR_7D, 5.0, 96.2, snap)
check("no relief without fetched_at (cache-success condition)", r is None)

clear_state()
r = ug.relief_check("max_20x", THR_5H, THR_7D, 5.0, 96.8, usage_snap(sd=96.8))
check("no relief when projected position exceeds cap (96.8+0.5 > 97)", r is None)

# FATAL-1: relief is OPT-IN per calibrated plan. A plan absent from
# DISPATCH_COST is denied outright -- otherwise a small plan's [thr_7d, 97)
# band opens far past its own threshold (pro thr 88 -> 9pt of free grants).
check("DISPATCH_COST registers max_20x ONLY (owner decision 2026-08-12)",
      set(ug.DISPATCH_COST) == {"max_20x"},
      "got: %r" % (ug.DISPATCH_COST,))

clear_state()
t5, t7 = ug.PLAN_THRESHOLDS["pro"]  # 75 / 88
r = ug.relief_check("pro", t5, t7, 5.0, 90.0, usage_snap(sd=90.0))
check("unregistered plan (pro) denied even deep inside [thr,97) band", r is None)
check("unregistered-plan deny never touches the ledger",
      not os.path.exists(os.path.join(ug.STATE_DIR, ug.LEDGER_NAME)))

clear_state()
t5, t7 = ug.PLAN_THRESHOLDS["max_5x"]
r = ug.relief_check("max_5x", t5, t7, 5.0, 95.2, usage_snap(sd=95.2))
check("unregistered plan (max_5x) denied relief (opt-in table)", r is None)

clear_state()
t5, t7 = ug.PLAN_THRESHOLDS["free"]  # 60 / 78
r = ug.relief_check("free", t5, t7, 5.0, 80.0, usage_snap(sd=80.0))
check("unregistered plan (free) denied relief (opt-in table)", r is None)

# ---------------------------------------------------------------------------
# ledger: same-bucket accumulation + bucket rotation (DGN-835 N1 keying)
# ---------------------------------------------------------------------------
print("== relief ledger (herd control) ==")

# The ledger key is now a TIME BUCKET (floor(now / CACHE_TTL_SEC)), shared by
# every hook in the window regardless of its per-hook fetched_at. Pin the
# bucket so two back-to-back relief_check calls land in the same one.
_saved_epoch_key = ug._relief_epoch_key
BUCKET = [1000]
ug._relief_epoch_key = lambda now=None: BUCKET[0]

clear_state()
epoch = time.time()
# distinct fetched_at per call -> a fetched_at-keyed ledger would NOT
# accumulate; the time bucket makes it accumulate anyway.
r1 = ug.relief_check("max_20x", THR_5H, THR_7D, 5.0, 96.2, usage_snap(fetched_at=epoch))
r2 = ug.relief_check("max_20x", THR_5H, THR_7D, 5.0, 96.2, usage_snap(fetched_at=epoch + 7))
check("grant #1 passes in fresh bucket", bool(r1))
check(
    "grant #2 denied in SAME bucket even with a different fetched_at "
    "(96.2+0.5 eff, +0.5 > 97)",
    r2 is None,
    "got: %r" % (r2,),
)
check("ledger recorded one grant for the bucket", ug.ledger_count(BUCKET[0]) == 1)

# next time bucket resets the count
BUCKET[0] = 1001
r3 = ug.relief_check("max_20x", THR_5H, THR_7D, 5.0, 96.2, usage_snap())
check("new time bucket resets the ledger (grant passes again)", bool(r3))
check("old-bucket count no longer visible", ug.ledger_count(1000) == 0)

# corrupt ledger -> counts as 0, never raises
with open(os.path.join(ug.STATE_DIR, ug.LEDGER_NAME), "w") as f:
    f.write("{not json")
check("corrupt ledger reads as 0", ug.ledger_count(BUCKET[0]) == 0)

# _relief_epoch_key really is floor(now / CACHE_TTL_SEC)
check("epoch key = floor(now / CACHE_TTL_SEC)",
      _saved_epoch_key(now=901.0) == int(901.0 // ug.CACHE_TTL_SEC))
# base = start of a bucket; base and base+TTL-1 sit in the same bucket.
_base = 5 * ug.CACHE_TTL_SEC
check("two 'now's in the same TTL window share a bucket",
      _saved_epoch_key(now=_base) == _saved_epoch_key(now=_base + ug.CACHE_TTL_SEC - 1))
check("a 'now' one TTL later is a different bucket",
      _saved_epoch_key(now=_base) != _saved_epoch_key(now=_base + ug.CACHE_TTL_SEC))

ug._relief_epoch_key = _saved_epoch_key  # restore for the E2E section

# ---------------------------------------------------------------------------
# exempt_dispatch allowlist matrix
# ---------------------------------------------------------------------------
print("== exempt_dispatch (DGN-835 A2) ==")

ti = {"subagent_type": "baseline-editor", "model": "sonnet"}
check("baseline-editor + sonnet under cap -> exempt",
      ug.exempt_dispatch("Task", ti, 5.0, 98.0))
check("Agent tool also exempt",
      ug.exempt_dispatch("Agent", ti, 5.0, 98.0))
check("non-heavy tool never exempt",
      not ug.exempt_dispatch("Skill", ti, 5.0, 98.0))
check("fable model voids exemption",
      not ug.exempt_dispatch("Task", {"subagent_type": "baseline-editor", "model": "fable"}, 5.0, 98.0))
check("opus model voids exemption",
      not ug.exempt_dispatch("Task", {"subagent_type": "baseline-editor", "model": "opus"}, 5.0, 98.0))
check("missing model voids exemption",
      not ug.exempt_dispatch("Task", {"subagent_type": "baseline-editor"}, 5.0, 98.0))
check("unlisted subagent not exempt",
      not ug.exempt_dispatch("Task", {"subagent_type": "dogany-dev-junior", "model": "sonnet"}, 5.0, 98.0))
check("at/above CAP_EXEMPT (99) exemption voided",
      not ug.exempt_dispatch("Task", ti, 5.0, 99.0))
check("5h window at cap also voids",
      not ug.exempt_dispatch("Task", ti, 99.5, 50.0))
for sub in ("propagation-editor", "release-closer", "doctrine-reviewer"):
    check("allowlisted: %s" % sub,
          ug.exempt_dispatch("Task", {"subagent_type": sub, "model": "haiku"}, 5.0, 98.0))

# ---------------------------------------------------------------------------
# sentinel integer count
# ---------------------------------------------------------------------------
print("== sentinel count semantics ==")

clear_state()
os.makedirs(ug.STATE_DIR, exist_ok=True)
sentinel = os.path.join(ug.STATE_DIR, ug.SENTINEL_NAME)

# legacy: bare touch (empty file) = 1
open(sentinel, "w").close()
check("empty sentinel consumed once", ug.consume_fresh_sentinel())
check("empty sentinel removed after consume", not os.path.exists(sentinel))
check("no sentinel -> False", not ug.consume_fresh_sentinel())

# echo 3 > sentinel = 3 approvals
with open(sentinel, "w") as f:
    f.write("3\n")
orig_mtime = os.stat(sentinel).st_mtime
check("count=3: consume #1", ug.consume_fresh_sentinel())
check("count file persists after #1", os.path.exists(sentinel))
check("mtime preserved on rewrite (TTL window not extended)",
      abs(os.stat(sentinel).st_mtime - orig_mtime) < 1.0)
check("count=3: consume #2", ug.consume_fresh_sentinel())
check("count=3: consume #3", ug.consume_fresh_sentinel())
check("sentinel gone after 3 consumes", not os.path.exists(sentinel))
check("consume #4 -> False", not ug.consume_fresh_sentinel())

# garbage body treated as 1 (legacy tolerance)
with open(sentinel, "w") as f:
    f.write("banana")
check("garbage sentinel body treated as count=1", ug.consume_fresh_sentinel())
check("garbage sentinel removed", not os.path.exists(sentinel))

# stale sentinel (mtime beyond TTL) rejected
with open(sentinel, "w") as f:
    f.write("5")
old = time.time() - ug.SENTINEL_TTL_SEC - 5
os.utime(sentinel, (old, old))
check("stale sentinel rejected regardless of count", not ug.consume_fresh_sentinel())
os.remove(sentinel)

# ---------------------------------------------------------------------------
# cache shape: additive five_hour_resets_at (old cache read compat)
# ---------------------------------------------------------------------------
print("== cache additive key compat ==")

clear_state()
os.makedirs(ug.STATE_DIR, exist_ok=True)
old_shape = {
    "five_hour_util": 10.0,
    "seven_day_util": 20.0,
    "seven_day_resets_at": iso_in(3600),
    "fetched_at": time.time(),
}
cache = os.path.join(ug.STATE_DIR, ug.CACHE_NAME)
with open(cache, "w") as f:
    json.dump(old_shape, f)
got = ug.get_usage()
check("old-shape cache still readable", isinstance(got, dict))
check("old-shape cache: five_hour_resets_at defaults empty",
      (got or {}).get("five_hour_resets_at", "") == "")

# ---------------------------------------------------------------------------
# E2E: sandboxed workspace + stub claude-usage.sh, real subprocess
# ---------------------------------------------------------------------------
print("== E2E (sandboxed workspace) ==")

WS = os.path.join(TMP, "ws")
os.makedirs(os.path.join(WS, "routines"), exist_ok=True)
os.makedirs(os.path.join(WS, "config"), exist_ok=True)
shutil.copy(GATE_PY, os.path.join(WS, "routines", "usage-gate.py"))
# DGN-916: usage-gate.py imports the single conf parser from routines/lib/.
# Mirror that dependency into the sandbox; without it the guarded fallback
# returns "" for PLAN and read_plan degrades to DEFAULT_PLAN (pro), which
# is denied relief and the ledger assertions below dry-fail.
os.makedirs(os.path.join(WS, "routines", "lib"), exist_ok=True)
shutil.copy(os.path.join(ROUTINES, "lib", "conf_reader.py"),
            os.path.join(WS, "routines", "lib", "conf_reader.py"))
with open(os.path.join(WS, "config", "agent.conf"), "w") as f:
    f.write("PLAN=max_20x\n")

STUB_USAGE = os.path.join(WS, "routines", "claude-usage.sh")


def set_stub(fh, sd, sd_reset_in):
    body = json.dumps({
        "five_hour": {"utilization": fh, "resets_at": iso_in(1800)},
        "seven_day": {"utilization": sd, "resets_at": iso_in(sd_reset_in)},
    })
    with open(STUB_USAGE, "w") as f:
        f.write("#!/bin/bash\ncat <<'EOF'\n%s\nEOF\n" % body)
    os.chmod(STUB_USAGE, 0o755)
    # drop the cache so the new stub values are fetched
    try:
        os.remove(os.path.join(WS, ".claude", ug.CACHE_NAME))
    except OSError:
        pass
    try:
        os.remove(os.path.join(WS, ".claude", ug.LEDGER_NAME))
    except OSError:
        pass


PAYLOAD_CWD = os.path.join(TMP, "elsewhere")  # deliberately NOT the workspace
os.makedirs(PAYLOAD_CWD, exist_ok=True)


def run_gate(tool_name="Task", tool_input=None):
    payload = json.dumps({
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "cwd": PAYLOAD_CWD,
    })
    proc = subprocess.run(
        [sys.executable, os.path.join(WS, "routines", "usage-gate.py")],
        input=payload, capture_output=True, text=True, timeout=30,
    )
    return proc.returncode, proc.stdout.strip()


# deny: far reset, over 7d threshold, no exemption
set_stub(5, 98, 3 * 86400)
rc, out = run_gate("Task", {"description": "big", "model": "fable", "prompt": "x"})
check("E2E deny over threshold (far reset)", '"deny"' in out, out)
check("E2E deny sentinel path anchored at WORKSPACE .claude",
      os.path.join(WS, ".claude") in out, out)
check("E2E deny mentions echo-N fan-out approval", "echo N" in out, out)
check("E2E state landed in workspace, not payload cwd",
      os.path.isfile(os.path.join(WS, ".claude", ug.CACHE_NAME))
      and not os.path.exists(os.path.join(PAYLOAD_CWD, ".claude")))

# exempt: baseline-editor + sonnet, sd=98 (< 99)
set_stub(5, 98, 3 * 86400)
rc, out = run_gate("Task", {"subagent_type": "baseline-editor", "model": "sonnet", "prompt": "x"})
check("E2E exempt baseline-editor+sonnet passes silently", out == "", out)

# exemption void on fable -> deny
set_stub(5, 98, 3 * 86400)
rc, out = run_gate("Task", {"subagent_type": "baseline-editor", "model": "fable", "prompt": "x"})
check("E2E fable voids exemption -> deny", '"deny"' in out, out)

# relief: sd=96.2, reset in 3h -> allow; second call same snapshot -> deny
set_stub(5, 96.2, 3 * 3600)
rc, out = run_gate("Task", {"description": "big", "model": "fable", "prompt": "x"})
check("E2E relief allow near reset", '"allow"' in out and "relief" in out.lower(), out)
rc, out2 = run_gate("Task", {"description": "big2", "model": "fable", "prompt": "x"})
check("E2E second dispatch on same snapshot denied (ledger herd control)",
      '"deny"' in out2, out2)

# 5h hot excludes relief even near 7d reset
set_stub(93, 96.2, 3 * 3600)
rc, out = run_gate("Task", {"description": "big", "model": "fable", "prompt": "x"})
check("E2E 5h hot -> no relief, deny", '"deny"' in out, out)

# non-heavy passes through untouched
set_stub(99, 99, 3600)
rc, out = run_gate("Read", {"file_path": "/tmp/x"})
check("E2E non-heavy tool passes silently", out == "", out)

# ---------------------------------------------------------------------------
# E2E parallel herd: flock-atomic ledger (DGN-835 MAJOR-B)
# ---------------------------------------------------------------------------
print("== E2E parallel herd (ledger flock) ==")

# sd=96.0, C=0.5, cap 97 -> EXACTLY 2 grants fit (96.5, 97.0); the 3rd
# projects 97.5 > 97. Pre-warm the cache so every process reads the SAME
# snapshot, then fire 8 gates concurrently: without the flock-atomic
# read+bump several would observe n=0 and over-grant (the pre-fix defect).
set_stub(5, 96.0, 3 * 3600)
_cache_path = os.path.join(WS, ".claude", ug.CACHE_NAME)
os.makedirs(os.path.dirname(_cache_path), exist_ok=True)
with open(_cache_path, "w") as f:
    json.dump(usage_snap(fh=5.0, sd=96.0, reset_in=3 * 3600), f)

_herd_payload = json.dumps({
    "tool_name": "Task",
    "tool_input": {"description": "herd", "model": "fable", "prompt": "x"},
    "cwd": PAYLOAD_CWD,
})
_herd_procs = [
    subprocess.Popen(
        [sys.executable, os.path.join(WS, "routines", "usage-gate.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    for _ in range(8)
]
_herd_out = [p.communicate(_herd_payload, timeout=30)[0] for p in _herd_procs]
_allows = sum(1 for o in _herd_out if '"allow"' in o)
_denies = sum(1 for o in _herd_out if '"deny"' in o)
check("herd: exactly 2 of 8 parallel dispatches granted (ledger serializes)",
      _allows == 2, "allows=%d denies=%d" % (_allows, _denies))
check("herd: the other 6 denied", _denies == 6,
      "allows=%d denies=%d" % (_allows, _denies))
with open(os.path.join(WS, ".claude", ug.LEDGER_NAME)) as f:
    _ledger = json.load(f)
check("herd: ledger recorded exactly 2 grants", _ledger.get("count") == 2,
      "ledger=%r" % (_ledger,))

# --- N1 regression: cache-MISS herd, distinct fetched_at per hook ---
# The N1 defect: on a cache miss every parallel hook runs its own
# fetch_usage() and stamps a distinct fetched_at, so a fetched_at-keyed
# ledger gave all 8 n=0 -> 8/8 grant (100% punch-through). With the time
# bucket key they share one counter -> still exactly 2/8. Drop the cache
# and the ledger so all 8 hooks fetch fresh.
set_stub(5, 96.0, 3 * 3600)  # clears cache + ledger, keeps the stub warm
_herd2_procs = [
    subprocess.Popen(
        [sys.executable, os.path.join(WS, "routines", "usage-gate.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    for _ in range(8)
]
_herd2_out = [p.communicate(_herd_payload, timeout=30)[0] for p in _herd2_procs]
_allows2 = sum(1 for o in _herd2_out if '"allow"' in o)
_denies2 = sum(1 for o in _herd2_out if '"deny"' in o)
check("N1 herd (cache-miss, distinct fetched_at): exactly 2 of 8 granted",
      _allows2 == 2, "allows=%d denies=%d (was 8/8 before N1 fix)" % (_allows2, _denies2))
check("N1 herd: the other 6 denied", _denies2 == 6,
      "allows=%d denies=%d" % (_allows2, _denies2))
with open(os.path.join(WS, ".claude", ug.LEDGER_NAME)) as f:
    _ledger2 = json.load(f)
check("N1 herd: ledger key is a time bucket, count == 2",
      _ledger2.get("count") == 2 and "epoch_key" in _ledger2,
      "ledger=%r" % (_ledger2,))
# distinct fetched_at really did occur (cache written by fetch, not pre-seeded):
# the ledger must NOT be keyed on fetched_at anymore.
check("N1 herd: ledger no longer keyed on fetched_at",
      "fetched_at_epoch" not in _ledger2, "ledger=%r" % (_ledger2,))

shutil.rmtree(TMP, ignore_errors=True)

print("-------------------------------------------")
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
