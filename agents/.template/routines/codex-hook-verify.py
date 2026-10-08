#!/usr/bin/env python3
"""codex-hook-verify.py -- ask codex ITSELF whether the project hook layer
at a given cwd is discovered, enabled and trusted (DGN-1444 forcing point).

WHY THIS EXISTS: a codex worker's PreToolUse/Stop gates live in the target
checkout's .codex/config.toml, and codex SILENTLY SKIPS them unless two
things hold at once (verified 2026-09-14, .codex/config.toml STATUS block):
  1. the project path is trusted (projects."<path>".trust_level="trusted"),
  2. every non-managed hook has persisted hook trust for its CURRENT hash
     (user config `hooks.state."<key>".trusted_hash`, key format
     "<abs sourcePath>:<event>:<group>:<index>" -- PATH-SCOPED, so trust for
     the source repo does NOT transfer to a freshly minted worktree path).
"Silently" is literal: zero warning, zero log line, the tool call proceeds
unguarded. So the only honest spawn-time evidence is codex's own verdict:
the app-server `hooks/list` RPC reports, per cwd, every hook it would load
with `enabled` and `trustStatus` (managed|trusted|modified|untrusted).
That is what this script reads. It never writes anything.

PASS (rc 0) requires the hooks/list call to succeed for the target cwd,
plus ONE of two batteries:

MANAGED battery (checked first -- the machine-wide requirements.toml route,
owner-installed, trustStatus=managed by policy, no hash bookkeeping):
  - at least one enabled isManaged preToolUse hook whose command invokes
    codex-hook-launcher.py with an apply_patch matcher (the write gate), and
  - the INSTALLED launcher (path taken from that hook's own command string)
    answers `--probe --cwd <target>` with rc 0, proving THIS instance has a
    reachable routines/codex-hook-adapter.py. For a foreign worktree target,
    --adapter-root names the dispatching instance; that exact root is passed
    to the launcher probe and later inherited by the codex worker. A gateless
    dispatching instance fails here and the dispatch is refused --
    the launcher would deny every write at runtime anyway, but refusing at
    spawn is the honest gate. If the managed battery exists but the probe
    fails, that is a FAIL (no fallback to the project battery: the managed
    layer IS the active enforcement then).

PROJECT battery (legacy .codex/config.toml route, unchanged):
  - at least one project-source hook was discovered there (zero project
    hooks == the gate battery is inert: either no .codex layer or an
    untrusted project -- codex does not distinguish the two from outside),
  - at least one of them is a preToolUse hook (the write gates),
  - EVERY project-source hook is enabled with trustStatus trusted|managed.

FAIL CLOSED (rc 2) on anything else, including spawn/timeout/parse
failures: an unreadable trust state is not permission (same doctrine as the
dynamic-cap fallback in dispatch-detached.sh).

WHAT THIS CHECK CANNOT KNOW (stated, not hidden):
  - whether the hook COMMANDS actually refuse at runtime. A hook that
    errors mid-run fails OPEN in codex (observed 2026-09-13: hook marked
    Failed, tool call continued). Efficacy is an E2E test's job (DGN-1444
    required-work 5), not a spawn gate's.
  - anything about claude-side .claude/settings.json hooks -- a codex
    worker never reads those; only codex-visible layers are checked.
  - future state: trust is verified for the cwd AS IT IS NOW; a hook file
    edited after this check would flip to "modified" and be skipped by
    codex itself (which is the safe direction).

Exit codes: 0 verified active; 1 usage error; 2 NOT verified (refuse).
"""
import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time

OK_TRUST = ("trusted", "managed")
LAUNCHER_NAME = "codex-hook-launcher.py"


def fail(reason, detail=""):
    print(json.dumps({"ok": False, "reason": reason, "detail": detail},
                     ensure_ascii=False))
    sys.exit(2)


def managed_write_gates(hooks):
    """Managed-battery write gates: enabled, managed-trust, launcher-driven
    preToolUse hooks matching apply_patch."""
    return [
        h for h in hooks
        if h.get("isManaged")
        and h.get("enabled")
        and h.get("trustStatus") in OK_TRUST
        and h.get("eventName") == "preToolUse"
        and LAUNCHER_NAME in (h.get("command") or "")
        and "apply_patch" in (h.get("matcher") or "")
    ]


