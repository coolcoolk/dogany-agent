"""why_net.events -- the only writers of events.jsonl and probes.jsonl
(spec 2.2, 2.4).  Every floor check runs inside the store lock, against
the same file state the append lands on.  A WhyRefusal means nothing was
written.

Rules enforced here (2.2):
  - confirm by actor `agent` needs a TurnRef from the resolver (2.5);
    confirm by actor `ups-hook` needs a pick probe on a shown slot whose
    verbatim is a ledgered owner prompt of its session (R1g);
  - contest is written only by derive or ups-hook;
  - no event deletes or overwrites text; revise keeps text_before.

AMEND-1 (1.3, 1.4): a confirm (any actor) lands `confirmed-provisional` and
is the node's words anchor (its otl).  `settle` -- the only way to reach
`confirmed` -- is written only by derive, inside the act pass lock, through
a builder that re-checks "still provisional, same words anchor".  `resolve`
is a split answer (ups-hook); `act-void` drops one act from every count.

AMEND-1 rev 3 (multiple Whys): a work item may hold several Whys.  A "both"
answer writes `cowhy` (or `resolve` side=both on a split): both sides are the
owner's words, and neither one's picks count against the other.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from why_net import LazyModule, WhyRefusal  # noqa: E402
why_owner_turn = LazyModule("why_owner_turn")
from why_net import nodes as N  # noqa: E402
from why_net import store  # noqa: E402

EVENT_KINDS = ("propose", "confirm", "attach", "pivot", "retire", "revise",
               "contest", "settle", "resolve", "act-void", "cowhy",
               "work", "execution", "ticket-judgment", "r3-judgment")
ACTORS = ("agent", "ups-hook", "derive", "seed", "why-net")
SETTLE_POLICY = "why/2"
VERDICTS = ("pick", "pick-multi", "free", "defer", "none")
INTERP_VERDICTS = ("partial", "closest", "reject", "reject-all", "free-new")
INTERP_SCOPES = ("item", "global")


def _turn_source(ref, kind):
    return {"kind": kind, "runtime": ref.runtime, "session_id": ref.session_id,
            "message_uuid": ref.message_uuid, "dec": None}


def _resolve_or_refuse(quote, session_id, message_uuid=None):
    ref = why_owner_turn.resolve(quote, session_id, message_uuid)
    if isinstance(ref, why_owner_turn.Refusal):
        raise WhyRefusal(ref.reason)
    return ref


def _event(rows, kind, wid, actor, data):
    if kind not in EVENT_KINDS or actor not in ACTORS:
        raise ValueError("bad event kind/actor")
    if kind == "contest" and actor not in ("derive", "ups-hook"):
        raise WhyRefusal("contest-actor")
    if kind == "settle" and actor != "derive":
        raise WhyRefusal("settle-actor")
    return {"ev": kind, "id": store.next_id(rows, "evt", 6), "why": wid,
            "at": store.now_iso(), "actor": actor, "data": data}


def _node_or_refuse(nodes, wid):
    n = nodes.get(wid)
    if n is None:
        raise WhyRefusal("unknown-why")
    return n


def _not_terminal(n):
    if n["status"] in N.TERMINAL:
        raise WhyRefusal("terminal-status")


def nodes():
    from pathlib import Path
    from .runtime import enabled
    root = store._CANONICAL_ROOT.get() or store.root_dir()
    if enabled(root) and (Path(root) / "config/why-authority.json").is_file():
        from .authority import shared_nodes
        return shared_nodes(root)
    return N.fold(store.read_rows(store.EVENTS))


def propose(text, scope, parents=(), work=None, session_id=None, *,
            evidence=None, target=None, born=None):
    """AMEND-2 2.1-2.3: `evidence` / `target` / `born` (keyword-only) are
    written by the answer writers (ups U3, `frame --answer`) and `propose
    --target`.  A born propose whose `born.(ask, slot)` already named a node
    returns that node's propose row instead of a second node (F16, FR18)."""
    with store.locked():
        return propose_unlocked(text, scope, parents, work, session_id,
                                evidence=evidence, target=target, born=born)


