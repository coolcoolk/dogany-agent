"""View-only Why retro projection and historical reading/tap compatibility.

FDGN-594 removes readings-bubble and correction-follow-up creation. Rendering
never solicits an answer. Historical rows, probes and idempotent view-only
migration remain readable; no owner-cleared reason is rewritten.
"""

import json
import os

from . import WhyRefusal, budget, events, store

PROVIDER = "why-net"
VERSION = 1
READINGS_MAX = 1           # signals in one readings bubble (final grill B3)
RETRO_CAP = 3              # DGN-1961: items per retro day; conf may lower it, never raise
SIGNAL_LABEL_MAX = 40      # chars of the owner's words quoted in a stem / line
WRONG_LABEL_MAX = 31.0     # button width (ask.BUTTON_LABEL_MAX_WIDTH)


def _signals():
    """signal id -> {"text", "work", "otl"} for every owner record that can
    carry a reading."""
    from . import surfaces
    out = {}
    for c in surfaces.fold_corrections():
        if c.get("id") and isinstance(c.get("text"), str):
            out[c["id"]] = {"text": c["text"], "work": c.get("work"), "otl": c.get("otl"),
                            "at": c.get("said_at")}
    for p in store.read_rows(store.PROBES):
        if p.get("id") and isinstance(p.get("verbatim"), str) \
                and p.get("trigger") != "reveal" and p.get("verdict") == "free":
            out[p["id"]] = {"text": p["verbatim"], "work": p.get("work_ref"),
                            "otl": p.get("otl"), "at": p.get("at")}
    # A rejected confirm already names the reading being corrected. Reuse
    # that sentence when no original signal was attached to the ask.
    for a in budget.fold_asks(store.read_rows(store.ASKS)).values():
        texts = a.get("cand_texts") or []
        if a.get("shape") == "confirm" and len(texts) == 1 and any(
                s.get("wrong") for s in a.get("shown") or []):
            out[a["ask"]] = {"text": texts[0], "work": a.get("work_ref"),
                             "at": a.get("issued_ts")}
    return out


def signal_label(sig):
    """The owner's words of a signal, capped for a stem or a line."""
    s = (_signals().get(sig) or {}).get("text") or ""
    s = " ".join(s.split())
    return s if len(s) <= SIGNAL_LABEL_MAX else s[:SIGNAL_LABEL_MAX - 1] + "\u2026"


def _rows():
    return [r for r in store.read_rows(store.PROBES) if "reading" in r or "reading_wrong" in r]


def current(rows=None):
    """signal -> its latest reading row, unless a wrong tap came after it."""
    out = {}
    for r in _rows() if rows is None else rows:
        if "reading" in r:
            out[r["reading"]] = r
        elif r.get("reading_wrong") in out:
            out.pop(r["reading_wrong"])
    return out


def reading_works(wid):
    """Distinct work items of the signals currently read as `wid` (the
    umbrella rule counts them)."""
    works = set()
    for r in current().values():
        w = budget.canonical_topic(r.get("work") or "")
        if wid in (r.get("whys") or ()) and w and w.startswith("DGN-"):
            works.add(w)
    return works


def read(ctx, signal, whys):
    """`why-net read --signal S --why W [--why W2]`: the agent's reading of
    one signal.  One row per signal (a later one replaces it); the Whys must
    be live.  Writes nothing else and confirms nothing."""
    whys = list(dict.fromkeys(whys or []))
    if not whys:
        raise ValueError("no-why")
    with store.locked():
        sig = _signals().get(signal)
        if sig is None:
            raise WhyRefusal("signal-unknown")
        known = events.nodes()
        for w in whys:
            if w not in known or known[w]["status"] in ("pivoted", "retired"):
                raise WhyRefusal("why-not-live")
        row = {"reading": signal, "whys": whys,
               "work": budget.canonical_topic(sig.get("work") or "") or None,
               "at": store.iso_at(ctx.now), "by": "agent"}
        return store.append_unlocked(store.PROBES, lambda rows: row)


def _shown_signals(asks):
    """Signals already handed out: on a readings bubble, or given back on
    the settled line (DGN-1961)."""
    done = set()
    for a in asks.values():
        if a.get("trigger") == "readings":
            done.update(s.get("signal") for s in a.get("shown") or [])
    done.update(r["reading_settled"] for r in store.read_rows(store.PROBES)
                if r.get("reading_settled"))
    return done


