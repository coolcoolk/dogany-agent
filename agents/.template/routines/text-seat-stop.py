#!/usr/bin/env python3
"""Stop text-layer seat adapter (v2.7 D2, dec-328b).

The seat knows the reply is final, classifies its situation
(routines/lib/text_seat.py) and, in block mode, blocks ONCE per turn with
the one matching cold rule.  Spec: docs/v27-D2-spec.md.  The template wires
it with TEXT_SEAT_MODE=detect pinned in the hook command: it lands
detect-only, and the block switch (spec section 7) is a separate decision.

Hook mode (stdin = Claude Code Stop payload):
  - Stop of the user's own session only: hook_event_name Stop, no agent_id,
    transcript session id listed in the bridge sessions file
    (lib/owner_session.py, the guard D1 and the workbench footer share).
  - No stop_hook_active exit (grill F2): when another Stop gate blocked,
    the rewritten reply is what reaches the user, so this seat reads it
    too.  It still decides once per turn: an atomic per-turn latch keyed
    by session id + the turn-opening prompt (uuid, else timestamp), taken
    only when a row would block.  This seat re-prompts at most once; the
    other gates guard their own stop_hook_active.
  - TEXT_SEAT_MODE (env, then config/agent.conf): off | detect | block.
    Default detect: the would-block row goes to the canary, nothing to
    stdout.  A locale other than a measured one (text_seat.MEASURED_LANGS)
    runs every row detect-only.
  - Switches (routines/gate-switch.py, master GATE_STOP_ALL): family
    `text-seat` and `text-seat-<row>`; G03 checks its own
    output-gate-idle-promise* slugs.  Off -> no block (block mode prints
    the off notice); the canary line still records the finding.
  - Fail-open: any error exits 0 with no stdout, and writes one
    TEXT_SEAT_SKIP reason=<...> canary line so a silent seat is visible.

Canary lines (<BOT_DATA_DIR>/logs/output-gate-canary.log):
  TS  TEXT_SEAT_BLOCK|DETECT  row=<id>  role=winner|loser  would_block=0|1
      ref=<transcript>|<message id>  [latch=held] [switch=<slug>]  [excerpt]
  TS  TEXT_SEAT_RAW  row=<id>  role=raw  would_block=0  guard=<name>  ...
  TS  TEXT_SEAT_SKIP  reason=<stdin|guard|transcript|vocab:<lang>|row:<id>|
      latch|error:<type>>  [ref=...]

Replay mode (measurement, never blocks):
  text-seat-stop.py --replay [--all-rows] [--post-block] <transcript.jsonl>...
  prints one JSON line per firing turn and a per-row count summary on
  stderr.  It scores the PRE-block reply of a turn another Stop gate
  re-prompted (the reply this seat sees first); --post-block scores the
  rewrite instead.
"""
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))

_CANARY = "output-gate-canary.log"
_LATCH_DIR = "text-seat-latch"
_LATCH_KEEP_S = 3 * 86400
_LATCH_PRUNE_AT = 500
# Claude Code records a Stop hook's block reason as an isMeta user entry.
_STOP_FEEDBACK = "Stop hook feedback"
# Tail read (grill F13): never parse more than this much of a transcript.
_TAIL_CHUNK = 256 * 1024
_TAIL_CAP = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Transcript -> Turn (one parser for hook and replay)
# ---------------------------------------------------------------------------

def _injected(prompt):
    """A prompt opened by the machine, not typed by the user: the
    owner-turn resolver's one definition (why_owner_turn.is_injected,
    DGN-1967 step 3), loaded on first use."""
    import why_owner_turn
    return why_owner_turn.is_injected(prompt)


def _prompt_text(msg):
    """Text of a genuine user prompt, or None for tool feedback / empty."""
    c = msg.get("content")
    if isinstance(c, str):
        return c if c.strip() else None
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result"
               for b in c):
            return None
        t = "\n".join(b.get("text") or "" for b in c
                      if isinstance(b, dict) and b.get("type") == "text")
        return t if t.strip() else None
    return None


def _prompt_of(e):
    """Prompt text when entry `e` opens a turn, else None."""
    if not isinstance(e, dict) or e.get("type") != "user" \
            or e.get("isSidechain") or e.get("isMeta"):
        return None
    return _prompt_text(e.get("message") or {})


