"""FDGN-594 owner-purpose reuse and own-agent projections.

Semantic fit is supplied as cited reasoning. Status, scope, chronology and
proof are verified here. A unique fit never grants work execution authority.
"""

from . import nodes


def owner_backed_reusable(node, owner, as_of, required_scope, evidence_resolver):
    if not isinstance(node, dict) or node.get("status") not in nodes.LIVE_CONFIRMED:
        return False
    if node.get("owner_id") != owner or not node.get("revision"):
        return False
    if node.get("scope") != required_scope:
        return False
    if node.get("invalidated") or node.get("erased") or evidence_resolver is None:
        return False
    # The resolver must prove current wording/revision and reject any later
    # applicable contest, pivot, retirement, revision or erasure as of as_of.
    return evidence_resolver.node_confirmation(node, owner, as_of, required_scope) is True


def match_purpose(known, candidates, owner, as_of, required_scope, evidence_resolver):
    """Keep only proven fits; ancestor + child is one adequate path."""
    fits = {}
    for fit in candidates:
        node = known.get(fit.get("node_id"))
        if (fit.get("relation") and node and fit.get("revision") == node.get("revision")
                and owner_backed_reusable(node, owner, as_of, required_scope, evidence_resolver)):
            fits[node["id"]] = node

    def ancestors(wid):
        seen, stack = set(), list(known[wid].get("parents", ()))
        while stack:
            current = stack.pop()
            if current == wid:
                raise ValueError("tree-cycle")
            if current in seen:
                continue
            seen.add(current)
            stack.extend(known.get(current, {}).get("parents", ()))
        return seen

    covered = set()
    for wid in fits:
        covered.update(ancestors(wid))
    leaves = sorted(set(fits) - covered)
    if len(leaves) == 1:
        node = fits[leaves[0]]
        return {"state": "linked", "node": node["id"], "revision": node["revision"],
                "text": node["text"]}
    return {"state": "candidates", "nodes": leaves[:3]}


def agent_tree_view(owner_id, agent_id, known, links, origins=None):
    """One shared tree, own responsibility branches and ancestors only."""
    own = [r for r in links if r.get("owner_id") == owner_id
           and r.get("agent_id") == agent_id]
    visible, stack = set(), [r["node_id"] for r in own]
    stack.extend(wid for wid, agent in (origins or {}).items() if agent == agent_id)
    while stack:
        wid = stack.pop()
        if wid in visible:
            continue
        node = known.get(wid)
        if not node or node.get("owner_id") != owner_id:
            continue
        visible.add(wid)
        stack.extend(node.get("parents", ()))
    return {"nodes": {k: known[k] for k in sorted(visible)}, "links": own}
