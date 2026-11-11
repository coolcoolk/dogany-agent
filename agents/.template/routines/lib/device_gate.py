#!/usr/bin/env python3
"""General device trust and restart approval, independent of release policy."""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LIVE_TTL_SEC = 120
MODE_KEYS = ("SELF_RESTART_OWNER_GATE",)
QUESTIONS = {
    "self_restart": "Restart now, skipping the idle guard?",
}
# Q-R6: the Claude Code CLI as a process (native binary or the node entry)
CLAUDE_ARGS_RE = re.compile(r"(^|/)claude(\s|$)|@anthropic-ai/claude-code|"
                            r"claude-agent-sdk/.*cli\.js")
ANCESTRY_MAX = 64


# --- who is calling -----------------------------------------------------------

def test_mode(env=None):
    env = os.environ if env is None else env
    return env.get("DOGANY_TEST_MODE") == "1" and env.get("CLAUDECODE") != "1"


def _ps(pid):
    try:
        p = subprocess.run(["ps", "-o", "ppid=", "-o", "comm=", "-p",
                            str(pid)], capture_output=True, text=True,
                           timeout=2)
        a = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    line = p.stdout.strip()
    if not line:
        return None
    parts = line.split(None, 1)
    try:
        ppid = int(parts[0])
    except ValueError:
        return None
    comm = parts[1].strip() if len(parts) > 1 else ""
    return ppid, comm, a.stdout.strip()


def claude_ancestor(pid=None):
    """True when a `claude` CLI process is among this process's ancestors
    (`ps -o ppid=` walk).  A probe error says nothing (False)."""
    pid = os.getppid() if pid is None else pid
    for _ in range(ANCESTRY_MAX):
        if pid is None or pid <= 1:
            return False
        got = _ps(pid)
        if got is None:
            return False
        ppid, comm, args = got
        if os.path.basename(comm) == "claude" or CLAUDE_ARGS_RE.search(args):
            return True
        pid = ppid
    return False


def agent_caller(env=None):
    """Q-R6: CLAUDECODE=1 or a claude ancestor means the agent is calling."""
    env = os.environ if env is None else env
    if env.get("CLAUDECODE") == "1":
        return True
    if test_mode(env):
        return False
    return claude_ancestor()


# --- which mode ---------------------------------------------------------------