def _unshown_readings(asks, known):
    """Readings not handed out yet, newest first (DGN-1961: the newest is
    the least settled)."""
    done = _shown_signals(asks)
    cur = [r for s, r in current().items() if s not in done
           and all(w in known for w in r.get("whys") or [])]
    cur.sort(key=lambda r: (r.get("at") or "", r["reading"]), reverse=True)
    return cur


def cap(ctx):
    """Items one retro message hands out: WHY_RETRO_MAX, never above 3 a
    day.  DGN-1961 weekly cadence: the one weekly message carries the
    week's 7 daily budgets, so the week's items are walked, not settled."""
    import retro_slot
    return max(0, min(ctx.conf_int("WHY_RETRO_MAX"), RETRO_CAP)) * retro_slot.span_days(ctx)


def settle_rest(ctx, keep):
    """DGN-1961: the read signals past tonight's budget are given back in
    one settled line instead of being asked.  Keeps the `keep` newest
    unshown readings for the later turns; every older one gets a
    `reading_settled` row (the agent's reading stands; nothing is confirmed,
    the same as an untapped line).  -> how many were settled."""
    keep = max(0, keep)
    with store.locked():
        asks = budget.fold_asks(store.read_rows(store.ASKS))
        rest = _unshown_readings(asks, events.nodes())[keep:]
        for r in rest:
            store.append_row_unlocked(store.PROBES, {
                "reading_settled": r["reading"], "whys": list(r.get("whys") or []),
                "at": store.iso_at(ctx.now)})
    return len(rest)


def readings_bubble(ctx, known=None):
    """Retired: reports are views and cannot manufacture a question."""
    return None


def render_readings(ctx, shown, known=None):
    """Head line (it invites free text), then the signal's line: its words
    -> the agent's reading.  One signal per bubble (final grill B3)."""
    from . import ask
    known = events.nodes() if known is None else known
    lines = [ask.t(ctx, "why.retro.head")]
    fmt = ask.t(ctx, "why.retro.line")
    for s in shown:
        reading = " \u00b7 ".join(known.get(w, {}).get("text", w) for w in s.get("reading") or [])
        lines.append(fmt.replace("{n}", str(s["slot"])).replace(
            "{signal}", signal_label(s.get("signal"))).replace("{reading}", reading)
            + cite(ctx, s.get("signal")))
    return "\n".join(lines)


def cite(ctx, sig):
    """DGN-1961: a recall prompt names the event the agent already knows
    (when, on which work item), never "what do you remember".  -> " (..)"
    or ""."""
    from . import ask
    rec = _signals().get(sig) or {}
    at = store.parse_iso(rec.get("at"))
    if at is None:
        return ""
    import datetime
    d = datetime.datetime.utcfromtimestamp(at + budget.KST).date()
    names = [n.strip() for n in (ask.t(ctx, "retro.slot.weekdays") or "").split(",")]
    if len(names) != 7 or not all(names):
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    date = "%s(%s)" % (d.isoformat(), names[d.weekday()])
    work = budget.canonical_topic(rec.get("work") or "")
    key = "why.retro.cite.work" if work else "why.retro.cite"
    return " " + ask.t(ctx, key).replace("{date}", date).replace("{work}", work or "")


def candidates(ctx, signal, wrong, known=None):
    """Up to READING_MAX_CANDS concrete Whys for a signal whose reading was
    wrong: live Whys of the signal's work item first, then other live
    non-umbrella Whys the agent proposed for nothing else.  Never the wrong
    reading, never an umbrella."""
    from . import ask
    known = events.nodes() if known is None else known
    sig = _signals().get(signal) or {}
    work = budget.canonical_topic(sig.get("work") or "")
    pool = ask.work_whys(ctx, work, known) if work else []
    out = []
    for w in pool:
        if w in wrong or w in out or ask.is_umbrella(ctx, w, known):
            continue
        out.append(w)
    return out[:ask.READING_MAX_CANDS]


def on_wrong(ctx, signal, wrong, probe_id):
    """Retain a historical correction tap as evidence without a follow-up."""
    store.append_row(store.PROBES, {"reading_wrong": signal, "whys": list(wrong or []),
                                    "probe": probe_id, "at": store.iso_at(ctx.now)})
    return None


def on_reading_answer(ctx, a, whys):
    """A pick on a follow-up: the signal's reading becomes the picked Whys
    (several allowed).  The pick itself is words (ups confirms as usual)."""
    sig = a.get("signal")
    if not sig or not whys:
        return None
    try:
        return read(ctx, sig, whys)
    except (WhyRefusal, ValueError):
        return None


# -- the provider interface (the retro slot, DGN-1961) ------------------------

