"""Legacy inline ask rendering and historical answer compatibility.

FDGN-594 retires public enqueue. Legacy inline refusal/fallback, reveal and
frame privileges still need migration to the trusted structured intake path.

`ask` prints the owner-facing question line + a labeled [[OPTIONS: ..]]
marker only when every never-ask check passes; otherwise it records the ask
as refused + queued and exits 4 with a machine signal (never owner chat).
The verb prints i18n copy. The agent MAY restyle the confirm stem and its
two button words into its persona tone (including address and interjections),
but MUST keep the candidate sentence verbatim and exactly two buttons in
the same order and meaning: yes first, wrong reading second. Picks bind by
slot index. Other shapes retain their existing copy contract.

A passing inline ask is recorded as `create` only.  It counts against the
caps from then on (budget.sent_at), but it is `shown` only once the agent's
reply is seen to carry its labels -- by `ups` on the owner's next turn or by
the nightly derive (grill fix 3: printing is not sending).
"""

import datetime
import os
import re
import sys
import unicodedata

from . import budget, events, store
from . import nodes as N

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import machine_signal  # noqa: E402

EXIT_OK, EXIT_INTERNAL, EXIT_USAGE, EXIT_FLOOR, EXIT_BUDGET = 0, 1, 2, 3, 4

TRIGGERS = ("new-work", "fork", "correction", "ripe", "contest", "reveal", "before-work")
# AMEND-1 2.4 N9: act-driven asks are not inline -- `enqueue` takes them,
# the inline `ask` verb never does.  Rev 3: the one inline door for them is
# `gate` (before irreversible / trial-and-error work).
ACT_TRIGGERS = ("act-ripe", "act-contest", "fork-mixed")
# AMEND-1 rev 3: `reading` is the retro follow-up after a "wrong" tap (up to
# 3 concrete candidates + free text); `readings` is the retro's
# reading-per-signal bubble; `before-work` is a fresh `gate` ask.
RETRO_TRIGGERS = ("reading", "readings")
ENQUEUE_TRIGGERS = TRIGGERS + ACT_TRIGGERS + RETRO_TRIGGERS
# N7 exemption: these asks exist to move a provisional node, or (reading) to
# re-read one signal, where a provisional node is a fair answer.
N7_EXEMPT = ("act-contest", "fork-mixed", "reading")
SHAPES = ("reveal", "contrast", "set", "pole", "scatter", "confirm",
          "act-confirm", "split", "readings")
# AMEND-1 rev 3 (trial-run rule): the follow-up after a wrong reading offers
# at most this many concrete candidates, plus free text.
READING_MAX_CANDS = 3
GATE_REASONS = ("irreversible", "trial")
# Why states a gate asks about (rev 3 timing): a settled Why needs no ask.
GATE_OPEN = ("hypothesis", "confirmed-provisional", "contested")
YES_SHAPES = ("confirm", "act-confirm")
KINDS = ("why", "understanding")

# Vendored from bridge/options.py (_BUTTON_LABEL_MAX_WIDTH / _label_width,
# DGN-779). Pinned by routines/tests/test-why-net-elicit.py parity test --
# the bridge is never imported here (it pulls python-telegram-bot).
BUTTON_LABEL_MAX_WIDTH = 31.0

# Grill fix 11: a candidate worded as a go-ahead / reply word would turn a
# plain "go on" tap into a confirmed Why.  Such a node never becomes a
# button: the agent rewords it as a reason first.  Matched on the NFC label
# (owner-speech corpus, matched never shown).
_ACTION_LABEL = re.compile(
    "(^|\\s)(\uadf8\ub0e5|\ubc14\ub85c|\uc9c0\uae08 \ubc14\ub85c)(\\s|$)"
    "|(\ud574\uc918|\ud574\uc8fc\uc138\uc694|\ud558\uc790|\uac00\uc790|\uc9c4\ud589|\uc9c4\ud589\ud574|\uc9c4\ud589\ud574\uc918|\ud574\ubd10|\uace0\uace0|\u3131|\u3131\u3131)[.!~ ]*$"
    "|^(\ub124|\uc751|\uc608|\uc544\ub2c8\uc694|\uc544\ub2c8|\uc88b\uc544|\uc88b\uc544\uc694|\ub9de\uc544|\ub9de\uc544\uc694"
    "|ok|okay|yes|no|go|go ahead|proceed|do it|ship it)[.!~ ]*$",
    re.IGNORECASE)


def is_action_label(text):
    return bool(_ACTION_LABEL.search(unicodedata.normalize("NFC", text or "").strip()))


def label_width(text):
    width = 0.0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 1.5
        elif ch.isspace():
            width += 0.4
        else:
            width += 1.0
    return width


