"""why-net surfaces (v2.7 T1 piece P3, LOCKED spec section 5).

  ticket_why / lint_ticket / resolve_ticket   5.2 ticket lint, 5.3 dispatch
  head_lines                                  5.1 session-start Why head
  recent_corrections                          5.1 corrections lines
  board_counts                                5.4 board section data

Read-only on nodes, asks and corrections; lint_ticket only writes
derived/unattached.json. Ticket reads never enqueue an owner question.

Owner-chat contract (4.5): no why-/prb-/ask- id is rendered by head or
corrections lines; hypothesis lines carry the literal "[가설]" token.

English/ASCII only in code; the [가설] token is the frozen marker.
"""

import datetime
import glob
import json
import os
import re
import subprocess
import sys

from . import budget
from . import store
from . import nodes as nodes_mod

HYP_MARK = nodes_mod.HYP_TAG
# owner-confirm: agent-facing placeholder copy (spec 5.1 wording).
UNRESOLVED_LINE = nodes_mod.UNRESOLVED_LINE
HEAD_MAX_NEW_LINES = 2
HEAD_LINE_CHARS = 100
CORR_LINE_CHARS = 120
CORR_MAX = 3

# Ticket statuses that count as in-progress work for the head (spec 5.1
# "in-progress tickets"): the non-terminal, non-shelved lifecycle values of
# ticket-status-enum-lint VALID_STATUS.
IN_PROGRESS = frozenset(["open", "wip", "blocked"])
TERMINAL = frozenset(["done", "dismissed"])

_WHY_RX = re.compile(r"\bwhy-\d{4,}\b")
_TID_RX = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def load_nodes(root=None):
    """The fold of events.jsonl (P1 nodes.fold: the only node definition)."""
    from pathlib import Path
    if (Path(root or store.root_dir()) / "config/why-authority.json").is_file() and _shared_enabled(root):
        from .authority import shared_nodes
        return shared_nodes(root or store.root_dir())
    return nodes_mod.fold(store.read_rows(store.EVENTS, root))


def parse_ts(raw):
    return store.parse_dt(str(raw)) if raw else None


def _cap(text, n):
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[:n - 3] + "..."


# ---------------------------------------------------------------------------
# tickets
# ---------------------------------------------------------------------------

def frontmatter(path):
    """Flat `key: value` map of a ticket's YAML frontmatter ({} if none)."""
    fm = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = f.readline().rstrip("\n")
            if first != "---":
                return {}
            for n, line in enumerate(f):
                line = line.rstrip("\n")
                if line == "---" or n > 200:
                    break
                if ":" in line and not line.startswith((" ", "\t", "#")):
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip().strip("\"'")
    except OSError:
        return {}
    return fm


def ticket_id_of(path, fm=None):
    fm = frontmatter(path) if fm is None else fm
    tid = (fm.get("id") or "").strip()
    if _TID_RX.match(tid):
        return tid
    base = os.path.basename(path)
    m = re.match(r"^([A-Za-z][A-Za-z0-9]*-\d+)", base)
    return m.group(1) if m else base[:-3] if base.endswith(".md") else base


def find_ticket(ref, worklog_dir):
    """Ticket ref (id or path) -> path, or None."""
    if not ref:
        return None
    if os.path.isfile(ref):
        return ref
    if not _TID_RX.match(ref):
        return None
    for p in sorted(glob.glob(os.path.join(worklog_dir, ref + "*.md"))):
        base = os.path.basename(p)
        if base == ref + ".md" or base.startswith(ref + "-"):
            return p
    return None


def _node_state(nodes, wid):
    """-> (state, why_id, reason) for one Why id.  AMEND-1 F17: a
    provisional node is attached like a confirmed one (LIVE_CONFIRMED)."""
    n = nodes.get(wid)
    hops = 0
    while n is not None and n.get("status") == "pivoted" \
            and n.get("superseded_by") and hops < 4:
        wid = n["superseded_by"]
        n = nodes.get(wid)
        hops += 1
    if n is None:
        return "none", None, "unknown-id"
    st = n.get("status")
    if st in nodes_mod.LIVE_CONFIRMED:
        return "confirmed", wid, None
    if st in ("hypothesis", "contested"):
        return "hypothesis", wid, None
    return "none", wid, "why-" + str(st or "unknown")