# Covered: a queued Why ask whose every candidate is no longer open (a gate
# or an answer made it provisional, or it settled) asks nothing new.
_OPEN = ("hypothesis", "contested")


def _ask_whys(a):
    out = []
    for s in a.get("shown") or []:
        for w in ([s["why"]] if s.get("why") else s.get("both") or []):
            if w not in out:
                out.append(w)
    return out


def drop_covered(ctx):
    """Final grill B5 (and the T-corr / readings overlap of B3): drop every
    queued ask that would re-ask what is already answered.
      - a Why ask (not N7-exempt) whose candidates are all provisional,
        settled or ended;
      - a T-corr `correction` ask on a work item whose signals the retro
        already reads one by one (the same signal is never asked twice).
    Each gets `not_shown` with `dropped` = covered | read.  -> [ask ids]."""
    from . import ask
    out = []
    with store.locked():
        known = events.nodes()
        asks = budget.fold_asks(store.read_rows(store.ASKS))
        read_works = {budget.canonical_topic(r.get("work") or "")
                      for r in current().values()} - {None}
        for a in asks.values():
            if a.get("state") != "queued" or a.get("kind") != "why":
                continue
            trig = a.get("trigger")
            if trig in ask.N7_EXEMPT or trig in ask.RETRO_TRIGGERS:
                continue
            why = None
            whys = _ask_whys(a)
            if whys and all((known.get(w) or {}).get("status") not in _OPEN for w in whys):
                why = "covered"
            elif trig == "correction" and budget.canonical_topic(a.get("topic") or "") \
                    in read_works:
                why = "read"
            if why:
                store.append_row_unlocked(store.ASKS, {
                    "ask": a["ask"], "ev": "not_shown", "at": store.iso_at(ctx.now),
                    "dropped": why})
                out.append(a["ask"])
    return out


def with_stop(ctx, shown):
    """The bubble's slots plus the stop button (final grill B3)."""
    base = [s for s in shown or [] if not s.get("stop")]
    return base + [{"slot": len(base) + 1, "why": None,
                    "label": _t(ctx, "why.retro.stop"), "stop": True}]


def _t(ctx, key):
    from . import ask
    return ask.t(ctx, key)


def _retro_rows(ctx):
    """(shown rows of this retro day, stopped today?)."""
    ds = budget.day_start(ctx.now)
    shown, stopped = [], False
    for r in store.read_rows(store.ASKS):
        at = store.parse_iso(r.get("at")) or 0
        if at < ds:
            continue
        if r.get("ev") == "shown" and r.get("via") == "retro":
            shown.append(r)
        if r.get("retro_stop"):
            stopped = True
    return shown, stopped


def _waiting(ctx, asks, shown_rows):
    """The last retro bubble is still open: shown, unanswered, and no owner
    turn has come since it was shown (the slot asks for the next item only
    after the owner answers)."""
    if not shown_rows:
        return False
    last = shown_rows[-1]
    a = asks.get(last["ask"]) or {}
    if a.get("state") != "shown":
        return False
    since = store.parse_iso(last.get("at")) or 0
    for r in store.read_rows(store.TURN_LEDGER):
        if not r.get("skip") and (store.parse_iso(r.get("at")) or 0) > since:
            return False
    return True


def _bubble(ctx, a, text):
    from . import morning
    return {"ask": a["ask"], "text": text, "buttons": json.loads(
        morning.buttons_json(with_stop(ctx, a.get("shown") or [])))}


def section(ctx):
    """AMEND-2 P3.4 (merged design 9): the Why net's retro section is a
    VIEW-ONLY picture of the retro span -- the purposes the owner confirmed,
    then the agent's assumptions, explicitly unconfirmed (frames.jsonl
    `proceed` rows).  Rendering settles no ask, issues no ask, changes no
    candidate support and spends no question budget.  Empty when the net is
    off, in quiet hours, when the span holds nothing (no blank wrapper), and
    while its copy (MD 14 Q4) is still the shipped placeholder.
    -> {"provider", "v", "bubbles": [] (never), "text": str | None}."""
    from . import ask
    out = {"provider": PROVIDER, "v": VERSION, "bubbles": [], "text": None}
    if not ctx.enabled() or budget.in_window(ctx.now, ctx.conf("WHY_QUIET")):
        return out
    if not ask.copy_ready(ctx, ask.COPY_KEYS_RETRO_VIEW):
        return out
    out["text"] = picture(ctx)
    return out


PICTURE_MAX = 5   # lines per part of the picture