# i18n copy keys.  AMEND-1 section 4 rows 1-10c are owner-confirmed
# (dec-334, rev 3: 5f rewritten, the "both" button added).  Stems exist for
# every asking shape; reveal never prints.
COPY_KEYS_CONFIRMED = (
    "why.ask.stem.contrast", "why.ask.stem.set", "why.ask.stem.pole",
    "why.ask.stem.scatter", "why.ask.free_hint",
    "why.ask.escape", "why.ask.confirm_yes",
    "why.ask.confirm_no", "why.ask.stem.confirm", "why.ask.stem.confirm_no_address",
    "why.ask.stem.act_confirm", "why.ask.stem.split", "why.ask.split.said",
    "why.ask.split.acted", "why.ask.stem.mixed", "why.ask.both",
)
CONSOLE_KEYS_CONFIRMED = (
    "why.status.hypothesis", "why.status.confirmed-provisional",
    "why.status.confirmed", "why.status.contested", "why.status.pivoted",
    "why.status.retired", "why.flag.stale", "why.flag.mixed", "why.pivot.basis",
)
# AMEND-1 rev 3, section 10: the retro section copy is NEW and stays
# PENDING_OWNER_CONFIRM (UX-WRITING) until the owner confirms it.
COPY_KEYS_PENDING_OWNER_CONFIRM = (
    "why.ask.subject.framework", "why.ask.subject.work",
    "why.ask.transcribe", "why.ask.transcribe.undated", "why.ask.transcribe_yes",
    "why.retro.head", "why.retro.line", "why.retro.wrong", "why.ask.stem.reading",
    # final grill B3/B4: one signal per bubble, the stop button, a head that
    # invites free text
    "why.retro.stop",
    # DGN-1961: a recall line cites the event the agent already knows
    "why.retro.cite", "why.retro.cite.work",
)


# AMEND-2 (DGN-2028 slice 3): the in-turn Why question (MD 14 Q2) and the
# view-only retro picture (MD 14 Q4) are NEW owner copy.  Copy is an owner
# gate: these keys ship with a placeholder value (PENDING_PREFIX) that never
# reaches an owner surface -- `frame` degrades to `proceed` (reason
# copy-pending) and the retro picture stays empty until the owner's pick
# lands in a non-placeholder layer (config/i18n.local, a kit layer).
PENDING_PREFIX = "@@pending:"
COPY_KEYS_TURN = ("why.ask.stem.turn", "why.ask.turn.other", "why.ask.turn.escape")
COPY_KEYS_RETRO_VIEW = ("why.retro.view.head", "why.retro.view.confirmed",
                        "why.retro.view.assumed")
COPY_KEYS_PENDING_PLACEHOLDER = COPY_KEYS_TURN + COPY_KEYS_RETRO_VIEW


def copy_ready(ctx, keys):
    """True when every key resolves to real copy (not empty, not the
    shipped placeholder)."""
    for k in keys:
        v = t(ctx, k)
        if not isinstance(v, str) or not v.strip() or v.startswith(PENDING_PREFIX):
            return False
    return True


def stem_key(shape, trigger=None, both=False):
    if trigger == "turn":
        return "why.ask.stem.turn"   # AMEND-2: the in-turn Why question (Q2)
    # rev 3: an ask offering two Whys with a "both" button says so (5f)
    if trigger == "fork-mixed" or (both and shape == "contrast"):
        return "why.ask.stem.mixed"
    if trigger == "reading":
        return "why.ask.stem.reading"
    if shape == "readings":
        return "why.retro.head"
    return "why.ask.stem." + shape.replace("-", "_")


def t(ctx, key):
    import i18n_lookup
    import framework_dictionary
    lang = ctx.conf("AGENT_LANG") or "en"
    return framework_dictionary.expand(
        ctx.root, lang, i18n_lookup.lookup(ctx.root, lang, key))


def _signal(reason, **kw):
    return machine_signal.signal("WHY_REFUSED", reason=reason, **kw)


def _fold():
    return budget.fold_asks(store.read_rows(store.ASKS))


def _new_ask_id():
    return store.next_id(store.read_rows(store.ASKS), "ask", 4, field="ask")


def _write(rows):
    # AMEND-2 G4: ready-made rows, chained from the last line (no re-parse
    # of asks.jsonl per row; the same bytes as one append per row)
    store.append_rows_ready_unlocked(store.ASKS, rows)


def _base(ctx, aid, ev, **fields):
    row = {"ask": aid, "ev": ev, "at": store.iso_at(ctx.now)}
    row.update(fields)
    return row


def current_session(ctx):
    """The session the agent is answering (why_owner_turn.current_session);
    with no ledger yet, Claude Code's tool env.  Once the ledger has rows the
    resolver's None stands: it means the env named another session (the
    veto), and the env must not then supply that session (DGN-1967 bug 18)."""
    import why_owner_turn
    if why_owner_turn.ledger_rows():
        return why_owner_turn.current_session()
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get(
        "CLAUDE_SESSION_ID")


