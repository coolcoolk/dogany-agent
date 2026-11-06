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
import sys, os, json, glob, re, datetime

# Single conf parser (DGN-916): quote/whitespace normalization lives in
# routines/lib/conf_reader.py only.  Guarded import keeps the SessionStart
# hook fail-open contract even if the lib file is ever missing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from conf_reader import conf_get
except Exception:
    def conf_get(key, conf_path=None):
        return ""

# Defaults (used when config/agent.conf is absent, key is missing, or non-integer).
# RECAP_CHAR_CAP controls chars per half: first N chars + last N chars per message.
_DEFAULT_PAIRS = 2
_DEFAULT_CHAR_CAP = 200

# Substrings that mark injected framework/system context (not real conversation).
# A turn whose text carries any of these is a prior injection that leaked into a
# turn -- e.g. a SessionStart continuity block or UserPromptSubmit context that a
# reply echoed verbatim. Re-injecting it would compound the leak session over
# session (self-perpetuation), so such turns are dropped from the recap.
_INJECTION_MARKERS = (
    "hook additional context",
    "[session continuity]",
    "hookSpecificOutput",
    "additionalContext",
    "[기억규칙]",  # recall-block signature (pure machine string; 0-FP over full scan)
)


def _load_recap_config(cwd):
    """Read RECAP_PAIRS and RECAP_CHAR_CAP from <cwd>/config/agent.conf.
    Returns (pairs, char_cap). Falls back to defaults on any error (missing
    file, missing key, non-integer) -- this runs in a hook path and must
    never crash the session start."""
    pairs = _DEFAULT_PAIRS
    char_cap = _DEFAULT_CHAR_CAP
    conf_path = os.path.join(cwd, "config", "agent.conf")
    # Single conf parser (DGN-916); int() post-processing stays here.
    try:
        pairs = int(conf_get("RECAP_PAIRS", conf_path))
    except (ValueError, TypeError):
        pass
    try:
        char_cap = int(conf_get("RECAP_CHAR_CAP", conf_path))
    except (ValueError, TypeError):
        pass
    return pairs, char_cap


# Keep the old import surface for recap callers and tests.
from owner_term import owner_term as _owner_term


def _log_recap_size(cwd, ctx):
    """Append one size-log entry to .telegram_bot/logs/session-recap-size.log.
    Format: YYYY-MM-DD HH:MM:SS <chars> <bytes>  (v2.7 T1: bytes column
    added; readers that take field 3 keep working).
    Silent on any error -- hook path must never crash."""
    try:
        log_dir = os.path.join(cwd, ".telegram_bot", "logs")
        if not os.path.isdir(log_dir):
            return
        log_path = os.path.join(log_dir, "session-recap-size.log")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {len(ctx)} {len(ctx.encode('utf-8'))}\n")
    except Exception:
        pass


# v2.7 T1 (LOCKED spec 5.1): the whole injected block is capped at the
# measured p50 (1,353 chars). Order: Why head -> corrections -> raw tail; the
# tail only fills what the head and corrections leave, they are never cut
# for it. Head and corrections ride only when WHY_NET=on.
BLOCK_CAP_CHARS = 1353


def _why_net_on(cwd):
    try:
        v = conf_get("WHY_NET", os.path.join(cwd, "config", "agent.conf"))
    except Exception:
        return False
    return (v or "").strip().lower() == "on"


def _why_sections(cwd, prev_path):
    """(head_lines, correction_lines) for the previous session, or ([], [])
    when the Why net is off or anything fails (fail-open)."""
    if not _why_net_on(cwd):
        return [], []
    try:
        from why_net import surfaces
        since = surfaces.session_start_ts(prev_path) if prev_path else None
        paths = surfaces.touched_tickets(prev_path) if prev_path else []
        head = surfaces.head_lines(cwd, BLOCK_CAP_CHARS, paths, since)
        corr = surfaces.correction_lines(
            surfaces.recent_corrections(cwd, since))
        return head, corr
    except Exception:
        return [], []