def _whys_state(nodes, ids, via):
    """AMEND-1 rev 3: a work item may hold several Whys (`why: why-1,
    why-2`).  -> the ticket_why dict: `whys` = every resolved id, `why` =
    the first live-confirmed one (else the first), `state` = confirmed when
    any is live-confirmed, else hypothesis when any is open, else none."""
    res = [(m,) + _node_state(nodes, m) for m in ids]
    whys = []
    for raw, st, wid, _r in res:
        w = wid or raw
        if w not in whys:
            whys.append(w)
    pick = next((r for r in res if r[1] == "confirmed"), None) \
        or next((r for r in res if r[1] == "hypothesis"), None) or res[0]
    raw, st, wid, reason = pick
    return {"state": st, "why": wid or raw, "via": via, "reason": reason,
            "status": (nodes.get(wid) or {}).get("status"), "whys": whys}


def ticket_why(path, nodes, worklog_dir=None, records=None, root=None):
    """Spec 5.2 check (4): `why:` / `why_id:` frontmatter, else `anchor:` inheritance
    (one hop). -> {"state", "why", "whys", "via", "reason"}."""
    fm = frontmatter(path)
    # M1 citations use actual owner confirmation, independent of a legacy
    # status settled by repeated actions. Old unregistered tickets retain
    # their existing resolution contract.
    from . import citations
    records = citations.work_rows(store.read_rows(store.EVENTS, root)) if records is None else records
    registered = citations.registered_ticket(path, records)
    if registered:
        work = registered["data"]
        rows = store.read_rows(store.EVENTS, root)
        checks, inherited = citations.rechecks(work, rows, root=root)
        proof = citations.owner_confirmations(rows, root, inherited)
        ids = work["relations"]["purpose"]
        result = _whys_state(nodes, ids, "record:" + registered["id"])
        confirmed = [w for w in ids if proof.get(w, {}).get("state") == "confirmed"]
        changed = citations.ticket_changed(path, work, root)
        if confirmed and not checks and not changed:
            result.update(state="confirmed", why=confirmed[0])
        else:
            result["state"] = "hypothesis"
        result["rechecks"] = checks
        result["ticket_changed"] = changed
        return result
    raw = fm.get("why") or fm.get("why_id") or ""
    if raw:
        ids = _WHY_RX.findall(raw)
        if not ids:
            return {"state": "none", "why": None, "via": "why",
                    "reason": "unknown-id", "whys": []}
        return _whys_state(nodes, ids, "why")
    anchor = (fm.get("anchor") or "").strip()
    if anchor:
        wdir = worklog_dir or os.path.dirname(os.path.abspath(path))
        ap = find_ticket(anchor, wdir)
        if ap and os.path.abspath(ap) != os.path.abspath(path):
            afm = frontmatter(ap)
            araw = afm.get("why") or afm.get("why_id") or ""
            ids = _WHY_RX.findall(araw)
            if ids:
                return _whys_state(nodes, ids, "anchor:" + anchor)
    return {"state": "none", "why": None, "via": None, "reason": "no-why", "whys": []}


def resolve_ticket(ref, root=None, worklog_dir=None):
    """Dispatch value (spec 5.3): "none" | "hypothesis" | <why id>."""
    root = root or store.root_dir()
    wdir = worklog_dir or os.path.join(root, "worklog")
    p = find_ticket(ref, wdir)
    if not p:
        return "none"
    from . import citations
    r = ticket_why(p, load_nodes(root), wdir,
                   citations.work_rows(store.read_rows(store.EVENTS, root)), root)
    if r["state"] == "confirmed":
        return r["why"]
    return r["state"]


def lint_ticket(path, root=None, worklog_dir=None, enqueue=None):
    """`why-net lint-ticket <path>`: record the state in
    derived/unattached.json without owner-question side effects (FDGN-594).

    The obsolete enqueue callback is accepted for API compatibility but
    never invoked. Reading ticket state cannot authorize owner exposure.
    """
    root = root or store.root_dir()
    wdir = worklog_dir or os.path.dirname(os.path.abspath(path))
    from . import citations
    r = ticket_why(path, load_nodes(root), wdir,
                   citations.work_rows(store.read_rows(store.EVENTS, root)), root)
    tid = ticket_id_of(path)
    r["ticket"] = tid
    with store.locked(root):
        doc = store.read_derived("unattached.json", root) or {}
        tickets = doc.get("tickets") if isinstance(doc, dict) else None
        tickets = tickets if isinstance(tickets, dict) else {}
        tickets[tid] = {"state": r["state"], "why": r["why"],
                        "via": r["via"], "reason": r["reason"],
                        "status": r.get("status"),
                        "path": os.path.abspath(path), "at": store.now_iso()}
        store.write_json_atomic(store.derived_path("unattached.json", root),
                                {"tickets": tickets})
    r["enqueued"] = False
    return r