def span_start(ctx):
    """Start (epoch) of the retro span: today's retro day, or the week the
    weekly message covers (retro_slot.span_days)."""
    import retro_slot
    return budget.day_start(ctx.now) - (retro_slot.span_days(ctx) - 1) * budget.DAY


def picture_items(ctx, lo, hi=None):
    """(confirmed purposes, unconfirmed assumptions) recorded in [lo, hi):
    live provisional / confirmed Whys whose owner words or settle landed in
    the span, and the `proceed` assumptions of frames in it.  Newest first,
    each text once."""
    hi = ctx.now + 1 if hi is None else hi

    def inside(at):
        t = store.parse_iso(at)
        return t is not None and lo <= t < hi
    evs = store.read_rows(store.EVENTS)
    known = events.N.fold(evs)
    conf, seen = [], set()
    for e in reversed(evs):
        if e.get("ev") not in ("confirm", "cowhy", "settle", "resolve") or not inside(e.get("at")):
            continue
        ids = [e.get("why")]
        if e.get("ev") == "cowhy":
            ids.append((e.get("data") or {}).get("other"))
        for w in ids:
            n = known.get(w)
            if n and n["status"] in events.N.LIVE_CONFIRMED and w not in seen:
                seen.add(w)
                conf.append(n["text"])
    assumed, texts = [], set()
    for r in reversed(store.read_rows(store.FRAMES)):
        a = r.get("assumption")
        if r.get("ev") == "proceed" and a and inside(r.get("at")) and a not in texts:
            texts.add(a)
            assumed.append(a)
    return conf, assumed


def picture(ctx):
    """The picture text, or None when the span holds nothing."""
    from . import ask
    conf, assumed = picture_items(ctx, span_start(ctx))
    if not conf and not assumed:
        return None
    lines = [ask.t(ctx, "why.retro.view.head")]
    fc, fa = ask.t(ctx, "why.retro.view.confirmed"), ask.t(ctx, "why.retro.view.assumed")
    lines += [fc.replace("{text}", x) for x in conf[:PICTURE_MAX]]
    lines += [fa.replace("{text}", x) for x in assumed[:PICTURE_MAX]]
    return "\n".join(lines)


MIGRATION_MARKER = "retro-view-only.done"


def migrate_view_only(ctx):
    """AMEND-2 P3.4 one-shot (F23, FR24): close every open retro ask (a
    readings bubble, a reading follow-up, a bubble the retro sent) as
    `ignored`, reason `retro-view-only`.  Their probes stay as evidence.
    Guarded by a marker file, so it runs once; run by the nightly derive
    (rendering the section never writes).  -> [ask ids] closed."""
    marker = store.path(MIGRATION_MARKER)
    if os.path.exists(marker):
        return []
    out = []
    with store.locked():
        if os.path.exists(marker):
            return []
        asks = budget.fold_asks(store.read_rows(store.ASKS))
        for a in sorted(asks.values(), key=lambda x: x["ask"]):
            if a.get("state") in budget.TERMINAL:
                continue
            if a.get("via") == "retro" or a.get("trigger") in ("reading", "readings"):
                store.append_row_unlocked(store.ASKS, {
                    "ask": a["ask"], "ev": "ignored", "at": store.iso_at(ctx.now),
                    "reason": "retro-view-only"})
                out.append(a["ask"])
        store.atomic_write(marker, store.iso_at(ctx.now) + "\n")
    return out


def shown(ctx, aid, message_id, chat_id, text):
    """The slot sent bubble `aid`: record it like a morning bubble, so a tap
    on it binds (message id + chat id + text sha), with the keyboard as sent
    (`retro_shown`: the slots plus the stop button)."""
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    a = asks.get(aid)
    if a is None or a.get("state") != "queued":
        raise WhyRefusal("not-queued")
    return store.append_row(store.ASKS, {
        "ask": aid, "ev": "shown", "at": store.iso_at(ctx.now), "channel": "morning",
        "via": "retro", "bubble_message_id": message_id, "bubble_chat_id": chat_id,
        "bubble_text_sha": store.bubble_text_sha(text),
        "retro_shown": with_stop(ctx, a.get("shown") or [])})


def stop(ctx, a):
    """The owner tapped (or typed) the stop button on retro bubble `a`: it
    goes back to the queue and nothing more is handed out this retro day.
    No probe, no decline: the topic is not refused."""
    return store.append_row_unlocked(store.ASKS, {
        "ask": a["ask"], "ev": "queued", "at": store.iso_at(ctx.now),
        "retro_stop": True})
