"""DGN-1837 r2: durable "the first-contact opener was sent" state.

Measured 2026-10-03 14:33 (rehearsal, staging 84358bf6): after the
bridge-opened opener the owner said "annyeong" and the agent sent the SAME
opener again instead of greeting back and asking Q1.  The opener rule was
keyed only on "Name still unset", which stays true for the whole identity
setup, and a fresh / resumed session has no transcript memory of the opener
-- so every pending turn looked like the opening one.

State, not prose: the Stop gate (routines/onboarding-gate-stop.py) records
the opening reply here the moment it evaluates the opening turn.  Readers:
  - onboarding-gate-stop.py Stop seat: a later turn is NOT an opening turn
    (opener checks off) and a reply that repeats the opener is blocked once;
    UserPromptSubmit seat: every owner turn after the opener gets the
    identity-setup phase note (greet back briefly, ask Q1 in that reply).
  - onboarding-check.py SessionStart: a session started after the opener gets
    the identity-setup phase line instead of the kit-pending opener terms.
The record is cleared when a new onboarding cycle arms and when onboarding
releases (gate arm()/release_if_armed()).

DGN-1849 (dec-243): on a kit-pending main the bridge sends the fixed opener
itself right after /claim (no model turn) and records it here with source
"bridge".  The claim happens while onboarding is pending, so that record
opens the current cycle: the gate's arm() keeps it (arm clears only a
model-turn record left over from an earlier cycle).  The onboarding verb
(routines/onboarding-screen.py) records the last screen it delivered in
.onboarding-screen-last (JSON: ts, step, message_id), cleared with the opener.

File: <BOT_DATA_DIR>/logs/.onboarding-opener-sent (JSON: ts, text,
boundary, q1_due), next to the gate's other sentinels -- resolve_log_dir() mirrors
the gate's _resolve_log_dir() chain; change both together.
"""
import hashlib
import json
import os
import re
import time

OPENER_SENT_FILENAME = ".onboarding-opener-sent"
SCREEN_LAST_FILENAME = ".onboarding-screen-last"
SOURCE_BRIDGE = "bridge"

# A reply this close to the recorded opener (character-bigram Jaccard on the
# letters only) is the opener again.
REPEAT_SIMILARITY = 0.5

# Self-introduction cues: a pending reply that carries one of these and asks
# nothing is the opener re-run, not identity setup.  Korean escaped to keep
# this file ASCII (RULES Code=English/ASCII).
INTRO_CUES = (
    "\uac1c\uc778 \uc5d0\uc774\uc804\ud2b8",      # gaein eijeonteu (personal agent)
    "\uac1c\uc778\uc5d0\uc774\uc804\ud2b8",
    "\uc5d0\uc774\uc804\ud2b8\uc608\uc694",       # eijeonteu-yeyo
    "\uc5d0\uc774\uc804\ud2b8\uc5d0\uc694",
    "\uc5d0\uc774\uc804\ud2b8\uc785\ub2c8\ub2e4",  # eijeonteu-imnida
    "\uc0c8\ub85c \ud0dc\uc5b4\ub09c",            # saero taeeonan (newly born)
    "\uc0c8\ub85c \uc5f0\uacb0\ub41c",            # saero yeongyeoldoen (newly connected)
    "personal agent", "newly born", "newly connected",
)

# Q1 (name ask) cues: the first owner turn after the opener must ask it.
NAME_CUES = (
    "\uc774\ub984",    # ireum (name)
    "name",
)