# ---------------------------------------------------------------------------
# session head (5.1)
# ---------------------------------------------------------------------------

def touched_tickets(transcript_path):
    """worklog ticket paths the session wrote (Edit/Write/MultiEdit), most
    recent first, unique."""
    seen = []
    try:
        f = open(transcript_path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with f:
        for line in f:
            if '"tool_use"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            content = (o.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") in ("Edit", "Write", "MultiEdit")):
                    continue
                fp = (b.get("input") or {}).get("file_path") or ""
                base = os.path.basename(fp)
                if (os.path.basename(os.path.dirname(os.path.normpath(fp)))
                        == "worklog" and base.endswith(".md")
                        and not base.startswith("_")):
                    if fp in seen:
                        seen.remove(fp)
                    seen.append(fp)
    return list(reversed(seen))


def session_start_ts(transcript_path):
    """First timestamp of a transcript (its session start), or None."""
    try:
        for e in store.jsonl_iter(transcript_path, errors="replace"):
            ts = parse_ts(e.get("timestamp"))
            if ts:
                return ts
    except OSError:
        pass
    return None


def open_asks(root=None):
    """Ask ids whose lifecycle has not reached a terminal state."""
    asks = budget.fold_asks(store.read_rows(store.ASKS, root))
    return sorted(a for a, v in asks.items()
                  if v.get("state") not in budget.TERMINAL)


def _head_line(line):
    """nodes.head_lines line -> recap line: capped, every Why line a list
    item ("[가설] x" -> "- [가설] x")."""
    if line == UNRESOLVED_LINE:
        return line
    if line.startswith("- "):
        line = line[2:]
    return "- " + _cap(line, HEAD_LINE_CHARS)


def head_lines(root=None, budget_bytes=None, ticket_paths=(), since=None,
               worklog_dir=None):
    """Spec 5.1 Why head: <= 5 Why lines (confirmed path first, then at
    most one [가설] line; P1 nodes.head_lines) + <= 2 "new since last
    session" lines, or the one unresolved line. ticket_paths = tickets
    touched in the previous session; since = previous session start (aware
    datetime) or None."""
    root = root or store.root_dir()
    nodes = load_nodes(root)
    work = []
    for p in ticket_paths:
        if not os.path.isfile(p):
            continue
        if frontmatter(p).get("status") not in IN_PROGRESS:
            continue
        r = ticket_why(p, nodes, worklog_dir or os.path.dirname(p))
        for w in r.get("whys") or ([r["why"]] if r["why"] else []):
            if w in nodes and w not in work:
                work.append(w)
    lines = [_head_line(l) for l in nodes_mod.head_lines(nodes, work)]
    new = []
    if since is not None:
        # AMEND-1 4: a settle is the only confirm; words make it provisional
        n_conf = n_prov = n_cont = 0
        for e in store.read_rows(store.EVENTS, root):
            at = parse_ts(e.get("at"))
            if at is None or at < since:
                continue
            if e.get("ev") == "settle":
                n_conf += 1
            elif e.get("ev") in ("confirm", "resolve"):
                n_prov += 1
            elif e.get("ev") == "contest":
                n_cont += 1
        if n_conf or n_prov or n_cont:
            new.append("- new since last session: %d confirmed, %d provisional, "
                       "%d contested" % (n_conf, n_prov, n_cont))
    n_asks = len(open_asks(root))
    if n_asks:
        new.append("- open asks: %d" % n_asks)
    lines += new[:HEAD_MAX_NEW_LINES]
    if budget_bytes is not None:
        while len(lines) > 1 and len("\n".join(lines).encode("utf-8")) \
                > budget_bytes:
            lines.pop()
    return lines


# ---------------------------------------------------------------------------
# corrections (5.1)
# ---------------------------------------------------------------------------

def fold_corrections(root=None):
    """corrections.jsonl -> [record] with backfilled message_uuid and
    consumption folded in. A consumption is the record's own consumed_by or
    a later {"consumed": "cor-..", "consumed_by": ..} row."""
    recs, order = {}, []
    for row in store.read_rows(store.CORRECTIONS, root):
        if row.get("id"):
            recs[row["id"]] = dict(row)
            order.append(row["id"])
        elif row.get("backfill") in recs:
            recs[row["backfill"]]["message_uuid"] = row.get("message_uuid")
        elif row.get("consumed") in recs:
            recs[row["consumed"]]["consumed_by"] = (row.get("consumed_by")
                                                    or "consumed")
    return [recs[i] for i in order]


def recent_corrections(root=None, since=None, limit=CORR_MAX):
    """Unconsumed corrections said since `since`, newest first."""
    out = []
    for r in fold_corrections(root):
        if r.get("consumed_by"):
            continue
        at = parse_ts(r.get("said_at"))
        if since is not None and (at is None or at < since):
            continue
        out.append((at, r))
    out.sort(key=lambda x: x[0] or datetime.datetime.min.replace(
        tzinfo=datetime.timezone.utc), reverse=True)
    return [r for _at, r in out[:limit]]


def correction_lines(records):
    lines = []
    for r in records:
        at = parse_ts(r.get("said_at"))
        stamp = at.astimezone().strftime("%m-%d %H:%M") if at else "--"
        lines.append("- (%s) %s" % (stamp, _cap(r.get("text"),
                                                CORR_LINE_CHARS)))
    return lines


# ---------------------------------------------------------------------------
# board (5.4)
# ---------------------------------------------------------------------------

def running_dispatch_none(bot_data_dir, max_age_s=86400):
    """Items (ticket or label) of live dispatch runs recorded DISPATCH_WHY=none
    (run.env present, status.json not yet written = still running)."""
    out = []
    now = datetime.datetime.now().timestamp()
    for env in glob.glob(os.path.join(bot_data_dir, "dispatch", "*",
                                      "run.env")):
        d = os.path.dirname(env)
        try:
            if os.path.exists(os.path.join(d, "status.json")) or \
                    now - os.path.getmtime(env) > max_age_s:
                continue
            kv = {}
            with open(env, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.rstrip("\n").split("=", 1)
                        kv[k] = v.strip("'\"")
        except OSError:
            continue
        if kv.get("DISPATCH_WHY") == "none":
            out.append(kv.get("TICKET") or kv.get("LABEL") or
                       os.path.basename(d))
    return sorted(out)


def board_counts(root=None, is_live=None, bot_data_dir=None):
    """-> {"none": N, "hypothesis": N, "provisional": N, "contested": N,
    "asks": N, "fold": [<= 3 ids]}.  AMEND-1 4 #9: provisional and
    contested are counted on their own; a provisional ticket is attached,
    never in "none".  Each ticket is counted by its node's CURRENT status.
    is_live(path) -> bool keeps only tickets still open (None = no filter);
    only the tickets the lint recorded are ever opened."""
    doc = store.read_derived("unattached.json", root) or {}
    tickets = doc.get("tickets") if isinstance(doc, dict) else {}
    none_ids, hyp, prov, cont = [], 0, 0, 0
    nodes = load_nodes(root)
    for tid, rec in sorted((tickets or {}).items()):
        if is_live is not None and not is_live((rec or {}).get("path") or ""):
            continue
        st = (rec or {}).get("state")
        status = (nodes.get((rec or {}).get("why")) or {}).get("status")
        if st == "none":
            none_ids.append(tid)
        elif status == nodes_mod.PROVISIONAL:
            prov += 1
        elif status == "contested":
            cont += 1
        elif st == "hypothesis":
            hyp += 1
    for item in running_dispatch_none(bot_data_dir or store.bot_data_dir(root)):
        if item not in none_ids:
            none_ids.append(item)
    return {"none": len(none_ids), "hypothesis": hyp, "provisional": prov,
            "contested": cont, "asks": len(open_asks(root)), "fold": none_ids[:3]}


def _shared_enabled(root):
    from .runtime import enabled
    return enabled(root or store.root_dir())
