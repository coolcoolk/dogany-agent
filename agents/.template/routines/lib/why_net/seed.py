"""why_net.seed -- `why-net seed --from <decision ledger>` (spec 3, Q5).

One shot, on an empty events.jsonl only (R1l).  Imports the decision-ledger
entries that carry a quoted owner line + time as confirmed Whys:
worded_by=owner, source.kind=ledger-seed, owner_words=["seed:<dec id>"].

Ledger shape read: the DGN-1952 machine entry (routines/decision-ledger.py),
whose heading ends in the confirmed tag
    DECISION_LEDGER_CONFIRMED_TAG    default  [{status}: "{answer}"]
    DECISION_LEDGER_CONFIRMED_STATUS default  confirmed {date} {time}
The formats are read from the same agent.conf keys and defaults the writer
uses, so a reformatted instance ledger still parses.

AMEND-1 (F16): a seed lands `confirmed-provisional`, never `confirmed`.
The seed's dec is the words source (source.dec) and is never an act; the
words anchor is the newest turn-ledger id at seed time, so only later acts
count (I2).

"Owner line" is taken literally (2.1: worded_by=owner means the owner's own
bytes).  An answer that only picks a menu option (decision_ledger_fmt.pick:
a bare number, "N. <that label>", a number handle, or one of the entry's
option labels) is the agent's wording, not the owner's: it is skipped and
counted (`skipped_pick`), never imported as worded_by=owner.  Words that
merely open with a number ("1.5 hours is too long") are the owner's
(DGN-1967 bug 15).  An answer the writer clipped (ANSWER_MAX) is not
byte-exact and is skipped too.  The ledger stamps the instance's local
time (lib/tz_offset.sh); said_at carries that offset (bug 20).

Audit (events.audit_confirms -> seed_backed): a seed confirm is backed only
when (a) every event before it in events.jsonl was written by actor seed
(seed refuses on a non-empty store, so a real seed run is the file's
leading run) and (b) the ledger it names still holds that entry with the
same quote.  A later hand-appended seed confirm fails (a).

English/ASCII only.  Stdlib only.
"""

import datetime
import os
import re
import sys

from . import WhyRefusal, store

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import decision_ledger_fmt as fmt  # noqa: E402

SCOPE = "framework"


def _template_rx(template, groups):
    """A str.format template -> regex; `groups` maps a field name to its
    sub-pattern, every other character is literal."""
    out, pos = [], 0
    for m in re.finditer(r"\{(\w+)\}", template):
        out.append(re.escape(template[pos:m.start()]))
        out.append(groups.get(m.group(1), ".*?"))
        pos = m.end()
    out.append(re.escape(template[pos:]))
    return "".join(out)


def formats(ctx):
    """-> (id_rx, tag_rx, answer_max) for this instance's ledger."""
    def conf(key):
        return ctx.conf(key) or fmt.DEFAULTS.get(key, "")
    status_rx = _template_rx(conf("DECISION_LEDGER_CONFIRMED_STATUS"), {
        "date": r"(?P<date>\d{4}-\d{2}-\d{2})",
        "time": r"(?P<time>\d{2}:\d{2})"})
    tag_rx = _template_rx(conf("DECISION_LEDGER_CONFIRMED_TAG"), {
        "status": status_rx, "answer": r"(?P<answer>.*)",
        "date": r"\d{4}-\d{2}-\d{2}", "time": r"\d{2}:\d{2}"})
    return (fmt.id_regex(conf("DECISION_LEDGER_ID_FORMAT")),
            re.compile(tag_rx + r"\s*$"), fmt.ANSWER_MAX)


def _said_at(date, time_, zone):
    """The ledger's local stamp in the instance zone (host when unresolved)."""
    dt = datetime.datetime.strptime("%s %s" % (date, time_), "%Y-%m-%d %H:%M")
    dt = dt.replace(tzinfo=zone) if zone else dt.astimezone()
    return dt.isoformat(timespec="seconds")


