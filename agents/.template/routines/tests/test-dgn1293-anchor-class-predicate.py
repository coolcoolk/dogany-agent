#!/usr/bin/env python3
# test-lane: fast    # measured 0.2s (2026-09-05, DGN-1293)
"""Tests for anchor_crossref_lint.py's anchor predicate (DGN-1293).

DGN-1196 locked `class: anchor` frontmatter as the canonical anchor
predicate (same source scope_calc.py uses). anchor_crossref_lint.py used to
gate on a filename-contains-"anchor" heuristic instead, which silently
dropped real anchors whose filename didn't happen to say "anchor" (observed:
DGN-1276). This also verifies the zero-active-anchors-is-a-scan-failure
discipline: anchors_active=0 must never print a clean SUMMARY -- it exits 2
with a loud SCAN-FAIL line, same as any other structural scan failure.

Verifies:
  A1: a ticket whose filename has no "anchor" substring but whose
      frontmatter carries `class: anchor` + an active status IS
      counted as an active anchor.
  A2: the old filename-only heuristic is gone -- a ticket with "anchor" in
      its filename but no `class: anchor` frontmatter is NOT counted as an
      anchor (would have been a false hit before class-comparison mattered
      to callers; guards against re-introducing a filename shortcut).
  A3: zero active anchors -> exit 2 + a "SCAN-FAIL:" line (never a clean
      "SUMMARY ... anchors_active=0 drift=0" exit-0 report).
  A4: a real anchor present and active -> exit 0 (or 1 on genuine drift),
      SUMMARY line printed, anchors_active reflects it -- the non-empty
      control case for A3.

DGN-1294 (active axis moved from release_state: to status:) adds:
  A5: `status: open` + `class: anchor`, no release_state: field at all ->
      still counted as an active anchor (status: is the axis now).
  A6: `status: dismissed` (terminal) + `release_state: in-progress` (would
      have been active under the retired axis) -> NOT active. Proves
      release_state: is no longer read.

Run: python3 routines/tests/test-dgn1293-anchor-class-predicate.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LINT = os.path.join(HERE, "..", "anchor_crossref_lint.py")
PY = sys.executable

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"{name} PASS")
    else:
        print(f"{name} FAIL {detail}")
        FAILURES.append(name)


def wt(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def run(worklog_dir):
    env = dict(os.environ)
    env.pop("TICKET_PREFIX", None)
    env.pop("TICKET_FILE_GLOB", None)
    env.pop("DOGANY_AGENT_CONF", None)
    return subprocess.run([PY, LINT, worklog_dir], capture_output=True,
                          text=True, env=env, timeout=10)


# --- A1: class:anchor without "anchor" in the filename is counted ----------
with tempfile.TemporaryDirectory() as td:
    wt(os.path.join(td, "DGN-1276-oss-public-build-extraction.md"),
       "---\nid: DGN-1276\nstatus: open\nclass: anchor\n"
       "release: v9.0\nrelease_state: in-progress\n"
       "release_tickets: DGN-2001\n---\nbody\n")
    wt(os.path.join(td, "DGN-2001-child-ticket.md"),
       "---\nid: DGN-2001\nstatus: open\nrelease: v9.0\n---\nbody\n")
    r = run(td)
    check("A1 exits clean (0)", r.returncode == 0, r.stdout + r.stderr)
    check("A1 counts the no-'anchor'-filename ticket as an active anchor",
          "active anchors: 1" in r.stdout, r.stdout)
    check("A1 SUMMARY shows anchors_active=1",
          "anchors_active=1" in r.stdout, r.stdout)

# --- A2: "anchor" in filename but no class:anchor -> NOT an anchor --------
with tempfile.TemporaryDirectory() as td:
    wt(os.path.join(td, "DGN-3001-some-anchor-shaped-title.md"),
       "---\nid: DGN-3001\nstatus: open\nrelease_state: in-progress\n"
       "release: v9.0\n---\nbody\n")
    r = run(td)
    check("A2 exits 2 (no class:anchor tickets at all -> SCAN-FAIL)",
          r.returncode == 2, r.stdout + r.stderr)
    check("A2 SCAN-FAIL line present",
          "SCAN-FAIL:" in r.stdout, r.stdout)
    check("A2 never claims the filename-only hit as an active anchor",
          "active anchors: 1" not in r.stdout, r.stdout)

# --- A3: zero active anchors -> exit 2 + SCAN-FAIL, never a clean SUMMARY -
with tempfile.TemporaryDirectory() as td:
    # class:anchor present but release_state is not in the active set --
    # a real anchor ticket sitting outside the active-state axis.
    wt(os.path.join(td, "DGN-4001-shipped-anchor.md"),
       "---\nid: DGN-4001\nstatus: done\nclass: anchor\n"
       "release: v1.0\nrelease_state: done\n---\nbody\n")
    r = run(td)
    check("A3 exits 2", r.returncode == 2, r.stdout + r.stderr)
    check("A3 emits an explicit SCAN-FAIL: line",
          any(line.startswith("SCAN-FAIL:") for line in r.stdout.splitlines()),
          r.stdout)
    check("A3 SCAN-FAIL mentions zero active anchors",
          "anchors_active=0" in r.stdout, r.stdout)
    check("A3 never prints a clean exit-0-shaped SUMMARY line",
          "SUMMARY scanned=" not in r.stdout, r.stdout)

# --- A4: control -- a real active anchor produces a normal report ----------
with tempfile.TemporaryDirectory() as td:
    wt(os.path.join(td, "DGN-5001-anchor.md"),
       "---\nid: DGN-5001\nstatus: open\nclass: anchor\n"
       "release: v1.0\nrelease_state: queued\n"
       "release_tickets: DGN-5002\n---\nbody\n")
    wt(os.path.join(td, "DGN-5002-child.md"),
       "---\nid: DGN-5002\nstatus: open\nrelease: v1.0\n---\nbody\n")
    r = run(td)
    check("A4 exits 0 (clean cross-ref)", r.returncode == 0, r.stdout + r.stderr)
    check("A4 SUMMARY line present with anchors_active=1",
          "SUMMARY scanned=" in r.stdout and "anchors_active=1" in r.stdout,
          r.stdout)

# --- A5: DGN-1294 -- status: open, no release_state field at all ----------
with tempfile.TemporaryDirectory() as td:
    wt(os.path.join(td, "DGN-6001-anchor.md"),
       "---\nid: DGN-6001\nstatus: open\nclass: anchor\n"
       "release: v1.0\nrelease_tickets: DGN-6002\n---\nbody\n")
    wt(os.path.join(td, "DGN-6002-child.md"),
       "---\nid: DGN-6002\nstatus: open\nrelease: v1.0\n---\nbody\n")
    r = run(td)
    check("A5 exits clean (0)", r.returncode == 0, r.stdout + r.stderr)
    check("A5 status: open with no release_state field counts as active",
          "anchors_active=1" in r.stdout, r.stdout)

# --- A6: DGN-1294 -- terminal status excludes even if release_state says
# active (release_state: must no longer be read) --------------------------
with tempfile.TemporaryDirectory() as td:
    wt(os.path.join(td, "DGN-6101-anchor.md"),
       "---\nid: DGN-6101\nstatus: dismissed\nclass: anchor\n"
       "release: v1.0\nrelease_state: in-progress\n---\nbody\n")
    r = run(td)
    check("A6 exits 2 (terminal status -> zero active anchors -> SCAN-FAIL)",
          r.returncode == 2, r.stdout + r.stderr)
    check("A6 stale release_state: in-progress does not override status: dismissed",
          "anchors_active=1" not in r.stdout, r.stdout)

print()
if FAILURES:
    print(f"FAILED: {FAILURES}")
    sys.exit(1)
print("All tests PASSED (A1-A6)")