def _is_stop_feedback(e):
    if not isinstance(e, dict) or e.get("type") != "user" \
            or not e.get("isMeta"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list):
        c = "\n".join(b.get("text") or "" for b in c if isinstance(b, dict))
    return isinstance(c, str) and _STOP_FEEDBACK in c


def _iter_entries(entries, pre_block=False):
    """Yield (Turn, final message id, prompt timestamp, prompt uuid) for
    every turn in `entries` (parsed transcript lines, in order).

    The final message = the last assistant message group (by message.id)
    that carries text; the earlier ones are the Turn's interim messages.
    pre_block: when another Stop gate re-prompted the turn (an isMeta
    "Stop hook feedback" entry), score the reply as it stood at the first
    block -- the reply the seat saw first -- with the tools up to there."""
    from text_seat import HISTORY_PROMPTS, Tool, Turn
    cur = None
    typed = []  # earlier prompts the user typed (not machine-injected)

    def _close(t):
        if not t:
            return None
        msgs, tools = t["msgs"], t["tools"]
        if pre_block and t["cut"] and t["cut"][0] > 0:
            msgs, tools = msgs[:t["cut"][0]], tools[:t["cut"][1]]
        if not msgs:
            return None
        mid, parts = msgs[-1]
        interim = tuple((m, "\n".join(p)) for m, p in msgs[:-1])
        return (Turn("\n".join(parts), t["prompt"], tools,
                     _injected(t["prompt"]), t["history"],
                     interim, mid), mid, t["ts"], t["uuid"])

    for e in entries:
        et = e.get("type") if isinstance(e, dict) else None
        if et not in ("user", "assistant") or e.get("isSidechain"):
            continue
        if et == "user":
            if _is_stop_feedback(e):
                if cur and cur["cut"] is None:
                    cur["cut"] = (len(cur["msgs"]), len(cur["tools"]))
                continue
            p = _prompt_of(e)
            if p is None:
                continue
            done = _close(cur)
            if done:
                yield done
            if cur and not _injected(cur["prompt"]):
                typed.insert(0, cur["prompt"])
                del typed[HISTORY_PROMPTS:]
            cur = {"prompt": p, "tools": [], "msgs": [], "cut": None,
                   "ts": e.get("timestamp"), "uuid": e.get("uuid"),
                   "history": list(typed)}
            continue
        if cur is None:
            continue
        msg = e.get("message") or {}
        mid = msg.get("id")
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) \
                    else {}
                path_ = (inp.get("file_path") or inp.get("path")
                         or inp.get("notebook_path") or "")
                cur["tools"].append(Tool(b.get("name"), inp.get("command"),
                                         path_, inp.get("run_in_background")
                                         is True))
            elif b.get("type") == "text" and (b.get("text") or "").strip():
                if cur["msgs"] and cur["msgs"][-1][0] == mid:
                    cur["msgs"][-1][1].append(b["text"])
                else:
                    cur["msgs"].append([mid, [b["text"]]])
    done = _close(cur)
    if done:
        yield done


def _parse(raw):
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def iter_turns(path, pre_block=False):
    """Every turn of a whole transcript (replay / measurement)."""
    def _entries():
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                e = _parse(raw)
                if e is not None:
                    yield e
    return _iter_entries(_entries(), pre_block)


def tail_entries(path, chunk=_TAIL_CHUNK, cap=_TAIL_CAP):
    """Parsed entries from the end of `path` back to the turn-opening
    prompt plus HISTORY_PROMPTS earlier typed prompts (the HF48b register
    guard), or to the start of the file / `cap` bytes."""
    from text_seat import HISTORY_PROMPTS
    entries, prompts = [], []  # prompts: newest first
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        pos, buf, read = fh.tell(), b"", 0
        while pos > 0 and read < cap:
            n = min(chunk, pos)
            pos -= n
            read += n
            fh.seek(pos)
            buf = fh.read(n) + buf
            lines = buf.split(b"\n")
            buf = lines[0] if pos > 0 else b""
            new = [_parse(ln.decode("utf-8", "replace"))
                   for ln in (lines[1:] if pos > 0 else lines)]
            new = [e for e in new if e is not None]
            entries = new + entries
            prompts = [p for p in (_prompt_of(e) for e in reversed(new))
                       if p is not None] + prompts
            typed = [p for p in prompts[1:] if not _injected(p)]
            if prompts and len(typed) >= HISTORY_PROMPTS:
                break
    return entries


