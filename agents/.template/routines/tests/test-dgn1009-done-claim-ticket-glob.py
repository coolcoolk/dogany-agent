#!/usr/bin/env python3
"""Tier-1 tests for done-claim-gate.py TICKET_FILE_GLOB portability
(cross-instance ticket-naming convention -- DGN-1009 handoff pattern).

Verifies:
  G1: no config, no env -> default DGN-*.md behavior unchanged (a dated
      other-instance-style filename is NOT recognized as a ticket -> ALLOW passthrough
      even with status:done and no evidence).
  G2: env TICKET_FILE_GLOB=*.md -> a dated filename IS recognized -> DENY
      fires when evidence is missing.
  G3: config/agent.conf TICKET_FILE_GLOB=*.md (another-instance-style install,
      own temp fixture dir -- never touches a real instance directory) -> hook
      mode picks it up via the "cwd" field in the PreToolUse payload.
  G4: same config-file override, --scan mode via --repo.
  G5: false-positive guard -- widening to *.md must NOT flag a non-ticket
      worklog/*.md file that has no literal "status: done" text, even though
      it now matches the glob.

Run: python3 routines/test_done_claim_ticket_glob.py
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


def run_hook(payload, env=None):
    e = dict(os.environ)
    e.pop("TICKET_FILE_GLOB", None)  # clean slate unless test sets it below
    if env:
        e.update(env)
    r = subprocess.run([PY, GATE], input=json.dumps(payload),
                        capture_output=True, text=True, env=e, timeout=10)
    return r


def is_deny(stdout):
    if not stdout.strip():
        return False
    try:
        out = json.loads(stdout)
    except Exception:
        return False
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def wt(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --- G1: default (no config, no env) -- dated filename NOT recognized -----
with tempfile.TemporaryDirectory() as td:
    tick = os.path.join(td, "worklog", "2026-07-14-some-otherinst-slug.md")
    wt(tick, "status: done\n(no evidence)\n")
    payload = {"tool_name": "Write", "cwd": td,
               "tool_input": {"file_path": tick,
                               "content": "status: done\n(no evidence)\n"}}
    r = run_hook(payload)
    check("G1 exits 0", r.returncode == 0, r.stderr)
    check("G1 default glob does not catch dated filename -> ALLOW",
          not is_deny(r.stdout), r.stdout)

# --- G2: env override widens the glob -> dated filename now caught --------
with tempfile.TemporaryDirectory() as td:
    tick = os.path.join(td, "worklog", "2026-07-14-some-otherinst-slug.md")
    wt(tick, "status: wip\n")
    payload = {"tool_name": "Write", "cwd": td,
               "tool_input": {"file_path": tick,
                               "content": "status: done\n(no evidence)\n"}}
    r = run_hook(payload, env={"TICKET_FILE_GLOB": "*.md"})
    check("G2 exits 0", r.returncode == 0, r.stderr)
    check("G2 env-widened glob catches dated filename -> DENY",
          is_deny(r.stdout), r.stdout)

# --- G3: config/agent.conf override (own fixture dir, hook mode) ----------
with tempfile.TemporaryDirectory() as td:
    wt(os.path.join(td, "config", "agent.conf"),
       "SLUG=test-otherinst-fixture\nTICKET_FILE_GLOB=*.md\n")
    tick = os.path.join(td, "worklog", "2026-07-14-some-otherinst-slug.md")
    wt(tick, "status: wip\n")
    payload = {"tool_name": "Write", "cwd": td,
               "tool_input": {"file_path": tick,
                               "content": "status: done\n(no evidence)\n"}}
    r = run_hook(payload)
    check("G3 exits 0", r.returncode == 0, r.stderr)
    check("G3 config-file glob (via cwd) catches dated filename -> DENY",
          is_deny(r.stdout), r.stdout)

# --- G4: config/agent.conf override, --scan mode via --repo ---------------
with tempfile.TemporaryDirectory() as td:
    def git(args, cwd=td):
        subprocess.run(["git"] + args, cwd=cwd, check=True,
                        capture_output=True)

    git(["init", "-q"])
    git(["config", "user.email", "t@t"])
    git(["config", "user.name", "t"])
    wt(os.path.join(td, "config", "agent.conf"),
       "SLUG=test-otherinst-fixture\nTICKET_FILE_GLOB=*.md\n")
    tick = os.path.join(td, "worklog", "2026-07-14-some-otherinst-slug.md")
    wt(tick, "---\nstatus: wip\n---\nbody\n")
    git(["add", "-A"])
    git(["commit", "-q", "-m", "init"],)
    wt(tick, "---\nstatus: done\n---\nbody, no evidence\n")
    git(["add", "-A"])
    git(["commit", "-q", "-m", "flip done"])
    r = subprocess.run(
        [PY, GATE, "--scan", "--repo", td, "--since", "2026-01-01"],
        capture_output=True, text=True, timeout=10)
    check("G4 scan exits 1 (finding)", r.returncode == 1, r.stdout)
    check("G4 scan flags the dated filename",
          "2026-07-14-some-otherinst-slug.md" in r.stdout, r.stdout)

# --- G5: widened glob does not false-positive on non-ticket *.md content --
with tempfile.TemporaryDirectory() as td:
    wt(os.path.join(td, "config", "agent.conf"), "TICKET_FILE_GLOB=*.md\n")
    note = os.path.join(td, "worklog", "PICKUP-20260821-1800.md")
    wt(note, "just a pickup memo, no status field at all\n")
    payload = {"tool_name": "Write", "cwd": td,
               "tool_input": {"file_path": note,
                               "content": "just a pickup memo, "
                                          "no status field at all\n"}}
    r = run_hook(payload)
    check("G5 exits 0", r.returncode == 0, r.stderr)
    check("G5 non-ticket *.md without status:done text -> ALLOW (no false positive)",
          not is_deny(r.stdout), r.stdout)

print()
if FAILURES:
    print(f"FAILED: {FAILURES}")
    sys.exit(1)
print("All tests PASSED (G1-G5)")
