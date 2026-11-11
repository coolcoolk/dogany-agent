"""why_net.derive -- nightly rebuild of derived/* (spec 2.6, 3 `derive`).

Order: historical ask lifecycle -> act/support/conflict facts -> tuning ->
derived files. FDGN-594 retires every cold-question producer; queue.json is
an empty compatibility projection. Existing historical evidence is retained.
The `--audit` half (hash chain + confirm re-proof) lives in the CLI.
"""

import os
import sys

from . import WhyRefusal
from . import acts as acts_mod
from . import ask as ask_mod
from . import budget, events, store, ups
from . import nodes as N

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import why_owner_turn  # noqa: E402

IGNORE_AFTER = ups.IGNORE_AFTER
NOT_SHOWN_SETTLE = 600  # an ask younger than this may still be mid-reply
WEIGHTS = {"pick": 1.0, "pick-multi": 1.0, "partial": 1.0, "closest": 0.5}
# AMEND-1 2.4: act-contest > fork-from-mixed > contest > act-ripe > ripe > age
PRIORITY = {"act-contest": 0, "fork-mixed": 1, "contest": 2, "act-ripe": 3, "ripe": 4}


def _assistant_text_after(path, since):
    """Concatenated text of the assistant reply that follows `since` (epoch):
    every assistant text block up to the next owner text entry."""
    if not path:
        return None
    out, started = [], False
    try:
        for e in store.jsonl_iter(path):
            ts = store.parse_iso(e.get("timestamp"))
            if ts is None or ts < since:
                continue
            if e.get("type") == "assistant":
                started = True
                out.extend(ups._assistant_texts(e))
            elif started and why_owner_turn.entry_text(e) is not None:
                break
    except OSError:
        return None
    return "\n".join(out) if started else None


def mark_lifecycle(ctx, asks):
    """Caller holds store.locked()."""
    now, rows = ctx.now, []
    for a in asks.values():
        st = a.get("state")
        at = a.get("sent_at")
        if st == "awaiting":
            # AMEND-2 2.7: "another reason" with no typed reason (FR28)
            aw = a.get("awaiting_at")
            if aw is not None and now - aw >= IGNORE_AFTER:
                rows.append({"ask": a["ask"], "ev": "ignored", "at": store.iso_at(now)})
            continue
        if at is None or st not in ("create", "shown"):
            continue
        if st == "create" and a.get("channel") == "inline":
            # printed by the verb; did the reply carry it? (E14, grill fix 3)
            if now - at < NOT_SHOWN_SETTLE:
                continue
            text = _assistant_text_after(why_owner_turn.session_transcript(a.get("session_id")), at)
            if text is not None and ups.ask_carried(text, a):
                rows.append({"ask": a["ask"], "ev": "shown", "at": store.iso_at(at),
                             "verified_at": store.iso_at(now), "by": "derive"})
                if now - at >= IGNORE_AFTER:
                    rows.append({"ask": a["ask"], "ev": "ignored", "at": store.iso_at(now)})
            elif text is not None or now - at >= IGNORE_AFTER:
                rows.append({"ask": a["ask"], "ev": "not_shown", "at": store.iso_at(now)})
            continue
        if now - at >= IGNORE_AFTER:
            rows.append({"ask": a["ask"], "ev": "ignored", "at": store.iso_at(now)})
    for r in rows:
        store.append_row_unlocked(store.ASKS, r)
    return rows


def repair_born(ctx):
    """AMEND-2 2.3 (F16, FR18b): a born node whose probe picked its slot but
    that has no confirm (a crash between the U3 propose and its confirm) is
    confirmed once; if that confirm refuses, it is retired `born-unbacked`
    (actor derive; derive --audit reports it).  The tap's turn is resolved
    off the hot path (audit mode: the turn must be on the transcript's main
    chain by now).  Caller holds no lock.  -> [(why, outcome)]."""
    out = []
    evs = store.read_rows(store.EVENTS)
    known = N.fold(evs)
    backed = set()
    for e in evs:
        if e.get("ev") == "confirm":
            backed.add(e.get("why"))
        if e.get("ev") == "cowhy":
            backed.update((e.get("why"), (e.get("data") or {}).get("other")))
    todo = [w for w, n in sorted(known.items())
            if (n.get("born") or {}).get("slot") is not None and w not in backed
            and n["status"] == "hypothesis"]
    if not todo:
        return out
    probes = {p["id"]: p for p in store.read_rows(store.PROBES) if "id" in p and "verbatim" in p}
    with store.locked():
        for wid in todo:
            probe = probes.get(known[wid]["born"].get("probe")) or {}
            src = probe.get("source") or {}
            ref = why_owner_turn.resolve_prompt(probe.get("verbatim"), src.get("session_id"),
                                                audit=True)
            try:
                if isinstance(ref, why_owner_turn.Refusal):
                    raise WhyRefusal(ref.reason)
                events.confirm_tap_unlocked(wid, probe.get("id"), ref=ref)
                out.append((wid, "confirmed"))
            except WhyRefusal as r:
                events.retire_born_unbacked(wid, r.reason)
                out.append((wid, "born-unbacked"))
    return out