def _reach_line(cwd):
    """DGN-1390 (c): one count line of owner quotes not reached to code in
    <cwd>/worklog, or "" (OWNER_REACH off, nothing unmet, any failure)."""
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "owner-reach.py")
        spec = importlib.util.spec_from_file_location("owner_reach_cli", path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        if cli.mode(cwd) == "off":
            return ""
        text, _ = cli.count_line(os.path.join(cwd, "worklog"))
        return text
    except Exception:
        return ""


def _fit_tail(tail, label, room):
    """Newest turns that fit in `room` chars, oldest-first. The oldest kept
    turn may be cut short; a turn that cannot keep 40 chars is dropped."""
    kept = []
    for role, t in reversed(tail):
        piece = f"### {label.get(role, role)}\n{t}\n"
        cost = len(piece) + 1
        if cost <= room:
            kept.append(piece)
            room -= cost
            continue
        head = f"### {label.get(role, role)}\n"
        keep = room - len(head) - len(" ...(cut)\n") - 1
        if keep >= 40:
            kept.append(f"{head}{t[:keep]} ...(cut)\n")
        break
    return list(reversed(kept))


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


# Degenerate-repetition collapse (DGN-951). A flooded turn -- generation that
# collapsed into one short token repeated many times ("course course course ...")
# -- is NOT an injection marker, so it survives _INJECTION_MARKERS and would be
# re-injected as the previous-session tail, re-arming the trigger next session.
# Collapse the repeated run to a short marker so the tail carries the fact of the
# turn without its trigger. Exact-match, >= 5 copies: 5+ identical back-to-back
# runs are effectively never natural prose, so false positives stay ~0.
_MIN_RUN = 5
# 5+ whitespace-separated identical tokens in a row (token <= 60 chars).
_TOKEN_RUN = re.compile(r"(\S{1,60})(?:\s+\1){" + str(_MIN_RUN - 1) + r",}")
# 5+ contiguous copies of a short unit with no separator ("coursecourse...").
# Guarded: unit must hold a word char (spares ===/---/... separator lines).
_SUBSTR_RUN = re.compile(r"((?=\S*\w)\S{1,16}?)\1{" + str(_MIN_RUN - 1) + r",}")


def _collapse_run(match):
    unit = match.group(1)
    span = match.group(0)
    # token-level span is space-joined identical tokens; count them.
    n = span.count(unit) if unit else 0
    return f"{unit} {unit} {unit} ...(x{n} repetition collapsed)..."


def collapse_repetition(text):
    """Neutralize degenerate repetition in one turn's text (DGN-951).
    Token-level first (the observed flood shape), then a guarded substring-level
    pass for the no-whitespace case. Returns text unchanged when no run hits the
    threshold -- normal conversation is untouched."""
    try:
        text = _TOKEN_RUN.sub(_collapse_run, text)
        # substring pass: only worth it on a long unbroken run (>= 60 chars),
        # which further shields separators / short natural repeats.
        text = _SUBSTR_RUN.sub(
            lambda m: (f"{m.group(1)} ...(repetition collapsed)..."
                       if len(m.group(0)) >= 60 else m.group(0)),
            text,
        )
    except Exception:
        return text
    return text


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
                # drop turns carrying injected framework context anywhere in the
                # text -- stops the continuity block from self-perpetuating when a
                # prior reply leaked it into its output (DGN-862).
                if any(mark in t for mark in _INJECTION_MARKERS):
                    continue
                # collapse degenerate repetition BEFORE the char-cap trim, so a
                # flooded turn's trigger is gone rather than head/tail-sampled
                # straight back into the next session (DGN-951).
                t = collapse_repetition(t)
                if len(t) > char_cap * 2:
                    t = t[:char_cap] + " ...(middle trimmed)... " + t[-char_cap:]
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

    head, corr = _why_sections(cwd, files[0])
    reach = _reach_line(cwd)
    if not turns and not head and not corr and not reach:
        return
    tail = turns[-max_msgs:]

    owner_term = _owner_term(cwd)
    label = {"user": owner_term, "assistant": "me (previous session)"}
    # v2.7 T1: header <= 140 chars (was 318) to free room under the cap.
    lines = [f"[session continuity] Previous-session context; drop it if "
             f"{owner_term} starts a new topic. Background only: never quote "
             "it.\n"]
    if head:
        lines.append("### Why (current work)\n" + "\n".join(head) + "\n")
    if corr:
        lines.append("### corrections (verbatim)\n" + "\n".join(corr) + "\n")
    if reach:
        lines.append(reach + " -- owner-reach.py check <ticket>\n")
    room = BLOCK_CAP_CHARS - len("\n".join(lines)) - 1
    lines.extend(_fit_tail(tail, label, room))
    ctx = "\n".join(lines)
    _log_recap_size(cwd, ctx)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
