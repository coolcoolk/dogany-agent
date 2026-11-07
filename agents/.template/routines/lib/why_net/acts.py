"""why_net.acts -- owner decisions and actions linked to a Why node.

Spec: docs/design/v27-t1-why-net-AMEND-1.md sections 1.2, 2.1-2.3, 3.3.

What the owner says is the What; how the owner decides and acts is the Why
(dec-335).  An act is one owner decision or action, linked to one Why node,
in state/why-net/acts.jsonl (append-only, hash chained, same lock):

  A1 decision    a pick on a reveal-tagged [[OPTIONS]] menu (derive, from
                 the probe; rule D', delegated = the recommended slot)
  A2 decision    a machine-confirmed decision-ledger entry (agent, floor)
  A3 correction  a correction record (agent, floor)
  A4 accept      ticket -> done with evidence, owner turn names it (lint)
  A5 reject      ticket -> dismissed / reopened (lint; class unknown forever)
  A6 initiate    ticket created in a typed owner turn (lint)

Acts are recorded quietly (I4, N8): nothing here prints owner copy.  The
confirm mechanics are the shared library's (lib/confirm_mechanics.py,
dec-336); this module only builds Evidence rows from THIS store and writes
the Why net's own results (settle / contest) through events.py.

Pure stdlib.  English/ASCII only.
"""

import collections
import os
import re
import sys

from . import WhyRefusal, budget, events, store
from . import nodes as N

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import confirm_mechanics as cm  # noqa: E402
import decision_ledger_fmt as fmt  # noqa: E402
import why_owner_turn  # noqa: E402

KINDS = ("decision", "correction", "accept", "reject", "initiate")
DIRECTIONS = ("consistent", "contradicts", "unknown")
CLASSES = cm.CLASSES
LINKS = ("agent-prior", "agent", "machine")
MACHINE_KINDS = ("accept", "reject", "initiate")
NOTE_MAX = 120
NOTE_OVERLAP = 8  # a shared run this long (whitespace removed) is a quote
DAY = 86400
POLICY_TAG = events.SETTLE_POLICY

# Lib state vocabulary (3.2) for each Why-net status.
STATE_OF = {"hypothesis": "hypothesis", N.PROVISIONAL: "provisional",
            "confirmed": "confirmed", "contested": "contested"}

_DEC_MARK = fmt.MARK_RE
_QUOTE_MARKS = ('"', "\u201c", "\u201d", "\u300c", "\u300d", "\u300e", "\u300f",
                "\u2018", "\u2019")


def policy(ctx):
    """WHY_POLICY (2.1) with the conf keys of this instance."""
    n = ctx.conf_int("WHY_SETTLE_WINS")
    return {"threshold": n, "window_n": ctx.conf_int("WHY_ACT_WINDOW_N"),
            "distinct_turns": n, "distinct_topics": 2, "min_prior": 1,
            "ripe_min": 2, "mixed_after": 2}


def dedupe(otl, wid):
    return cm.dedupe_key(otl, wid)


def act_rows(rows=None):
    rows = store.read_rows(store.ACTS) if rows is None else rows
    return [r for r in rows if r.get("act")]


def voided(evs):
    return {(e.get("data") or {}).get("act") for e in evs if e.get("ev") == "act-void"}


def _fingerprint(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k != "prev"}
    return store.sha256_text(store.dumps(obj))


# -- owner turns ----------------------------------------------------------

def ledger_row(otl):
    for r in store.read_rows(store.TURN_LEDGER):
        if r.get("id") == otl:
            return r
    return None


def latest_owner_row(session_id):
    rows = why_owner_turn.ledger_rows(session_id) if session_id else []
    return rows[-1] if rows else None


def _transcript(row):
    return why_owner_turn.session_transcript(row.get("session_id"))