def born_key(born):
    """The birth identity of a node: (ask, slot), or (ask, None, probe) for a
    free-text birth (no slot)."""
    if not isinstance(born, dict) or not born.get("ask"):
        return None
    if born.get("slot") is not None:
        return (born["ask"], born["slot"])
    return (born["ask"], None, born.get("probe"))


def born_node(rows, born):
    """The propose row of the node already born as `born`, or None."""
    key = born_key(born)
    if key is None:
        return None
    for e in rows:
        if e.get("ev") == "propose" and born_key((e.get("data") or {}).get("born")) == key:
            return e
    return None


def propose_unlocked(text, scope, parents=(), work=None, session_id=None, *,
                     evidence=None, target=None, born=None):
    """propose() for a caller holding store.locked()."""
    if not isinstance(text, str) or not text.strip():
        raise WhyRefusal("empty-text")
    if not N.SCOPE_RE.match(scope or ""):
        raise WhyRefusal("bad-scope")
    if target is not None and not N.TARGET_RE.match(target):
        raise WhyRefusal("unknown-target")
    hit = {}

    def build(rows):
        prior = born_node(rows, born)
        if prior is not None:
            hit["row"] = prior
            raise _Dup()
        cur = N.fold(rows)
        for p in parents:
            _not_terminal(_node_or_refuse(cur, p))
        wid = store.next_id([{"id": w} for w in cur], "why", 4)
        data = {
            "text": text, "worded_by": "agent", "status": "hypothesis",
            "scope": scope, "parents": list(dict.fromkeys(parents)),
            "work": work, "said_at": None, "owner_words": [],
            "source": {"kind": "turn", "runtime": why_owner_turn.RUNTIME,
                       "session_id": session_id, "message_uuid": None,
                       "dec": None}}
        # AMEND-2: only the writers that have them add the new fields, so a
        # plain propose row stays byte-identical (FR12).
        if evidence:
            data["evidence"] = list(evidence)
        if target is not None:
            data["target"] = target
        if born is not None:
            data["born"] = dict(born)
        return _event(rows, "propose", wid, "agent", data)
    try:
        return store.append_unlocked(store.EVENTS, build)
    except _Dup:
        return hit["row"]


def confirm_quote(wid, quote, session_id, message_uuid=None):
    """Agent confirm: verified owner wording, or an exact candidate selection
    re-read on its session's main chain. Only verbatim words get worded_by=owner;
    a menu selection retains agent wording and records its candidate for audit.

    AMEND-2 2.6: the resolve (a transcript read) runs before the lock; the
    build is confirm_quote_unlocked, shared with `frame --answer` path (a)."""
    n = _node_or_refuse(nodes(), wid)
    candidate = None
    ref = why_owner_turn.resolve(quote, session_id, message_uuid,
                                 allow_past=True)
    if isinstance(ref, why_owner_turn.Refusal) or (
            why_owner_turn.numeric_reply(quote) and why_owner_turn.quote_key(quote) !=
            why_owner_turn.quote_key(n["text"])):
        selection = why_owner_turn.resolve(quote, session_id, message_uuid,
                                           allow_past=True, candidate=n["text"])
        if isinstance(selection, why_owner_turn.Refusal):
            raise WhyRefusal(ref.reason if isinstance(ref, why_owner_turn.Refusal)
                             else "text-not-quote")
        ref, candidate = selection, n["text"]
    # AMEND-1 1.3: the otl of the quoted owner turn is the words anchor.
    otl = why_owner_turn.turn_otl(ref.session_id, ref.message_uuid)
    if not otl:
        raise WhyRefusal("turn-not-in-ledger")
    with store.locked():
        return confirm_quote_unlocked(wid, quote, ref, otl, candidate=candidate)