def is_transcription(shape, node):
    return bool(shape == "confirm" and node.get("status") in N.LIVE_CONFIRMED
                and node.get("confirm"))


def build_slots(ctx, cands, shape, known=None):
    """cands: why ids or literal label text. Returns (shown[], error)."""
    known = events.nodes() if known is None else known
    shown = []
    if shape == "split":
        return split_slots(ctx, cands, known)
    if both_pair(cands, shape, known):
        # rev 3: "both" is a first-class answer, first button (dec-334 5f)
        shown.append({"slot": 1, "why": None, "label": t(ctx, "why.ask.both"),
                      "both": list(cands)})
    base = len(shown)
    for i, c in enumerate(cands, base + 1):
        if c in known:
            if known[c]["status"] in ("pivoted", "retired"):
                return None, "terminal-status"
            if shape in YES_SHAPES:
                label = t(ctx, "why.ask.transcribe_yes" if is_transcription(shape, known[c])
                          else "why.ask.confirm_yes")
            else:
                label = known[c]["text"]
                if is_action_label(label):
                    return None, "action-label"
            shown.append({"slot": i, "why": c, "label": label})
        elif c.startswith("why-"):
            return None, "unknown-why"
        else:
            shown.append({"slot": i, "why": None, "label": c})
    confirm = shape == "confirm" and not any(
        is_transcription(shape, known.get(c, {})) for c in cands)
    esc = t(ctx, "why.ask.confirm_no" if confirm else "why.ask.escape")
    shown.append({"slot": len(shown) + 1, "why": None, "label": esc, "escape": True,
                  **({"wrong": True} if confirm else {})})
    return shown, None


def turn_slots(ctx, cands, mode, known):
    """AMEND-2 2.3: the slots of a trigger-`turn` ask, in order: "both" (only
    for exactly two candidates, F7: `both` ids in pick mode, `both_slots`
    in infer mode), one slot per candidate (`why` = a live node, else a
    text slot that a tap gives birth to), "another reason" (`other`, F8),
    then skip / proceed (`escape`).  -> (shown[], error)."""
    shown = []
    if len(cands) == 2 and cands[0] != cands[1]:
        if mode == "pick":
            shown.append({"slot": 1, "why": None, "label": t(ctx, "why.ask.both"),
                          "both": list(cands)})
        else:
            shown.append({"slot": 1, "why": None, "label": t(ctx, "why.ask.both"),
                          "both_slots": [2, 3]})
    for i, c in enumerate(cands, len(shown) + 1):
        if c in known:
            if known[c]["status"] in ("pivoted", "retired"):
                return None, "terminal-status"
            label = known[c]["text"]
            if is_action_label(label):
                return None, "action-label"
            shown.append({"slot": i, "why": c, "label": label})
        elif c.startswith("why-"):
            return None, "unknown-why"
        else:
            if is_action_label(c):
                return None, "action-label"
            shown.append({"slot": i, "why": None, "label": c})
    shown.append({"slot": len(shown) + 1, "why": None,
                  "label": t(ctx, "why.ask.turn.other"), "other": True})
    shown.append({"slot": len(shown) + 1, "why": None,
                  "label": t(ctx, "why.ask.turn.escape"), "escape": True})
    return shown, None


def both_pair(cands, shape, known):
    """AMEND-1 rev 3: an ask between exactly two live Whys (contrast or
    split) offers "both" -- no ask may push toward a single Why."""
    return shape in ("contrast", "split") and len(cands) == 2 and cands[0] != cands[1] \
        and all(c in known and known[c]["status"] not in ("pivoted", "retired")
                for c in cands)


def split_slots(ctx, cands, known):
    """AMEND-1 2.5 split: slot 1 = "both" (rev 3), slot 2 = the said side
    (the contested node), slot 3 = the acted side.  Labels are the two Why
    texts when both fit the button width, else the split.said /
    split.acted constants."""
    if len(cands) != 2 or any(c not in known for c in cands):
        return None, "unknown-why"
    if any(known[c]["status"] in ("pivoted", "retired") for c in cands):
        return None, "terminal-status"
    texts = [known[c]["text"] for c in cands]
    if all(label_width(x) <= BUTTON_LABEL_MAX_WIDTH and not is_action_label(x)
           and "|" not in x and "\n" not in x and "]]" not in x for x in texts) \
            and texts[0] != texts[1]:
        labels = texts
    else:
        labels = [t(ctx, "why.ask.split.said"), t(ctx, "why.ask.split.acted")]
    shown = [{"slot": 1, "why": None, "label": t(ctx, "why.ask.both"), "side": "both",
              "both": list(cands)},
             {"slot": 2, "why": cands[0], "label": labels[0], "side": "said"},
             {"slot": 3, "why": cands[1], "label": labels[1], "side": "acted"},
             {"slot": 4, "why": None, "label": t(ctx, "why.ask.escape"), "escape": True}]
    return shown, None


