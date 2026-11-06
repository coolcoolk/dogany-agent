"""user_yes -- the "user yes" sentinel shared by D3 (Bash safety gate) and
D4 (write-paths gate), and the script-side devices (release approval, the
push gate, self_restart's owner flags).  v2.7, wired in DETECT mode only.
Spec: docs/v27-D3-D4-spec.md section 2 (rewritten after the grill,
docs/v27-D3-D4-grill.md F1-F5, F27, and round 2, docs/v27-D3-D4-grill-r2.md
R2-1 to R2-4, R2-6, R2-7, R2-11 to R2-13).

A gated action runs only when the OWNER said yes to it.  This module answers
one question: may action <key> of class <cls> run now, in this session?

The yes turn Y is the NEWEST user-type entry on the transcript's main chain
(owner or injected; tool_result entries and isMeta entries excluded).  The
chain is walked from the entry that holds the gated call's own tool_use when
the hook payload names it (R2-13: a line written after that entry -- a
forged owner turn appended by a parallel tool call -- is never its
ancestor; the entry not found in the tail -> no yes), else from the
transcript's last entry.  Every check below is on Y; an injected turn after
the owner's yes (cron-inject, bridge injected turn, dispatch-return,
task-notification, a session-inbox spool) becomes Y itself and closes the
yes (F3).

Y gives a yes only when ALL hold:
  - Y is an owner turn by why_owner_turn's definition (entry_text +
    is_injected + owner_region);
  - Y is backed by the owner-turn LEDGER (`why-net ups`, the
    UserPromptSubmit hook): a ledger row of this session whose sha is Y's
    text, whose time is Y's and whose transcript_path is this transcript,
    not `skip` (forwarded / injected), not source=voice (F5).  A `claude -p`
    cron or routine prompt, a forwarded bubble and a hand-appended
    transcript line have no row, so they are never a yes;
  - Y is at most YES_TTL_SEC old.

Then one of two paths:

  A. bound ask (every class).  A gate denied action <key> earlier, recorded
     an ask {key, cls, label, questions, asked_at, before_uuid} and printed
     the exact QUESTION LINE the agent must ask (R2-1):
         Run `<command>`? (binding <key>)       (ko: `<command>` ...)
         Edit `<path>`? (binding <key>)
     Y passes for <key> when
       1. For text, Y is the FIRST owner turn after the ask: the owner turn just
          before Y on the chain is before_uuid (F1a), and Y is newer than
          the ask. A callback instead binds to the carrier bubble below;
       2. the reply right before Y ENDS with that question line, verbatim
          (whitespace-normalized; a trailing [[OPTIONS: ...]] marker is
          stripped first).  A question about several actions ends with each
          one's line, one per line, and passes only the keys whose lines
          are in that final run (R2-1: a bare key, a quote, a code fence, a
          negation or a superstring command never passes);
       3. Y is a bound yes: yes words only, plus punctuation / emoji -- no
          rider ("yes but not the push", "ok, lunch first", R2-3) -- or Y
          is a button tap (ledger source=callback) whose label is a yes word
          on the keyboard of that reply AND whose ledger message_id is the
          bubble that carried that keyboard (R2-2: the Why net's own
          carrier-bubble binding, why_net.ups.carrier_bubble; a tap on an
          older bubble with the same labels is another question). Telegram's
          numbered labels and explicit hidden [approve] slot are supported.
     Live-test asks may store a plain owner question instead of the command
     line; only the NEWEST live-test ask can use that surface, and a yes
     older than it never binds (FDGN-596). The command and key remain in
     the internal ask ledger. For that surface (FDGN-596):
       - a Telegram reply-quote shows the ask too: Y's outbound-context
         block quotes the bot bubble the owner replied to (bridge
         outbound_context), and a line of that bubble is the question;
       - "first owner turn after the ask" skips owner turns in between
         that are themselves bare yeses (a repeated approval, e.g. one
         queued while the denied retry was still running, is no rider).
  B. direct intent (only PATH_B_CLASSES: service, baseline, owner-file).
     Y itself asks for the class with an object noun (vocab `cls:<cls>`)
     plus an action word, no hold (temporal deferrals included: "tomorrow",
     R2-4), no inspect word, not a question, and Y is not a tap (F4).
     A service intent binds the VERB it names (R2-4): "restart the bridge"
     passes kickstart -k / self_restart / restart / reload only, never
     bootout / disable / remove / unload / kill / self-update; "self-update"
     passes self-update.sh only.

Lifetime: a yes covers Y while Y stays the newest user-type entry and at
most YES_TTL_SEC -- and, for a D3 command, ONCE (R2-11): a second run on
the same yes passes only when the tool result right before the call is an
error result of that same command (a failed command may be re-run; a
relative destructive command that succeeded is not replayed).  The ask
record expires after ASK_TTL_SEC.

Detect mode (R2-12): a gate in detect records a SHADOW ask
(asks-shadow/, never read in block) and checks it with check(shadow=True),
which matches the question line OR the exact command shown (the pre-R2-1
rule, for measurement only), so two weeks of detect measure the sentinel in
both directions.

Script-side devices (device_yes / device_audit, R2-6): the same binding --
the quote must be in Y, Y is ledgered and fresh, and Y is either a bound yes
answering a reply whose last line is the device's own question line, or Y
names the device's object.  They are ACCIDENT-grade: a Bash-capable agent
can edit a script it may run (spec 6).

Reading cost (F27): the transcript is read BACKWARDS in blocks, only as far
as the chain walk needs, once per decision (`View`, passed to record_ask).

A dispatch worker (DOGANY_DISPATCH_WORKER) never has a yes: its user turn
is a brief.  A subagent call is decided by the gate (D3 denies the
release / tag / push / gate-switch classes for any agent_id, F6).

Fail mode: CLOSED.  Unreadable transcript or ledger, missing owner turn,
unloadable resolver or vocabulary -> no yes (Verdict.ok False with the
reason).  A missed yes costs one more question; a false yes runs an
irreversible action.

Pure stdlib.  English/ASCII only.
"""

