"""why_net.metacog -- the four internal bands per target (DGN-2028 merged
design section 8, AMEND-2 2.11).

  unknown | discovering | mixed | settled

`derive` writes derived/metacog.json keyed by target:
{band, evidence, revisions, episodes, at}.  `frame` and `lookup` read the
snapshot; missing or corrupt means unknown (MC6).  The band only orders
candidates; it never requires a question and never confirms anything.

Provisional engineering constants (MD 8, not measurements of a person):
14-day recency half-life; weighted evidence below 3 -> unknown; revision
share >= 0.4 -> discovering; <= 0.15 -> settled; otherwise mixed.  Age is
counted in whole days, so the episodes of one day weigh the same.

Episodes (MC3): evidence is counted once per owner turn (otl).  A turn
ask's answer belongs to its frame's episode (frame otl + answer), and a
tap, its confirm and a later settle are one episode.  Only `cause:
owner-revision` makes an episode a revision (MC5).  Not counted:
`born-unbacked` retirements and their nodes, unanswered turn asks (they
write nothing), acts (voided or not), and evidence older than the
target's metacog-reset (MC7).  Nodes without a target have no domain.
"""

import json
import math

from . import store
from . import nodes as N

HALF_LIFE_DAYS = 14
MIN_EVIDENCE = 3.0
DISCOVERING_SHARE = 0.4
SETTLED_SHARE = 0.15
BANDS = ("unknown", "discovering", "mixed", "settled")
SNAPSHOT = "metacog.json"
DAY = 86400


def band(target):
    """The snapshot's band for `target`; unknown when absent or corrupt."""
    if not target:
        return "unknown"
    doc = store.read_derived(SNAPSHOT)
    rec = doc.get(target) if isinstance(doc, dict) else None
    if not isinstance(rec, dict) or rec.get("band") not in BANDS:
        return "unknown"
    return rec["band"]


def resets():
    """target -> reset epoch (metacog-reset.json); {} when unreadable."""
    try:
        with open(store.path(store.METACOG_RESET), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for t, v in (doc.items() if isinstance(doc, dict) else []):
        at = store.parse_iso((v or {}).get("at")) if isinstance(v, dict) else None
        if at is not None:
            out[t] = at
    return out


def episodes(events=None, probes=None, frames=None, asks=None):
    """target -> {episode key: {"ts": epoch, "rev": bool}}."""
    events = store.read_rows(store.EVENTS) if events is None else events
    probes = store.read_rows(store.PROBES) if probes is None else probes
    frames = store.read_rows(store.FRAMES) if frames is None else frames
    asks = store.read_rows(store.ASKS) if asks is None else asks
    known = N.fold(events)
    by_evt = {e.get("id"): e for e in events}
    probe = {p["id"]: p for p in probes if "id" in p and "verbatim" in p}
    frame_of = {r["ask"]: r.get("frame_otl") for r in asks
                if r.get("ev") == "create" and r.get("trigger") == "turn"}
    unbacked = {e.get("why") for e in events if e.get("ev") == "retire"
                and (e.get("data") or {}).get("reason") == "born-unbacked"}
    probe_frame = {pid: frame_of.get(p.get("ask")) for pid, p in probe.items()
                   if p.get("ask") in frame_of}
    otl_frame = {p.get("otl"): probe_frame[pid] for pid, p in probe.items()
                 if pid in probe_frame and p.get("otl")}
    out = {}

    def key_of(otl):
        return otl_frame.get(otl) or otl

    def add(wid, key, at, rev):
        n = known.get(wid)
        if not n or not n.get("target") or wid in unbacked or not key:
            return
        ts = store.parse_iso(at)
        if ts is None:
            return
        eps = out.setdefault(n["target"], {})
        cur = eps.get(key)
        if cur is None:
            eps[key] = {"ts": ts, "rev": bool(rev)}
        else:
            cur["rev"] = cur["rev"] or bool(rev)
            cur["ts"] = max(cur["ts"], ts)

    for e in events:
        kind, wid, d = e.get("ev"), e.get("why"), e.get("data") or {}
        if kind == "confirm":
            add(wid, key_of(d.get("otl")), e.get("at"), False)
        elif kind == "cowhy":
            for w in (wid, d.get("other")):
                add(w, key_of(d.get("otl")), e.get("at"), False)
        elif kind == "settle":
            words = by_evt.get(d.get("words")) or {}
            add(wid, key_of((words.get("data") or {}).get("otl")), e.get("at"), False)
        elif kind == "revise":
            p = probe.get(d.get("probe")) or {}
            k = key_of(p.get("otl")) or "evt:" + str(e.get("id"))
            add(wid, k, e.get("at"), d.get("cause") == "owner-revision")
        elif kind in ("pivot", "retire") and d.get("cause"):
            add(wid, "evt:" + str(e.get("id")), e.get("at"), d.get("cause") == "owner-revision")
    for r in frames:
        if r.get("ev") == "answer" and r.get("node"):
            add(r["node"], key_of(r.get("otl")), r.get("at"), r.get("cause") == "owner-revision")
    return out


def classify(eps, now, reset_at=None):
    """{band, evidence, revisions, episodes} for one target's episodes."""
    w_all = w_rev = 0.0
    n = 0
    for ep in eps.values():
        if reset_at is not None and ep["ts"] < reset_at:
            continue
        days = max(0, math.floor((now - ep["ts"]) / DAY))
        w = 0.5 ** (days / float(HALF_LIFE_DAYS))
        w_all += w
        n += 1
        if ep["rev"]:
            w_rev += w
    if w_all < MIN_EVIDENCE:
        b = "unknown"
    else:
        share = w_rev / w_all
        b = "discovering" if share >= DISCOVERING_SHARE else (
            "settled" if share <= SETTLED_SHARE else "mixed")
    return {"band": b, "evidence": round(w_all, 3), "revisions": round(w_rev, 3),
            "episodes": n}


def snapshot(ctx):
    now = ctx.now
    rs = resets()
    doc = {}
    for target, eps in sorted(episodes().items()):
        rec = classify(eps, now, rs.get(target))
        rec["at"] = store.iso_at(now)
        doc[target] = rec
    return doc


def write(ctx):
    """derive: rebuild derived/metacog.json."""
    doc = snapshot(ctx)
    store.write_json_atomic(store.derived_path(SNAPSHOT), doc)
    return doc
