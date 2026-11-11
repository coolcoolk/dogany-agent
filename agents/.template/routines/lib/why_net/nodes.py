"""why_net.nodes -- fold events.jsonl into Why nodes (spec 2.1) + DAG reads.

The fold is the only definition of a node; derived/nodes.json and `show`
are renders of it.  support/counter are DERIVED (2.6) and filled by derive
(P2); the fold leaves them 0.
"""

import re

HYP_TAG = "[\uac00\uc124]"  # the literal [ga-seol] marker (spec 4.5 b)
# AMEND-1 2.6: agent-facing head prefix of a provisional line ([im-si]).
PROV_TAG = "[\uc784\uc2dc]"
UNRESOLVED_LINE = "Why unresolved for current work"
HEAD_MAX_LINES = 5
HEAD_MAX_HYP = 1
HEAD_MAX_PROV = 1

TERMINAL = ("pivoted", "retired")
PROVISIONAL = "confirmed-provisional"
# AMEND-1 1.4 (F17): wherever LOCKED code tests "is confirmed", a provisional
# node counts too (derive.contests, the ups pick loop, the revise reset,
# head_lines, the ticket lint).
LIVE_CONFIRMED = ("confirmed", PROVISIONAL)
STATUSES = ("hypothesis", PROVISIONAL, "confirmed", "contested", "pivoted",
            "retired")
SCOPE_RE = re.compile(r"^(framework|kit:[A-Za-z0-9._-]+|product:[A-Za-z0-9._-]+)$")
# AMEND-2 2.2: target identity `<pack_id>/<target id>` (both halves
# base_contract.ID_RE); the `/` keeps it from ever matching SCOPE_RE.
TARGET_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}/[a-z][a-z0-9_-]{0,63}$")
# AMEND-2 2.11: why a Why changed.  Metacog counts owner-revision only.
CAUSES = ("owner-revision", "agent-misread", "external")


def _new_node(ev):
    d = ev.get("data") or {}
    return {
        "id": ev["why"],
        "text": d.get("text", ""),
        "worded_by": d.get("worded_by", "agent"),
        "status": d.get("status", "hypothesis"),
        "parents": list(d.get("parents") or []),
        "superseded_by": None,
        "scope": d.get("scope", "framework"),
        "said_at": d.get("said_at"),
        "source": d.get("source"),
        "confirm": None,
        "owner_words": list(d.get("owner_words") or []),
        "revisions": [],
        "support": 0.0,
        "counter": 0,
        "work": d.get("work"),
        # AMEND-1 1.4: the words anchor -- the otl of the owner words that
        # made the node provisional (`anchor`) and the event that wrote them
        # (`anchor_evt`).  Acts count only when strictly later (I2).
        "anchor": None,
        "anchor_evt": None,
        "settle": None,
        "words_at": None,
        # AMEND-1 rev 3 (multiple Whys): Whys the owner said hold TOGETHER
        # ("both") -- a pick of one is never a loss for the other.
        "co": [],
        # AMEND-2 2.1-2.3: the owner turns the node was inferred from and
        # answered in (evidence is not words), its target, and -- for a
        # node born from an answer -- the ask / slot / probe it was born in.
        "evidence": list(d.get("evidence") or []),
        "target": d.get("target"),
        "born": d.get("born"),
    }


def otl_order(otl):
    """otl-NNNNNN -> its ledger order (int); 0 for anything else."""
    if isinstance(otl, str) and otl.startswith("otl-") and otl[4:].isdigit():
        return int(otl[4:])
    return 0


def _set_anchor(n, ev, otl):
    n["anchor"] = otl
    n["anchor_evt"] = ev.get("id")
    n["words_at"] = ev.get("at")


def _pair(a, b):
    if a is None or b is None or a is b:
        return
    if b["id"] not in a["co"]:
        a["co"].append(b["id"])
    if a["id"] not in b["co"]:
        b["co"].append(a["id"])