def provisional_cands(cands, known):
    """N7: candidates that are already confirmed-provisional."""
    return [c for c in cands if (known.get(c) or {}).get("status") == "confirmed-provisional"]


def cand_texts(cands, known):
    """The wording of each candidate: a Why's text, or the literal label
    (grill-3 fix 10: topic keys cover the wording, not only the ids)."""
    return [known[c]["text"] if c in known else c for c in cands]


def label_error(shown):
    seen = set()
    for s in shown:
        lab = s["label"]
        if not lab or "|" in lab or "]]" in lab or "\n" in lab or lab in seen:
            return "label-shape"
        if label_width(lab) > BUTTON_LABEL_MAX_WIDTH:
            return "label-width"
        seen.add(lab)
    return None


def render(ctx, shape, shown, known=None, trigger=None, ask_row=None):
    """Owner-facing text: stem line, free-text hint, labeled OPTIONS marker.
    `ask_row` lets a reading follow-up name its signal (rev 3)."""
    ask_row = ask_row or {}
    known = events.nodes() if known is None else known
    both = any(s.get("both") for s in shown)
    if shape == "confirm" and trigger not in ("turn", "reading"):
        wid = next((s["why"] for s in shown if s.get("why")), None)
        node = known.get(wid, {})
        if not is_transcription(shape, node):
            import voice_vocab
            from owner_term import owner_term, DEFAULT_OWNER_TERM
            address = voice_vocab._persona_field(
                ctx.root, r"^\s*-\s*(?:Call (?:the )?user:?|Address the user as)\s*\*\*(.+?)\*\*")
            address = address.strip("\"'")
            if not address:
                address = owner_term(ctx.root)
                if address == DEFAULT_OWNER_TERM:
                    address = ""
            key = "why.ask.stem.confirm" if address else "why.ask.stem.confirm_no_address"
            stem = t(ctx, key).replace("{address_subject}", _ko_subject(address)) \
                .replace("{address}", address).replace("{text}", node.get("text", ""))
            return stem + "\n[[OPTIONS: %s]]" % " | ".join(s["label"] for s in shown)
    stem = t(ctx, stem_key(shape, trigger, both))
    if trigger == "reading":
        from . import retro
        stem = stem.replace("{signal}", retro.signal_label(
            (ask_row or {}).get("signal")))
    if shape in YES_SHAPES:
        wid = next((s["why"] for s in shown if s.get("why")), None)
        stem = stem.replace("{text}", known.get(wid, {}).get("text", "")) if wid else stem
    if shape == "split":
        side = {s.get("side"): known.get(s.get("why"), {}).get("text", "") for s in shown}
        stem = stem.replace("{said}", side.get("said", "")).replace(
            "{acted}", side.get("acted", ""))
    nodes = [known[s["why"]] for s in shown if s.get("why") in known]
    transcribe = len(nodes) == 1 and is_transcription(shape, nodes[0])
    if transcribe:
        topic = ask_row.get("topic") or ""
        work = ask_row.get("work_ref") or (topic if topic.startswith("DGN-") else None)
        subject = work or ask_row.get("target") or topic
        if subject and not str(subject).startswith("tgt:"):
            from . import surfaces
            path = surfaces.find_ticket(subject, os.path.join(ctx.root, "worklog"))
            if path:
                subject = surfaces.frontmatter(path).get("title") or subject
        if all(n.get("scope") == "framework" for n in nodes) \
                and not work and not ask_row.get("target"):
            subject = t(ctx, "why.ask.subject.framework")
        if not subject:
            subject = " / ".join(n["text"] for n in nodes) or " / ".join(
                s.get("label", "") for s in shown if not s.get("escape")) or t(ctx, "why.ask.subject.work")
        one_line = lambda s: " ".join(str(s).split())
        node = nodes[0]
        picked = store.parse_dt((node["confirm"] or {}).get("said_at"))
        date = picked.astimezone(datetime.timezone(
            datetime.timedelta(seconds=budget.KST))).strftime("%m/%d") if picked else ""
        stem = t(ctx, "why.ask.transcribe" if picked else "why.ask.transcribe.undated").format(
            date=date, subject=one_line(subject), text=one_line(node["text"]))
    # copy row 6: the free-text hint is the second line of every ask but 5f
    # (5f, and the rev-3 reading follow-up, carry their own free-text line;
    # AMEND-2: a turn ask has its own "another reason" button)
    if transcribe or stem_key(shape, trigger, both) in ("why.ask.stem.mixed", "why.ask.stem.reading",
                                          "why.ask.stem.turn"):
        hint = ""
    else:
        hint = t(ctx, "why.ask.free_hint")
    lines = [stem, hint]
    marker = "[[OPTIONS: %s]]" % " | ".join(s["label"] for s in shown)
    return "\n".join(l for l in lines if l) + "\n" + marker


