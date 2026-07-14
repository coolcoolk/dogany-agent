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

PAIRS = 4          # number of trailing user/assistant pairs from the last session
MAX_MSGS = PAIRS * 2
CHAR_CAP = 1000    # per-message length cap


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


def turns_of(path):
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
                if len(t) > CHAR_CAP:
                    t = t[:CHAR_CAP] + " ...(truncated)"
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

    tp = data.get("transcript_path") or ""
    if tp and os.path.isfile(tp):
        proj_dir = os.path.dirname(tp)
        cur = os.path.basename(tp)
    else:
        cwd = data.get("cwd") or os.getcwd()
        enc = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        proj_dir = os.path.expanduser(f"~/.claude/projects/{enc}")
        cur = os.path.basename(tp) if tp else None
    if not os.path.isdir(proj_dir):
        return

    files = [f for f in glob.glob(os.path.join(proj_dir, "*.jsonl"))
             if os.path.basename(f) != cur]
    if not files:
        return

    # Walk backwards from the most recent session (mtime desc) until MAX_MSGS is
    # filled. Each session is in time order; prepend so older sessions come first.
    files.sort(key=os.path.getmtime, reverse=True)
    turns = []
    for path in files:
        if len(turns) >= MAX_MSGS:
            break
        turns = turns_of(path) + turns

    if not turns:
        return
    tail = turns[-MAX_MSGS:]

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
