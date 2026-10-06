#!/usr/bin/env python3
# test-lane: fast
"""Tier-1 tests for codex-hook-launcher.py (managed-hooks route, DGN-1444).

Launcher cases:
  A: --probe at a provisioned instance root -> rc 0, adapter path reported
  B: --probe from a SUBDIR of the instance root -> rc 0 (walk-up)
  C: --probe at a gateless ESTATE dir (marker, no adapter) -> rc 2, estate
  C2: --probe at an out-of-estate dir -> rc 2, estate false
  D: PreToolUse payload, adapter present -> adapter invoked with same args,
     stdout forwarded verbatim, rc 0 kept
  E: PreToolUse payload, gateless ESTATE cwd -> deny JSON, rc 0
  E2: PreToolUse payload, out-of-estate cwd -> allow (empty stdout), rc 0
  F: --stop payload, gateless ESTATE cwd -> empty stdout (allow), rc 0
  G: malformed stdin, gateless ESTATE process cwd -> deny (fail closed)
  H: malformed stdin, adapter present -> raw stdin forwarded untouched
  I: adapter exits an undefined code (3) -> converted to deny, rc 0
  J: adapter exits 2 (documented block path) -> rc 2 propagated
  K: payload cwd wins over process cwd for adapter resolution

Verify-helper cases (pure functions imported from codex-hook-verify.py):
  L: managed_write_gates filters on isManaged/enabled/trust/event/cmd/matcher
  M: launcher_path_from_command extracts the absolute launcher path only

Run: python3 routines/tests/test-codex-hook-launcher.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(HERE, "..", "codex-hook-launcher.py")
VERIFY = os.path.join(HERE, "..", "codex-hook-verify.py")
PYTHON = sys.executable

FAKE_ADAPTER = """\
import json, sys
raw = sys.stdin.read()
print(json.dumps({"marker": "fake-adapter", "argv": sys.argv[1:], "stdin": raw}))
sys.exit(int(__import__("os").environ.get("FAKE_RC", "0")))
"""

failures = []


def check(name, cond, detail=""):
    if cond:
        print("PASS %s" % name)
    else:
        failures.append(name)
        print("FAIL %s %s" % (name, detail))


def run_launcher(args, stdin="", cwd=None, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [PYTHON, LAUNCHER] + args, input=stdin, text=True,
        capture_output=True, timeout=30, cwd=cwd, env=full_env)


def make_instance(root):
    routines = os.path.join(root, "routines")
    os.makedirs(routines, exist_ok=True)
    adapter = os.path.join(routines, "codex-hook-adapter.py")
    with open(adapter, "w") as f:
        f.write(FAKE_ADAPTER)
    return adapter


def deny_reason(stdout):
    doc = json.loads(stdout)
    hook = doc.get("hookSpecificOutput", {})
    if hook.get("permissionDecision") == "deny":
        return hook.get("permissionDecisionReason", "")
    return None


with tempfile.TemporaryDirectory() as tmp:
    instance = os.path.join(tmp, "instance")
    adapter_path = make_instance(instance)
    subdir = os.path.join(instance, "files", "tmp")
    os.makedirs(subdir)
    bare = os.path.join(tmp, "bare")
    os.makedirs(bare)
    estate = os.path.join(tmp, "estate")  # marker but no adapter (gateless instance)
    os.makedirs(os.path.join(estate, "config"))
    with open(os.path.join(estate, "config", "agent.conf"), "w") as f:
        f.write("PLAN=test\n")

    # A: probe at instance root
    r = run_launcher(["--probe", "--cwd", instance])
    doc = json.loads(r.stdout)
    check("A-probe-root",
          r.returncode == 0 and doc.get("adapter") == adapter_path,
          r.stdout)

    # B: probe from subdir walks up
    r = run_launcher(["--probe", "--cwd", subdir])
    check("B-probe-subdir", r.returncode == 0 and
          json.loads(r.stdout).get("ok") is True, r.stdout)

    # C: probe at gateless estate dir (marker, no adapter)
    r = run_launcher(["--probe", "--cwd", estate])
    doc = json.loads(r.stdout)
    check("C-probe-gateless-estate", r.returncode == 2 and
          doc.get("ok") is False and doc.get("estate") is True, r.stdout)

    # C2: probe at out-of-estate dir (tempdir has no marker anywhere above
    # in practice; if an ancestor had one this test env is unusable)
    r = run_launcher(["--probe", "--cwd", bare])
    doc = json.loads(r.stdout)
    check("C2-probe-outside", r.returncode == 2 and
          doc.get("ok") is False and doc.get("estate") is False, r.stdout)

    # D: adapter invoked, args + stdin forwarded, rc 0
    payload = json.dumps({"cwd": instance, "tool_name": "apply_patch"})
    r = run_launcher(["--gate", "memory"], stdin=payload, cwd=bare)
    doc = json.loads(r.stdout)
    check("D-forward", r.returncode == 0 and doc.get("marker") == "fake-adapter"
          and doc.get("argv") == ["--gate", "memory"]
          and doc.get("stdin") == payload, r.stdout + r.stderr)

    # E: gateless estate cwd -> deny, rc 0
    payload = json.dumps({"cwd": estate, "tool_name": "apply_patch"})
    r = run_launcher(["--gate", "memory"], stdin=payload, cwd=bare)
    reason = deny_reason(r.stdout)
    check("E-deny-gateless-estate", r.returncode == 0 and reason is not None
          and "not provisioned" in reason, r.stdout)

    # E2: out-of-estate cwd -> allow (owner's unrelated projects untouched)
    payload = json.dumps({"cwd": bare, "tool_name": "apply_patch"})
    r = run_launcher(["--gate", "memory"], stdin=payload, cwd=bare)
    check("E2-allow-outside", r.returncode == 0 and r.stdout.strip() == ""
          and "outside the dogany estate" in r.stderr, r.stdout + r.stderr)

    # F: --stop with gateless estate cwd -> allow (empty stdout), rc 0
    payload = json.dumps({"cwd": estate})
    r = run_launcher(["--stop"], stdin=payload, cwd=bare)
    check("F-stop-allow", r.returncode == 0 and r.stdout.strip() == "",
          r.stdout)

    # G: malformed stdin, gateless estate process cwd -> deny
    r = run_launcher(["--gate", "memory"], stdin="not json", cwd=estate)
    check("G-malformed-deny", r.returncode == 0 and
          deny_reason(r.stdout) is not None, r.stdout)

    # H: malformed stdin, adapter present (process cwd fallback) -> raw pass
    r = run_launcher(["--gate", "memory"], stdin="not json", cwd=instance)
    doc = json.loads(r.stdout)
    check("H-malformed-forward", doc.get("stdin") == "not json", r.stdout)

    # I: adapter undefined exit code -> converted to deny, rc 0
    payload = json.dumps({"cwd": instance})
    r = run_launcher(["--gate", "memory"], stdin=payload, cwd=bare,
                     env={"FAKE_RC": "3"})
    reason = deny_reason(r.stdout)
    check("I-badrc-deny", r.returncode == 0 and reason is not None
          and "exited 3" in reason, r.stdout)

    # J: adapter exit 2 (documented block path) propagates
    r = run_launcher(["--gate", "memory"], stdin=payload, cwd=bare,
                     env={"FAKE_RC": "2"})
    check("J-rc2-propagates", r.returncode == 2, str(r.returncode))

    # K: payload cwd beats process cwd
    payload = json.dumps({"cwd": instance})
    r = run_launcher(["--gate", "memory"], stdin=payload, cwd=bare)
    check("K-payload-cwd-wins",
          json.loads(r.stdout).get("marker") == "fake-adapter", r.stdout)

    # L: a foreign worktree with no adapter is governed by the dispatching
    # instance explicitly exported by dispatch-detached.sh.
    foreign = os.path.join(tmp, "kit-repo")
    os.makedirs(foreign)
    r = run_launcher(["--probe", "--cwd", foreign],
                     env={"DOGANY_CODEX_ADAPTER_ROOT": instance})
    check("L-probe-foreign-dispatch-root",
          r.returncode == 0 and json.loads(r.stdout).get("adapter") == adapter_path,
          r.stdout + r.stderr)
    payload = json.dumps({"cwd": foreign, "tool_name": "apply_patch"})
    r = run_launcher(["--gate", "memory"], stdin=payload, cwd=bare,
                     env={"DOGANY_CODEX_ADAPTER_ROOT": instance})
    check("L2-forward-foreign-dispatch-root",
          r.returncode == 0 and json.loads(r.stdout).get("marker") == "fake-adapter",
          r.stdout + r.stderr)

# L/M: verify-script pure helpers
spec = importlib.util.spec_from_file_location("chv", VERIFY)
chv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chv)

good = {"isManaged": True, "enabled": True, "trustStatus": "managed",
        "eventName": "preToolUse", "matcher": "apply_patch",
        "command": "python3 /etc/codex/hooks/codex-hook-launcher.py --gate memory"}
cases = [
    dict(good),
    dict(good, isManaged=False),
    dict(good, enabled=False),
    dict(good, trustStatus="untrusted"),
    dict(good, eventName="stop"),
    dict(good, matcher="spawn_agent"),
    dict(good, command="python3 routines/codex-hook-adapter.py --gate memory"),
]
got = chv.managed_write_gates(cases)
check("L-managed-filter", got == [cases[0]], repr(got))

check("M1-path-extract",
      chv.launcher_path_from_command(good["command"])
      == "/etc/codex/hooks/codex-hook-launcher.py")
check("M2-relative-rejected",
      chv.launcher_path_from_command(
          "python3 codex-hook-launcher.py --gate memory") is None)
check("M3-garbage-none", chv.launcher_path_from_command("'unclosed") is None)
check("M4-empty-none", chv.launcher_path_from_command(None) is None)

print()
if failures:
    print("FAILED: %d (%s)" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("ALL PASS (%d checks)" % (16 + 5))
sys.exit(0)