def fold(events):
    """events (in file order) -> {why_id: node}.  Events naming an unknown
    node are ignored (the writers refuse them; a hand-append is an audit
    finding, not a fold crash)."""
    nodes = {}
    for ev in events:
        kind, wid = ev.get("ev"), ev.get("why")
        d = ev.get("data") or {}
        if kind == "propose":
            if wid and wid not in nodes:
                nodes[wid] = _new_node(ev)
            continue
        n = nodes.get(wid)
        if n is None:
            continue
        if kind == "confirm":
            # AMEND-1 1.3: owner words make a Why provisional, never confirmed.
            n["status"] = PROVISIONAL
            n["settle"] = None
            _set_anchor(n, ev, d.get("otl"))
            n["confirm"] = {"probe": d.get("probe"), "quote": d.get("quote"),
                            "said_at": d.get("said_at"),
                            "source": d.get("source")}
            if d.get("worded_by") == "owner":
                n["worded_by"] = "owner"
            if d.get("said_at"):
                n["said_at"] = d.get("said_at")
            if d.get("probe") and d["probe"] not in n["owner_words"]:
                n["owner_words"].append(d["probe"])
        elif kind == "attach":
            p = d.get("parent")
            if p and p not in n["parents"]:
                n["parents"].append(p)
        elif kind == "revise":
            v = len(n["revisions"]) + 2
            rev = {"v": v, "at": ev.get("at"),
                   "text_before": d.get("text_before"),
                   "text_after": d.get("text_after"),
                   "by_probe": d.get("probe")}
            if d.get("cause") is not None:
                rev["cause"] = d["cause"]   # AMEND-2 2.11
            n["revisions"].append(rev)
            for e in d.get("evidence") or []:
                if e not in n["evidence"]:
                    n["evidence"].append(e)  # AMEND-2 2.1: the probe's otl
            n["text"] = d.get("text_after", n["text"])
            # Revised wording is the agent's, so the owner-verbatim
            # invariant of worded_by=owner would no longer hold.
            n["worded_by"] = "agent"
            # Grill-3 fix 1: the owner confirmed the OLD wording; the new
            # one is a hypothesis until the owner confirms it again.
            if n["status"] in LIVE_CONFIRMED + ("contested",):
                n["status"] = "hypothesis"
                n["confirm"] = None
                n["settle"] = None
                n["anchor"] = n["anchor_evt"] = None
            if d.get("probe") and d["probe"] not in n["owner_words"]:
                n["owner_words"].append(d["probe"])
        elif kind == "pivot":
            n["status"] = "pivoted"
            n["superseded_by"] = d.get("to")
            n["pivot_at"] = ev.get("at")
            if isinstance(d.get("basis"), dict):
                n["pivot_basis"] = d["basis"]
            if d.get("cause") is not None:
                n["end_cause"] = d["cause"]
        elif kind == "retire":
            n["status"] = "retired"
            if d.get("cause") is not None:
                n["end_cause"] = d["cause"]
            if d.get("reason") is not None:
                n["end_reason"] = d["reason"]
        elif kind == "contest":
            n["status"] = "contested"
            rev = d.get("revision")
            if isinstance(rev, dict):
                n["revisions"].append(rev)  # text kept: before == after
        elif kind == "settle":
            # Written only by derive through the re-checking builder.
            if n["status"] == PROVISIONAL and n["anchor_evt"] == d.get("words"):
                n["status"] = "confirmed"
                n["settle"] = {"evt": ev.get("id"), "acts": list(d.get("acts") or []),
                               "at": ev.get("at")}
        elif kind == "cowhy":
            # AMEND-1 rev 3: a "both" answer.  Each side is the owner's words
            # (provisional, anchored at the answer); the pair is recorded so
            # neither one's picks count against the other.
            other = nodes.get(d.get("other"))
            for x in (n, other):
                if x is None or x["status"] in TERMINAL:
                    continue
                if x["status"] != "confirmed":
                    x["status"] = PROVISIONAL
                    x["settle"] = None
                    _set_anchor(x, ev, d.get("otl"))
            _pair(n, other)
        elif kind == "resolve":
            # AMEND-1 2.3: a split answer.  Both sides land provisional; the
            # side the owner did not pick (or the said side itself) is
            # re-anchored at the answer's otl, so its old rows stop counting.
            side, other = d.get("side"), nodes.get(d.get("other"))
            if n["status"] not in TERMINAL:
                n["status"] = PROVISIONAL
                n["settle"] = None
                _set_anchor(n, ev, d.get("otl"))
            if side == "both":
                _pair(n, other)
            if side in ("acted", "both") and other is not None \
                    and other["status"] not in TERMINAL + ("confirmed",):
                other["status"] = PROVISIONAL
                other["settle"] = None
                # the contest's losses carry as the acted side's wins
                _set_anchor(other, ev, d.get("carry_from") or d.get("otl"))
    return nodes


