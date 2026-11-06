"""Retro review of automatic links stored by the owning work record."""
import json
from pathlib import Path

from . import runtime, store

MAX_LINKS = 7


def _files(root):
    base = runtime.state_dir(root)
    return sorted(list((base / "records").glob("*.json")) +
                  list((base / "tickets").glob("*/*.json")))


def _identity(path, link):
    return {"record": str(path), "work_key": link["work_key"],
            "node_id": link["node_id"], "node_revision": link["node_revision"]}


def pending(root, limit=MAX_LINKS, logger=None):
    rows = []
    for path in _files(root):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for link in state.get("agent_links", []):
            if link.get("owner_confirmed") is not False:
                continue
            work, why = link.get("work_label"), link.get("why_label")
            if not all(isinstance(v, str) and v.strip() for v in (work, why)):
                if logger:
                    logger("link-review skipped unreadable work label")
                continue
            rows.append(dict(link=_identity(path, link), work=work.strip(), why=why.strip(),
                             candidates=link.get("why_candidates", []),
                             linked_at=link.get("linked_at", 0)))
    rows.sort(key=lambda row: (row["linked_at"], row["work"], row["why"]))
    return rows[:max(0, min(MAX_LINKS, limit))]


def menu(rows, copy):
    rows = list(rows)[:MAX_LINKS]
    if not rows:
        return None
    text = copy["heading"] + "\n" + "\n".join(
        "%d. %s -> %s" % (i, row["work"], row["why"])
        for i, row in enumerate(rows, 1))
    buttons = [[{"text": copy["all"], "callback_data": "opt:all"}]]
    buttons.extend([[{"text": copy["fix"].replace("{n}", str(i)),
                      "callback_data": "opt:fix:%d" % i}]]
                   for i in range(1, len(rows) + 1))
    return {"text": text, "buttons": buttons, "rows": rows}


def update(root, identities, *, node=False, confirmed_at=None):
    grouped = {}
    for identity in identities:
        grouped.setdefault(identity["record"], []).append(identity)
    with store.canonical(root), store.locked(wait=2.0):
        allowed = set(_files(root))
        for name, wanted in grouped.items():
            path = Path(name)
            if path not in allowed:
                return False
            state = json.loads(path.read_text())
            for identity in wanted:
                matches = [row for row in state.get("agent_links", []) if all(
                    row.get(k) == identity.get(k) for k in
                    ("work_key", "node_id", "node_revision"))]
                if len(matches) != 1:
                    return False
                old = matches[0]
                if node is None:
                    state["agent_links"].remove(old)
                elif node:
                    old.update(node_id=node["id"], node_revision=node["revision"],
                               why_label=node["label"], owner_confirmed=True,
                               confirmed_at=confirmed_at)
                else:
                    old.update(owner_confirmed=True, confirmed_at=confirmed_at)
            runtime.atomic(path, state)
    return True