def launcher_path_from_command(command):
    """Absolute launcher path out of a managed hook command, else None."""
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        return None
    for token in tokens:
        if os.path.basename(token) == LAUNCHER_NAME and os.path.isabs(token):
            return token
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=True,
                    help="directory the codex worker will run in")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--adapter-root", default="",
                    help="dispatching instance root for a foreign worktree")
    args = ap.parse_args()

    try:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
    except Exception as e:
        fail("spawn_fail", str(e))

    lines = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:
                lines.put(line)
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + args.timeout

    def send(obj):
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except Exception as e:
            proc.kill()
            fail("no_response", "stdin write failed: %s" % e)

    def wait_id(rpc_id):
        # Unrelated notifications interleave; skip them, never mistake one
        # for the answer (same lesson as codex-ratelimit.py).
        while time.time() < deadline:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    proc.kill()
                    fail("no_response",
                         "app-server exited rc=%s before answering" % proc.returncode)
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("id") == rpc_id:
                return doc
        proc.kill()
        fail("timeout", "no reply for id=%s within %ss" % (rpc_id, args.timeout))

    send({"id": 1, "method": "initialize", "params": {"clientInfo": {
        "name": "dispatch-hook-verify", "title": "dispatch-hook-verify",
        "version": "0"}}})
    wait_id(1)
    send({"method": "initialized", "params": {}})
    send({"id": 2, "method": "hooks/list", "params": {"cwds": [args.cwd]}})
    reply = wait_id(2)
    proc.kill()

    if "error" in reply:
        fail("rpc_error", json.dumps(reply.get("error"), ensure_ascii=False))
    try:
        entries = reply["result"]["data"]
        entry = next(e for e in entries if e.get("cwd") == args.cwd)
        hooks = entry["hooks"]
    except (KeyError, TypeError, StopIteration) as e:
        fail("parse_fail", "unexpected hooks/list shape: %s" % e)

    managed_gates = managed_write_gates(hooks)
    if managed_gates:
        launcher = launcher_path_from_command(managed_gates[0].get("command"))
        if launcher is None or not os.path.isfile(launcher):
            fail("managed_battery_unprobeable",
                 "managed launcher hook found but its command does not name "
                 "an existing absolute %s: %r"
                 % (LAUNCHER_NAME, managed_gates[0].get("command")))
        try:
            probe_env = dict(os.environ)
            if args.adapter_root:
                probe_env["DOGANY_CODEX_ADAPTER_ROOT"] = args.adapter_root
            probe = subprocess.run(
                [sys.executable, launcher, "--probe", "--cwd", args.cwd],
                capture_output=True, text=True, timeout=10, env=probe_env)
        except Exception as e:
            fail("managed_probe_fail", "could not run installed launcher "
                 "%s: %s" % (launcher, e))
        if probe.returncode != 0:
            fail("no_instance_adapter",
                 "managed gate battery is active machine-wide, but the "
                 "installed launcher found no routines/codex-hook-adapter.py "
                 "at or above %s -- this instance is not provisioned for "
                 "codex writes (probe: %s)"
                 % (args.cwd, (probe.stdout or probe.stderr).strip()[:400]))
        print(json.dumps({
            "ok": True,
            "mode": "managed",
            "cwd": args.cwd,
            "managed_write_gates": len(managed_gates),
            "probe": (probe.stdout or "").strip()[:400],
            "hooks": [{"key": h.get("key"), "event": h.get("eventName"),
                       "trust": h.get("trustStatus")} for h in managed_gates],
        }, ensure_ascii=False))
        sys.exit(0)

    project = [h for h in hooks if h.get("source") == "project"]
    if not project:
        fail("no_project_hooks",
             "codex discovered 0 project-source hooks at %s -- the .codex "
             "layer is absent or the project path is untrusted (codex does "
             "not say which; both mean the gates are inert)" % args.cwd)
    bad = [h for h in project
           if not h.get("enabled") or h.get("trustStatus") not in OK_TRUST]
    if bad:
        fail("untrusted_hooks", "; ".join(
            "%s: enabled=%s trustStatus=%s" % (
                h.get("key", "?"), h.get("enabled"), h.get("trustStatus"))
            for h in bad))
    if not any(h.get("eventName") == "preToolUse" for h in project):
        fail("no_pretooluse_hook",
             "project hooks exist but none is preToolUse -- the write gates "
             "specifically are missing")

    print(json.dumps({
        "ok": True,
        "mode": "project",
        "cwd": args.cwd,
        "project_hooks": len(project),
        "hooks": [{"key": h.get("key"), "event": h.get("eventName"),
                   "trust": h.get("trustStatus")} for h in project],
    }, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