def needs_owner_turn(node):
    """pivot/retire need the resolver unless the node is a bare agent
    hypothesis (spec 3: hypothesis with empty owner_words)."""
    return not (node["status"] == "hypothesis" and not node["owner_words"]
                and node["confirm"] is None)


def ancestors(nodes, wid):
    """All ancestors of wid (excluding wid) by parent edges."""
    seen, stack = set(), list(nodes.get(wid, {}).get("parents", []))
    while stack:
        p = stack.pop()
        if p in seen or p not in nodes:
            continue
        seen.add(p)
        stack.extend(nodes[p]["parents"])
    return seen


def would_cycle(nodes, child, parent):
    """True when adding edge child -> parent closes a cycle."""
    return parent == child or child in ancestors(nodes, parent)


def _live(nodes, wid):
    """Follow superseded_by from a pivoted node to its live successor."""
    seen = set()
    while wid in nodes and nodes[wid]["status"] == "pivoted" \
            and nodes[wid]["superseded_by"] and wid not in seen:
        seen.add(wid)
        wid = nodes[wid]["superseded_by"]
    return wid


def head_lines(nodes, work_whys):
    """The Why-path part of the head (spec 5.1): at most 5 lines, confirmed
    path first (root first, each node once even when reached by two parent
    paths), then at most 1 relevant hypothesis line prefixed with HYP_TAG.

    `work_whys` = the Why ids the current work resolves to.  Resolving the
    current work from tickets, and the "new since last session" lines, are
    the head verb's job (P3 seam), not this function's."""
    dist = {}
    frontier = [_live(nodes, w) for w in work_whys if w in nodes]
    for w in frontier:
        dist.setdefault(w, 0)
    while frontier:
        nxt = []
        for w in frontier:
            for p in nodes[w]["parents"]:
                p = _live(nodes, p)
                if p in nodes and p not in dist:
                    dist[p] = dist[w] + 1
                    nxt.append(p)
        frontier = nxt
    reach = [w for w in dist if nodes[w]["status"] not in TERMINAL]
    conf = [w for w in reach if nodes[w]["status"] == "confirmed"]
    provs = sorted((w for w in reach if nodes[w]["status"] == PROVISIONAL),
                   key=lambda w: (dist[w], w))[:HEAD_MAX_PROV]
    hyps = sorted((w for w in reach if nodes[w]["status"] == "hypothesis"),
                  key=lambda w: (dist[w], w))[:HEAD_MAX_HYP]
    keep = sorted(conf, key=lambda w: (dist[w], w))[
        :HEAD_MAX_LINES - len(hyps) - len(provs)]
    if not keep and not hyps and not provs:
        return [UNRESOLVED_LINE]
    # Root first: deeper (farther from the work) nodes print first.
    keep.sort(key=lambda w: (-dist[w], w))
    lines = ["- " + nodes[w]["text"] for w in keep]
    # AMEND-1 2.6: then at most 1 provisional line, then at most 1 [ga-seol].
    lines += ["%s %s" % (PROV_TAG, nodes[w]["text"]) for w in provs]
    lines += ["%s %s" % (HYP_TAG, nodes[w]["text"]) for w in hyps]
    return lines