def render_with_gloss(ctx, shape, shown, known=None, trigger=None, ask_row=None,
                      locked=False):
    """Issue copy with each present golden-circle term defined once per
    instance. Pure previews use render(); issuing callers claim the terms
    under the store lock. The inline path already holds that lock.
    """
    text = render(ctx, shape, shown, known, trigger, ask_row)
    terms = [term for term in ("why", "how", "what") if re.search(
        r"(?<![A-Za-z0-9_])" + term.title() + r"(?![A-Za-z0-9_])", text)]
    if not terms:
        return text

    def claim():
        name = store.TERM_GLOSSES
        seen = {r.get("term") for r in store.read_rows(name, ctx.root)}
        new = [term for term in terms if term not in seen]
        lines = [t(ctx, "why.ask.gloss." + term) for term in new]
        if not all(lines):
            raise ValueError("missing framework gloss copy")
        store.append_rows_ready_unlocked(name, [
            {"term": term, "at": store.iso_at(ctx.now)} for term in new], ctx.root)
        body, marker = text.rsplit("\n[[OPTIONS:", 1)
        # the gloss sits right under the question line (owner-shown layout)
        head, _, rest = body.partition("\n")
        return "\n".join([head] + lines + ([rest] if rest else [])) + "\n[[OPTIONS:" + marker

    if locked:
        return claim()
    with store.locked(ctx.root):
        return claim()


def default_shape(n):
    return {0: "scatter", 1: "confirm", 2: "contrast"}.get(n, "set")


def is_umbrella(ctx, wid, known=None):
    """AMEND-1 rev 3 (trial-run rule): a broad umbrella Why is never
    confirmed -- the owner would say "roughly right" to it.  Umbrella = other
    Whys hang under it (it is a parent in the DAG), or it is read / acted on
    across WHY_UMBRELLA_WORKS or more distinct work items."""
    from . import acts, retro
    known = events.nodes() if known is None else known
    if any(wid in (n.get("parents") or ()) and n["status"] not in ("pivoted", "retired")
           for n in known.values()):
        return True
    works = set()
    w = budget.canonical_topic((known.get(wid) or {}).get("work") or "")
    if w and w.startswith("DGN-"):
        works.add(w)
    for a in acts.act_rows():
        t_ = budget.canonical_topic(a.get("topic") or "")
        if a.get("why") == wid and t_ and t_.startswith("DGN-"):
            works.add(t_)
    works |= retro.reading_works(wid)
    return len(works) >= ctx.conf_int("WHY_UMBRELLA_WORKS")


def _umbrella_refusal(ctx, shape, cands, known):
    """Confirm-shaped asks (yes + escape) refuse on an umbrella Why."""
    if shape in YES_SHAPES and any(c in known and is_umbrella(ctx, c, known) for c in cands):
        return "umbrella"
    return None


def _ko_subject(word):
    """word + Korean subject particle: i after a final consonant, ga after
    a vowel (Hangul syllables); non-Hangul endings take i."""
    if not word:
        return word
    code = ord(word[-1]) - 0xAC00
    if 0 <= code <= 11171 and code % 28 == 0:
        return word + "\uac00"
    return word + "\uc774"


def cmd_ask(ctx, a, out=None):
    if ctx.enabled():
        (out or sys.stdout).write(_signal("retired-use-four-slot-intake") + "\n")
        return EXIT_FLOOR
    out = out or sys.stdout
    if a.reveal:
        return cmd_reveal(ctx, a, out)
    if a.trigger not in TRIGGERS or a.shape not in SHAPES or not a.topic:
        return EXIT_USAGE
    topic = budget.canonical_topic(a.topic)
    if topic is None:
        out.write(_signal("bad-topic") + "\n")
        return EXIT_FLOOR
    cands = list(a.cand or [])
    if a.trigger == "fork" and len(cands) < 2:
        out.write(_signal("no-fork", topic=topic) + "\n")
        return EXIT_FLOOR
    if a.shape in ("split", "readings"):
        return EXIT_USAGE  # split asks are act-driven: enqueue only (N9)
    return issue_inline(ctx, out, a.trigger, a.shape, topic, cands, a.work, a.urgent)