def parse(ctx, path):
    """-> [entry], one per confirmed ledger entry in file order:
    {"dec", "quote", "said_at", "labels", "kind": "owner"|"pick"|"clipped"}."""
    id_rx, tag_rx, answer_max = formats(ctx)
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    zone = fmt.tz(ctx.root)
    out, cur = [], None
    for line in lines:
        if line.startswith("#"):
            cur = None
            m = tag_rx.search(line)
            dm = id_rx.search(line)
            if m and dm:
                cur = {"dec": dm.group(0), "quote": m.group("answer"),
                       "said_at": _said_at(m.group("date"), m.group("time"),
                                           zone),
                       "labels": []}
                out.append(cur)
            continue
        labels = fmt.options_of_line(line)
        if cur is not None and labels is not None:
            cur["labels"].extend(labels)
    for e in out:
        q = e["quote"]
        if not q.strip() or fmt.pick_shaped(q, e["labels"]):
            e["kind"] = "pick"
        elif len(q) >= answer_max:
            e["kind"] = "clipped"
        else:
            e["kind"] = "owner"
    return out


def run(ctx, src):
    """Write the seed events.  -> {"imported", "skipped_pick",
    "skipped_clipped", "entries"}.  Refuses (WhyRefusal) on a non-empty
    store (R1l) or an unreadable ledger."""
    path = src if os.path.isabs(src) else os.path.join(ctx.root, src)
    if not os.path.isfile(path):
        raise WhyRefusal("seed-source-missing")
    entries = parse(ctx, path)
    rel = os.path.relpath(path, ctx.root)
    stats = {"entries": len(entries), "imported": 0, "skipped_pick": 0,
             "skipped_clipped": 0}

    ledger = store.read_rows(store.TURN_LEDGER)
    anchor = ledger[-1].get("id") if ledger else "otl-000000"

    def build(rows):
        if rows:
            raise WhyRefusal("events-not-empty")
        new = []
        n_evt = 0
        n_why = 0
        for e in entries:
            if e["kind"] != "owner":
                stats["skipped_" + e["kind"]] += 1
                continue
            n_why += 1
            wid = "why-%04d" % n_why
            source = {"kind": "ledger-seed", "runtime": "claude-code",
                      "session_id": None, "message_uuid": None,
                      "dec": e["dec"], "ledger": rel}
            now = store.now_iso()
            n_evt += 1
            new.append({"ev": "propose", "id": "evt-%06d" % n_evt, "why": wid,
                        "at": now, "actor": "seed", "data": {
                            "text": e["quote"], "worded_by": "owner",
                            "status": "hypothesis", "scope": SCOPE,
                            "parents": [], "work": None,
                            "said_at": e["said_at"],
                            "owner_words": ["seed:" + e["dec"]],
                            "source": source}})
            n_evt += 1
            new.append({"ev": "confirm", "id": "evt-%06d" % n_evt, "why": wid,
                        "at": now, "actor": "seed", "data": {
                            "quote": e["quote"], "said_at": e["said_at"],
                            "worded_by": "owner", "probe": None,
                            "otl": anchor, "source": source}})
            stats["imported"] += 1
        return new

    store.append_many(store.EVENTS, build)
    return stats


def backed(ctx, events, index):
    """Audit rule for the seed confirm at events[index] -> None (backed) or
    the reason it is not."""
    ev = events[index]
    if ev.get("actor") != "seed":
        return "seed-unbacked"
    if any(e.get("actor") != "seed" for e in events[:index]):
        return "seed-unbacked"  # not part of the leading seed run
    d = ev.get("data") or {}
    src = d.get("source") or {}
    if src.get("kind") != "ledger-seed" or not src.get("ledger"):
        return "seed-unbacked"
    path = src["ledger"]
    path = path if os.path.isabs(path) else os.path.join(ctx.root, path)
    try:
        entries = parse(ctx, path)
    except (OSError, ValueError):
        return "seed-source-missing"
    for e in entries:
        if e["dec"] == src.get("dec") and e["kind"] == "owner" \
                and e["quote"] == d.get("quote"):
            return None
    return "seed-not-in-ledger"