import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
VOCAB_PATH = os.path.join(HERE, "user_yes_vocab.tsv")

YES_TTL_SEC = 3600       # a yes stops counting an hour after its turn
ASK_TTL_SEC = 6 * 3600   # an unanswered ask is forgotten after 6h
SHORT_YES_MAX = 40       # a bound yes is a short reply, not a new request
LABEL_MAX = 4000         # the ask keeps the exact command for F1b
# A ledger row is written by the UserPromptSubmit hook just before Claude
# Code logs the turn: its `at` (whole seconds) sits within this window of
# the transcript entry's timestamp.
LEDGER_BEFORE_SEC = 30
LEDGER_AFTER_SEC = 5
TAIL_BLOCK = 256 * 1024
# R2-13: how far back the gated call's own tool_use entry is looked for
# (it is the newest or almost newest entry; past this -> no yes)
ANCHOR_MAX_BYTES = 4 * 1024 * 1024
# the reply right before Y is read back at most this many chain entries (a
# question the owner answers is at the end of that reply; F27 keeps the read
# bounded on a long session with no earlier user turn)
SAID_MAX_ENTRIES = 200
# FDGN-596: how many repeated bare-yes owner turns may sit between a
# live-test ask and the yes that answers it
REPEAT_YES_MAX = 20
TAP_NUMBER_RE = re.compile(r"^\d+\.\s+")
WORKER_ENV = "DOGANY_DISPATCH_WORKER"
QUESTION_END = ("?", "\uff1f")
PATH_B_CLASSES = ("service", "baseline", "owner-file")
OPTIONS_RE = re.compile(r"\[\[OPTIONS:\s*(.*?)\]\]", re.S)
TRAILING_OPTIONS_RE = re.compile(r"\[\[OPTIONS:\s*(.*?)\]\]\s*$", re.S)

# R2-1: the exact question line a gate prints and the reply must end with.
# en first, ko second; {label} is the whitespace-normalized command / path.
QUESTION_FORMS = {
    "cmd": ("Run `{label}`? (binding {key})",
            "`{label}` \uc2e4\ud589\ud560\uae4c\uc694? (binding {key})"),
    "path": ("Edit `{label}`? (binding {key})",
             "`{label}` \uc218\uc815\ud560\uae4c\uc694? (binding {key})"),
}
QUESTION_LINE_RE = re.compile(
    r"^(?:(?:Run|Edit) `.+`\? |`.+` (?:\uc2e4\ud589|\uc218\uc815)"
    r"\ud560\uae4c\uc694\? )\(binding [0-9a-f]{16}\)$")

# R2-4: path B binds the verb its object phrase names (intent -> allowed
# command, then the verbs that intent never covers).
PATH_B_VERBS = {
    "service/restart": (
        re.compile(r"\bkickstart\b(?=.*\s-k\b)|self_restart\.sh|"
                   r"\b(restart|try-restart|reload|reload-or-restart|"
                   r"try-reload-or-restart|force-reload)\b"),
        re.compile(r"\b(bootout|disable|remove|unload|mask|stop|kill|"
                   r"pkill|killall)\b|\s-9\b|-KILL\b|update\.sh\b")),
    "service/self-update": (
        re.compile(r"(^|[/\s])self-update\.sh\b"),
        re.compile(r"\b(bootout|disable|remove|unload|mask|kill|pkill|"
                   r"killall)\b|\s-9\b")),
}


class Verdict(object):
    __slots__ = ("ok", "reason", "grade", "turn_uuid")

    def __init__(self, ok, reason, grade=None, turn_uuid=None):
        self.ok, self.reason, self.grade = ok, reason, grade
        self.turn_uuid = turn_uuid

    def __repr__(self):
        return "Verdict(%r, %r, %r)" % (self.ok, self.reason, self.grade)


# --- resolver reuse (why_owner_turn is the one owner-turn definition) -------

_WOT = None