def turn_context(row):
    """The ledgered owner turn `row` on its transcript's main chain ->
    {"owner_text", "prev_reply", "prev_tools"}: the owner's own text, and the
    assistant reply right before it (its text and the files its tool calls
    wrote).  None when the turn is not on the chain (not a real turn)."""
    if not row or row.get("skip"):
        return None
    path = _transcript(row)
    if not path:
        return None
    try:
        on_chain, _every, _chain, entries = why_owner_turn.chain_turns(path)
    except (OSError, UnicodeDecodeError):
        return None
    bound, _ = why_owner_turn.bind_ledger(
        why_owner_turn.ledger_rows(row.get("session_id")), on_chain)
    uuid = next((u for u, r in bound.items() if r.get("id") == row.get("id")), None)
    if uuid is None:
        return None
    idx = next((i for i, e in enumerate(entries) if e.get("uuid") == uuid), None)
    if idx is None:
        return None
    owner = why_owner_turn.entry_text(entries[idx]) or ""
    texts, tools = [], []
    for e in reversed(entries[:idx]):
        if e.get("type") == "assistant":
            texts.extend(reversed(why_owner_turn.assistant_texts(e)))
            content = (e.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    fp = (b.get("input") or {}).get("file_path")
                    if isinstance(fp, str):
                        tools.append(fp)
            continue
        if why_owner_turn.entry_text(e) is not None:
            break
    return {"owner_text": owner, "prev_reply": "\n".join(reversed(texts)),
            "prev_tools": tools}


def owner_turn_of(otl, env=None):
    """Linking floor (1.2): the otl must be a real owner turn -- a non-skip,
    non-voice ledger row on its transcript's main chain, outside a dispatch
    worker.  -> (row, context) or WhyRefusal act-unlinked."""
    env = os.environ if env is None else env
    if env.get(why_owner_turn.DISPATCH_ENV_MARKER):
        raise WhyRefusal("act-unlinked", why="dispatch-worker")
    row = ledger_row(otl) if otl else None
    if not row or row.get("skip") or row.get("source") == "voice":
        raise WhyRefusal("act-unlinked", why="no-owner-turn")
    tc = turn_context(row)
    if tc is None:
        raise WhyRefusal("act-unlinked", why="turn-off-chain")
    return row, tc


# -- decision ledger (A2, the informational dec join) ---------------------

def _ledger_path(ctx):
    p = ctx.conf("DECISION_LEDGER_PATH")
    if not p:
        return None
    return p if os.path.isabs(p) else os.path.join(ctx.root, p)


def decisions(ctx):
    """Machine entries of the decision ledger: dec id -> {state, req,
    labels, fingerprint}.  `req` is set only on a machine confirm (6.2)."""
    path = _ledger_path(ctx)
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except (OSError, TypeError):
        return out
    cur = None
    for i, line in enumerate(lines):
        m = _DEC_MARK.search(line)
        if m:
            head = lines[i - 1] if i > 0 else ""
            cur = {"dec": m.group(1), "state": m.group(2), "req": m.group(3),
                   "labels": [], "fingerprint": store.sha256_text(head + "\n" + line)}
            out[m.group(1)] = cur
            continue
        if line.startswith("#"):
            cur = None
            continue
        labels = fmt.options_of_line(line)
        if cur is not None and labels is not None:
            cur["labels"].extend(labels)
    return out


def machine_decs(decs):
    """request_id -> dec id, machine-confirmed entries only."""
    return {d["req"]: k for k, d in decs.items() if d["state"] == "confirmed" and d["req"]}


def seed_decs(evs):
    return {((e.get("data") or {}).get("source") or {}).get("dec") for e in evs
            if e.get("actor") == "seed"} - {None}


# -- the act row ------------------------------------------------------------

def _append(rows, fields):
    """Builder body: one act row, refusing a second act for the same
    (owner turn, node) (F4)."""
    key = dedupe(fields["owner_turn"]["otl"], fields["why"])
    if any(r.get("dedupe") == key for r in act_rows(rows)):
        raise WhyRefusal("act-same-turn")
    row = {"act": store.next_id(act_rows(rows), "act", 4, field="act")}
    row.update(fields)
    row["dedupe"] = key
    return row


def _write(fields):
    """Caller holds store.locked()."""
    return store.append_unlocked(store.ACTS, lambda rows: _append(rows, fields))


def _fields(kind, ref, wid, direction, cls, link, topic, row, by, at=None,
            dec=None, note=None, ref_sha=None):
    return {"at": at or row.get("at") or store.now_iso(), "kind": kind, "ref": ref,
            "dec": dec, "why": wid, "direction": direction, "class": cls,
            "link": link, "topic": topic,
            "owner_turn": {"otl": row.get("id"), "session_id": row.get("session_id"),
                           "request_id": row.get("request_id")},
            "by": by, "note": note, "ref_sha": ref_sha}


def _live(nodes, wid):
    n = nodes.get(wid)
    return n is not None and n["status"] not in N.TERMINAL


# -- A1: reveal picks -> acts (derive; also first thing in `act`) -----------

def _probe_rows(probes):
    return [p for p in probes if "verbatim" in p and p.get("id")]


def _reveal_picks(probes):
    for p in _probe_rows(probes):
        if p.get("trigger") != "reveal" or p.get("verdict") != "pick":
            continue
        if (p.get("source") or {}).get("inbound_source") == "voice":
            continue  # Q6: a voice-answered reveal pick is no act (GS25)
        if not p.get("otl"):
            continue
        yield p


def d_prime(shown, nodes):
    """Rule D' (1.2): every non-escape slot tagged with a live Why, >= 2
    distinct live Whys.  -> (holds, {why: slot count})."""
    tags = collections.Counter()
    ok = True
    for s in shown or []:
        if s.get("escape"):
            continue
        w = s.get("why")
        if not w or not _live(nodes, w):
            ok = False
            continue
        tags[w] += 1
    return ok and len(tags) >= 2, tags


def a1_fields(probe, nodes, asks, decs_by_req, row):
    """The A1 rows of one reveal pick probe: one per Why on the menu."""
    shown = probe.get("shown") or []
    by_slot = {s.get("slot"): s for s in shown}
    picked = by_slot.get((probe.get("slots") or [None])[0]) or {}
    holds, tags = d_prime(shown, nodes)
    rec = probe.get("recommended")
    # fail closed (1.2): an unreadable carrying reply makes the pick delegated
    delegated = rec is None or (rec != "" and store.nfc(rec).strip() == store.nfc(picked.get("label")).strip())
    topic = (asks.get(probe.get("ask")) or {}).get("topic")
    dec = decs_by_req.get(row.get("request_id"))
    out = []
    for wid in sorted(set(s.get("why") for s in shown if s.get("why"))):
        if not _live(nodes, wid):
            continue
        if picked.get("why") == wid:
            direction = "consistent"
        elif picked.get("why"):
            direction = "contradicts"
        else:
            direction = "unknown"
        if not holds or tags.get(wid) != 1 or direction == "unknown":
            cls = "unknown"
        else:
            cls = "delegated" if delegated else "choice"
        out.append(_fields("decision", probe["id"], wid, direction, cls, "agent-prior",
                           topic, row, "derive", at=probe.get("at"), dec=dec,
                           ref_sha=_fingerprint(probe)))
    return out


def convert_reveals_unlocked(ctx, decs=None):
    """Turn every not-yet-converted reveal pick probe into A1 rows.  A probe
    whose owner turn is not (yet) on its transcript chain waits.  Caller
    holds store.locked().  -> the new rows."""
    probes = store.read_rows(store.PROBES)
    have = act_rows()
    done = {(r.get("ref"), r.get("why")) for r in have}
    keys = {r.get("dedupe") for r in have}
    nodes = N.fold(store.read_rows(store.EVENTS))
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    decs = decisions(ctx) if decs is None else decs
    by_req = machine_decs(decs)
    new = []
    for p in _reveal_picks(probes):
        if any(ref == p["id"] for ref, _w in done):
            continue
        row = ledger_row(p["otl"])
        if not row or row.get("skip") or row.get("source") == "voice":
            continue
        if turn_context(row) is None:
            continue
        for f in a1_fields(p, nodes, asks, by_req, row):
            key = dedupe(f["owner_turn"]["otl"], f["why"])
            if key in keys:
                continue  # never two acts for one (turn, node)
            new.append(_write(f))
            keys.add(key)
            done.add((p["id"], f["why"]))
    return new


def pending_reveal(otl, wid):
    """True when a reveal pick of `otl` names `wid` and has no act yet."""
    have = {(r.get("ref"), r.get("why")) for r in act_rows()}
    for p in _reveal_picks(store.read_rows(store.PROBES)):
        if p.get("otl") == otl and any(s.get("why") == wid for s in p.get("shown") or []) \
                and (p["id"], wid) not in have:
            return True
    return False


# -- the `act` verb (A2, A3) -------------------------------------------------

def check_note(note, probes=None, corrections=None):
    """F14: <= 120 chars (usage error) and no owner verbatim (quote refusal):
    owner words live only in probes and corrections."""
    if note is None or note == "":
        return None
    if len(note) > NOTE_MAX:
        raise ValueError("note-too-long")
    if any(q in note for q in _QUOTE_MARKS):
        raise WhyRefusal("note-quote")
    flat = "".join(store.nfc(note).split())
    words = [p.get("verbatim") for p in probes or store.read_rows(store.PROBES)
             if isinstance(p.get("verbatim"), str)]
    words += [c.get("text") for c in corrections or store.read_rows(store.CORRECTIONS)
              if isinstance(c.get("text"), str)]
    for w in words:
        w = "".join(store.nfc(w).split())
        if len(w) >= NOTE_OVERLAP and any(
                w[i:i + NOTE_OVERLAP] in flat for i in range(len(w) - NOTE_OVERLAP + 1)):
            raise WhyRefusal("note-quote")
        if 4 <= len(w) < NOTE_OVERLAP and w in flat:
            raise WhyRefusal("note-quote")
    return note


def _ticket_resolves(path, wid, nodes, root):
    from . import surfaces
    if not path or not os.path.isfile(path):
        return False
    r = surfaces.ticket_why(path, nodes, os.path.join(root, "worklog"))
    return wid in (r.get("whys") or [r.get("why")])


def _names_work(text, work):
    if not work:
        return False
    t = budget.canonical_topic(work)
    for m in re.finditer(r"\b[A-Za-z]+-\d+\b", text or ""):
        if budget.canonical_topic(m.group(0)) == t:
            return True
    return False


def _a2_floor(ctx, ref, wid, nodes, evs, decs):
    """A2 floor (1.2, F4/F6/F16).  -> (owner row, dec entry)."""
    d = decs.get(ref)
    if not d or d["state"] != "confirmed" or not d["req"]:
        raise WhyRefusal("act-unlinked", why="dec-not-machine-confirmed")
    rows = [r for r in store.read_rows(store.TURN_LEDGER) if r.get("request_id") == d["req"]]
    if len(rows) != 1 or rows[0].get("skip"):
        raise WhyRefusal("act-unlinked", why="dec-req-no-turn")
    row = rows[0]
    cur = why_owner_turn.current_session()
    if cur and row.get("session_id") != cur:
        raise WhyRefusal("act-unlinked", why="dec-other-session")
    row, tc = owner_turn_of(row.get("id"))
    # F4: one act per (owner turn, node); a reveal's A1 row wins its turn
    key = dedupe(row.get("id"), wid)
    if any(r.get("dedupe") == key for r in act_rows()) or pending_reveal(row.get("id"), wid):
        raise WhyRefusal("act-same-turn")
    if ref in seed_decs(evs):
        raise WhyRefusal("act-unlinked", why="dec-is-seed")
    labels = [store.nfc(x).strip() for x in d["labels"]]
    for a in budget.fold_asks(store.read_rows(store.ASKS)).values():
        if a.get("session_id") != row.get("session_id"):
            continue
        shown = [store.nfc(s.get("label")).strip() for s in a.get("shown") or []]
        if labels and shown == labels:
            raise WhyRefusal("act-unlinked", why="dec-is-why-menu"
                             if a.get("trigger") != "reveal" else "dec-is-reveal")
    n = nodes[wid]
    if not (_names_work(tc["prev_reply"], n.get("work"))
            or any(_ticket_resolves(p, wid, nodes, ctx.root) for p in tc["prev_tools"])):
        raise WhyRefusal("act-unlinked", why="dec-not-about-why")
    return row, d


def _a3_floor(ctx, ref, wid, nodes):
    from . import surfaces
    rec = next((r for r in surfaces.fold_corrections() if r.get("id") == ref), None)
    if rec is None or not rec.get("otl"):
        raise WhyRefusal("act-unlinked", why="no-correction")
    row, _tc = owner_turn_of(rec["otl"])
    n = nodes[wid]
    work = rec.get("work")
    same = work and n.get("work") and budget.canonical_topic(work) == budget.canonical_topic(n["work"])
    if not same:
        path = surfaces.find_ticket(work, os.path.join(ctx.root, "worklog")) if work else None
        if not _ticket_resolves(path, wid, nodes, ctx.root):
            raise WhyRefusal("act-unlinked", why="correction-other-work")
    return row, rec


def record(ctx, kind, ref, wid, direction, note=None):
    """`why-net act` (agent): A2 decision / A3 correction.  Every floor runs
    under the store lock; a refusal writes nothing.  Reveal picks are
    converted first, so a machine A1 row always wins its (turn, node)."""
    if kind not in ("decision", "correction"):
        raise WhyRefusal("act-unlinked", why="kind-is-machine")
    if direction not in ("consistent", "contradicts"):
        raise ValueError("bad-direction")
    if os.environ.get(why_owner_turn.DISPATCH_ENV_MARKER):
        raise WhyRefusal("act-unlinked", why="dispatch-worker")
    check_note(note)
    with store.locked():
        decs = decisions(ctx)
        convert_reveals_unlocked(ctx, decs)
        evs = store.read_rows(store.EVENTS)
        nodes = N.fold(evs)
        n = nodes.get(wid)
        if n is None or n["status"] in N.TERMINAL:
            raise WhyRefusal("act-unlinked", why="node")
        if kind == "decision":
            row, d = _a2_floor(ctx, ref, wid, nodes, evs, decs)
            cls, topic, sha = "choice", budget.canonical_topic(ref) or ref, d["fingerprint"]
        else:
            row, rec = _a3_floor(ctx, ref, wid, nodes)
            cls = "activity" if direction == "consistent" else "choice"
            topic = budget.canonical_topic(rec.get("work") or "") or n.get("work") or wid
            sha = _fingerprint({k: rec.get(k) for k in ("id", "said_at", "text", "session_id")})
        if pending_reveal(row["id"], wid):
            raise WhyRefusal("act-same-turn")
        return _write(_fields(kind, ref, wid, direction, cls, "agent", topic, row, "agent",
                              at=store.iso_at(ctx.now), dec=ref if kind == "decision" else None,
                              note=note, ref_sha=sha))


# -- machine acts (A4 / A5 / A6, from the ticket lint) ------------------------

# A4 go-ahead words beyond the ask._ACTION_LABEL corpus: merge / ship
# imperatives (owner-speech corpus, matched never shown).
_GO_AHEAD = re.compile(
    "^(\uba38\uc9c0|\ubcd1\ud569|\ubc30\ud3ec|merge|ship)\\s*"
    "(\ud574|\ud574\uc918|\ud574\uc8fc\uc138\uc694|\ud558\uc790|it)?[.!~ ]*$",
    re.IGNORECASE)


def is_go_ahead(text):
    from . import ask
    t = store.nfc(text).strip()
    return ask.is_action_label(t) or bool(_GO_AHEAD.match(t))


def _names_ticket(text, tid, path):
    from . import surfaces
    t = store.nfc(text)
    if _names_work(t, tid):
        return True
    fm = surfaces.frontmatter(path)
    for k, v in fm.items():
        if k.startswith("display_title") and v and len(v.strip()) >= 2 and store.nfc(v).strip() in t:
            return True
    return False


def record_machine(ctx, kind, ticket_path, session_id, env=None):
    """`why-net act --machine`: the lint saw a ticket transition in an
    owner-session turn.  -> the row, or None (no act: silently)."""
    from . import surfaces
    env = os.environ if env is None else env
    if kind not in MACHINE_KINDS or env.get(why_owner_turn.DISPATCH_ENV_MARKER):
        return None
    if not ticket_path or not os.path.isfile(ticket_path):
        return None
    tid = surfaces.ticket_id_of(ticket_path)
    row = latest_owner_row(session_id)
    if not row or row.get("source") == "voice":
        return None  # cron / worker / headless sessions have no ledger row
    if kind == "initiate" and row.get("source") != "message":
        return None  # A6: a typed owner turn only (GS12)
    tc = turn_context(row)
    if tc is None:
        return None
    if kind in ("accept", "reject"):
        named = _names_ticket(tc["owner_text"], tid, ticket_path)
        go = is_go_ahead(tc["owner_text"]) and _names_ticket(
            tc["prev_reply"], tid, ticket_path)
        if not (named or go):
            return None
    with store.locked():
        nodes = N.fold(store.read_rows(store.EVENTS))
        r = surfaces.ticket_why(ticket_path, nodes, os.path.dirname(os.path.abspath(ticket_path)))
        # AMEND-1 rev 3: a ticket may serve several Whys; the act counts for
        # each (one row per (turn, node), F4).  -> the first row written.
        wids = [w for w in (r.get("whys") or [r.get("why")]) if w and _live(nodes, w)]
        if not wids:
            return None
        mine = [a for a in act_rows() if a.get("ref") == tid]
        if kind == "initiate" and any(a["kind"] == "initiate" for a in mine):
            return None
        if kind == "accept":
            last = [a["kind"] for a in mine if a["kind"] in ("accept", "reject")]
            if last and last[-1] == "accept":
                return None
        if kind == "reject":
            direction, cls, link = "unknown", "unknown", "machine"
        elif kind == "accept":
            direction, cls, link = "consistent", "activity", "machine"
        else:
            direction, cls, link = "consistent", "activity", "agent"
        first = None
        for wid in wids:
            try:
                written = _write(_fields(kind, tid, wid, direction, cls, link, tid, row,
                                         "lint", at=store.iso_at(ctx.now)))
            except WhyRefusal:
                continue
            first = first or written
        return first


# -- evidence and the act pass ------------------------------------------------

def evidence(acts, void=(), unbacked=()):
    """Why-net rows -> library Evidence (3.3): consistent -> win, contradicts
    -> loss; unknown direction rows never vote."""
    out = []
    for a in acts:
        d = a.get("direction")
        if d not in ("consistent", "contradicts"):
            continue
        out.append({"id": a["act"], "claim": a["why"],
                    "turn": a["owner_turn"]["otl"], "topic": a.get("topic"),
                    "order": N.otl_order(a["owner_turn"]["otl"]),
                    "cls": a.get("class"), "link": a.get("link"),
                    "polarity": "win" if d == "consistent" else "loss",
                    "scope": "narrow",
                    "void": a["act"] in void or a["act"] in unbacked})
    return out


def co_filter(node, mine, acts):
    """AMEND-1 rev 3 (multiple Whys): a pick of a Why the owner said holds
    together with this one ("both") is not a loss for it -- the two are not
    rivals.  Drops those loss rows; wins are untouched."""
    co = set(node.get("co") or ())
    if not co:
        return mine
    picked = {}
    for b in acts:
        if b.get("direction") == "consistent":
            picked.setdefault(b.get("ref"), set()).add(b.get("why"))
    return [a for a in mine if not (a.get("direction") == "contradicts"
                                    and picked.get(a.get("ref"), set()) & co)]


def resolved_conflicts(evs, wid):
    """`resolve` events touching the node since its last non-split words
    event (a confirm) -- counts contests, not days (3.5 rule 4)."""
    n = 0
    for e in evs:
        d = e.get("data") or {}
        if e.get("ev") == "confirm" and e.get("why") == wid:
            n = 0
        elif e.get("ev") == "cowhy" and wid in (e.get("why"), d.get("other")):
            n = 0  # rev 3: "both" is words, and it ends the back-and-forth
        elif e.get("ev") == "resolve" and wid in (e.get("why"), d.get("other")):
            n = 0 if d.get("side") == "both" else n + 1
    return n


def _recent(acts, now, days):
    lo = now - days * DAY
    return [a for a in acts if (store.parse_iso(a.get("at")) or 0) >= lo]


def node_verdict(ctx, node, acts, evs, void, unbacked, pol=None):
    pol = pol or policy(ctx)
    state = STATE_OF.get(node["status"])
    if state is None:
        return {"verdict": "none", "ids": []}
    mine = co_filter(node, [a for a in acts if a.get("why") == node["id"]], acts)
    if state == "hypothesis":
        mine = _recent(mine, ctx.now, ctx.conf_int("WHY_ACT_WINDOW_DAYS"))
        since = 0
    else:
        since = since_order(node, acts)
    t = cm.tally(evidence(mine, void, unbacked), node["id"], since, pol)
    return cm.verdict(state, t, pol, resolved_conflicts(evs, node["id"]))


def since_order(node, acts):
    """3.3 since_order: the words anchor of a provisional node; for a
    confirmed node the settle's anchor -- the last act the settle counted --
    so going down needs WHY_SETTLE_WINS net losses after the settle, the
    same count that brought it up (I3)."""
    since = N.otl_order(node.get("anchor"))
    if node["status"] == "confirmed" and node.get("settle"):
        by_id = {a["act"]: a for a in acts}
        orders = [N.otl_order(by_id[i]["owner_turn"]["otl"])
                  for i in node["settle"].get("acts") or [] if i in by_id]
        if orders:
            since = max(since, max(orders))
    return since


def acted_side(loss_ids, acts):
    """The Why the losses picked: the consistent row of the same ref."""
    by_id = {a["act"]: a for a in acts}
    c = collections.Counter()
    for i in loss_ids:
        a = by_id.get(i)
        if not a:
            continue
        for b in acts:
            if b.get("ref") == a.get("ref") and b.get("direction") == "consistent" \
                    and b.get("why") != a.get("why"):
                c[b["why"]] += 1
    return c.most_common(1)[0][0] if c else None


def _open_contest_ask(asks, wid):
    return any(a.get("topic") == wid and a.get("trigger") in ("contest", "act-contest")
               and a.get("state") not in budget.TERMINAL for a in asks.values())


def act_pass(ctx, words_pass=None):
    """AMEND-1 2.2: ONE locked section.  Re-fold, convert reveal picks, call
    the library per live node, write settle (re-checking builder) and
    act-contests; then `words_pass(evs, nodes)` (the LOCKED words contests)
    in the same lock.  Returns what derive enqueues after the lock:
    {"settled", "contested": [(wid, other)], "mixed": [(wid, other)],
     "ripe": [wid]}.  Prints nothing (N8)."""
    out = {"settled": [], "contested": [], "mixed": [], "ripe": []}
    pol = policy(ctx)
    with store.locked():
        decs = decisions(ctx)
        convert_reveals_unlocked(ctx, decs)
        evs = store.read_rows(store.EVENTS)
        nodes = N.fold(evs)
        acts = act_rows()
        void = voided(evs)
        unbacked = unbacked_acts(ctx, acts, decs=decs)
        asks = budget.fold_asks(store.read_rows(store.ASKS))
        for wid in sorted(nodes):
            n = nodes[wid]
            if n["status"] in N.TERMINAL or n["status"] == "contested":
                continue
            v = node_verdict(ctx, n, acts, evs, void, unbacked, pol)
            if v["verdict"] == "settle" and n["status"] == N.PROVISIONAL:
                try:
                    events.settle(wid, v["ids"], n["anchor_evt"])
                    out["settled"].append(wid)
                except WhyRefusal:
                    pass  # stale: a words event moved the anchor (GS34)
            elif v["verdict"] == "conflict" and n["status"] in N.LIVE_CONFIRMED:
                if _open_contest_ask(asks, wid):
                    continue
                other = acted_side(v["ids"], acts)
                if other is None or not _live(nodes, other):
                    continue  # no tagged other side: no contest (2.3)
                rev = {"v": len(n["revisions"]) + 2, "at": store.iso_at(ctx.now),
                       "text_before": n["text"], "text_after": n["text"],
                       "by_acts": list(v["ids"])}
                try:
                    events.contest_acts(wid, v["ids"], rev)
                    out["contested"].append((wid, other))
                except WhyRefusal:
                    pass
            elif v["verdict"] == "mixed":
                other = acted_side(v["ids"], acts)
                if other is not None and _live(nodes, other):
                    out["mixed"].append((wid, other))
            elif v["verdict"] == "ripe" and n["status"] == "hypothesis":
                out["ripe"].append(wid)
        if words_pass is not None:
            # step 7: the LOCKED words contests, same lock, fresh fold
            evs = store.read_rows(store.EVENTS)
            out["words_contested"] = words_pass(evs, N.fold(evs))
    return out


def contest_carry(evs, wid, acts):
    """(contest evt id, carry_from otl) of the node's latest act-contest:
    the acted side's anchor sits just before the contest's rows (2.3)."""
    by_id = {a["act"]: a for a in acts}
    for e in reversed(evs):
        d = e.get("data") or {}
        if e.get("why") == wid and e.get("ev") == "contest":
            orders = [N.otl_order(by_id[i]["owner_turn"]["otl"]) for i in d.get("acts") or []
                      if i in by_id]
            carry = "otl-%06d" % (min(orders) - 1) if orders else None
            return e.get("id"), carry
    return None, None


# -- derived flags (console / render only, never stored) ---------------------

def flags(ctx, node, acts, evs):
    """`stale`: provisional with no counted choice act for WHY_STALE_DAYS
    (7.2).  `mixed`: mixed_after resolved act-contests since the last
    non-split words event (2.3)."""
    out = []
    if node["status"] == N.PROVISIONAL:
        since = N.otl_order(node.get("anchor"))
        last = store.parse_iso(node.get("words_at")) or 0
        for a in acts:
            if a.get("why") == node["id"] and a.get("class") == "choice" \
                    and N.otl_order(a["owner_turn"]["otl"]) > since:
                last = max(last, store.parse_iso(a.get("at")) or 0)
        if last and ctx.now - last >= ctx.conf_int("WHY_STALE_DAYS") * DAY:
            out.append("stale")
    if node["status"] not in N.TERMINAL and \
            resolved_conflicts(evs, node["id"]) >= policy(ctx)["mixed_after"]:
        out.append("mixed")
    return out


# -- audit (detect-only, S1 class) --------------------------------------------

def _act_backed(ctx, a, probes, decs, cors):
    kind, ref = a.get("kind"), a.get("ref")
    otl = (a.get("owner_turn") or {}).get("otl")
    if not otl:
        return False
    if kind == "decision" and a.get("by") == "derive":
        p = probes.get(ref)
        return (p is not None and p.get("trigger") == "reveal" and p.get("otl") == otl
                and any(s.get("why") == a.get("why") for s in p.get("shown") or [])
                and _fingerprint(p) == a.get("ref_sha"))
    if kind == "decision":
        d = decs.get(ref)
        return d is not None and d["state"] == "confirmed" and bool(d["req"]) \
            and d["fingerprint"] == a.get("ref_sha") \
            and (a.get("owner_turn") or {}).get("request_id") == d["req"]
    if kind == "correction":
        rec = cors.get(ref)
        return rec is not None and rec.get("otl") == otl and a.get("ref_sha") == _fingerprint(
            {k: rec.get(k) for k in ("id", "said_at", "text", "session_id")})
    if kind in MACHINE_KINDS:
        from . import surfaces
        return surfaces.find_ticket(ref, os.path.join(ctx.root, "worklog")) is not None
    return False


def unbacked_acts(ctx, acts=None, decs=None):
    from . import surfaces
    acts = act_rows() if acts is None else acts
    probes = {p["id"]: p for p in _probe_rows(store.read_rows(store.PROBES))}
    decs = decisions(ctx) if decs is None else decs
    cors = {r["id"]: r for r in surfaces.fold_corrections()}
    return {a["act"] for a in acts if not _act_backed(ctx, a, probes, decs, cors)}


def _words_anchor(evs, wid, words_evt):
    """The otl a settle's words event anchored the node at, or None when
    that event is not a words event of this node."""
    for e in evs:
        if e.get("id") != words_evt:
            continue
        d = e.get("data") or {}
        if e.get("ev") == "confirm" and e.get("why") == wid:
            return d.get("otl") or "otl-000000"
        if e.get("ev") == "resolve":
            if e.get("why") == wid:
                return d.get("otl")
            if d.get("other") == wid and d.get("side") in ("acted", "both"):
                return d.get("carry_from") or d.get("otl")
        if e.get("ev") == "cowhy" and wid in (e.get("why"), d.get("other")):
            return d.get("otl")
    return None


def audit(ctx):
    """-> [{"kind", "id", "why", "reason"}]: act-unbacked, settle-unbacked,
    reveal-unknown-why.  Detect-only: nothing refuses, nothing is changed;
    an unbacked act drops out of every count (act_pass)."""
    out = []
    evs = store.read_rows(store.EVENTS)
    nodes = N.fold(evs)
    acts = act_rows()
    by_id = {a["act"]: a for a in acts}
    void = voided(evs)
    bad = unbacked_acts(ctx, acts)
    for i in sorted(bad):
        out.append({"kind": "act-unbacked", "id": i, "why": by_id[i].get("why"),
                    "reason": "act-unbacked"})
    pol = policy(ctx)
    for e in evs:
        if e.get("ev") != "settle":
            continue
        d, wid = e.get("data") or {}, e.get("why")
        ids = list(d.get("acts") or [])
        anchor = _words_anchor(evs, wid, d.get("words"))
        rows = [by_id[i] for i in ids if i in by_id]
        ok = (e.get("actor") == "derive" and anchor is not None and len(rows) == len(ids)
              and len(ids) >= pol["threshold"]
              and all(i not in void and i not in bad for i in ids)
              and all(a.get("why") == wid for a in rows)
              and all(N.otl_order(a["owner_turn"]["otl"]) > N.otl_order(anchor) for a in rows)
              and any(a.get("link") in ("agent-prior", "machine") and a.get("class") == "choice"
                      for a in rows))
        if ok:
            t = cm.tally(evidence(rows), wid, N.otl_order(anchor), pol)
            ok = cm.verdict("provisional", t, pol)["verdict"] == "settle"
        if not ok:
            out.append({"kind": "settle-unbacked", "id": e.get("id"), "why": wid,
                        "reason": "settle-unbacked"})
    seen = set()
    for r in store.read_rows(store.ASKS):
        if r.get("trigger") != "reveal" or r.get("ev") != "create":
            continue
        for s in r.get("shown") or []:
            w = s.get("why")
            if w and w not in nodes and (r.get("ask"), w) not in seen:
                seen.add((r.get("ask"), w))
                out.append({"kind": "reveal-unknown-why", "id": r.get("ask"), "why": w,
                            "reason": "reveal-unknown-why"})
    return out


# -- erasure (F14) ---------------------------------------------------------------

def erase(act_id):
    """Owner-request removal: rebuild acts.jsonl without the row, chain
    recomputed, under the lock.  The act-void event that must precede it is
    the audit line.  -> True when a row was removed."""
    with store.locked():
        evs = store.read_rows(store.EVENTS)
        if act_id not in voided(evs):
            raise WhyRefusal("not-void")
        rows = store.read_rows(store.ACTS)
        keep = [r for r in rows if r.get("act") != act_id]
        if len(keep) == len(rows):
            return False
        store.rewrite_chain(store.ACTS, keep)
        return True