def support_counter(probes):
    """LOCKED 2.6 support, renamed words_support by AMEND-1 1.3: a pick on a
    reveal menu is an act (A1), never words (F3), so trigger=reveal probes
    and interpretations of them are excluded.  The counter is unchanged."""
    sup, cnt = {}, {}
    by_id = {p["id"]: p for p in probes if "id" in p}
    reveal = {i for i, p in by_id.items() if p.get("trigger") == "reveal"}
    for p in probes:
        if "id" in p and p.get("verdict") in WEIGHTS and p.get("id") not in reveal:
            by_slot = {s["slot"]: s for s in p.get("shown") or []}
            for n in p.get("slots") or []:
                slot = by_slot.get(n) or {}
                # rev 3: a "both" pick is words for each of the two
                for wid in ([slot["why"]] if slot.get("why") else slot.get("both") or []):
                    sup[wid] = sup.get(wid, 0.0) + WEIGHTS[p["verdict"]]
        elif "interp" in p and p.get("why"):
            v = p.get("verdict")
            if v in WEIGHTS and p.get("interp") not in reveal:
                sup[p["why"]] = sup.get(p["why"], 0.0) + WEIGHTS[v]
            elif v in ("reject", "reject-all"):
                cnt[p["why"]] = cnt.get(p["why"], 0) + 1
    return sup, cnt, by_id


def contests(ctx, known, probes, evs):
    """Caller holds store.locked()."""
    done = {(e.get("data") or {}).get("interp") for e in evs if e.get("ev") == "contest"}
    out = []
    for p in probes:
        if "interp" not in p or p["interp"] in done:
            continue
        wid = p.get("why")
        node = known.get(wid)
        if not node or node["status"] not in N.LIVE_CONFIRMED:
            continue
        global_reject = p.get("verdict") in ("reject", "reject-all") and p.get("scope") == "global"
        free_new = p.get("verdict") == "free-new"
        if not (global_reject or free_new):
            continue
        rev = {"v": len(node["revisions"]) + 2, "at": store.iso_at(ctx.now),
               "text_before": node["text"], "text_after": node["text"], "by_probe": p["interp"]}
        events.contest(wid, p["interp"], rev)
        node["status"] = "contested"
        node["revisions"].append(rev)
        done.add(p["interp"])
        out.append(wid)
    return out


def morning_order(asks):
    # rev 3: the readings bubble is the retro slot's own, never pushed alone
    q = [a for a in asks.values() if a.get("state") == "queued"
         and a.get("trigger") != "readings"]
    return sorted(q, key=lambda a: (PRIORITY.get(a.get("trigger"), len(PRIORITY)),
                                    a.get("queued_at") or a.get("created_at") or 0, a["ask"]))


def last_words_at(evs, wid):
    """`at` (epoch) of the node's last non-split words event (a confirm)."""
    at = 0
    for e in evs:
        if e.get("ev") == "confirm" and e.get("why") == wid:
            at = store.parse_iso(e.get("at")) or 0
    return at


def run(ctx, out=None):
    if not ctx.enabled():
        return 0  # final grill B1: off writes no derived/ (ups makes the dir)
    store._makedirs(ctx.ddir)
    from . import retro
    retro.migrate_view_only(ctx)   # AMEND-2 P3.4 one-shot (F23, FR24)
    # AMEND-2 P3.4: the view-only retro renders without settling asks, so
    # the covered / read queued asks are dropped here (final grill B5).
    retro.drop_covered(ctx)
    with store.locked():
        asks = budget.fold_asks(store.read_rows(store.ASKS))
        mark_lifecycle(ctx, asks)
    repair_born(ctx)
    probes = store.read_rows(store.PROBES)
    res = acts_mod.act_pass(ctx, lambda evs, known: contests(ctx, known, probes, evs))
    # FDGN-594: timers derive facts and projections, never owner questions.
    evs = store.read_rows(store.EVENTS)
    known = events.N.fold(evs)
    sup, cnt, _ = support_counter(probes)
    acts = acts_mod.act_rows()
    for wid, n in known.items():
        n["support"], n["counter"] = sup.get(wid, 0.0), cnt.get(wid, 0)
        n["words_support"] = n["support"]
        n["flags"] = acts_mod.flags(ctx, n, acts, evs)
    b = budget.tune(ctx)
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    rate, n = budget.answer_rate(asks, ctx.now)
    states = {}
    for a in asks.values():
        states[a.get("state")] = states.get(a.get("state"), 0) + 1
    store.write_json_atomic(store.derived_path("nodes.json"), known)
    store.write_json_atomic(store.derived_path("queue.json"),
                            [])  # retired cold-question queue
    store.write_json_atomic(store.derived_path("metrics.json"),
                            {"answer_rate_7d": rate, "answer_n_7d": n, "ask_states": states,
                             "inline_cap": b.get("inline_cap"), "at": store.iso_at(ctx.now)})
    from . import metacog
    metacog.write(ctx)   # AMEND-2 / MD 8: derived/metacog.json
    return 0
