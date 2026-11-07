#!/usr/bin/env python3
"""PreToolUse Bash safety gate -- v2.7 D3, normally wired in DETECT mode.
Raw routine scheduling bypasses are always denied, regardless of mode/switch.
Spec: docs/v27-D3-D4-spec.md section 3 (reworked after
docs/v27-D3-D4-grill.md and docs/v27-D3-D4-grill-r2.md).

Rows (v2.7 rule inventory):
  HF11/HF31  service ops with no script in between: launchctl / systemctl /
             service / brew services, kill of the bridge, bridge start.sh /
             watchdog, routine units, self-update / update.sh
  HF31/HF32b destructive ops: git reset --hard / clean -f / checkout -f /
             restore / branch -D / force or delete push, dd/mkfs/shred,
             crontab -r, DROP/DELETE
  DGN-1812   `mv` of an agent root (a dir holding .instance.conf) outside
             scratch: a hand retirement; the deny names the retire verb
  HF32a      `rm` -> trash: a top-level rm/unlink outside scratch is
             REWRITTEN to routines/lib/trash.sh (updatedInput); an rm the
             gate cannot rewrite (nested, xargs, find -exec/-delete, sudo)
             is denied
  TP08       gh release create/delete/edit/upload, package publish (no
             script and no pre-push sees them)
  (self)     gate-switch.py off, git push --no-verify, git config
             core.hooksPath / include.path / alias.* (also --file forms),
             GIT_CONFIG_* / --config-env, git send-pack, and any device knob
             set or unset anywhere in the command (R2-8): the agent never
             disarms a device itself

MOVED OUT (grill section D): release / publish scripts check a recorded
owner approval themselves (scripts/release-approval.py); the push remote URL
and tag refs are judged by git-hooks/pre-push (it sees the real URL and
refs); bridge/self_restart.sh guards --trigger user / --force / --idle-mins /
--_worker in its own flag parser.  The hook no longer judges those.

A yes-required finding passes only on a user yes (lib/user_yes.py: the
owner's FIRST turn after the ask, a bare yes to a reply that ENDS with the
exact question line this gate printed (R2-1), backed by the owner-turn
ledger; a tap only from the bubble that carried that question (R2-2); or,
for service ops, the owner turn itself naming the object AND the verb --
"restart the bridge" covers a restart, never a bootout, R2-4).  A yes runs
its command once; a re-run passes only right after that command failed
(R2-11).  The chain is anchored at this call's own tool_use entry when the
payload names it (R2-13).  On deny, the gate records the ask, binding the
exact command.

Live record (R2-7): before a command naming a script-side device runs
(self_restart.sh, release-approval.py / the release scripts, git), the gate
writes <data>/state/bash-gate/live/<session>.json from the CLI's own payload
(session, transcript path, tool_use id).  That is how the device knows its
caller's session without trusting env.  Written in every mode but off.

A subagent call (payload agent_id) never runs release / tag / push /
gate-switch classes, yes or not (F6: the main session runs them).

Exceptions (allowed, canary-logged):
  - dawn-memory-queue.sh --live / cli-updater.py --mode install while
    ~/.dogany/cli-updates/armed exists (the updater checks idle itself)
  - git destructive verbs and rm inside a SANDBOXED dispatch worker's own
    worktree (DOGANY_DISPATCH_WORKTREE, exported only under the DGN-1504
    write profile, and the cwd inside it)
  - plain rm under files/tmp, /tmp, $TMPDIR, the scratchpad (realpath)

The rm rewrite answers `allow` only under permission_mode
bypassPermissions; otherwise `ask` with the rewritten command, so an
interactive session keeps its permission prompt (F16 / Q12).

Mode (BASH_GATE_MODE, set by the settings line, not by the agent; parsed by
bash_safety.gate_mode, shared with D4: case and whitespace ignored, unknown
-> detect): off | detect (default: log only, no deny, and NO rm rewrite) |
block.  Detect
records a SHADOW ask and logs SHADOW_YES / SHADOW_NO so the two weeks of
detect measure the sentinel too (R2-12).  Switch: gate-switch slug
`bash-safety` (GATE_BASH_SAFETY; masters GATE_ALL and GATE_STOP_ALL): off
-> block mode emits the off-notice instead of the deny, never silent, and
no rm rewrite (the rm runs as typed); the canary keeps logging (an observation is never switched off).

Fail mode: a finding that names an irreversible class fails CLOSED -- an
unparseable command with a gated verb in command position is denied
(yes-able); an unreadable transcript means no yes.  A crash of the gate
itself fails OPEN except when the raw command matches the danger prefilter
(then deny, yes-able).

Canary: <BOT_DATA_DIR>/logs/bash-safety-gate.log, one line per decision.
Always exits 0.
"""
import datetime
import importlib.util
import json
import os
import re
import shlex
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(SCRIPT_DIR, "lib")
GATE_SLUG = "bash-safety"
# deny order: the first class present names the reason
CLASS_ORDER = ("destructive", "unparsed", "service", "release", "gate-switch")
YES_CLASS = {"unparsed": "destructive"}
# F6: classes a subagent never runs, whatever the yes
MAIN_SESSION_ONLY = ("release", "gate-switch")
MODE_KEY = "BASH_GATE_MODE"


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(LIB, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mode(env):
    """Test seams (BASH_GATE_ROOT) are honoured only under DOGANY_TEST_MODE=1,
    as lib/device_gate.py does.  Unlike device_gate.test_mode, CLAUDECODE
    is not checked: a hook always runs under Claude Code, and its env comes
    from the CLI's settings, not from the agent's Bash call."""
    return env.get("DOGANY_TEST_MODE") == "1"


def _root(env=None):
    env = os.environ if env is None else env
    if test_mode(env) and env.get("BASH_GATE_ROOT"):
        return env["BASH_GATE_ROOT"]
    return os.path.dirname(SCRIPT_DIR)


def _data_dir(root):
    return os.environ.get("DOGANY_BOT_DATA_DIR") or \
        os.path.join(root, ".telegram_bot")


def _log(root, line):
    try:
        d = os.path.join(_data_dir(root), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "bash-safety-gate.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (
                datetime.datetime.now().isoformat(timespec="seconds"), line))
    except OSError:
        pass