def confirm_quote_unlocked(wid, quote, ref, otl, candidate=None):
    """The confirm_quote build for a caller holding store.locked(); `ref` is
    the resolved TurnRef of the quote and `otl` its ledger id."""
    def build(rows):
        n = _node_or_refuse(N.fold(rows), wid)
        _not_terminal(n)
        if n["status"] in N.LIVE_CONFIRMED:
            raise WhyRefusal("already-confirmed")
        if why_owner_turn.quote_key(n["text"]) != why_owner_turn.quote_key(
                candidate if candidate is not None else quote):
            raise WhyRefusal("text-not-quote")
        data = {
            "quote": why_owner_turn.owner_span(ref.text, quote),
            "said_at": ref.said_at, "worded_by": "agent" if candidate is not None else "owner",
            "probe": None, "otl": otl,
            "source": _turn_source(ref, "tap" if candidate is not None else "turn")}
        if candidate is not None:
            data["candidate"] = candidate
        return _event(rows, "confirm", wid, "agent", data)
    return store.append_unlocked(store.EVENTS, build)


def confirm_tap(wid, probe_id, env=None, reanchor=False):
    """Tap confirm (spec 4.4 step 4, Q1 = yes; fixture R1g), written with
    actor ups-hook.  The probe must be a pick on a shown slot that names
    `wid` (never a reveal pick), and its verbatim must be a ledgered owner
    prompt of its session (why_owner_turn.resolve_prompt).  worded_by stays
    agent; the probe id joins owner_words.

    AMEND-1: the node lands provisional, anchored at the probe's otl.
    `reanchor` (a fork-from-mixed pick, 2.5) re-anchors a node that is
    already provisional or confirmed instead of refusing.

    AMEND-2 2.6: a thin locked wrapper over confirm_tap_unlocked."""
    with store.locked():
        return confirm_tap_unlocked(wid, probe_id, env=env, reanchor=reanchor)


def confirm_tap_unlocked(wid, probe_id, env=None, reanchor=False, ref=None):
    """confirm_tap() for a caller holding store.locked().  `ref` (S-P1b): a
    TurnRef the caller resolved before taking the lock -- then only the
    probe is re-checked here, no transcript is read under the lock."""
    probe = _probe(store.read_rows(store.PROBES), probe_id)
    check_tap_probe(probe, wid, N.fold(store.read_rows(store.EVENTS)))
    src = probe.get("source") or {}
    if ref is None:
        ref = why_owner_turn.resolve_prompt(probe.get("verbatim"),
                                            src.get("session_id"), env=env)
    if isinstance(ref, why_owner_turn.Refusal):
        raise WhyRefusal(ref.reason)

    otl = probe.get("otl") or why_owner_turn.prompt_otl(
        probe.get("verbatim"), src.get("session_id"))
    if not otl:
        raise WhyRefusal("turn-not-in-ledger")

    def build(rows):
        n = _node_or_refuse(N.fold(rows), wid)
        _not_terminal(n)
        if n["status"] in N.LIVE_CONFIRMED and not reanchor:
            raise WhyRefusal("already-confirmed")
        return _event(rows, "confirm", wid, "ups-hook", {
            "quote": probe["verbatim"], "said_at": ref.said_at,
            "worded_by": "agent", "probe": probe_id, "otl": otl,
            "source": _turn_source(ref, "tap")})
    return store.append_unlocked(store.EVENTS, build)


def picked_slots(probe):
    """The probe's picked slots plus, for a turn ask's "both" slot, the two
    candidate slots it stands for (AMEND-2 2.3 `both_slots`)."""
    slots = set(probe.get("slots") or [])
    for s in probe.get("shown") or []:
        if s.get("slot") in slots:
            slots.update(s.get("both_slots") or ())
    return slots


