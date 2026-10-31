"""Forcing point for a stalled dispatch-return turn (DGN-1715 round 2).

The result-first gate (routines/dispatch-return-result-first-gate.py) denies
follow-up work until the owner has seen the result line.  A model that is
denied can still end the turn on "I will do it next conversation" -- the
follow-up then sits idle until the owner asks.  The owner's rule: a turn that
cannot continue its follow-up must END on a one-line proposal of the next
step plus an [[OPTIONS]] marker, so the owner's tap starts the next turn.

DGN-1934: that menu is for a real owner decision only. DGN-1940 (dec-323):
the return is handled autonomously; a long follow-up gets a one-line
direction to the owner, never a question. A result line the
bridge delivered mid-turn opens the latch in the same turn (the bridge
records it on the latch row, ``delivered_via``), so safe, reversible
follow-up work simply continues -- no proceed / hold menu per return.

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
# DGN-1934: a result line the bridge has delivered MID-TURN opens the latch
# in the same turn, so ending the turn is never the way to get unblocked. A
# menu is asked for only when the follow-up is a real owner decision; the
# old blanket "proceed / hold" menu made every dispatch return end on one.
# DGN-1940 (dec-323): a dispatch return is handled autonomously. The owner
# wants a one-line direction only when the follow-up runs long, not a
# question per return.
CONTINUE_INSTRUCTION = (
    "Once your result line has been delivered to the owner, follow-up tools "
    "open in this same turn: do not end the turn to get unblocked, and do "
    "not ask before safe, reversible follow-up work -- decide it yourself "
    "and just do it. When that follow-up will run long, tell the owner the "
    "direction in one line and keep going; do not wait for a reply."
)
PROPOSAL_INSTRUCTION = (
    CONTINUE_INSTRUCTION + " Only when the next step is a real owner "
    "decision (consequential or owner-gated) and you cannot continue, do not "
    "promise it for a later conversation: END this turn with a one-line "
    "proposal of that decision followed by the " + OPTIONS_MARKER + " marker, "
    "so the owner's tap starts the next turn. If the result line you send "
    "already carries that proposal and marker, it is the owner's menu: end "
    "the turn there and never repeat it."
)

STOP_BLOCK_REASON = (
    "Dispatch-return forcing (DGN-1715): follow-up work was denied in this "
    "turn, never resumed, and the final message carries no " + OPTIONS_MARKER
    + " marker, so nothing will start the follow-up. If the result line is "
    "already delivered, tools are open: continue the follow-up now when it is "
    "safe and reversible. " + PROPOSAL_INSTRUCTION
)

# DGN-1941: the Stop-time half of "a delivered result line opens tools in
# the same turn". Live evidence 2026-10-08 (5 of 6 returns): the model's
# first follow-up tool ran BEFORE any text, was rightly denied, then the
# model wrote the result line -- which opened the latch -- and ended the turn
# on a menu it invented because it believed it was still blocked. The latch
# was open at Stop; nothing told the model so.
RESUME_REASON = (
    "Dispatch-return forcing (DGN-1941): a follow-up tool was denied earlier "
    "in this turn only because your result line had not reached the owner "
    "yet. It has been delivered now, so follow-up tools are open: re-run the "
    "denied step and finish the follow-up in this same turn. Do not repeat "
    "the result line or a menu you already sent. If that step truly needs "
    "the owner's decision, end the turn now without new text."
)

MISS_REMINDER = (
    "Dispatch-return forcing (DGN-1715): your PREVIOUS dispatch-return turn "
    "ended with follow-up work still pending and no " + OPTIONS_MARKER +
    " proposal. Do not apologize or repeat the result; if that follow-up is "
    "still open, either do it now (safe, reversible) or, when it needs the "
    "owner's decision, offer it as a one-line proposal with " + OPTIONS_MARKER
    + "."
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


def _seconds(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _await_open(ctx, row):
    """DGN-1941: the Stop hook can run before the bridge has finished pushing
    the final text (the same race the PreToolUse gate waits out, DGN-1715).
    Re-read the row for a short grace (and through an in-flight delivery);
    return the latest row. Never longer than the hook budget allows."""
    grace = _seconds("DISPATCH_RETURN_GRACE_SECONDS", 1.5)
    delivery = _seconds("DISPATCH_RETURN_STOP_DELIVERY_WAIT_SECONDS", 5.0)
    start = time.monotonic()
    while row is not None and row.get("visible") is not True:
        limit = delivery if row.get("delivering") is True else grace
        if time.monotonic() - start >= limit:
            break
        time.sleep(0.05)
        fresh = read_row(ctx)
        if fresh is None or fresh.get("turn_id") != row.get("turn_id"):
            break
        row = fresh
    return row


def stop_verdict(data, cwd, turn_text=""):
    """Verdict for a Stop event, or None when this seat has nothing to say.

    "RESUME"        (DGN-1941) a follow-up was denied, never resumed, and
                    the latch is OPEN now (the result line reached the
                    owner): re-prompt once to re-run the denied step, ahead
                    of every marker test -- a menu written while the model
                    believed it was blocked is not an owner decision.
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
    if not data.get("stop_hook_active"):
        row = _await_open(ctx, row) or row
        if row.get("visible") is True:
            return "RESUME"
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