def issue_inline(ctx, out, trigger, shape, topic, cands, work=None, urgent=False,
                 gate=None, moved_from=None):
    """The inline ask core (cmd_ask; rev 3 `gate`).  Every never-ask check,
    the same budgets.  Prints the ask and returns 0, or records the refusal
    and returns 4.  `gate` (irreversible | trial) lifts N7 for the asks a
    gate may pull inline; `moved_from` = the queued ask it replaces.

    AMEND-2 2.6: a locked wrapper over issue_inline_unlocked."""
    if ctx.enabled():
        out.write(_signal("retired-use-four-slot-intake") + "\n")
        return EXIT_FLOOR
    with store.locked():
        rc, res = issue_inline_unlocked(ctx, trigger, shape, topic, cands, work, urgent,
                                        gate, moved_from)
    if res.get("signal"):
        out.write(res["signal"] + "\n")
    if res.get("text") is not None:
        out.write(res["text"] + "\n")
    return rc


def issue_inline_unlocked(ctx, trigger, shape, topic, cands, work=None, urgent=False,
                          gate=None, moved_from=None, frame=None, queue=True,
                          session_id=None, known=None, asks=None):
    """issue_inline() for a caller holding store.locked().  Prints nothing:
    -> (rc, {"signal": line | None, "text": the ask | None, "ask": id | None,
    "reason": refusal | None}).

    `frame` (AMEND-2 2.5, trigger `turn` only): the extra `create` row fields
    (frame_otl, tf, mode, target, stakes, cands, cs, ..).  A turn ask's slots
    are turn_slots(); its per-turn cap counts the turn asks of its own
    frame_otl (F15).  `queue` False: a refusal writes create + refused only
    (a turn ask is never queued or parked)."""
    if ctx.enabled():
        return EXIT_FLOOR, dict(signal=_signal("retired-use-four-slot-intake"),
                               text=None, ask=None, reason="retired-use-four-slot-intake")
    res = {"signal": None, "text": None, "ask": None, "reason": None}
    known = events.nodes() if known is None else known
    if trigger not in N7_EXEMPT and gate is None and provisional_cands(cands, known):
        # AMEND-1 N7: asking for words again adds nothing; acts move it.
        res.update(signal=_signal("provisional", topic=topic), reason="provisional")
        return EXIT_BUDGET, res
    um = _umbrella_refusal(ctx, shape, cands, known)
    if um:
        res.update(signal=_signal(um, topic=topic), reason=um)
        return EXIT_BUDGET, res
    if frame is not None:
        shown, err = turn_slots(ctx, cands, frame.get("mode"), known)
    else:
        shown, err = build_slots(ctx, cands, shape, known)
    err = err or label_error(shown)
    if err:
        res.update(signal=_signal(err, topic=topic), reason=err)
        return EXIT_FLOOR, res
    sid = session_id or current_session(ctx)
    asks = _fold() if asks is None else asks
    texts = cand_texts(cands, known)
    keys = budget.topic_keys(topic, work, [x["why"] for x in shown if x.get("why")],
                             texts, (frame or {}).get("target"))
    reason, flags = budget.check(ctx, topic, "inline", urgent, sid,
                                 {k: v for k, v in asks.items() if k != moved_from}, keys)
    if reason is None and frame is not None:
        # F15: the per-turn cap of a turn ask counts its own frame's asks,
        # never the owner-turn ring (ups for N+1 can move it mid-turn)
        mine = [a for a in asks.values() if a.get("trigger") == "turn"
                and a.get("frame_otl") == frame.get("frame_otl")
                and "refused" not in a["events"]]
        if len(mine) >= ctx.conf_int("WHY_ASK_PER_TURN"):
            reason = "budget-turn"
    aid = _new_ask_id()
    res["ask"] = aid
    common = dict(kind="why", trigger=trigger, shape=shape, topic=topic,
                  work_ref=work, channel="inline", session_id=sid,
                  bubble_message_id=None, shown=shown, refuse_reason=None,
                  cand_texts=texts)
    if gate:
        common["gate"] = gate
    if moved_from:
        common["moved_from"] = moved_from
    if flags.get("rush_would_refuse"):
        common["rush_would_refuse"] = True
    if frame is not None:
        common.update(frame)
        common["cs"] = "cs-" + aid.split("-", 1)[1]
    if reason:
        res["reason"] = reason
        if moved_from and frame is None:
            # a gate that cannot ask leaves the queued ask where it was
            res.update(signal=_signal(reason, topic=topic, ask=moved_from), ask=None)
            return EXIT_BUDGET, res
        rows = [_base(ctx, aid, "create", **common),
                _base(ctx, aid, "refused", refuse_reason=reason)]
        if not queue:
            pass
        elif flags.get("park"):
            rows.append(_base(ctx, aid, "parked", refuse_reason=reason))
        elif not reason.startswith("topic-"):
            rows.append(_base(ctx, aid, "queued", channel="morning"))
        res["signal"] = _signal(reason, topic=topic, ask=aid)
        res["rows"] = rows
        _write(rows)
        return EXIT_BUDGET, res
    # grill-2 fix 6: the exact text this ask prints.  It is `shown` only
    # when a reply carries it verbatim, under its own question line.
    text = render_with_gloss(ctx, shape, shown, known, trigger, common, locked=True)
    # grill-3 fix 5: the exact issue time (`at` is whole seconds) -- the
    # bubble that carries this ask is one the bridge sent after it
    rows = [_base(ctx, aid, "create", rendered=text, issued_ts=ctx.now, **common)]
    if moved_from:
        rows.insert(0, _base(ctx, moved_from, "moved", moved_to=aid))
    res["rows"] = rows
    _write(rows)
    res["text"] = text
    return EXIT_OK, res


