"""Forcing point for a stalled dispatch-return turn (DGN-1715 round 2).

The result-first gate (routines/dispatch-return-result-first-gate.py) denies
follow-up work until the owner has seen the result line.  A model that is
denied can still end the turn on "I will do it next conversation" -- the
follow-up then sits idle until the owner asks.  The owner's rule: a turn that
cannot continue its follow-up must END on a one-line proposal of the next
step plus an [[OPTIONS]] marker (proceed / hold), so the owner's tap starts
the next turn.

Two seats share this module:
  * the PreToolUse gate records a denial (``record_denial``) and clears it
    when a later tool passes the open latch (``clear_denial``: the follow-up
    resumed, nothing stalled);
  * the Stop seat (``stop_verdict``, called from onboarding-gate-stop.py --
    the shipped Stop/UserPromptSubmit hook that already owns the MISS_VISIBLE
    miss sentinel) checks the final message for the marker.

The marker test recognizes every marker SHAPE, never wording, so it is
language-agnostic.  All functions fail open.

DGN-1732: when the bridge records that the delivered first text already
carried the keyboard (``options_delivered`` on the latch row), the owner's
tap is already available, so the Stop seat passes instead of asking for a
second proposal + keyboard (the duplicate-bubble incident, 2026-09-26).

DGN-1715 r3: the marker test used to be a bare substring check
(``OPTIONS_MARKER in text``), which missed the LABELED shape entirely --
"[[OPTIONS]]" is never a substring of "[[OPTIONS: a | b]]" (DGN-992). A
dispatch-return final that ended on "...[[OPTIONS: 진행 | 보류]]" therefore
read as marker-less: the Stop seat forced a second menu on top of the one the
owner could already tap (measured 2026-09-27 06:22, dev instance on dev.56, owner:
"notification comes twice"). ``has_options_marker`` now recognizes every
marker shape the bridge arms a keyboard from -- bare or labeled, standalone
line, outside a fenced code block -- mirroring bridge/formatting.py's
``is_options_marker_line`` (plus bridge/options.py's fenced-code exclusion)
byte for byte. This stays a MIRROR, not an import: this module must load
stdlib-only, outside the bridge venv (the CLI hook runner has no
python-telegram-bot). routines/tests/test-dgn1715-options-marker-shapes.py
asserts the mirror never drifts from the bridge original over a shared
corpus.
"""
import json
import os
import re
import time

CONTEXT_FILENAME = "dispatch-return-context.json"
DENIED_FILENAME = "dispatch-return-denied.json"
MAX_AGE_SECONDS = 3600
OPTIONS_MARKER = "[[OPTIONS]]"
# Mirrors bridge/formatting.py OPTIONS_LABELED_RE (DGN-992 labeled marker
# shape) and bridge/options.py _FENCED_CODE_RE (fenced-code exclusion).
_OPTIONS_LABELED_RE = re.compile(r"^\[\[OPTIONS:(.*)\]\]$")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)

# Model-facing wording shared by the gate deny reason and the Stop block.
PROPOSAL_INSTRUCTION = (
    "If you cannot continue the follow-up work in this turn, do not promise "
    "it for a later conversation. END this turn with a one-line proposal of "
    "the next step followed by the " + OPTIONS_MARKER + " marker with "
    "proceed / hold choices, so the owner's tap starts the next turn. If the "
    "result line you send already carries that proposal and marker, it is "
    "the owner's menu: end the turn there and never repeat it."
)

STOP_BLOCK_REASON = (
    "Dispatch-return forcing (DGN-1715): follow-up work was denied in this "
    "turn and the final message carries no " + OPTIONS_MARKER + " marker, so "
    "nothing will start the follow-up. " + PROPOSAL_INSTRUCTION + " Reply "
    "with only that one-line proposal and the marker."
)

MISS_REMINDER = (
    "Dispatch-return forcing (DGN-1715): your PREVIOUS dispatch-return turn "
    "ended with follow-up work still pending and no " + OPTIONS_MARKER +
    " proposal. Do not apologize or repeat the result; if that follow-up is "
    "still open, offer it now as a one-line proposal with " + OPTIONS_MARKER +
    " (proceed / hold)."
)