def _conf_get():
    """conf_reader.conf_get loaded from this lib dir by path, so a caller
    without routines/lib on sys.path (the bridge, DGN-1849) resolves the
    same BOT_DATA_DIR as the gate.  A no-op reader when it cannot load."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "conf_reader_for_fc_state",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "conf_reader.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.conf_get
    except Exception:
        def conf_get(key, conf_path=None):
            return ""
        return conf_get


def resolve_log_dir(cwd):
    """BOT_DATA_DIR from config/agent.conf + /logs, else .telegram_bot/logs
    under cwd (the onboarding gate's resolution chain)."""
    conf_get = _conf_get()
    try:
        data_dir = conf_get(
            "BOT_DATA_DIR", os.path.join(cwd, "config", "agent.conf"))
    except Exception:
        data_dir = ""
    if data_dir:
        data_dir = os.path.expanduser(data_dir)
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(cwd, data_dir)
    else:
        data_dir = os.path.join(cwd, ".telegram_bot")
    return os.path.join(data_dir, "logs")


def _path(log_dir):
    return os.path.join(log_dir, OPENER_SENT_FILENAME)


def boundary_key(text):
    """Stable short key of a turn's boundary message (which turn opened)."""
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def read_opener(log_dir):
    """The recorded opener {"ts", "text", "boundary"}, or None when the
    opener has not been sent this onboarding cycle.  A present but
    unreadable record still means "sent" (fail toward no second opener)."""
    path = _path(log_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}


def _write_json(path, rec):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def record_opener(log_dir, text, boundary="", q1_due=True, source=""):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "text": text or "", "boundary": boundary, "q1_due": q1_due}
    if source:
        rec["source"] = source
    return _write_json(_path(log_dir), rec)


def opener_from_bridge(rec):
    """True iff the record is the machine-sent opener (DGN-1849)."""
    return isinstance(rec, dict) and rec.get("source") == SOURCE_BRIDGE


def mark_q1_checked(log_dir):
    """The first owner turn after the opener was evaluated: later turns only
    get the repeat check (Q2..Q5 replies carry no name ask)."""
    rec = read_opener(log_dir)
    if not rec:
        return False
    return record_opener(log_dir, rec.get("text") or "",
                         rec.get("boundary") or "", q1_due=False,
                         source=rec.get("source") or "")


def clear_opener(log_dir):
    """Clear the opener record (and the last-screen record with it: both
    belong to one onboarding cycle)."""
    clear_screen(log_dir)
    try:
        os.remove(_path(log_dir))
        return True
    except Exception:
        return False


def record_screen(log_dir, step, message_id=None):
    """The onboarding verb delivered the screen of `step` (DGN-1849)."""
    return _write_json(os.path.join(log_dir, SCREEN_LAST_FILENAME),
                       {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "step": step, "message_id": message_id})


def read_screen(log_dir):
    """The last delivered screen {"ts", "step", "message_id"} or None."""
    try:
        with open(os.path.join(log_dir, SCREEN_LAST_FILENAME),
                  encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def clear_screen(log_dir):
    try:
        os.remove(os.path.join(log_dir, SCREEN_LAST_FILENAME))
        return True
    except Exception:
        return False


def _bigrams(text):
    letters = re.sub(r"[\W_]+", "", (text or "").lower())
    return {letters[i:i + 2] for i in range(len(letters) - 1)}


def similarity(a, b):
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / float(len(x | y))


def asks_question(text):
    return "?" in (text or "") or "\uff1f" in (text or "")


def asks_name(text):
    """A question that carries a name cue (the Q1 shape)."""
    low = (text or "").lower()
    return asks_question(text) and any(c in low for c in NAME_CUES)


def repeat_problem(reply, opener_text, q1_due=False):
    """None when a post-opener reply is identity setup, else the reason
    clause: it repeats the recorded opener, re-introduces itself and asks
    nothing, or -- the first owner turn after the opener (q1_due) -- does
    not ask Q1 (a question carrying a name cue)."""
    body = reply or ""
    if opener_text and similarity(body, opener_text) >= REPEAT_SIMILARITY:
        return "repeated your opening message"
    low = body.lower()
    if not asks_question(body) and any(c in low for c in INTRO_CUES):
        return ("introduced yourself again and asked nothing (no Q1 name "
                "ask)")
    if q1_due and not asks_name(body):
        return "did not ask Q1 (the name the user wants to give you)"
    return None