def check_tap_probe(probe, wid, known=None):
    """AMEND-2 2.3 born rule: besides a shown slot naming `wid`, a node whose
    `born.probe` is this probe and whose `born.slot` is a picked slot
    (directly or through that slot's `both_slots`) passes.  `known`: the
    folded nodes (needed only for the born rule).  The voice, reveal and
    verdict checks run first, unchanged (F16)."""
    if probe.get("verdict") not in ("pick", "pick-multi"):
        raise WhyRefusal("probe-not-pick")
    if probe.get("trigger") == "reveal":
        raise WhyRefusal("reveal-pick")
    if (probe.get("source") or {}).get("inbound_source") == "voice":
        raise WhyRefusal("voice-pick")  # grill-2 fix 4: Whisper text, not a tap
    slots = picked_slots(probe)
    if any((s.get("why") == wid or wid in (s.get("both") or ()))
           and s.get("slot") in slots for s in probe.get("shown") or []):
        return
    born = ((known or {}).get(wid) or {}).get("born") or {}
    if probe.get("id") and born.get("probe") == probe.get("id") \
            and born.get("slot") in slots:
        return
    raise WhyRefusal("why-not-picked")


def contest(wid, interp_id, revision, actor="derive"):
    """T-contest (2.6): status contested, text kept, one revision line."""
    def build(rows):
        n = _node_or_refuse(N.fold(rows), wid)
        _not_terminal(n)
        return _event(rows, "contest", wid, actor,
                      {"interp": interp_id, "revision": revision})
    return store.append_unlocked(store.EVENTS, build)


def contest_acts(wid, act_ids, revision):
    """T-act-contest (AMEND-1 2.2 step 5): `data.acts` instead of
    `data.interp`.  Caller holds store.locked()."""
    def build(rows):
        n = _node_or_refuse(N.fold(rows), wid)
        _not_terminal(n)
        if n["status"] not in N.LIVE_CONFIRMED:
            raise WhyRefusal("contest-stale")
        return _event(rows, "contest", wid, "derive",
                      {"acts": list(act_ids), "revision": revision})
    return store.append_unlocked(store.EVENTS, build)


def settle(wid, act_ids, words_evt):
    """AMEND-1 1.4 / 2.2 step 4 (F12): the settle builder.  Re-folds the
    rows it appends to and refuses unless the node is STILL provisional
    with the same words anchor the act pass computed.  derive only; caller
    holds store.locked()."""
    def build(rows):
        n = _node_or_refuse(N.fold(rows), wid)
        if n["status"] != N.PROVISIONAL or n["anchor_evt"] != words_evt:
            raise WhyRefusal("settle-stale")
        return _event(rows, "settle", wid, "derive", {
            "acts": list(act_ids), "words": words_evt,
            "policy": SETTLE_POLICY})
    return store.append_unlocked(store.EVENTS, build)


def resolve(wid, side, other, contest_evt, otl, carry_from=None, probe=None):
    """AMEND-1 2.3: a split answer.  side=said keeps the node's own Why;
    side=acted gives the Why the losses picked (`other`).  Caller holds
    store.locked()."""
    if side not in ("said", "acted", "both"):
        raise ValueError(side)

    def build(rows):
        cur = N.fold(rows)
        n = _node_or_refuse(cur, wid)
        if n["status"] != "contested":
            raise WhyRefusal("not-contested")
        _not_terminal(_node_or_refuse(cur, other))
        data = {"contest": contest_evt, "side": side, "other": other,
                "otl": otl, "probe": probe}
        if side in ("acted", "both") and carry_from:
            data["carry_from"] = carry_from
        return _event(rows, "resolve", wid, "ups-hook", data)
    return store.append_unlocked(store.EVENTS, build)


def co_whys(wid, other, probe_id, otl):
    """AMEND-1 rev 3: a "both" pick on a 2-Why ask (fork, fork-mixed,
    before-work).  One `cowhy` event: both nodes land provisional at the
    answer's otl (a settled one stays confirmed) and are paired.  The probe
    must be a pick of a `both` slot naming the two.  Caller holds
    store.locked()."""
    def build(rows):
        probe = _probe(store.read_rows(store.PROBES), probe_id)
        cur = N.fold(rows)
        check_tap_probe(probe, wid, cur)
        check_tap_probe(probe, other, cur)
        for w in (wid, other):
            _not_terminal(_node_or_refuse(cur, w))
        if wid == other:
            raise WhyRefusal("cowhy-self")
        return _event(rows, "cowhy", wid, "ups-hook",
                      {"other": other, "otl": otl, "probe": probe_id})
    return store.append_unlocked(store.EVENTS, build)


