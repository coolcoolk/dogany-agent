#!/usr/bin/env python3
"""
SessionStart hook: inject the last few turns of the previous session into the
new session context. Restores continuity across sessions. Called from the
__AGENT_LABEL__ workspace.

stdin(JSON): {session_id, transcript_path, cwd, source, ...}
stdout(JSON): {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": "..."}}
No output = nothing injected (no previous session / not a target).
"""
import sys, os, json, glob, re

# Defaults (used when config/agent.conf is absent, key is missing, or non-integer).
_DEFAULT_PAIRS = 2
_DEFAULT_CHAR_CAP = 500


def _load_recap_config(cwd):
    """Read RECAP_PAIRS and RECAP_CHAR_CAP from <cwd>/config/agent.conf.
    Returns (pairs, char_cap). Falls back to defaults on any error (missing
    file, missing key, non-integer) -- this runs in a hook path and must
    never crash the session start."""
    pairs = _DEFAULT_PAIRS
    char_cap = _DEFAULT_CHAR_CAP
    try:
        conf_path = os.path.join(cwd, "config", "agent.conf")
        with open(conf_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("RECAP_PAIRS="):
                    val = line.split("=", 1)[1].strip()
                    try:
                        pairs = int(val)
                    except (ValueError, TypeError):
                        pass
                elif line.startswith("RECAP_CHAR_CAP="):
                    val = line.split("=", 1)[1].strip()
                    try:
                        char_cap = int(val)
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return pairs, char_cap


def text_of(message):
    """Plain text only from a user/assistant message. Tool/noise blocks -> None."""
    c = message.get("content")
    if isinstance(c, str):
        return c.strip() or None
    if not isinstance(c, list):
        return None
    parts = []
    for b in c:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        # skip tool_use / tool_result / thinking blocks
    s = "\n".join(p for p in parts if p).strip()
    return s or None


def turns_of(path, char_cap):
    """Pull the user/assistant text turns from one transcript file, in time order."""
    turns = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") not in ("user", "assistant"):
                    continue
                m = o.get("message", {})
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                t = text_of(m)
                if not t:
                    continue
                # skip command/system noise
                low = t.lstrip()
                if low.startswith("<") and ("command-name" in t or "task-notification" in t
                                            or "system-reminder" in t):
                    continue
                # skip machine prompts (cron inject spool tail / continuous-work loop)
                if low.startswith(("[cron-inject]", "[DGN-")):
                    continue
                if len(t) > char_cap:
                    t = t[:char_cap] + " ...(truncated)"
                turns.append((role, t))
    except Exception:
        return []
    return turns


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if data.get("source") not in ("startup", "clear"):
        return

    cwd = data.get("cwd") or os.getcwd()
    pairs, char_cap = _load_recap_config(cwd)
    max_msgs = pairs * 2

    tp = data.get("transcript_path") or ""
    if tp and os.path.isfile(tp):
        proj_dir = os.path.dirname(tp)
        cur = os.path.basename(tp)
    else:
        enc = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        proj_dir = os.path.expanduser(f"~/.claude/projects/{enc}")
        cur = os.path.basename(tp) if tp else None
    if not os.path.isdir(proj_dir):
        return

    files = [f for f in glob.glob(os.path.join(proj_dir, "*.jsonl"))
             if os.path.basename(f) != cur]
    if not files:
        return

    # Walk backwards from the most recent session (mtime desc) until max_msgs is
    # filled. Each session is in time order; prepend so older sessions come first.
    files.sort(key=os.path.getmtime, reverse=True)
    turns = []
    for path in files:
        if len(turns) >= max_msgs:
            break
        turns = turns_of(path, char_cap) + turns

    if not turns:
        return
    tail = turns[-max_msgs:]

    label = {"user": "__USER_LABEL__", "assistant": "me (previous session)"}
    lines = ["[session continuity] This is the tail of the previous session's "
             "conversation. Use it as context if the work continues, but ignore "
             "it if __USER_LABEL__ raises a new topic.\n"]
    for role, t in tail:
        lines.append(f"### {label.get(role, role)}\n{t}\n")
    ctx = "\n".join(lines)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