def context_path(cwd):
    override = os.environ.get("DISPATCH_RETURN_CONTEXT_PATH", "").strip()
    if override:
        return override
    current = os.path.abspath(cwd)
    while True:
        candidate = os.path.join(current, ".telegram_bot", CONTEXT_FILENAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return candidate
        current = parent


def _denied_path(ctx_path):
    return os.path.join(os.path.dirname(ctx_path), DENIED_FILENAME)


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            row = json.load(fh)
        return row if isinstance(row, dict) else None
    except Exception:
        return None


def read_row(ctx_path):
    """The live latch row, or None when absent / malformed / expired / a
    self-cancel recovery (the DGN-1688 exemption)."""
    row = _load(ctx_path)
    if not row or row.get("schema") != 1 or row.get("cancel_by") == "self":
        return None
    if not isinstance(row.get("turn_id"), str) or not row["turn_id"]:
        return None
    ts = row.get("ts")
    if not isinstance(ts, (int, float)) or time.time() - ts > MAX_AGE_SECONDS:
        return None
    return row


def owns_row(row, data):
    """DGN-1738: the latch belongs to the one session that runs the
    dispatch-return turn.  The context file is instance-wide, so a detached
    worker (or any other session) shares it; only a hook payload whose
    session_id equals the record's is the owner.  Mismatch or a missing id
    on either side is not the owner (fail open)."""
    try:
        want = row.get("session_id")
        got = data.get("session_id")
        return isinstance(want, str) and bool(want) and got == want
    except Exception:
        return False


def record_denial(ctx_path, turn_id):
    try:
        prior = _load(_denied_path(ctx_path)) or {}
        count = prior.get("count", 0) if prior.get("turn_id") == turn_id else 0
        payload = {"turn_id": turn_id, "count": int(count) + 1, "ts": time.time()}
        path = _denied_path(ctx_path)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def clear_denial(ctx_path, turn_id=None):
    try:
        path = _denied_path(ctx_path)
        rec = _load(path)
        if rec is not None and (turn_id is None or rec.get("turn_id") == turn_id):
            os.remove(path)
    except Exception:
        pass


def is_options_marker_line(line):
    """Mirrors bridge/formatting.py is_options_marker_line: True when the
    stripped line is the bare marker exactly, or a labeled marker
    "[[OPTIONS: a | b]]" (DGN-992). Exposed (not underscored) so sibling hooks
    that compare a single line against the literal (onboarding-gate-stop.py)
    can share this one predicate instead of re-deriving it."""
    stripped = line.strip()
    return stripped == OPTIONS_MARKER or _OPTIONS_LABELED_RE.match(stripped) is not None


def has_options_marker(text):
    """True iff `text` carries a standalone bare or labeled [[OPTIONS]] marker
    line outside a fenced code block -- every shape that arms the owner's
    keyboard (bridge/formatting.py is_options_marker_line), not just the bare
    substring (DGN-1715 r3: the substring check missed the labeled shape)."""
    if not isinstance(text, str) or not text:
        return False
    spans = [m.span() for m in _FENCED_CODE_RE.finditer(text)]
    offset = 0
    for line in text.split("\n"):
        start, end = offset, offset + len(line)
        offset = end + 1
        if any(s <= start and end <= e for s, e in spans):
            continue
        if is_options_marker_line(line):
            return True
    return False


def stop_verdict(data, cwd, turn_text=""):
    """Verdict for a Stop event, or None when this seat has nothing to say.

    "PASS"          the final message carries the marker, or the delivered
                    first text already did (DGN-1732 options_delivered).
    "BLOCK"         denied follow-up, no marker, the final message has not
                    reached the owner: re-prompt once with STOP_BLOCK_REASON.
    "MISS_VISIBLE"  same, but the final message is already on the owner's
                    screen (the turn's only text, delivered): a block would
                    duplicate it.  The caller logs and leaves a reminder.
    "YIELD"         already re-prompted once (stop_hook_active) and still no
                    marker: never loop.
    ``turn_text`` is every owner-visible text of the turn (may be "").
    """
    ctx = context_path(cwd)
    row = read_row(ctx)
    if row is None or not owns_row(row, data):
        return None
    denial = _load(_denied_path(ctx))
    if not denial or denial.get("turn_id") != row["turn_id"]:
        return None
    final = data.get("last_assistant_message")
    final = final if isinstance(final, str) else ""
    if row.get("options_delivered") is True:
        clear_denial(ctx)
        return "PASS"
    if has_options_marker(final) or (not final.strip() and has_options_marker(turn_text)):
        clear_denial(ctx)
        return "PASS"
    if data.get("stop_hook_active"):
        clear_denial(ctx)
        return "YIELD"
    delivered = row.get("visible") is True or row.get("delivering") is True
    only_text = bool(final.strip()) and turn_text.strip() == final.strip()
    if delivered and only_text:
        clear_denial(ctx)
        return "MISS_VISIBLE"
    return "BLOCK"


def _miss_path(state_dir):
    return os.path.join(state_dir, ".dispatch-return-forcing-miss")


def record_miss(state_dir):
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(_miss_path(state_dir), "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
    except Exception:
        pass


def consume_miss(state_dir):
    try:
        path = _miss_path(state_dir)
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False
