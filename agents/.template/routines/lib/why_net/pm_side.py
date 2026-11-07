"""why_net.pm_side -- the PM-side reads of M1-4 (G10, G11); read-only.

G10 `route`: is Why W live in the leader's current frame?  Live means the
newest owner turn of the leader's session holds an open turn ask whose
topic touches W (shown candidate, or a work item W is linked to).  Live ->
the PM's question goes through the leader; not live -> direct.  The route
never changes who decides: it only names the channel.

G11 `bundle`: the same-day re-check bundle after a Why bend / split / end.
One entry per affected ticket (impact, evidence, proposal, new done), built
from citations.rechecks / trace over the existing event log, plus one
`pending` line (target / owner / next check) while the owner has not
answered.  Writes nothing; no new ledger.
"""

from . import budget, citations, events, store
from . import nodes as N

LEDGER_TAIL_BYTES = 256 * 1024
OPEN_STATES = ("create", "shown", "awaiting")
BEND_KINDS = ("bend", "split", "end")
PROPOSAL = {"end": "cancel", "bend": "carry-over", "split": "carry-over"}


def _leader_rows(leader_session):
    rows = [r for r in store.read_tail_rows(store.TURN_LEDGER, LEDGER_TAIL_BYTES)
            if not r.get("skip")]
    sid = leader_session or (rows[-1].get("session_id") if rows else None)
    return sid, [r for r in rows if r.get("session_id") == sid] if sid else []


def _touches(ctx, ask, why, known):
    from . import ask as A
    if why in budget.ask_topic_keys(ask):
        return True
    for key in (ask.get("work_ref"), ask.get("topic")):
        if key and why in A.work_whys(ctx, budget.canonical_topic(key), known):
            return True
    return False


def route(ctx, why, leader_session=None):
    """-> {"why", "live", "via": "leader" | "direct", "otl", "asks", "basis"}."""
    doc = {"why": why, "live": False, "via": "direct", "otl": None, "asks": [],
           "basis": "net-off" if not ctx.enabled() else "no-leader-frame"}
    if not ctx.enabled():
        return doc
    sid, rows = _leader_rows(leader_session)
    if not rows:
        return doc
    otl = rows[-1]["id"]
    known = events.nodes()
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    hit = sorted(a["ask"] for a in asks.values()
                 if a.get("trigger") == "turn" and a.get("frame_otl") == otl
                 and a.get("state") in OPEN_STATES and _touches(ctx, a, why, known))
    doc.update(otl=otl, asks=hit, basis="open-turn-ask" if hit else "why-not-in-frame")
    if hit:
        doc.update(live=True, via="leader")
    return doc


def _day(at):
    return (at or "")[:10]


def bundle(day, new_done=None, root=None):
    """-> {"day", "tickets": [...], "pending": [lines]} for the Why changes
    of `day` (YYYY-MM-DD).  `new_done` maps ticket -> the PM's new done
    line; a missing one is reported as needed, never invented."""
    new_done = new_done or {}
    rows = store.read_rows(store.EVENTS, root)
    changes = {}
    for e in rows:
        if e.get("ev") not in citations.SEMANTIC or _day(e.get("at")) != day:
            continue
        kind = (e.get("data") or {}).get("change_kind") or {
            "pivot": "bend", "retire": "end"}.get(e["ev"])
        if kind in BEND_KINDS:
            changes[e["why"]] = (kind, e["id"])
    confirmed = citations.owner_confirmations(rows, root)
    out, pending = [], []
    for tid, ev in sorted(citations.work_rows(rows).items()):
        work = ev["data"]
        hits = [c for c in citations.rechecks(work, rows, root=root)[0]
                if c.get("why") in changes]
        if not hits:
            continue
        kinds = sorted({changes[c["why"]][0] for c in hits})
        verdict = "cancel" if "end" in kinds else PROPOSAL[kinds[0]]
        try:
            ex = citations.trace(work["path"], root)["executions"]
        except OSError:
            ex = {}
        out.append({
            "ticket": tid, "target": work["target"],
            "impact": [{"why": c["why"], "kind": c["kind"], "to": c.get("to")} for c in hits],
            "evidence": sorted({"%s:%s" % (d["receipt"].get("kind"), d["state"])
                                for d in ex.values()}),
            "proposal": verdict,
            "done": work["define"]["done"],
            "new_done": new_done.get(tid) or "needed"})
        for w in sorted({c["why"] for c in hits}):
            if confirmed.get(w, {}).get("state") != "confirmed":
                pending.append("pending: target=%s owner=owner next=owner answer on %s (%s)"
                               % (work["target"], w, changes[w][1]))
    return {"day": day, "tickets": out, "pending": sorted(set(pending))}