def work_whys(ctx, work, known=None):
    """Every live Why of a work item: the ticket's `why:` ids (several
    allowed, rev 3) and the nodes proposed for it (`propose --work`)."""
    from . import surfaces
    known = events.nodes() if known is None else known
    out = []
    path = surfaces.find_ticket(work, os.path.join(ctx.root, "worklog"))
    if path:
        out += surfaces.ticket_why(path, known, os.path.dirname(path)).get("whys") or []
    for wid in sorted(known):
        if budget.canonical_topic(known[wid].get("work") or "") == work and wid not in out:
            out.append(wid)
    return [w for w in out if w in known and known[w]["status"] not in ("pivoted", "retired")]


# Which queued asks a gate may pull inline, in morning priority order.
# Final grill B5: `before-work` too -- a gate the budget refused queues its
# own ask, and the next gate on the work item must move that one, not build
# a second.
GATE_TRIGGERS = ("act-contest", "fork-mixed", "contest", "act-ripe", "ripe",
                 "fork", "correction", "new-work", "before-work")


def cmd_gate(ctx, a, out=None):
    """AMEND-1 rev 3 timing: `why-net gate --work DGN-N --before
    irreversible|trial`.  Run before work that is hard to undo or expected
    to need much trial and error.  When the work item has a Why that is a
    hypothesis, provisional or contested, the confirm / contest / split ask
    for it fires INLINE now (same shapes, same budgets): a queued one is
    moved inline, else a fresh one is built for open hypotheses / contests.
    A provisional Why with nothing queued is not re-asked (N7: the owner
    already said it).  Exit 0 with the ask printed, or WHY_OK gate=clear;
    exit 4 when the budget refuses (the queued ask stays queued).

    AMEND-2 2.10: `--target T` is an alternative to `--work`, and the
    current frame (the newest owner turn's otl) is read first:
      an open turn ask of this otl     -> its text again, gate=frame-pick
      a turn ask on this work / target declined (skip / proceed) within
      WHY_ASK_PARK_DAYS                -> WHY_OK gate=owner-deferred
      stakes high, no live confirmed / provisional Why and no answered turn
      ask, whatever the mode           -> exit 4 frame-undecided
      anything else                    -> the logic above, byte-equal."""
    if ctx.enabled():
        (out or sys.stdout).write(_signal("retired-use-four-slot-intake") + "\n")
        return EXIT_FLOOR
    out = out or sys.stdout
    from . import derive
    work = budget.canonical_topic(a.work or "")
    if work is not None and work.startswith(budget.TGT_PREFIX):
        work = None
    target = getattr(a, "target", None)
    if (not work and not target) or a.before not in GATE_REASONS:
        return EXIT_USAGE
    if target:
        from . import frame as F
        try:
            target, _scope = F.resolve_target(ctx.root, target)
        except events.WhyRefusal as r:
            out.write(_signal(r.reason) + "\n")
            return EXIT_FLOOR
    known = events.nodes()
    rc = _gate_frame(ctx, out, work, target, known)
    if rc is not None:
        return rc
    whys = work_whys(ctx, work, known) if work else []
    if target:
        whys += [w for w in sorted(known) if known[w].get("target") == target
                 and known[w]["status"] not in ("pivoted", "retired") and w not in whys]
    label = {"work": work} if work else {"target": target}
    topic = work or budget.TGT_PREFIX + target
    open_ = [w for w in whys if known[w]["status"] in GATE_OPEN]
    if not open_:
        out.write(machine_signal.signal("WHY_OK", gate="clear", **label) + "\n")
        return EXIT_OK
    asks = _fold()
    keys = set(open_) | {topic}
    for q in derive.morning_order(asks):
        if q.get("trigger") not in GATE_TRIGGERS or q.get("kind") != "why":
            continue
        if not (keys & ({q.get("topic"), q.get("work_ref")}
                        | {s.get("why") for s in q.get("shown") or []})):
            continue
        cands = [s["why"] if s.get("why") else s["label"] for s in q.get("shown") or []
                 if not s.get("escape") and not s.get("both")]
        return issue_inline(ctx, out, q["trigger"], q.get("shape"), q["topic"], cands,
                            q.get("work_ref") or work, gate=a.before, moved_from=q["ask"])
    hyps = [w for w in open_ if known[w]["status"] == "hypothesis"]
    cont = [w for w in open_ if known[w]["status"] == "contested"]
    if cont:
        return issue_inline(ctx, out, "contest", "confirm", cont[0], [cont[0]], work,
                            gate=a.before)
    if hyps:
        hyps = hyps[:READING_MAX_CANDS]
        return issue_inline(ctx, out, "before-work", default_shape(len(hyps)), topic, hyps,
                            work, gate=a.before)
    out.write(machine_signal.signal("WHY_OK", gate="provisional", **label) + "\n")
    return EXIT_OK