def current_turn(path):
    """The last turn of the transcript (one tail parse shared by every
    row), or None when there is no turn boundary in reach."""
    last = None
    for t in _iter_entries(tail_entries(path)):
        last = t
    return last


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _conf_paths(cwd):
    return [os.path.join(cwd, "config", "agent.conf"),
            os.path.join(ROOT, "config", "agent.conf")]


# Knobs read from env only under DOGANY_TEST_MODE=1 (as lib/device_gate.py
# honours its seams); in production they come from agent.conf alone.
TEST_ONLY_ENV = ("TEXT_SEAT_G03", "TEXT_SEAT_DELEGATE_TERMS",
                 "TEXT_SEAT_DISPATCH_NOUNS")


def test_mode():
    """Unlike device_gate.test_mode, CLAUDECODE is not checked: a hook
    always runs under Claude Code, and its env comes from the CLI's
    settings, not from the agent's Bash call."""
    return os.environ.get("DOGANY_TEST_MODE") == "1"


def _conf_opt(cwd, key):
    """Value of `key` (env, then the first agent.conf that sets it), or
    None when unset -- so an explicitly empty knob is told apart from an
    absent one (grill F7: an empty TEXT_SEAT_DETECT_ROWS really
    disables).  TEST_ONLY_ENV knobs skip env outside test mode."""
    v = os.environ.get(key) if key not in TEST_ONLY_ENV or test_mode() \
        else None
    if v is not None:
        return v.strip()
    for path in _conf_paths(cwd):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1].strip().strip('"') \
                            .strip("'")
        except OSError:
            continue
    return None


def _conf(cwd, key):
    return _conf_opt(cwd, key) or ""


def _split(v):
    return [p.strip() for p in (v or "").split(",") if p.strip()]


def _rows_knob(cwd, key):
    v = _conf_opt(cwd, key)
    return None if v is None else _split(v)


def locale(cwd):
    lang = (_conf(cwd, "AGENT_LANG") or "en").strip().lower()
    return re.split(r"[-_.]", lang, maxsplit=1)[0] or "en"


def load_config(cwd):
    from text_seat import Config
    owner = []
    try:
        from voice_vocab import owner_terms
        owner = owner_terms(cwd)
    except Exception:
        pass
    return Config(
        rows=_rows_knob(cwd, "TEXT_SEAT_ROWS"),
        owner_terms=owner,
        self_session_terms=_split(_conf(cwd, "TEXT_SEAT_SELF_SESSION_TERMS")),
        auth_commands=_split(_conf(cwd, "TEXT_SEAT_AUTH_COMMANDS")),
        decision_ledger=_split(_conf(cwd, "TEXT_SEAT_DECISION_LEDGER")),
        technical_user=_conf(cwd, "TEXT_SEAT_TECHNICAL_USER") in ("1", "yes",
                                                                  "true"),
        detect_rows=_rows_knob(cwd, "TEXT_SEAT_DETECT_ROWS"),
    )