def _wot():
    global _WOT
    if _WOT is None:
        spec = importlib.util.spec_from_file_location(
            "why_owner_turn_for_user_yes",
            os.path.join(HERE, "why_owner_turn.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _WOT = mod
    return _WOT


def _nfc(s):
    from why_net import store  # lib is on sys.path (HERE); the one copy
    return store.nfc(s)


# --- vocabulary ---------------------------------------------------------------

_VOCAB = None


def vocab(path=VOCAB_PATH):
    """{kind: [compiled regex, ...]}.  Raises on an unreadable file or a
    bad pattern (callers fail closed)."""
    global _VOCAB
    if _VOCAB is not None and path == VOCAB_PATH:
        return _VOCAB
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            kind, _lang, pat = line.split("\t", 2)
            out.setdefault(kind, []).append(re.compile(pat, re.I))
    if path == VOCAB_PATH:
        _VOCAB = out
    return out


def _any(kind, text, v):
    return any(p.search(text) for p in v.get(kind, ()))


def _clean(text):
    return " ".join(text.strip().split())


def _filler(ch):
    """Whitespace, punctuation, symbols (emoji) and emoji joiners."""
    if ch.isspace():
        return True
    cat = unicodedata.category(ch)
    return cat[0] in ("P", "S") or cat in ("Mn", "Me", "Cf")


def _yes_word(t, v):
    """End index of a yes word at the start of `t`, or None.  The word must
    end there: a word going on ("eoje" after "eo") is not a yes."""
    best = None
    for p in v.get("yes", ()):
        m = p.match(t)
        if not m or not m.end():
            continue
        rest = t[m.end():]
        if rest and (rest[0].isalnum() or rest[0] == "_"):
            continue
        if best is None or m.end() > best:
            best = m.end()
    return best


def is_yes(text, v=None):
    """A bound yes (R2-3): yes words only, with punctuation / emoji between
    or after them -- "\uc751", "\uc751 \uc9c4\ud589\ud574", "ok!", "\ub124 \U0001f44d".  Anything else after
    the yes ("yes but not the push", "ok, lunch first", "\uc751 \uc810\uc2ec\ub9cc") is a
    rider: not a yes, ask again.  No hold word, not a question."""
    v = vocab() if v is None else v
    t = _clean(text)
    if not t or len(t) > SHORT_YES_MAX or _any("hold", t, v):
        return False
    if t.endswith(QUESTION_END):
        return False
    rest, seen = t, False
    while True:
        k = 0
        while k < len(rest) and _filler(rest[k]):
            k += 1
        rest = rest[k:]
        if not rest:
            return seen
        end = _yes_word(rest, v)
        if end is None:
            return False
        seen, rest = True, rest[end:]


def direct_intents(text, v=None):
    """Path B intents the owner text directly asks for: `<class>` or
    `<class>/<verb>` (vocab kinds cls:service/restart, ...).  Only
    PATH_B_CLASSES come back."""
    v = vocab() if v is None else v
    t = _clean(text)
    if not t or t.endswith(QUESTION_END):
        return set()
    if _any("hold", t, v) or _any("inspect", t, v) or not _any("act", t, v):
        return set()
    out = set()
    for kind, pats in v.items():
        if not kind.startswith("cls:"):
            continue
        intent = kind[4:]
        if intent.split("/", 1)[0] in PATH_B_CLASSES and \
                any(p.search(t) for p in pats):
            out.add(intent)
    return out


def direct_classes(text, v=None):
    """Classes the owner text directly asks to run (path B)."""
    return {i.split("/", 1)[0] for i in direct_intents(text, v)}


def intent_covers(intent, label):
    """R2-4: does path-B `intent` cover this exact command?  An intent with
    no verb table (baseline, owner-file) covers its class."""
    verbs = PATH_B_VERBS.get(intent)
    if verbs is None:
        return "/" not in intent
    ok, never = verbs
    lab = label or ""
    return bool(ok.search(lab)) and not never.search(lab)


# --- the question line (R2-1) -------------------------------------------------

def question_lines(kind, label, key):
    """The exact question lines (en, ko) a gate prints for an ask."""
    lab = " ".join((label or "").split())
    return [f.format(label=lab, key=key) for f in
            QUESTION_FORMS.get(kind, QUESTION_FORMS["cmd"])]


def strip_options(said):
    """(reply text without its trailing [[OPTIONS: ...]] marker, the
    buttons of that marker or [])."""
    s = (said or "").rstrip()
    m = TRAILING_OPTIONS_RE.search(s)
    if not m:
        return s, []
    return s[:m.start()].rstrip(), [b.strip() for b in m.group(1).split("|")
                                    if b.strip()]


def last_line(said):
    """The reply's last non-empty line (after the OPTIONS marker),
    whitespace-normalized."""
    body, _ = strip_options(_nfc(said or ""))
    for line in reversed(body.split("\n")):
        if line.strip():
            return _clean(line)
    return ""


def question_run(said):
    """The final run of gate question lines the reply ends with (R2-1)."""
    body, _ = strip_options(_nfc(said or ""))
    lines = body.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    run = []
    for line in reversed(lines):
        c = _clean(line)
        if not c or not QUESTION_LINE_RE.match(c):
            break
        run.append(c)
    return run


def _ask_questions(ask, key):
    qs = ask.get("questions")
    if isinstance(qs, list) and qs:
        return [_clean(_nfc(q)) for q in qs if isinstance(q, str)]
    return [_clean(_nfc(q)) for q in
            question_lines(ask.get("kind") or "cmd", ask.get("label"), key)]


def reply_quote(full):
    """The bot bubble an owner turn replies to, as the bridge quoted it in
    the turn's outbound-context block (bridge/outbound_context.py), or ""."""
    oc = getattr(_wot(), "OUTBOUND_CONTEXT", None)
    t = _nfc(full or "")
    if oc is None or not t.startswith(oc.OPEN_MARK):
        return ""
    end = t.rfind(oc.CLOSE_MARK)
    if end < 0:
        return ""
    lines, out, on = t[:end].split("\n"), [], False
    heads = (oc.REPLY_HEAD, oc.PLAIN_REPLY_HEAD)
    answering = oc.ANSWERING.split("{")[0]
    for line in lines:
        if line in heads:
            on, out = True, []
        elif on and line.startswith(answering):
            break
        elif on:
            out.append(line)
    return "\n".join(out)


def shown(said, key, ask, quoted=""):
    """R2-1: the reply right before Y ends with this ask's question line.
    A live-test owner question is also shown by the bubble Y reply-quotes
    (FDGN-596): the owner answered exactly that bubble."""
    if ask.get("owner_question") and ask.get("cls") == "test-suite":
        qs = _ask_questions(ask, key)
        if last_line(said) in qs:
            return True
        return any(_clean(l) in qs for l in (quoted or "").split("\n")
                   if l.strip())
    run = question_run(said)
    return any(q in run for q in _ask_questions(ask, key))


def shown_lenient(said, key, ask):
    """Shadow measurement only (R2-12): the question line, or the pre-R2-1
    rule (the key or the exact command anywhere in the reply)."""
    if shown(said, key, ask):
        return True
    if not said:
        return False
    if key and key in said:
        return True
    lab = _clean(_nfc(ask.get("label") or ""))
    return bool(lab) and lab in _clean(said)


def shadow_tag(v):
    """The detect log's shadow field (R2-12, r3 P1): `SHADOW_YES strict`
    (block passes it too), `SHADOW_YES lenient` (only the pre-R2-1 rule
    held: block says ask-not-shown), `SHADOW_YES direct` (path B), or
    `SHADOW_NO <reason>`."""
    if not v.ok:
        return "SHADOW_NO %s" % v.reason
    g = v.grade or ""
    return "SHADOW_YES %s" % (
        "lenient" if "-lenient" in g else "direct" if g == "direct"
        else "strict")


def _button_labels(said):
    _, buttons = strip_options(_nfc(said or ""))
    return [b.removeprefix("[approve] ").strip() for b in buttons]


def tapped_yes(said, text, v, row=None):
    """A tap binds the reply's keyboard and a yes label or explicit approve slot."""
    _, raw = strip_options(_nfc(said or ""))
    buttons = _button_labels(said)
    label = _nfc(text).strip()
    # Telegram hides [approve] and numbers the labels. Bind the received
    # slot to the entire recorded keyboard before trusting that flag.
    if row is not None and "keyboard" in row:
        from why_net import ups
        slot = ups.tap_slot(label, row, [{"label": b, "slot": i}
                            for i, b in enumerate(buttons)], False)
        if slot is None:
            return False
        idx = row["option_index"] - 1
        approving = [i for i, b in enumerate(raw) if b.startswith("[approve] ")]
        return (approving == [idx] or is_yes(buttons[idx], v))
    return label in buttons and is_yes(label, v)


class _BotCtx(object):
    def __init__(self, bot_dir):
        self.bot_dir = bot_dir


def carrier_ok(bot_dir, ask, row, said):
    """R2-2: the tap's bubble (ledger message_id) is the bubble that carried
    this reply's keyboard -- the Why net's binding (why_net.ups
    .carrier_bubble: the first bubble the bridge rendered in this chat at or
    after the ask whose buttons are this keyboard's).  Fail closed."""
    buttons = _button_labels(said)
    mid = row.get("message_id")
    if not buttons or mid is None or isinstance(mid, bool):
        return False
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        from why_net import ups
    except Exception:
        return False
    synthetic = {"issued_ts": float(ask.get("asked_at") or 0),
                 "shown": [{"label": b} for b in buttons]}
    try:
        got = ups.carrier_bubble(_BotCtx(bot_dir), synthetic,
                                 row.get("chat_id"))
    except Exception:
        return False
    if got is None:
        return False
    # DGN-1958: a tap on a carried re-render answers its origin's ask.
    try:
        mid = ups.bubble_origin(_BotCtx(bot_dir), row.get("chat_id"), mid)
    except Exception:
        pass
    return got == mid


# --- transcript tail ----------------------------------------------------------

def _user_type(entry):
    """True for a user-type text entry that opens a turn: owner or injected
    (tool_result entries and isMeta entries do not)."""
    if not isinstance(entry, dict) or entry.get("isMeta") or "toolUseResult" in entry:
        return False
    if entry.get("type") == "attachment":
        a = entry.get("attachment")
        return (isinstance(a, dict) and a.get("type") == "queued_command"
                and a.get("commandMode") == "prompt" and isinstance(a.get("prompt"), str))
    if entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list) or not content:
        return False
    return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                   for b in content)


