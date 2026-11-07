"""confirm_mechanics -- the shared confirm mechanics (dec-336).

Spec: docs/design/v27-t1-why-net-AMEND-1.md section 3.

signal -> hypothesis -> confirm-on-repeat, and narrow-scope reject, live
ONCE here.  Each domain (the Why net, the life kit taste rows) keeps its own
store, ids, axes, evidence extraction, copy and policy values, builds plain
Evidence dicts from ITS store and passes them in.  Data is never merged
across domains: a call reads one claim of one domain.

Pure functions only: no file I/O, no store, no clock, no i18n, no model
call, no conf read.  Inputs and outputs are plain dicts; nothing is written.
Ordering is the domain's monotonic `order` (ledger order), never wall clock.

Evidence = {"id", "claim", "turn", "topic", "order": int,
            "cls": choice|delegated|activity|unknown,
            "link": machine|agent-prior|agent, "polarity": win|loss,
            "scope": narrow|global, "void": bool}
Policy   = {"threshold", "window_n", "distinct_turns", "distinct_topics",
            "min_prior", "ripe_min", "mixed_after"}
State    = hypothesis | provisional | confirmed | contested

Pure stdlib.  English/ASCII only.
"""

import hashlib

STATES = ("hypothesis", "provisional", "confirmed", "contested")
CLASSES = ("choice", "delegated", "activity", "unknown")
LINKS = ("machine", "agent-prior", "agent")
VERDICTS = ("none", "ripe", "settle", "conflict", "mixed")

# Dedupe precedence: a machine row beats an agent-prior row beats an agent
# row for the same (turn, claim); ties go to the lowest order.
_PRECEDENCE = {"machine": 0, "agent-prior": 1, "agent": 2}
_PRIOR_LINKS = ("machine", "agent-prior")
_WINDOWED = ("choice", "delegated")
_RIPE = ("choice", "delegated", "activity")


def dedupe_key(turn, claim):
    """sha256(turn \\0 claim): one row per (owner turn, claim)."""
    return hashlib.sha256(("%s\0%s" % (turn, claim)).encode("utf-8")).hexdigest()


def _rank(e):
    return (_PRECEDENCE.get(e.get("link"), len(_PRECEDENCE)), e.get("order", 0))


def window(evidence, claim, since_order, policy):
    """Rows for `claim` with order > since_order, void rows dropped, deduped
    by (turn, claim) (machine > agent-prior > agent, then lowest order);
    then the last window_n rows of class choice/delegated plus every
    activity row.  Unknown rows never enter the window.  Order-sorted."""
    best = {}
    for e in evidence:
        if e.get("claim") != claim or e.get("void"):
            continue
        if not e.get("order", 0) > since_order:
            continue
        k = dedupe_key(e.get("turn"), claim)
        if k not in best or _rank(e) < _rank(best[k]):
            best[k] = e
    rows = sorted(best.values(), key=lambda e: (e.get("order", 0), e.get("id")))
    voting = [e for e in rows if e.get("cls") in _WINDOWED]
    n = policy.get("window_n") or 2 * policy["threshold"]
    keep = set(id(e) for e in voting[-n:])
    return [e for e in rows
            if id(e) in keep or e.get("cls") == "activity"]


def tally(evidence, claim, since_order, policy):
    """Counts over window().  wins / losses are class choice only (scope
    narrow or global alike); net = wins - losses."""
    rows = window(evidence, claim, since_order, policy)
    t = {"wins": 0, "losses": 0, "net": 0, "win_turns": [], "win_topics": [],
         "loss_turns": [], "loss_topics": [], "prior_wins": 0,
         "prior_losses": 0, "ripe_rows": 0, "ripe_losses": 0,
         "ids_for": [], "ids_against": [], "ids_ripe": []}
    for e in rows:
        cls, win = e.get("cls"), e.get("polarity") == "win"
        if cls in _RIPE:
            if win:
                t["ripe_rows"] += 1
                t["ids_ripe"].append(e["id"])
            else:
                t["ripe_losses"] += 1
        if cls != "choice":
            continue
        prior = e.get("link") in _PRIOR_LINKS
        side = "win" if win else "loss"
        t["wins" if win else "losses"] += 1
        for key, val in ((side + "_turns", e.get("turn")),
                         (side + "_topics", e.get("topic"))):
            if val is not None and val not in t[key]:
                t[key].append(val)
        if prior:
            t["prior_wins" if win else "prior_losses"] += 1
        t["ids_for" if win else "ids_against"].append(e["id"])
    t["net"] = t["wins"] - t["losses"]
    return t


def _conflict(t, policy):
    return (-t["net"] >= policy["threshold"]
            and len(t["loss_turns"]) >= policy.get("distinct_turns", policy["threshold"])
            and t["prior_losses"] >= policy.get("min_prior", 1))


def verdict(state, tally, policy, resolved_conflicts=0):
    """The only branching logic of the library (AMEND-1 3.2):

      hypothesis             ripe      ripe_rows >= ripe_min, no ripe loss
      provisional            settle    net >= threshold, distinct turns and
                                       topics, prior_wins >= min_prior
      provisional/confirmed  conflict  the same rules with polarity reversed
                                       (no topic rule)
      provisional/confirmed  mixed     conflict holds and resolved_conflicts
                                       >= mixed_after
      contested              none      always: the domain must ask

    Returns {"verdict", "ids"}: ids name the rows that met the rule."""
    t = tally
    if state == "hypothesis":
        if t["ripe_rows"] >= policy["ripe_min"] and t["ripe_losses"] == 0:
            return {"verdict": "ripe", "ids": list(t["ids_ripe"])}
        return {"verdict": "none", "ids": []}
    if state == "provisional" and (
            t["net"] >= policy["threshold"]
            and len(t["win_turns"]) >= policy.get("distinct_turns", policy["threshold"])
            and len(t["win_topics"]) >= policy.get("distinct_topics", 1)
            and t["prior_wins"] >= policy.get("min_prior", 1)):
        return {"verdict": "settle", "ids": list(t["ids_for"])}
    if state in ("provisional", "confirmed") and _conflict(t, policy):
        if resolved_conflicts >= policy.get("mixed_after", 2):
            return {"verdict": "mixed", "ids": list(t["ids_against"])}
        return {"verdict": "conflict", "ids": list(t["ids_against"])}
    return {"verdict": "none", "ids": []}