def _load_path(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def g03_finding(tp, cwd, cfg, turn):
    """IDLE_PROMISE (G03) as the seat's first row, through the shipped
    output-gate verdict with the generic vocabulary overlay
    (lib/text_seat_g03.py; Korean only).  Opt-in (TEXT_SEAT_G03=1 in
    agent.conf) while output-gate-stop.py still owns G03 on its own Stop
    seat.  DGN-1967 step 8 decision: it stays, off by default, until detect
    data shows whether the seat copy adds anything over output-gate-stop;
    if it does not, delete this function and lib/text_seat_g03.py.  Reads the
    seat's own parse of the turn; only the cross-turn armed lookback scans
    the transcript again.  Returns (Finding, the G03 switch slugs) or
    (None, ()).  Raises on error (the caller logs row:G03)."""
    import text_seat_g03 as G
    from text_seat import Finding
    ogs = _load_path("output_gate_stop", os.path.join(HERE,
                                                      "output-gate-stop.py"))
    import output_gate as og
    G.apply_vocab(og, G.Vocab(
        cfg.owner_terms,
        _split(_conf(cwd, "TEXT_SEAT_DELEGATE_TERMS")),
        _split(_conf(cwd, "TEXT_SEAT_DISPATCH_NOUNS"))))
    if turn.tools is None:
        return None, ()
    blob = G.tool_blob(turn.tools, ogs._strip_heredocs)
    armed = ogs._armed_earlier(tp, og.idle_promise_armed_kinds,
                               og.IDLE_PROMISE_ARMED_LOOKBACK_S, with_tickets=True)
    v = og.idle_promise_block_verdict(turn.reply, blob, armed)
    if not v:
        return None, ()
    slugs = (ogs._GATE_SLUG, ogs._SHAPE_GATE_SLUGS.get(v[0], ""))
    return Finding("G03", "kind=%s" % v[0], v[1]), tuple(s for s in slugs
                                                         if s)


# ---------------------------------------------------------------------------
# Hook plumbing
# ---------------------------------------------------------------------------

def _data_dir(cwd):
    env = os.environ.get("DOGANY_TEXT_SEAT_DIR") if test_mode() else None
    if env:
        return env
    v = _conf(cwd, "BOT_DATA_DIR")
    return v or os.path.join(cwd, ".telegram_bot")


def _canary(cwd, kind, fields, excerpt=None):
    try:
        d = os.path.join(_data_dir(cwd), "logs")
        os.makedirs(d, exist_ok=True)
        line = "%s  TEXT_SEAT_%s  %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), kind,
            "  ".join("%s=%s" % kv for kv in fields))
        if excerpt is not None:
            line += "  [%s]" % " ".join(excerpt.split())[:120]
        with open(os.path.join(d, _CANARY), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _skip(cwd, reason, ref=None):
    _canary(cwd, "SKIP", [("reason", reason)] + ([("ref", ref)] if ref
                                                   else []))


def _latch_take(cwd, key):
    """"taken" the first time `key` (session + turn) is claimed, "held"
    after; "error" when the latch cannot be written (the caller does not
    block).  One O_CREAT|O_EXCL file per key: atomic across concurrent
    Stops and bridge users, no read-modify-write."""
    try:
        d = os.path.join(_data_dir(cwd), "state", _LATCH_DIR)
        os.makedirs(d, exist_ok=True)
        name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
        try:
            fd = os.open(os.path.join(d, name),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return "held"
        with os.fdopen(fd, "w") as fh:
            fh.write(key + "\n")
        _latch_prune(d)
        return "taken"
    except Exception:
        return "error"


def _latch_prune(d):
    try:
        names = os.listdir(d)
        if len(names) < _LATCH_PRUNE_AT:
            return
        cutoff = time.time() - _LATCH_KEEP_S
        for n in names:
            p = os.path.join(d, n)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except Exception:
        pass


def _gate_switch():
    """routines/gate-switch.py, or None (absent/broken -> armed)."""
    try:
        return _load_path("gate_switch", os.path.join(HERE, "gate-switch.py"))
    except Exception:
        return None


def _switched_off(gs, slugs, cwd):
    if gs is None:
        return None
    try:
        for slug in slugs:
            if not gs.is_enabled(slug, cwd):
                return slug
    except Exception:
        pass  # a broken switch never disarms the seat
    return None


def hook_main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = None
    if not isinstance(data, dict):
        _skip(os.getcwd(), "stdin")
        return 0
    if data.get("hook_event_name") not in (None, "Stop"):
        return 0
    cwd = str(data.get("cwd") or os.getcwd())
    try:
        return _decide(data, cwd)
    except Exception as e:
        _skip(cwd, "error:%s" % e.__class__.__name__)
        return 0


def _decide(data, cwd):
    mode = (_conf(cwd, "TEXT_SEAT_MODE") or "detect").lower()
    if mode not in ("detect", "block"):
        return 0
    try:
        import owner_session
        who = owner_session.hook_status(data, ROOT)
    except Exception:
        who = "error"
    if who == "error":
        _skip(cwd, "guard")
        return 0
    if who != "owner":
        return 0
    tp = data.get("transcript_path") or ""
    try:
        got = current_turn(tp) if tp and os.path.isfile(tp) else None
    except Exception:
        got = None
    if not got:
        _skip(cwd, "transcript")
        return 0
    turn, mid, ts, uuid = got
    ref = "%s|%s" % (os.path.basename(tp), mid or "-")
    import text_seat as T
    for lang, _why in T.VOCAB_ERRORS:
        _skip(cwd, "vocab:%s" % lang, ref)
    cfg = load_config(cwd)
    errors = []
    found = T.classify_all(turn, cfg, rows=set(cfg.rows) | set(cfg.detect_rows),
                           raw=True, errors=errors)
    slugs = {}
    if _conf(cwd, "TEXT_SEAT_G03") in ("1", "yes", "true"):
        try:
            g, g_slugs = g03_finding(tp, cwd, cfg, turn)
        except Exception:
            g, g_slugs = None, ()
            errors.append("G03")
        if g:
            found.insert(0, g)
            slugs["G03"] = g_slugs
    for row in errors:
        _skip(cwd, "row:%s" % row, ref)
    measured = locale(cwd) in T.MEASURED_LANGS
    hits = [f for f in found if f.guard is None]
    eligible = [f for f in hits if measured
                and (f.row == "G03" or f.row in cfg.rows)]
    winner = eligible[0] if eligible else None
    gs, off, would_block, note = None, None, 0, []
    if winner is not None:
        gs = _gate_switch()
        off = _switched_off(gs, slugs.get(winner.row) or (
            "text-seat", "text-seat-" + winner.row.lower()), cwd)
        if off:
            note.append(("switch", off))
        else:
            sess = data.get("session_id") or os.path.basename(tp)
            turn_key = uuid or ts
            got_latch = _latch_take(cwd, "%s|%s" % (sess, turn_key)) \
                if turn_key else "error"
            if got_latch == "taken":
                would_block = 1
            elif got_latch == "held":
                note.append(("latch", "held"))
            else:
                _skip(cwd, "latch", ref)

    def _ref(f):
        return "%s|%s" % (os.path.basename(tp), f.mid or mid or "-")

    # The winner's line is written only after the latch attempt (grill F1).
    if winner is not None:
        kind = "BLOCK" if mode == "block" and would_block else "DETECT"
        _canary(cwd, kind, [("row", winner.row), ("role", "winner"),
                            ("would_block", would_block),
                            ("ref", _ref(winner))] + note, winner.excerpt)
    for f in found:
        if f is winner:
            continue
        if f.guard is None:
            _canary(cwd, "DETECT", [("row", f.row), ("role", "loser"),
                                    ("would_block", 0), ("ref", _ref(f))],
                    f.excerpt)
        else:
            _canary(cwd, "RAW", [("row", f.row), ("role", "raw"),
                                 ("would_block", 0), ("guard", f.guard),
                                 ("ref", _ref(f))], f.excerpt)
    if mode != "block" or winner is None:
        return 0
    if would_block:
        print(json.dumps({"decision": "block", "reason": winner.message},
                         ensure_ascii=False), flush=True)
    elif off and gs is not None:
        try:
            print(json.dumps(gs.off_notice(off, winner.message, cwd),
                             ensure_ascii=False), flush=True)
        except Exception:
            pass
    return 0


def replay_main(argv):
    from text_seat import classify, classify_all
    all_rows = "--all-rows" in argv
    pre_block = "--post-block" not in argv
    paths = [a for a in argv if not a.startswith("--")]
    cfg = load_config(os.getcwd())
    counts, turns, seen = {}, 0, set()
    for p in paths:
        try:
            it = list(iter_turns(p, pre_block=pre_block))
        except Exception:
            continue
        for turn, mid, ts, _uuid in it:
            key = mid or (p, ts)
            if key in seen:
                continue  # resumed sessions repeat history
            seen.add(key)
            turns += 1
            found = classify_all(turn, cfg) if all_rows else \
                [f for f in [classify(turn, cfg)] if f]
            for f in found:
                counts[f.row] = counts.get(f.row, 0) + 1
                print(json.dumps({"row": f.row, "file": os.path.basename(p),
                                  "ts": ts, "mid": f.mid or mid,
                                  "injected": turn.injected,
                                  "excerpt": f.excerpt,
                                  "prompt": turn.prompt[:200],
                                  "reply": turn.reply},
                                 ensure_ascii=False))
    sys.stderr.write("turns=%d %s\n" % (turns, json.dumps(counts)))
    return 0


def main():
    return  # Retired answer-end rewrite seat.
    if len(sys.argv) > 1 and sys.argv[1] == "--replay":
        return replay_main(sys.argv[2:])
    try:
        return hook_main()
    except BaseException:
        return 0


if __name__ == "__main__":
    sys.exit(main())
