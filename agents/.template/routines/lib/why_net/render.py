"""why_net.render -- `why-net render` -> state/why-net/why-net.md (spec 1, 5.5).

The read-only markdown render is the console's second input beside
derived/nodes.json (data contract:
docs/design/v27-t1-why-net-console-contract.md).  nodes.json carries the 2.1 node fold; this file adds what
the fold only names by id: the owner's verbatim words behind every
owner_words key, and the open asks.  Console-only surface: ids appear here
and never in owner chat (4.5 d).

Line grammar (stable; the console parses it):
  # Why net (read-only render; source: events.jsonl)
  ## <status>                          one per non-empty status, fixed order
  - <why-id> [<hyp tag> ]<text>          node line; text JSON-escaped only
                                         when it holds a newline
    (worded_by=<w>, scope=<s>) is appended to the node line
    - parents: <id>, <id>
    - support: <float>, counter: <int>
    - owner: <json string>             confirm quote
    - owner_words: <key> <json string> one per owner_words key, verbatim
    - v<n>: <json before> -> <json after>
    - flags: <flag>, <flag>            AMEND-1: derived stale / mixed
    - act: <kind> <ref> <class> <direction>   one per act row (no note)
    - pivot: <date> basis=<evt-id> acts=<n>   a pivoted node's basis
  ## open asks
  - <ask-id> state=<s> kind=<k> trigger=<t> topic=<topic> channel=<c>
    - <slot>. <json label>[ (<why-id>)]

English/ASCII only.  Stdlib only.
"""

import json

from . import budget, store
from . import nodes as N

# AMEND-1 5.1 ConsoleContract: six statuses in this order.
STATUS_ORDER = ("confirmed", "confirmed-provisional", "contested", "hypothesis",
                "pivoted", "retired")
TITLE = "# Why net (read-only render; source: events.jsonl)"
ASKS_HEADING = "## open asks"


def _q(s):
    return json.dumps(s if s is not None else "", ensure_ascii=False)


def _text(s):
    s = s or ""
    return _q(s) if "\n" in s else s


def owner_word_texts(node, probes):
    """owner_words key -> the owner's verbatim words: a probe id -> that
    probe's verbatim; seed:<dec> -> the seeded quote (the confirm quote)."""
    by_id = {p.get("id"): p for p in probes if p.get("id")}
    out = []
    for key in node.get("owner_words") or []:
        if key.startswith("seed:"):
            text = (node.get("confirm") or {}).get("quote") or node.get("text")
        else:
            text = (by_id.get(key) or {}).get("verbatim")
        out.append((key, text))
    return out


def render_text(nodes, probes, asks, support=None, counter=None, acts=None,
                flags=None):
    support = support or {}
    counter = counter or {}
    acts = acts or []
    flags = flags or {}
    out = [TITLE, ""]
    for status in STATUS_ORDER:
        group = sorted((n for n in nodes.values() if n["status"] == status),
                       key=lambda n: n["id"])
        if not group:
            continue
        out.append("## %s" % status)
        for n in group:
            tag = N.HYP_TAG + " " if status == "hypothesis" else ""
            out.append("- %s %s%s (worded_by=%s, scope=%s)" % (
                n["id"], tag, _text(n["text"]), n["worded_by"], n["scope"]))
            if n["parents"]:
                out.append("  - parents: %s" % ", ".join(n["parents"]))
            # AMEND-2 2.1-2.3 (only when set, so older nodes render as before)
            if n.get("target"):
                out.append("  - target: %s" % n["target"])
            if n.get("evidence") or n.get("born"):
                born = n.get("born") or {}
                out.append("  - evidence: %d%s" % (len(n.get("evidence") or []), (
                    " born=%s/%s" % (born.get("ask"), born.get("slot") or born.get("probe"))
                    if born else "")))
            out.append("  - support: %s, counter: %d" % (
                float(support.get(n["id"], n.get("support") or 0.0)),
                int(counter.get(n["id"], n.get("counter") or 0))))
            if n.get("confirm") and n["confirm"].get("quote"):
                out.append("  - owner: %s" % _q(n["confirm"]["quote"]))
            for key, text in owner_word_texts(n, probes):
                out.append("  - owner_words: %s %s" % (key, _q(text)))
            for r in n["revisions"]:
                out.append("  - v%s: %s -> %s" % (r.get("v"), _q(r.get("text_before")),
                                                  _q(r.get("text_after"))))
            if flags.get(n["id"]):
                out.append("  - flags: %s" % ", ".join(flags[n["id"]]))
            # F14: kind ref class direction only -- the note never renders
            for a in acts:
                if a.get("why") == n["id"]:
                    out.append("  - act: %s %s %s %s" % (a.get("kind"), a.get("ref"),
                                                         a.get("class"), a.get("direction")))
            basis = n.get("pivot_basis")
            if status == "pivoted" and isinstance(basis, dict):
                out.append("  - pivot: %s basis=%s acts=%d" % (
                    (n.get("pivot_at") or "")[:10] or "-", basis.get("contest"),
                    len(basis.get("acts") or [])))
        out.append("")
    live = sorted((a for a in asks.values()
                   if a.get("state") not in budget.TERMINAL),
                  key=lambda a: a["ask"])
    if live:
        out.append(ASKS_HEADING)
        for a in live:
            out.append("- %s state=%s kind=%s trigger=%s topic=%s channel=%s" % (
                a["ask"], a.get("state"), a.get("kind"), a.get("trigger"),
                a.get("topic"), a.get("channel")))
            for s in a.get("shown") or []:
                wid = s.get("why")
                out.append("  - %s. %s%s" % (s.get("slot"), _q(s.get("label")),
                                             " (%s)" % wid if wid else ""))
        out.append("")
    return "\n".join(out) + "\n"


def write(root=None):
    """Render the store into why-net.md (atomic).  -> the path."""
    from . import acts as acts_mod
    from . import derive
    store._makedirs(store.store_dir(root))
    evs = store.read_rows(store.EVENTS, root)
    nodes = N.fold(evs)
    probes = store.read_rows(store.PROBES, root)
    asks = budget.fold_asks(store.read_rows(store.ASKS, root))
    sup, cnt, _ = derive.support_counter(probes)
    acts = acts_mod.act_rows(store.read_rows(store.ACTS, root))
    ctx = budget.Ctx(root=root)
    flags = {w: acts_mod.flags(ctx, n, acts, evs) for w, n in nodes.items()}
    dest = store.path("why-net.md", root)
    store.atomic_write(dest, render_text(nodes, probes, asks, sup, cnt, acts,
                                         flags), private=False)
    return dest
