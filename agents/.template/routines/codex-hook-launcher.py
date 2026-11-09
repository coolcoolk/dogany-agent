#!/usr/bin/env python3
"""codex-hook-launcher.py -- managed-hook entry point for codex gate batteries.

SOURCE OF TRUTH: this file lives in the instance repo (routines/). The copy
that actually runs is installed BY THE OWNER (root) at
/etc/codex/hooks/codex-hook-launcher.py and referenced from
/etc/codex/requirements.toml managed hook entries. Managed hooks are trusted
by policy (trustStatus=managed, no hash bookkeeping), which removes the
path-scoped hook-trust persistence problem that made per-worktree project
hooks unusable (DGN-1444). Drift check between this file and the installed
copy: shasum both (documented in the managed-hooks plan, files/outbox).

WHY A LAUNCHER: codex runs hook commands with the SESSION CWD as working
directory (codex-rs hooks/src/engine/command_runner.rs: current_dir(cwd);
manual "Commands run with the session cwd as their working directory").
managed_dir does NOT constrain where hook commands live -- it is only the
source label for the managed layer (codex-rs discovery.rs
managed_hooks_source_path). So one machine-wide managed entry can dispatch to
EACH INSTANCE'S OWN gate battery: this launcher derives the instance root
from the worker's cwd (walking up to find routines/codex-hook-adapter.py)
and hands the payload to that instance's adapter unchanged.

FOREIGN WORKTREE CONTRACT: dispatch-detached.sh may set
DOGANY_CODEX_ADAPTER_ROOT to its own absolute instance root when it cuts a
worktree from a kit/pack repo. In that case this launcher uses that root's
adapter instead of looking beside the target cwd. The same environment is
used for --probe, so verification proves the exact runtime routing.

FAIL-CLOSED CONTRACT (an instance with no gate battery must never write),
three-way because managed hooks fire MACHINE-WIDE, including the owner's
own codex sessions in unrelated projects:
  1. Adapter found at/above cwd -> delegate to it (the gated case).
  2. No adapter, but a dogany-instance marker (config/agent.conf or
     identity/hot.custom.agent.md) found at/above cwd -> this IS an estate
     instance without a gate battery (e.g. an instance before gate provisioning):
     PreToolUse -> deny; --stop -> ALLOW with stderr note (a Stop gate is a
     completeness check, not a write gate; blocking Stop in a gateless
     instance would loop the worker forever without protecting anything).
  3. Neither found -> out-of-estate directory (the owner's other work):
     ALLOW with a stderr note. Estate dispatches never land here -- the
     spawn-time verifier (codex-hook-verify.py) refuses any cwd whose
     probe finds no adapter, so this branch only relaxes the owner's own
     non-estate codex use.
  - Adapter present but unrunnable/hung/crashed -> deny (PreToolUse) or
    block (--stop), matching the adapter's own fail-closed policy.
Exit code 0 with a decision JSON is used instead of exit 2 because a hook
that merely "fails" (non-zero, non-2) fails OPEN in codex (observed
2026-09-13); this launcher never exits with an undefined code on purpose.

--probe mode (no stdin): resolve the adapter for --cwd and report. Used by
routines/codex-hook-verify.py at spawn time so the EXACT installed copy is
what answers "would this instance's battery actually run".
"""
import json
import os
import subprocess
import sys

ADAPTER_REL = os.path.join("routines", "codex-hook-adapter.py")
ESTATE_MARKERS = (
    os.path.join("config", "agent.conf"),
    os.path.join("identity", "hot.custom.agent.md"),
)
ADAPTER_TIMEOUT = 75  # < managed-hook timeout in requirements.toml (90)
ADAPTER_ROOT_ENV = "DOGANY_CODEX_ADAPTER_ROOT"


def walk_up(start, rel_paths):
    """Walk up from start; return the first existing rel_path hit, else None."""
    current = os.path.abspath(start)
    while True:
        for rel in rel_paths:
            candidate = os.path.join(current, rel)
            if os.path.isfile(candidate):
                return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def find_adapter(start):
    adapter_root = os.environ.get(ADAPTER_ROOT_ENV, "")
    if adapter_root:
        candidate = os.path.join(os.path.abspath(adapter_root), ADAPTER_REL)
        if os.path.isfile(candidate):
            return candidate
        # An explicit dispatcher root is a fail-closed claim: never fall
        # through to an unrelated adapter near the foreign target.
        return None
    return walk_up(start, (ADAPTER_REL,))


def in_estate(start):
    return walk_up(start, ESTATE_MARKERS) is not None


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }))


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))


def refuse(is_stop, reason):
    if is_stop:
        block(reason)
    else:
        deny(reason)


def probe(argv):
    cwd = os.getcwd()
    if "--cwd" in argv:
        cwd = argv[argv.index("--cwd") + 1]
    adapter = find_adapter(cwd)
    if adapter is None:
        print(json.dumps({"ok": False, "cwd": cwd,
                          "estate": in_estate(cwd),
                          "reason": "no %s at or above cwd" % ADAPTER_REL}))
        sys.exit(2)
    print(json.dumps({"ok": True, "cwd": cwd, "adapter": adapter}))
    sys.exit(0)


def main():
    argv = sys.argv[1:]
    if "--probe" in argv:
        probe(argv)
        return

    is_stop = "--stop" in argv
    raw = sys.stdin.read()
    cwd = None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("cwd"), str):
            cwd = data["cwd"]
    except Exception:
        pass  # adapter owns malformed-payload policy; still needs locating
    if cwd is None:
        cwd = os.getcwd()

    adapter = find_adapter(cwd)
    if adapter is None:
        if not in_estate(cwd):
            # Case 3 of the FAIL-CLOSED CONTRACT: not our estate, stay out.
            print("codex-hook-launcher: %s is outside the dogany estate; "
                  "no gate applied" % cwd, file=sys.stderr)
            return
        if is_stop:
            # Allow: see FAIL-CLOSED CONTRACT in the module docstring.
            print("codex-hook-launcher: no gate battery at %s; "
                  "Stop allowed, writes are denied separately" % cwd,
                  file=sys.stderr)
            return
        deny("codex-hook-launcher: no gate battery (%s) found at or above %s"
             " -- this instance is not provisioned for codex writes"
             % (ADAPTER_REL, cwd))
        return

    try:
        result = subprocess.run(
            [sys.executable, adapter] + argv, input=raw, text=True,
            capture_output=True, timeout=ADAPTER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        refuse(is_stop, "codex-hook-launcher: %s timed out after %ss"
               % (adapter, ADAPTER_TIMEOUT))
        return
    except Exception as exc:
        refuse(is_stop, "codex-hook-launcher: could not run %s: %s"
               % (adapter, exc))
        return
    if result.returncode not in (0, 2):
        # Any other exit code would read as a plain hook failure, which
        # fails OPEN in codex. Convert to an explicit refusal instead.
        refuse(is_stop, "codex-hook-launcher: %s exited %s (stderr: %s)"
               % (adapter, result.returncode, result.stderr.strip()[:400]))
        return
    if result.stderr:
        sys.stderr.write(result.stderr)
    sys.stdout.write(result.stdout)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