def conf_value(conf_path, key):
    val = None
    try:
        with open(conf_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if line.startswith(key + "="):
                    val = line.split("=", 1)[1].split("#", 1)[0].strip() \
                        .strip("'\"")
    except OSError:
        return None
    return val


def device_mode(key, conf_path, env=None):
    """R2-8: the conf line decides (default detect); env may only tighten;
    case-insensitive; an unknown value is block.  Deliberately unlike the
    hook gates' bash_safety.gate_mode (unknown -> detect): a hook's mode is
    the settings line the agent's Bash cannot set, a device's mode is read
    where an agent slip could land, so it fails toward block."""
    env = os.environ if env is None else env
    raw = conf_value(conf_path, key) if conf_path else None
    mode = (raw or "detect").strip().lower()
    if mode not in ("detect", "block"):
        mode = "block"
    e = (env.get(key) or "").strip().lower()
    if e and e != "detect":
        mode = "block"
    return mode


# --- where trust lives --------------------------------------------------------

def data_dir(root=ROOT):
    return os.path.join(root, ".telegram_bot")


def is_instance(root):
    return os.path.isdir(os.path.join(root, ".telegram_bot")) and \
        os.path.isfile(os.path.join(root, "routines", "lib",
                                    "owner_quote_check.py"))


def _git(repo, *args):
    try:
        p = subprocess.run(["git", "-C", repo] + list(args),
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def owner_instance(repo):
    """The instance whose owner approves actions in `repo`: the repo itself
    when it is an instance, else `git config dogany.ownerInstance` (a
    canonical checkout).  None when neither holds."""
    if repo and is_instance(repo):
        return os.path.realpath(repo)
    named = _git(repo, "config", "--get", "dogany.ownerInstance") \
        if repo else None
    if named and is_instance(os.path.expanduser(named)):
        return os.path.realpath(os.path.expanduser(named))
    return None


def resolver_for(repo, env=None):
    """Path of the owner_quote_check.py that judges quotes for `repo`."""
    env = os.environ if env is None else env
    if test_mode(env) and env.get("DOGANY_OWNER_QUOTE_CHECK"):
        return env["DOGANY_OWNER_QUOTE_CHECK"]
    inst = owner_instance(repo)
    if inst:
        return os.path.join(inst, "routines", "lib", "owner_quote_check.py")
    return None


# --- which session (R2-7) -----------------------------------------------------

def live_dir(data):
    return os.path.join(data, "state", "bash-gate", "live")


def live_caller(data, device, now=None, ttl=LIVE_TTL_SEC):
    """(record, None) for the one session whose D3 hook saw a command naming
    `device` in the last `ttl` seconds, else (None, reason)."""
    now = time.time() if now is None else now
    d = live_dir(data)
    try:
        names = os.listdir(d)
    except OSError:
        return None, "no-live-call"
    hits = {}
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(r, dict) or device not in (r.get("devices") or ()):
            continue
        at = r.get("at")
        if not isinstance(at, (int, float)) or not 0 <= now - at <= ttl:
            continue
        sid = r.get("session_id")
        if isinstance(sid, str) and sid:
            hits[sid] = r
    if not hits:
        return None, "no-live-call"
    if len(hits) > 1:
        return None, "two-sessions"
    return next(iter(hits.values())), None


def _user_yes():
    spec = importlib.util.spec_from_file_location(
        "user_yes_for_devices", os.path.join(HERE, "user_yes.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve(device, quote, questions, objects=(), intent=None, env=None,
            now=None, root=ROOT):
    """(True, info) or (False, reason): the owner said yes to THIS device
    action in the live caller's session (R2-6, R2-7)."""
    env = os.environ if env is None else env
    if env.get("DOGANY_DISPATCH_WORKER"):
        return False, "dispatch-worker"
    if not quote or not quote.strip():
        return False, "empty-quote"
    data = data_dir(root)
    live, why = live_caller(data, device, now)
    if live is None:
        return False, why
    tpath = live.get("transcript_path")
    if not isinstance(tpath, str) or not os.path.isfile(tpath):
        return False, "no-transcript"
    uy = _user_yes()
    ledger = os.path.join(data, "state", "why-net", "turn-ledger.jsonl")
    v = uy.device_yes(tpath, live["session_id"], ledger, questions, objects,
                      intent, quote, anchor=live.get("tool_use_id"), now=now)
    if not v.ok:
        return False, v.reason
    return True, {"session_id": live["session_id"], "message_uuid": v.turn_uuid,
                  "transcript_path": tpath, "grade": v.grade}


def audit(session, message_uuid, transcript, quote, questions, objects=(),
          intent=None, root=ROOT):
    """Re-check a recorded device approval."""
    if not (session and message_uuid and transcript) or \
            not os.path.isfile(transcript):
        return False, "record-incomplete"
    uy = _user_yes()
    ledger = os.path.join(data_dir(root), "state", "why-net",
                          "turn-ledger.jsonl")
    v = uy.device_audit(transcript, session, ledger, message_uuid, questions,
                        objects, intent, quote)
    return (True, {"grade": v.grade}) if v.ok else (False, v.reason)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("is-agent")
    mode = sub.add_parser("mode")
    mode.add_argument("key", choices=MODE_KEYS)
    mode.add_argument("--conf")
    args = ap.parse_args(argv)
    if args.cmd == "is-agent":
        return 0 if agent_caller() else 1
    print(device_mode(args.key, args.conf))
    return 0


if __name__ == "__main__":
    sys.exit(main())