def _gate_frame(ctx, out, work, target, known):
    """The AMEND-2 2.10 frame rules of `gate`; None = today's logic."""
    from . import frame as F
    cur = F.current_frame(ctx)
    asks = _fold()
    keys = {work} if work else set()
    if target:
        keys.add(budget.TGT_PREFIX + target)
    label = {"work": work} if work else {"target": target}
    opn = [x for x in cur["asks"] if x.get("state") in F.OPEN_STATES]
    if opn:
        x = opn[-1]
        if x.get("rendered"):
            out.write(x["rendered"] + "\n")
        out.write(machine_signal.signal("WHY_OK", gate="frame-pick", ask=x["ask"]) + "\n")
        return EXIT_OK
    mine = [x for x in F.turn_asks(asks) if budget.ask_topic_keys(x) & keys]
    park = ctx.conf_int("WHY_ASK_PARK_DAYS") * budget.DAY
    if any(x.get("state") == "declined" and (x.get("resolved_at") or 0) >= ctx.now - park
           for x in mine):
        out.write(machine_signal.signal("WHY_OK", gate="owner-deferred", **label) + "\n")
        return EXIT_OK
    if cur["stakes"] == "high":
        whys = work_whys(ctx, work, known) if work else []
        held = [w for w in whys if known[w]["status"] in ("confirmed", "confirmed-provisional")]
        held += [w for w in known if target and known[w].get("target") == target
                 and known[w]["status"] in ("confirmed", "confirmed-provisional")]
        answered = [x for x in mine if x.get("state") == "answered"]
        if not held and not answered:
            out.write(_signal("frame-undecided", **label) + "\n")
            return EXIT_BUDGET
    return None


def reveal_error(shown, known):
    """AMEND-1 1.2 (F1): the slot validation of `ask` -- every why- id
    exists and is live, and no label is an action label."""
    for s in shown:
        w = s.get("why")
        if w is not None:
            if w not in known or known[w]["status"] in ("pivoted", "retired"):
                return "reveal-invalid"
        if is_action_label(s.get("label")):
            return "reveal-invalid"
    return None


def cmd_reveal(ctx, a, out=None):
    """Record label->Why for OPTIONS the agent already emits. No budget, no
    stdout -- except the refusal signal (exit 4 reveal-invalid)."""
    if ctx.enabled():
        (out or sys.stdout).write(_signal("retired-use-four-slot-intake") + "\n")
        return EXIT_FLOOR
    out = out or sys.stdout
    topic = budget.canonical_topic(a.topic)
    if not topic or not a.opt:
        return EXIT_USAGE
    shown = []
    for i, spec in enumerate(a.opt, 1):
        if "=" not in spec:
            return EXIT_USAGE
        label, wid = spec.rsplit("=", 1)
        slot = {"slot": i, "why": wid.strip() or None, "label": label.strip()}
        if slot["why"] is None and slot["label"] == t(ctx, "why.ask.escape"):
            slot["escape"] = True  # a defer, not an untagged slot (rule D')
        shown.append(slot)
    with store.locked():
        err = reveal_error(shown, events.nodes())
        if err:
            out.write(_signal(err, topic=topic) + "\n")
            return EXIT_BUDGET
        aid = _new_ask_id()
        common = dict(kind="why", trigger="reveal", shape="reveal", topic=topic,
                      work_ref=a.work, channel="inline", session_id=current_session(ctx),
                      bubble_message_id=None, shown=shown, refuse_reason=None)
        _write([_base(ctx, aid, "create", issued_ts=ctx.now, **common)])
    return EXIT_OK


def enqueue(ctx, kind, trigger, topic, cands, shape=None, work=None):
    """Retired public API; retained for source compatibility without writes."""
    return None, "retired"


def cmd_enqueue(ctx, a, out=None):
    """Old automation receives a deterministic refusal and no queue writes."""
    (out or sys.stdout).write(_signal("retired") + "\n")
    return EXIT_FLOOR