def gate_mode(env):
    """Stable observes shell classifications; only raw routine labels deny."""
    return "off" if (env.get(MODE_KEY) or "").strip().lower() == "off" else "detect"


def _switch_on(cwd):
    try:
        spec = importlib.util.spec_from_file_location(
            "gate_switch_bash", os.path.join(SCRIPT_DIR, "gate-switch.py"))
        gs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gs)
        return gs.is_enabled(GATE_SLUG, cwd), gs
    except Exception:
        return True, None  # a broken switch never disarms the gate


def _deny(reason):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}


def deny_reason(f, key, subagent=False, questions=None):
    what = {
        "service": "a service op (restart/stop/reconfigure of a bot, the "
                   "bridge or a unit)",
        "destructive": "a destructive op",
        "unparsed": "a command the gate cannot parse that names a gated verb",
        "release": "a release / publish step",
        "gate-switch": "switching a safety device off",
    }[f.cls]
    if subagent:
        return ("Bash safety gate (%s): %s -- %s. A subagent never runs this "
                "class; return to the main session, which asks the user." % (
                    f.row, what, f.detail))
    extra = ""
    if f.row == "HF32a":
        extra = (" For a reversible delete use routines/lib/trash.sh <path> "
                 "(moves to files/_archive/trash/), which needs no yes.")
    if f.row == "DGN-1812":
        extra = (" To retire an agent run <DOGANY_REPO_ROOT from "
                 ".instance.conf>/scripts/pack/mint_run.sh retire --root "
                 "<agent root> (--dry-run first): it also removes the "
                 "install registry row, the main's peer entry, launchd units, "
                 "crew rows and observer labels, and can restore them. A hand "
                 "mv leaves all of those behind.")
    qs = questions or []
    return (
        "Bash safety gate (%s): %s -- %s. This needs the user's yes first. "
        "Ask the user now: END your reply with exactly this line (or its "
        "Korean form)\n%s\nOnly the user's NEXT message approves it, and only "
        "a bare yes (or the yes button of THAT reply's keyboard); a yes with a "
        "rider ('yes but ...'), a later or unrelated yes, an edited message "
        "or voice does not. To take a yes back, the user sends a new message. "
        "Run the command unchanged after the yes, once (a re-run only after "
        "it failed); a changed command needs a new yes. If the user "
        "interrupts before answering, run the command again first so this "
        "gate records a fresh ask, then re-ask.%s" % (
            f.row, what, f.detail, "\n".join(qs) or key, extra))