def _tool_uses(entry):
    """[(tool_use id, input)] of an assistant entry."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return []
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [(b.get("id"), b.get("input") or {}) for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
            and isinstance(b.get("id"), str)]


def _tool_result(entry):
    """(tool_use_id, is_error) of a tool_result entry, or None."""
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return None
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            return b.get("tool_use_id"), bool(b.get("is_error"))
    return None


class _Tail(object):
    """Transcript entries keyed by uuid, loaded backwards in blocks on
    demand.  Strict UTF-8 per line (a bad line raises: fail closed)."""

    def __init__(self, path, block=TAIL_BLOCK):
        self.closed = False
        self.fh = open(path, "rb")
        self.fh.seek(0, os.SEEK_END)
        self.pos = self.size = self.fh.tell()
        self.block, self.carry = block, b""
        self.by_uuid, self.leaf = {}, None
        self.tool_use_at = {}  # tool_use id -> uuid of the entry holding it

    def close(self):
        self.closed = True
        try:
            fh = getattr(self, "fh", None)
            if fh is not None:
                fh.close()
        except OSError:
            pass

    def __del__(self):
        self.close()

    def _more(self):
        if self.closed:
            return False
        if self.pos <= 0 and not self.carry:
            return False
        if self.pos <= 0:
            data, self.carry = self.carry, b""
        else:
            start = max(0, self.pos - self.block)
            self.fh.seek(start)
            data = self.fh.read(self.pos - start) + self.carry
            self.pos = start
            if start > 0:
                k = data.find(b"\n")
                if k < 0:
                    self.carry = data
                    return True
                self.carry, data = data[:k], data[k + 1:]
            else:
                self.carry = b""
        for raw in reversed(data.split(b"\n")):
            raw = raw.strip()
            if not raw:
                continue
            line = raw.decode("utf-8")  # strict: raises on a bad line
            try:
                e = json.loads(line)
            except ValueError:
                continue
            u = e.get("uuid") if isinstance(e, dict) else None
            if not isinstance(u, str) or not u or e.get("isSidechain"):
                continue
            if self.leaf is None:
                self.leaf = u
            self.by_uuid.setdefault(u, e)  # a later line wins (file order)
            for tid, _inp in _tool_uses(e):
                self.tool_use_at.setdefault(tid, u)
        return True

    def get(self, uuid):
        while uuid not in self.by_uuid and self._more():
            pass
        return self.by_uuid.get(uuid)

    def anchor(self, tool_use_id):
        """R2-13: uuid of the entry holding `tool_use_id`, read back at
        most ANCHOR_MAX_BYTES; None when not found."""
        while tool_use_id not in self.tool_use_at and \
                self.size - self.pos < ANCHOR_MAX_BYTES and self._more():
            pass
        return self.tool_use_at.get(tool_use_id)

    def walk(self, start=None):
        """Main-chain entries, newest first (parentUuid walk from `start`,
        else the newest non-sidechain entry; a compact boundary continues
        through logicalParentUuid), as why_owner_turn.main_chain defines
        it."""
        while start is None and self.leaf is None and self._more():
            pass
        seen, cur = set(), start or self.leaf
        while isinstance(cur, str) and cur not in seen:
            e = self.get(cur)
            if e is None:
                return
            seen.add(cur)
            yield e
            cur = e.get("parentUuid") or e.get("logicalParentUuid")


class Turn(object):
    __slots__ = ("uuid", "ts", "text", "full", "owner", "queued")

    def __init__(self, uuid, ts, text, full, owner, queued=False):
        self.uuid, self.ts, self.text = uuid, ts, text
        self.full, self.owner, self.queued = full, owner, queued


class View(object):
    """What one gate decision needs from the transcript, read once and only
    as far back as needed (F27): Y (the newest user-type entry) and the
    assistant text right before Y on the first walk; the owner turn before
    Y (path A) and the newest owner turn (a new ask) continue the same walk
    lazily.  `anchor` = the gated call's tool_use id (R2-13): the walk
    starts at its entry; not found -> anchor_missing, no Y."""

    def __init__(self, transcript_path, anchor=None):
        self._wot = _wot()
        self.path = transcript_path
        self._tail = _Tail(transcript_path)
        self.anchor_missing = False
        start = None
        if anchor:
            start = self._tail.anchor(anchor)
            if start is None:
                self.anchor_missing = True
        self._walk = self._tail.walk(start) if not self.anchor_missing \
            else iter(())
        self._owners = []      # owner turns seen, newest first
        self._done = False
        self.yes, self.said = None, ""
        # R2-11: the newest tool result before the call, and the tool_use
        # inputs seen on the way (to tie that result to its command)
        self.last_result, self._inputs = None, {}
        said, seen = [], 0
        for e in self._walk:
            if self.yes is None:
                for tid, inp in _tool_uses(e):
                    self._inputs.setdefault(tid, inp)
                if self.last_result is None:
                    self.last_result = _tool_result(e)
            if _user_type(e):
                t = self._turn(e)
                if t.owner:
                    self._owners.append(t)
                if self.yes is None:
                    self.yes = t
                    continue
                if t.owner:
                    break
                # A non-owner turn between the question and the answer
                # must not hide the question. An injection AFTER Y still
                # becomes Y and closes approval, as before.
                seen += 1
                if seen >= SAID_MAX_ENTRIES:
                    break
                continue
            if self.yes is not None:
                said.extend(self._wot.assistant_texts(e))
                seen += 1
                if seen >= SAID_MAX_ENTRIES:
                    break
        else:
            self._done = True
        said.reverse()
        self.said = _nfc("\n".join(said))
        if self._done:
            self._tail.close()

    def _turn(self, e):
        wot = self._wot
        text = wot.entry_text(e)
        # The bridge's outbound context is agent text, not an injected
        # owner turn. Match UPS/owner_turns: classify the remaining text,
        # keeping the full wrapper below for the ledger's SHA binding.
        owner_text = wot.strip_outbound(text) if text is not None else None
        owner = bool(owner_text and owner_text.strip()) and \
            not wot.is_injected(owner_text)
        region = ""
        if owner:
            t = _nfc(text)
            lo, hi = wot.owner_region(t)
            region = t[lo:hi]
        return Turn(e.get("uuid"), _epoch(e.get("timestamp")), region,
                    text or "", owner, queued=e.get("type") == "attachment")

    def _owner_at(self, k):
        """The k-th newest owner turn on the chain (0 = newest), or None."""
        while len(self._owners) <= k and not self._done:
            for e in self._walk:
                if _user_type(e):
                    t = self._turn(e)
                    if t.owner:
                        self._owners.append(t)
                        break
            else:
                self._done = True
                self._tail.close()
        return self._owners[k] if len(self._owners) > k else None

    @property
    def newest_owner(self):
        return self._owner_at(0)

    @property
    def prev_owner(self):
        """The newest owner turn strictly before Y."""
        if self.yes is None:
            return None
        return self._owner_at(1 if self.yes.owner else 0)

    def retry_of(self, key):
        """R2-11: the tool result right before this call is an ERROR result
        of the command bound by `key`."""
        if not self.last_result:
            return False
        tid, is_error = self.last_result
        inp = self._inputs.get(tid)
        if not is_error or not isinstance(inp, dict):
            return False
        cmd = inp.get("command")
        return isinstance(cmd, str) and action_key("cmd", cmd) == key

    def close(self):
        self._tail.close()


def _epoch(ts):
    from why_net import store
    return store.parse_iso(ts)


def newest_owner_turn(transcript_path):
    """(uuid, timestamp_epoch, owner_text) of the newest on-chain owner turn,
    or None.  Raises on an unreadable transcript."""
    t = View(transcript_path).newest_owner
    return (t.uuid, t.ts, t.text) if t else None


# --- the ledger ---------------------------------------------------------------

def ledger_path_for(state_dir):
    """<data>/state/<gate> -> <data>/state/why-net/turn-ledger.jsonl (the
    why_net store's TURN_LEDGER)."""
    return os.path.join(os.path.dirname(os.path.normpath(state_dir)),
                        "why-net", "turn-ledger.jsonl")


def _same_file(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (TypeError, ValueError):
        return False


def ledger_row(ledger_path, session_id, turn, transcript_path=None,
               strict=False, newest=False):
    """The ledger row backing owner turn `turn`, or None.  A row naming
    another transcript file does not back it (grill-2 N3, R2 minor);
    strict=True (script devices) also refuses a row naming none.  Raises
    OSError on an unreadable ledger file (a missing file is no row)."""
    if turn is None or turn.ts is None or not session_id:
        return None
    sha = _wot().turn_sha(turn.full)
    try:
        fh = open(ledger_path, encoding="utf-8")
    except FileNotFoundError:
        return None
    best, latest = None, None
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("session_id") != session_id:
                continue
            latest = r
            if r.get("skip") or r.get("sha") != sha:
                continue
            tp = r.get("transcript_path")
            if transcript_path and isinstance(tp, str) and \
                    not _same_file(tp, transcript_path):
                continue
            if strict and not isinstance(tp, str):
                continue
            at = _epoch(r.get("at"))
            if at is None:
                continue
            # A running CLI may surface the queued attachment long after
            # receipt. Only a bridge-recorded inflight action gets this
            # window; it must still be this session's newest action.
            inflight = turn.queued and r.get("owner_delivery") == "inflight"
            before = YES_TTL_SEC if inflight else LEDGER_BEFORE_SEC
            if turn.ts - before <= at <= turn.ts + LEDGER_AFTER_SEC:
                best = r
    return None if newest and best is not latest else best


# --- ask records --------------------------------------------------------------

def action_key(kind, value):
    """Binding key.  kind 'cmd' -> whitespace-normalized command; kind
    'path' -> normalized absolute path."""
    if kind == "cmd":
        norm = " ".join(value.split())
    else:
        norm = os.path.normpath(value)
    return hashlib.sha256(("%s\0%s" % (kind, norm)).encode("utf-8")) \
        .hexdigest()[:16]


def _safe(session_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")


def _asks_path(state_dir, session_id, shadow=False):
    return os.path.join(state_dir, "asks-shadow" if shadow else "asks",
                        _safe(session_id) + ".jsonl")


def record_ask(state_dir, session_id, key, cls, label, transcript_path,
               now=None, view=None, kind="cmd", shadow=False,
               owner_question=None):
    """Append an ask (called by a gate at deny time; shadow=True in detect
    mode, R2-12).  Never raises.  Pass the decision's View to avoid a second
    transcript read (F27).  Returns the row (its `questions` are the lines
    the deny reason prints)."""
    now = time.time() if now is None else now
    before = None
    try:
        if view is None and transcript_path:
            view = View(transcript_path)
        if view is not None and view.newest_owner is not None:
            before = view.newest_owner.uuid
    except Exception:
        before = None
    row = {"key": key, "cls": cls, "kind": kind, "label": label[:LABEL_MAX],
           "questions": question_lines(kind, label[:LABEL_MAX], key),
           "asked_at": now, "before_uuid": before}
    if owner_question and cls == "test-suite":
        row.update(owner_question=True, questions=[owner_question])
    if shadow:
        row["shadow"] = True
    try:
        path = _asks_path(state_dir, session_id, shadow)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=True) + "\n")
    except OSError:
        pass
    return row


def _asks(state_dir, session_id, key, now, shadow=False):
    try:
        with open(_asks_path(state_dir, session_id, shadow),
                  encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("key") == key
            and bool(r.get("shadow")) == shadow
            and 0 <= now - float(r.get("asked_at") or 0) <= ASK_TTL_SEC]


def _latest_owner_question(state_dir, session_id, ask, answered_at):
    """A command-free question arms only the latest pending command.

    Several commands can have identical plain wording and keyboards. They
    must never share approval just because those surfaces happen to match:
    the newest live-test ask asked by the time of the answer must be this
    command's.  And only the NEWEST ask of this command counts: a re-ask
    supersedes the older ones, so a yes older than it never binds
    (FDGN-596).
    """
    try:
        with open(_asks_path(state_dir, session_id), encoding="utf-8") as fh:
            latest = newest_own = None
            for line in fh:
                r = json.loads(line)
                if not isinstance(r, dict) or r.get("cls") != "test-suite" \
                        or r.get("shadow"):
                    continue
                if float(r.get("asked_at") or 0) <= answered_at:
                    latest = r
                if r.get("key") == ask.get("key"):
                    newest_own = r
        return latest is not None and latest.get("key") == ask.get("key") \
            and newest_own is not None and \
            float(newest_own.get("asked_at") or 0) == \
            float(ask.get("asked_at") or 0)
    except (OSError, ValueError, TypeError):
        return False


def _bare_yes(text, v):
    """A bare yes, typed or a numbered tap label ("1. Approve")."""
    return is_yes(TAP_NUMBER_RE.sub("", text or "", count=1), v)


def _first_after_ask(view, ask, v):
    """FDGN-596: Y is the first owner turn after `ask`, not counting owner
    turns in between that are bare yeses themselves.  An owner turn
    counts as "before the ask" when it is the recorded before_uuid or its
    time is not after asked_at (the same turn re-read under another uuid)."""
    before = ask.get("before_uuid")
    at = float(ask.get("asked_at") or 0)
    k = 1 if view.yes.owner else 0
    for _ in range(REPEAT_YES_MAX + 1):
        t = view._owner_at(k)
        if t is None:
            return before is None
        if t.uuid == before or (t.ts is not None and t.ts <= at):
            return True
        if not _bare_yes(t.text, v):
            return False
        k += 1
    return False


# --- consumed yeses (R2-11) ---------------------------------------------------

def _consumed_path(state_dir, session_id):
    return os.path.join(state_dir, "consumed", _safe(session_id) + ".jsonl")


def consumed(state_dir, session_id, key, turn_uuid):
    try:
        with open(_consumed_path(state_dir, session_id),
                  encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("key") == key and \
                        r.get("turn") == turn_uuid:
                    return True
    except OSError:
        return False
    return False


def mark_consumed(state_dir, session_id, key, turn_uuid, now=None):
    """Record that the yes in turn `turn_uuid` ran action `key` once.
    Never raises."""
    try:
        path = _consumed_path(state_dir, session_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "turn": turn_uuid,
                                 "at": time.time() if now is None else now})
                     + "\n")
    except OSError:
        pass


