#!/usr/bin/env python3
"""Tier-1 tests for done-claim-gate.py --scan mode (DGN-1009 item 2 extension).

Cases (throwaway git repo in tmpdir):
  S1: done flip after --since with no evidence -> [NO-EVIDENCE]
  S2: done ticket WITH evidence marker -> clean
  S3: legacy done (flip before --since), later touched -> NOT flagged
  S4: hook mode unaffected by the extension (deny still fires, argv-free)

Run: python3 routines/test_done_claim_scan.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "..", "done-claim-gate.py")
PY = sys.executable

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"{name} PASS")
    else:
        print(f"{name} FAIL {detail}")
        FAILURES.append(name)


def run(cmd, cwd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    subprocess.run(cmd, cwd=cwd, env=e, check=True, capture_output=True)


def commit(cwd, msg, when):
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    run(["git", "add", "-A"], cwd)
    run(["git", "commit", "-q", "--allow-empty", "-m", msg], cwd, env)


def scan(cwd, since):
    r = subprocess.run([PY, GATE, "--scan", "--repo", cwd, "--since", since],
                       capture_output=True, text=True)
    return r.returncode, r.stdout


def wt(path, text):
    with open(path, "w") as fh:
        fh.write(text)


with tempfile.TemporaryDirectory() as td:
    run(["git", "init", "-q"], td)
    run(["git", "config", "user.email", "t@t"], td)
    run(["git", "config", "user.name", "t"], td)
    os.makedirs(os.path.join(td, "worklog"))
    p21 = os.path.join(td, "worklog", "DGN-21-a.md")
    p22 = os.path.join(td, "worklog", "DGN-22-b.md")
    p23 = os.path.join(td, "worklog", "DGN-23-c.md")

    wt(p21, "---\nid: DGN-21\nstatus: wip\n---\nbody\n")
    wt(p23, "---\nid: DGN-23\nstatus: done\n---\nlegacy, no evidence\n")
    commit(td, "init", "2026-08-01T10:00:00 +0900")

    # S1: flip DGN-21 to done, no evidence, after since
    wt(p21, "---\nid: DGN-21\nstatus: done\n---\nbody\n")
    commit(td, "flip 21 done", "2026-08-21T10:00:00 +0900")
    # S2: born-done with evidence, after since
    wt(p22, "---\nid: DGN-22\nstatus: done\n---\nverify: 5/5 tests pass\n")
    commit(td, "add 22", "2026-08-21T11:00:00 +0900")
    # S3: legacy done ticket touched after since (log append only)
    with open(p23, "a") as fh:
        fh.write("- log appended later\n")
    commit(td, "append 23 log", "2026-08-21T12:00:00 +0900")

    code, out = scan(td, "2026-08-20")
    check("S1", code == 1 and "DGN-21-a.md" in out, out)
    check("S2", "DGN-22-b.md" not in out, out)
    check("S3", "DGN-23-c.md" not in out, out)

# S4: hook mode regression -- deny still fires with no argv
with tempfile.TemporaryDirectory() as td:
    os.makedirs(os.path.join(td, "worklog"))
    tick = os.path.join(td, "worklog", "DGN-30-x.md")
    wt(tick, "---\nid: DGN-30\nstatus: wip\n---\nno evidence\n")
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": tick, "old_string": "status: wip",
        "new_string": "status: done"}})
    r = subprocess.run([PY, GATE], input=payload, capture_output=True, text=True)
    check("S4", r.returncode == 0 and '"deny"' in r.stdout, r.stdout)

print()
if FAILURES:
    print(f"FAILED: {FAILURES}")
    sys.exit(1)
print("All tests PASSED (S1-S4)")