def act_void(act_id, wid, reason="owner-request"):
    """AMEND-1 1.4 (F14): drop one act from every count.  The act row stays
    on its chain; this event is the audit line."""
    def build(rows):
        if any(e.get("ev") == "act-void" and (e.get("data") or {}).get("act") == act_id
               for e in rows):
            raise WhyRefusal("already-void")
        return _event(rows, "act-void", wid, "why-net",
                      {"act": act_id, "reason": reason})
    return store.append(store.EVENTS, build)


def audit_confirms():
    """Grill fix 5: the hash chain is recomputable by anyone, so the audit
    also re-proves every confirm.  agent -> the resolver is re-run on the
    recorded quote/session (transcript + ledger + clause); ups-hook -> the
    named probe must exist, be a non-reveal pick of that node and its
    verbatim must be a ledgered owner prompt.  Returns
    [{"evt", "why", "reason"}] for confirms that are not backed.
    Detect-only, like the chain check (S1)."""
    out = []
    probes = store.read_rows(store.PROBES)
    events = store.read_rows(store.EVENTS)
    known = N.fold(events)
    for i, ev in enumerate(events):
        if ev.get("ev") == "retire" and (ev.get("data") or {}).get("reason") == "born-unbacked":
            # AMEND-2 2.3: derive retired a born node its tap could not back
            out.append({"evt": ev.get("id"), "why": ev.get("why"), "reason": "born-unbacked"})
            continue
        if ev.get("ev") == "cowhy":
            # AMEND-1 rev 3: a "both" answer is re-proved like a tap confirm
            d = ev.get("data") or {}
            try:
                probe = _probe(probes, d.get("probe"))
                check_tap_probe(probe, ev.get("why"), known)
                check_tap_probe(probe, d.get("other"), known)
                if ev.get("actor") != "ups-hook" or probe.get("otl") != d.get("otl"):
                    raise WhyRefusal("cowhy-unbacked")
            except WhyRefusal as r:
                out.append({"evt": ev.get("id"), "why": ev.get("why"), "reason": r.reason})
            continue
        if ev.get("ev") != "confirm":
            continue
        d = ev.get("data") or {}
        src = d.get("source") or {}
        actor = ev.get("actor")
        reason = None
        if actor == "seed" or src.get("kind") == "ledger-seed":
            # Grill-3 fix 3: a seed confirm is backed only by the seed run
            # itself: the leading actor=seed run of events.jsonl, its entry
            # still in the ledger it names (why_net.seed.backed).  A
            # hand-appended seed confirm is never in that run.
            from why_net import budget, seed
            reason = seed.backed(budget.Ctx(), events, i)
        elif actor == "agent":
            ref = why_owner_turn.resolve(d.get("quote"), src.get("session_id"),
                                         src.get("message_uuid"), audit=True,
                                         candidate=d.get("candidate"))
            if isinstance(ref, why_owner_turn.Refusal):
                reason = ref.reason
            elif d.get("candidate") is not None and why_owner_turn.turn_otl(
                    ref.session_id, ref.message_uuid) != d.get("otl"):
                reason = "turn-not-in-ledger"
        elif actor == "ups-hook":
            try:
                probe = _probe(probes, d.get("probe"))
                check_tap_probe(probe, ev.get("why"), known)
                if probe.get("verbatim") != d.get("quote"):
                    raise WhyRefusal("quote-not-probe")
                psrc = probe.get("source") or {}
                ref = why_owner_turn.resolve_prompt(
                    probe["verbatim"], psrc.get("session_id"), audit=True)
                if isinstance(ref, why_owner_turn.Refusal):
                    raise WhyRefusal(ref.reason)
            except WhyRefusal as r:
                reason = r.reason
        else:
            reason = "confirm-actor"
        if reason:
            out.append({"evt": ev.get("id"), "why": ev.get("why"),
                        "reason": reason})
    out.extend(audit_spans(events))
    return out