def decide(data, env=None, ctx_kw=None, record=True, now=None, shadow=None, routine_only=False):
    """(output dict or None, canary line).  Pure enough for fixtures.
    record=False (detect mode): no real ask is recorded, since nothing was
    denied and no question reaches the user; a SHADOW ask is (R2-12) and
    the line carries SHADOW_YES / SHADOW_NO <reason>."""
    shadow = (not record) if shadow is None else shadow
    env = os.environ if env is None else env
    bs = _load("bash_safety_d3", "bash_safety.py")
    root = _root(env)
    cmd = (data.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None, None
    ctx = bs.Ctx(root, cwd=data.get("cwd") or root, env=env,
                 **(ctx_kw or {}))
    trash = os.path.join(SCRIPT_DIR, "lib", "trash.sh")
    res = bs.classify(cmd, ctx, trash_cmd=shlex.quote(trash)
                      if os.path.isfile(trash) else None)
    ref = "session=%s" % (data.get("session_id") or "-")
    if any(f.row == "ROUTINE_CTL" for f in res.findings):
        return _deny(
            "Raw routine scheduling is blocked. Use bash "
            "routines/lib/routine-ctl.sh enable|disable|load <name> "
            "so the retirement ledger and observer stay consistent."), \
            "DENY row=ROUTINE_CTL %s" % ref
    if routine_only:
        return None, None
    # Stable observes classifications without requesting owner proof.
    return None, "DETECT rows=%s %s" % (
        ",".join(f.row for f in res.findings), ref)


def _rewrite_out(data, res):
    # F16 / Q12: `allow` would also skip the permission prompt for the whole
    # command line; only a session already in bypass mode gets it.
    bypass = data.get("permission_mode") == "bypassPermissions"
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if bypass else "ask",
        "permissionDecisionReason":
            "Bash safety gate (HF32a): rm rewritten to the reversible trash "
            "helper.",
        "updatedInput": dict(data.get("tool_input") or {},
                             command=res.rewrite)}}


# R2-7: commands that reach a script-side device (the device then asks
# which session called it; see lib/device_gate.live_caller)
LIVE_DEVICES = (
    ("self_restart", re.compile(r"self_restart\.sh")),
)


def record_live(data, root, now=None):
    """Write the live-call record for a command naming a device.  Never
    raises; returns the devices named."""
    cmd = (data.get("tool_input") or {}).get("command")
    sid = data.get("session_id")
    if not isinstance(cmd, str) or not isinstance(sid, str) or not sid:
        return []
    devices = [n for n, rx in LIVE_DEVICES if rx.search(cmd)]
    if not devices:
        return []
    import hashlib
    import time
    row = {"session_id": sid,
           "transcript_path": data.get("transcript_path"),
           "tool_use_id": data.get("tool_use_id"),
           "cmd_sha": hashlib.sha256(cmd.encode("utf-8")).hexdigest(),
           "devices": devices, "agent_id": data.get("agent_id"),
           "at": time.time() if now is None else now}
    try:
        d = os.path.join(_data_dir(root), "state", "bash-gate", "live")
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sid)
        tmp = os.path.join(d, ".%s.%d.tmp" % (safe, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(row, fh)
        os.replace(tmp, os.path.join(d, safe + ".json"))
    except OSError:
        pass
    return devices


PREFILTER = re.compile(r"\b(launchctl|systemctl|pkill|killall|"
                       r"git\s+push|publish|reset\s+--hard|hooksPath|"
                       r"rm\s+-|gate-switch|self-update)\b")


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        sys.exit(0)
    mode = gate_mode(os.environ)
    root = _root()
    if mode != "off":
        record_live(data, root)
    try:
        out, line = decide(data, record=(mode == "block"),
                           routine_only=(mode == "off"))
    except Exception as exc:
        cmd = str((data.get("tool_input") or {}).get("command") or "")
        if mode != "off" and PREFILTER.search(cmd):
            out = _deny("Bash safety gate error (%s) on a command naming a "
                        "gated verb; ask the user before running it." %
                        type(exc).__name__)
            line = "ERROR_DENY %s" % type(exc).__name__
        else:
            out, line = None, "ERROR_OPEN %s" % type(exc).__name__
    on, gs = True, None
    routine_deny = bool(line and line.startswith("DENY row=ROUTINE_CTL "))
    if not routine_deny and line and line.startswith(("DENY", "REWRITE", "ERROR_DENY")):
        on, gs = _switch_on(data.get("cwd"))
        if not on:
            line += " switch=off"
    if line:
        _log(root, ("[%s] " % mode) + line)
    # detect: log only -- no deny, and the rm -> trash rewrite stays OFF
    if out is None or (mode != "block" and not routine_deny):
        sys.exit(0)
    hso = out.get("hookSpecificOutput") or {}
    if not on and "updatedInput" in hso:
        sys.exit(0)  # switched off: the rm runs as typed (review bug 14)
    if hso.get("permissionDecision") == "deny":
        if not on and gs is not None:
            print(json.dumps(gs.off_notice(GATE_SLUG,
                                           hso["permissionDecisionReason"],
                                           data.get("cwd")),
                             ensure_ascii=False))
            sys.exit(0)
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