# --- the check ----------------------------------------------------------------

def check(state_dir, session_id, transcript_path, key, cls, env=None,
          now=None, view=None, ledger_path=None, label=None, once=False,
          shadow=False):
    """Verdict for running action `key` of class `cls` now.

    label  -- the exact command (D3) for path B's verb binding (R2-4)
    once   -- R2-11: a yes already used for `key` passes again only as the
              retry of a failed run (the caller marks it with mark_consumed)
    shadow -- R2-12: read the detect-mode shadow asks with the lenient
              shown rule; measurement only, never authorizes in block."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    if env.get(WORKER_ENV):
        return Verdict(False, "dispatch-worker")
    if not transcript_path and view is None:
        return Verdict(False, "no-transcript")
    try:
        v = vocab()
        view = View(transcript_path) if view is None else view
    except Exception as exc:  # fail closed
        return Verdict(False, "unreadable:%s" % type(exc).__name__)
    if view.anchor_missing:
        return Verdict(False, "anchor-not-found")
    y = view.yes
    if y is None:
        return Verdict(False, "no-owner-turn")
    if not y.owner:
        # F3: an injected turn after the owner's yes closes it
        return Verdict(False, "newest-turn-not-owner", turn_uuid=y.uuid)
    if y.ts is None or not 0 <= now - y.ts < YES_TTL_SEC:
        return Verdict(False, "yes-expired", turn_uuid=y.uuid)
    try:
        row = ledger_row(ledger_path or ledger_path_for(state_dir),
                         session_id, y, transcript_path or view.path, newest=y.queued)
    except (OSError, UnicodeDecodeError) as exc:
        return Verdict(False, "ledger-unreadable:%s" % type(exc).__name__,
                       turn_uuid=y.uuid)
    if row is None:
        return Verdict(False, "not-in-ledger", turn_uuid=y.uuid)
    src = row.get("source") or "message"
    if src == "voice":
        return Verdict(False, "voice", turn_uuid=y.uuid)
    if once and not shadow and consumed(state_dir, session_id, key, y.uuid) \
            and not view.retry_of(key):
        return Verdict(False, "consumed", turn_uuid=y.uuid)
    reason = "no-yes"
    # A: bound ask, answered by the first owner turn after it
    asks = _asks(state_dir, session_id, key, now, shadow)
    prev = None
    if asks:
        p = view.prev_owner
        prev = p.uuid if p else None
    seen_shown = shown_lenient if shadow else shown
    bot_dir = os.path.dirname(os.path.dirname(os.path.normpath(state_dir)))
    quoted = reply_quote(y.full)
    for ask in reversed(asks):
        live_q = bool(ask.get("owner_question")) and \
            ask.get("cls") == "test-suite"
        if ask.get("owner_question") and not _latest_owner_question(
                state_dir, session_id, ask, y.ts):
            reason = "ask-superseded"
            continue
        if y.ts < float(ask.get("asked_at") or 0):
            reason = "turn-predates-ask"
            continue
        # A callback answers its specific carrier bubble, even when the
        # owner discussed the request in between. Text still has to be the
        # first owner answer; a typed copy of a button grants nothing.
        if src != "callback" and ask.get("before_uuid") != prev and not (
                live_q and _first_after_ask(view, ask, v)):
            reason = "not-first-turn-after-ask"
            continue
        if not (seen_shown(view.said, key, ask) or
                (live_q and shown(view.said, key, ask, quoted))):
            reason = "ask-not-shown"
            continue
        # r3 P1: a shadow pass says whether block's strict R2-1 rule held
        # too, so the detect log alone gives the strict / lenient split
        grade = ("shadow-ask-bound-%s" % (
            "strict" if shown(view.said, key, ask) else "lenient")) \
            if shadow else "ask-bound"
        if src == "callback":
            if not tapped_yes(view.said, y.text, v, row):
                reason = "not-a-yes"
                continue
            if not carrier_ok(bot_dir, ask, row, view.said):
                reason = "tap-not-on-carrier-bubble"
                continue
            return Verdict(True, "yes", grade + "-tap", y.uuid)
        if is_yes(y.text, v):
            return Verdict(True, "yes", grade, y.uuid)
        reason = "not-a-yes"
    # B: the owner turn itself asks for this class (object noun + act), and
    # for a service op the verb it names (R2-4)
    if cls in PATH_B_CLASSES and src != "callback":
        intents = {i for i in direct_intents(y.text, v)
                   if i.split("/", 1)[0] == cls}
        if intents:
            if any(intent_covers(i, label) for i in intents):
                return Verdict(True, "direct", "direct", y.uuid)
            reason = "intent-verb-mismatch"
    return Verdict(False, reason, turn_uuid=y.uuid)


# --- script-side devices (R2-6) -----------------------------------------------

def _names_object(text, objects, intent, v):
    t = _clean(_nfc(text))
    if not t or t.endswith(QUESTION_END) or _any("hold", t, v) or \
            _any("inspect", t, v) or not _any("act", t, v):
        return False
    low = t.casefold()
    if any(o and _clean(_nfc(o)).casefold() in low for o in objects or ()):
        return True
    return bool(intent) and intent in direct_intents(t, v)


def _device_turn_ok(turn, quote, row, v, now=None):
    if row is None:
        return "not-in-ledger"
    at = _epoch(row.get("at"))
    if now is not None and (at is None or not 0 <= now - at < YES_TTL_SEC):
        return "yes-expired"
    src = row.get("source") or "message"
    if src == "voice":
        return "voice"
    if quote is not None and _clean(_nfc(quote)) not in _clean(turn.text):
        return "quote-not-in-yes"
    return None


def _device_binding(turn, said, row, questions, objects, intent, v):
    qs = {_clean(_nfc(q)) for q in questions or () if isinstance(q, str) and q.strip()}
    if row.get("source") == "callback":
        kb, idx = row.get("keyboard"), row.get("option_index")
        if (isinstance(kb, list) and isinstance(idx, int) and not isinstance(idx, bool)
                and 1 <= idx <= len(kb) and isinstance(kb[idx - 1], str)
                and _clean(turn.text) == _clean(_nfc(kb[idx - 1]))
                and _clean(_nfc(row.get("device_question") or "")) in qs):
            return Verdict(True, "yes", "device-question-tap", turn.uuid)
        return Verdict(False, "tap", turn_uuid=turn.uuid)
    if is_yes(turn.text, v) and last_line(said) in qs:
        return Verdict(True, "yes", "device-question", turn.uuid)
    if _names_object(turn.text, objects, intent, v):
        return Verdict(True, "yes", "device-object", turn.uuid)
    return Verdict(False, "not-bound", turn_uuid=turn.uuid)


def device_yes(transcript_path, session_id, ledger_path, questions,
               objects=(), intent=None, quote=None, anchor=None, now=None):
    """R2-6: may a script-side device act now?  The SAME binding as path A
    / B: Y (the newest user-type entry of the CALLER's session -- which
    session that is the device learns from the D3 hook's live record,
    R2-7) is a fresh, ledgered owner turn containing `quote`, and either a
    bound yes answering a reply whose last line is one of `questions`, or a
    turn that names the device's object (`objects` strings, or the path-B
    `intent`).  Returns a Verdict; grade 'device-question' | 'device-object'.
    """
    now = time.time() if now is None else now
    try:
        v = vocab()
        view = View(transcript_path, anchor=anchor)
    except Exception as exc:
        return Verdict(False, "unreadable:%s" % type(exc).__name__)
    if view.anchor_missing:
        return Verdict(False, "anchor-not-found")
    y = view.yes
    if y is None:
        return Verdict(False, "no-owner-turn")
    if not y.owner:
        return Verdict(False, "newest-turn-not-owner", turn_uuid=y.uuid)
    if y.ts is None or not 0 <= now - y.ts < YES_TTL_SEC:
        return Verdict(False, "yes-expired", turn_uuid=y.uuid)
    try:
        row = ledger_row(ledger_path, session_id, y, transcript_path,
                         strict=True, newest=True)
    except (OSError, UnicodeDecodeError) as exc:
        return Verdict(False, "ledger-unreadable:%s" % type(exc).__name__)
    bad = _device_turn_ok(y, quote, row, v, now=now)
    if bad:
        return Verdict(False, bad, turn_uuid=y.uuid)
    return _device_binding(y, view.said, row, questions, objects, intent, v)


def device_audit(transcript_path, session_id, ledger_path, message_uuid,
                 questions, objects=(), intent=None, quote=None):
    """Re-check a RECORDED device approval later (release-approval --check):
    the recorded turn is an on-chain, ledgered owner turn containing the
    quote, and it was a bound yes to the device question or named the
    object.  Freshness and 'newest turn' were checked at record time."""
    wot = _wot()
    try:
        v = vocab()
        entries = wot.read_entries(transcript_path)
    except Exception as exc:
        return Verdict(False, "unreadable:%s" % type(exc).__name__)
    chain = wot.main_chain(entries)
    by_uuid = {e.get("uuid"): e for e in entries
               if isinstance(e.get("uuid"), str) and not e.get("isSidechain")}
    e = by_uuid.get(message_uuid)
    if e is None or message_uuid not in chain:
        return Verdict(False, "turn-off-chain")
    text = wot.entry_text(e)
    owner_text = wot.strip_outbound(text) if text is not None else None
    if not owner_text or not owner_text.strip() or wot.is_injected(owner_text):
        return Verdict(False, "not-owner-turn")
    t = _nfc(text)
    lo, hi = wot.owner_region(t)
    turn = Turn(message_uuid, _epoch(e.get("timestamp")), t[lo:hi], text,
                True, queued=e.get("type") == "attachment")
    try:
        row = ledger_row(ledger_path, session_id, turn, transcript_path,
                         strict=True)
    except (OSError, UnicodeDecodeError) as exc:
        return Verdict(False, "ledger-unreadable:%s" % type(exc).__name__)
    bad = _device_turn_ok(turn, quote, row, v)
    if bad:
        return Verdict(False, bad, turn_uuid=message_uuid)
    said, cur, n = [], e.get("parentUuid"), 0
    while isinstance(cur, str) and n < SAID_MAX_ENTRIES:
        p = by_uuid.get(cur)
        if p is None:
            break
        if _user_type(p):
            text = wot.entry_text(p)
            owner_text = wot.strip_outbound(text) if text is not None else None
            if owner_text and owner_text.strip() and not wot.is_injected(owner_text):
                break
        said.extend(wot.assistant_texts(p))
        cur, n = p.get("parentUuid") or p.get("logicalParentUuid"), n + 1
    said.reverse()
    return _device_binding(turn, "\n".join(said), row, questions, objects, intent, v)