def _turn_text(otl, ledger=None):
    """The transcript text of the ledgered owner turn `otl` (off the hot
    path: derive --audit only), or None when it cannot be read."""
    rows = why_owner_turn.ledger_rows() if ledger is None else ledger
    row = next((r for r in rows if r.get("id") == otl), None)
    if row is None:
        return None
    path = why_owner_turn.session_transcript(row.get("session_id"))
    if not path:
        return None
    try:
        on_chain = why_owner_turn.chain_turns(path)[0]
    except (OSError, UnicodeDecodeError):
        return None
    shas = {row.get("sha"), row.get("owner_sha")} - {None}
    for _entry, text in on_chain:
        if why_owner_turn.turn_sha(text) in shas \
                or why_owner_turn.turn_sha(why_owner_turn.strip_outbound(text)) in shas:
            return text
    return None


def audit_spans(events=None):
    """AMEND-2 2.1: a display `span` is never trusted.  Off the hot path it
    must be an NFC substring of the transcript turn of its otl (the frame
    otl of a turn ask's candidate, a born node's evidence).  A miss is
    `span-unbacked`, detect-only.  -> [{"evt", "why", "reason"}]."""
    out = []
    ledger = why_owner_turn.ledger_rows()
    cache = {}

    def backed(otl, span):
        if otl not in cache:
            cache[otl] = _turn_text(otl, ledger)
        text = cache[otl]
        return text is not None and store.nfc(span) in store.nfc(text)

    for r in store.read_rows(store.ASKS):
        if r.get("ev") != "create" or r.get("trigger") != "turn":
            continue
        for c in r.get("cands") or []:
            if c.get("span") and not backed(r.get("frame_otl"), c["span"]):
                out.append({"evt": r.get("ask"), "why": c.get("why") or "",
                            "reason": "span-unbacked"})
                break
    for ev in store.read_rows(store.EVENTS) if events is None else events:
        if ev.get("ev") != "propose":
            continue
        for e in (ev.get("data") or {}).get("evidence") or []:
            if e.get("span") and not backed(e.get("otl"), e["span"]):
                out.append({"evt": ev.get("id"), "why": ev.get("why"),
                            "reason": "span-unbacked"})
                break
    return out


def audit_unanswered_propose():
    """S-P1a (detect-only): a node the agent proposed with no owner answer
    behind it -- `born` null, never confirmed, no evidence -- after the frame
    path went live (the first turn ask or frames.jsonl row).  Before that,
    plain `propose` was the agent's only door, so older nodes are not
    findings.  -> [why ids]."""
    live = None
    for r in store.read_rows(store.ASKS):
        if r.get("ev") == "create" and r.get("trigger") == "turn":
            live = r.get("at")
            break
    frames = store.read_rows(store.FRAMES)
    if frames and (live is None or (frames[0].get("at") or "") < live):
        live = frames[0].get("at")
    if live is None:
        return []
    events = store.read_rows(store.EVENTS)
    known = N.fold(events)
    out = []
    for ev in events:
        if ev.get("ev") != "propose" or ev.get("actor") != "agent" \
                or (ev.get("at") or "") < live:
            continue
        n = known.get(ev.get("why"))
        if n and n["born"] is None and n["confirm"] is None and not n["evidence"] \
                and n["status"] == "hypothesis":
            out.append(n["id"])
    return out


def audit_ledger():
    """Grill-2 fixes 1-3: ledger rows with no real on-chain turn behind
    them (why_owner_turn.audit_ledger).  Detect-only (S1)."""
    return why_owner_turn.audit_ledger()


def attach(wid, parent):
    def build(rows):
        cur = N.fold(rows)
        _not_terminal(_node_or_refuse(cur, wid))
        _not_terminal(_node_or_refuse(cur, parent))
        if parent in cur[wid]["parents"]:
            raise WhyRefusal("edge-exists")
        if N.would_cycle(cur, wid, parent):
            raise WhyRefusal("cycle")
        return _event(rows, "attach", wid, "agent", {"parent": parent})
    return store.append(store.EVENTS, build)


def check_cause(cause, required=False):
    """AMEND-2 2.11: `cause` is one of nodes.CAUSES or None; `required`
    when the write names a probe of a trigger-`turn` ask (exit 2)."""
    if cause is None:
        if required:
            raise WhyRefusal("cause-required")
        return None
    if cause not in N.CAUSES:
        raise WhyRefusal("bad-cause")
    return cause


def revise(wid, text, probe_id, cause=None, change_kind=None, refine_evidence=None):
    """Grill-3 fix 1: the new wording is the agent's, so a revise on a
    confirmed or contested node sends it back to `hypothesis` (nodes.fold),
    and the probe must answer an ask that showed THIS Why (a free answer to
    another ask is no ground for rewording it).

    AMEND-2 2.1 / 2.11: on a probe of a turn ask the probe's otl is the
    revision's evidence and `cause` is required."""
    if not isinstance(text, str) or not text.strip():
        raise WhyRefusal("empty-text")
    if change_kind is not None:
        from .citations import CHANGE_KINDS
        if change_kind not in CHANGE_KINDS:
            raise WhyRefusal("bad-change-kind")
    with store.locked():
        probe = _probe(store.read_rows(store.PROBES), probe_id)
        turn = probe.get("trigger") == "turn"
        check_cause(cause, required=turn)
        shown_it = any(s.get("why") == wid for s in probe.get("shown") or [])

        def build(rows):
            n = _node_or_refuse(N.fold(rows), wid)
            # AMEND-2 2.3: a node born from a turn ask was shown as that
            # ask's text slot, so a probe of the same ask is about it
            born = n.get("born") or {}
            if not shown_it and not (turn and born.get("ask") and born["ask"] == probe.get("ask")):
                raise WhyRefusal("probe-not-about-why")
            _not_terminal(n)
            if n["text"] == text:
                raise WhyRefusal("no-change")
            data = {"text_before": n["text"], "text_after": text,
                    "probe": probe_id, "status_before": n["status"]}
            if change_kind is not None:
                data["change_kind"] = change_kind
            if refine_evidence is not None:
                data["refine_evidence"] = refine_evidence
            if cause is not None:
                data["cause"] = cause
            if turn and probe.get("otl"):
                data["evidence"] = [{"otl": probe["otl"], "role": "answer", "span": None}]
            return _event(rows, "revise", wid, "agent", data)
        return store.append_unlocked(store.EVENTS, build)


def end(kind, wid, to=None, quote=None, session_id=None, message_uuid=None,
        basis=None, cause=None):
    """pivot / retire.  The resolver is needed unless the node is a bare
    agent hypothesis (empty owner_words, never confirmed).  AMEND-2 2.11:
    optional `cause`."""
    if kind not in ("pivot", "retire"):
        raise ValueError(kind)
    check_cause(cause)
    cur = nodes()
    n = _node_or_refuse(cur, wid)
    ref = None
    if N.needs_owner_turn(n) or quote:
        if not quote:
            raise WhyRefusal("quote-required")
        ref = _resolve_or_refuse(quote, session_id, message_uuid)

    def build(rows):
        now = N.fold(rows)
        n2 = _node_or_refuse(now, wid)
        _not_terminal(n2)
        if ref is None and N.needs_owner_turn(n2):
            raise WhyRefusal("quote-required")
        data = {}
        if kind == "pivot" and to:
            if to == wid:
                raise WhyRefusal("cycle")
            _not_terminal(_node_or_refuse(now, to))
            data["to"] = to
        if ref is not None:
            data.update({"quote": quote, "said_at": ref.said_at,
                         "source": _turn_source(ref, "turn")})
        if kind == "pivot":
            # AMEND-1 7.3: which decisions moved the Why.  Acts are never
            # re-pointed; the old node keeps them as history.
            data["basis"] = basis if basis is not None else _pivot_basis(rows, wid)
        if cause is not None:
            data["cause"] = cause
        return _event(rows, kind, wid, "agent", data)
    return store.append(store.EVENTS, build)


def retire_born_unbacked(wid, reason):
    """AMEND-2 2.3: derive's crash repair could not back a born node with
    its tap -> retire it, actor derive (no owner turn: the node was never
    the owner's words).  Caller holds store.locked()."""
    def build(rows):
        _not_terminal(_node_or_refuse(N.fold(rows), wid))
        return _event(rows, "retire", wid, "derive",
                      {"reason": "born-unbacked", "refusal": reason})
    return store.append_unlocked(store.EVENTS, build)


def _pivot_basis(rows, wid):
    """The node's latest act-contest: {contest, acts}, or None."""
    for e in reversed(rows):
        if e.get("why") == wid and e.get("ev") == "contest" \
                and (e.get("data") or {}).get("acts"):
            return {"contest": e.get("id"), "acts": list(e["data"]["acts"])}
    return None


# -- probes ---------------------------------------------------------------

def _probe(rows, probe_id):
    for r in rows:
        if r.get("id") == probe_id and "verbatim" in r:
            return r
    raise WhyRefusal("unknown-probe")


def probe_dedupe(session_id, verbatim, ask):
    return store.sha256_text("%s\0%s\0%s" % (session_id or "", verbatim,
                                             ask or ""))


def write_probe(fields):
    """Append one verbatim probe row (2.4).  `verbatim` is stored untouched.
    A replay with the same dedupe key returns the existing row instead of a
    second one (double-tap redelivery, GRILL h)."""
    with store.locked():
        return write_probe_unlocked(fields)[0]


def write_probe_unlocked(fields):
    """write_probe() for a caller holding store.locked(); returns
    (row, created)."""
    if fields.get("verdict") not in VERDICTS:
        raise WhyRefusal("bad-verdict")
    if not isinstance(fields.get("verbatim"), str):
        raise WhyRefusal("bad-verbatim")
    src = fields.get("source") or {}
    key = probe_dedupe(src.get("session_id"), fields["verbatim"],
                       fields.get("ask"))
    hit = {}

    def build(rows):
        for r in rows:
            if r.get("dedupe") == key:
                hit["row"] = r
                raise _Dup()
        row = {"id": store.next_id(rows, "prb", 4),
               "at": fields.get("at") or store.now_iso(),
               "ask": fields.get("ask"), "trigger": fields.get("trigger"),
               "work_ref": fields.get("work_ref"),
               "shown": fields.get("shown") or [],
               "verdict": fields["verdict"], "slots": fields.get("slots") or [],
               "verbatim": fields["verbatim"],
               "source": {"runtime": src.get("runtime", why_owner_turn.RUNTIME),
                          "session_id": src.get("session_id"),
                          "message_uuid": src.get("message_uuid"),
                          "inbound_source": src.get("inbound_source"),
                          "bubble_message_id": src.get("bubble_message_id")},
               "outcome": fields.get("outcome", "answered"), "dedupe": key,
               # AMEND-1 1.3: the turn-ledger id of the answering turn, and
               # for a reveal pick the menu's recommended label (1.2)
               "otl": fields.get("otl")}
        if fields.get("trigger") == "reveal":
            row["recommended"] = fields.get("recommended")
        return row
    try:
        return store.append_unlocked(store.PROBES, build), True
    except _Dup:
        return hit["row"], False


class _Dup(Exception):
    pass


def interpret(probe_id, verdict, wid, note, scope="item"):
    if verdict not in INTERP_VERDICTS:
        raise WhyRefusal("bad-verdict")
    if scope not in INTERP_SCOPES:
        raise WhyRefusal("bad-scope")
    with store.locked():
        _node_or_refuse(nodes(), wid)

        def build(rows):
            _probe(rows, probe_id)
            return {"interp": probe_id, "verdict": verdict, "why": wid,
                    "scope": scope, "note": note or "", "at": store.now_iso()}
        return store.append_unlocked(store.PROBES, build)
