"""Build the weekly-picture renderer feed from the instance's audit records.

The adapter is deliberately conservative: relationships are made only from a
literal Why or ticket id.  In particular, prose is never treated as a join.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
from pathlib import Path

from conf_reader import conf_get
from why_net import nodes as why_nodes

KST = dt.timezone(dt.timedelta(hours=9))
WHY_RE = re.compile(r"\bwhy-[0-9]{4,}\b", re.I)
TICKET_RE = re.compile(r"\bDGN-[0-9]+(?:-[A-Z0-9-]+)?\b", re.I)
DATE_RE = re.compile(r"\b(20[0-9]{2}-[01][0-9]-[0-3][0-9])\b")
DONE_RE = re.compile(r"\b(done|completed|closed|landed|\uc644\ub8cc)\b", re.I)

LABELS = {
    "en": {"day": "Day {d}", "since": "since day {d}", "tree": "Why so far",
           "new": "new", "merged": "joined {m}",
           "kinds": {"done": "Done", "decision": "Decision"},
           "used": "used on {n} days", "closing": ""},
    "ko": {"day": "{d}\uc77c\ucc28", "since": "{d}\uc77c\ucc28\ubd80\ud130",
           "tree": "\uc9c0\uae08\uae4c\uc9c0\uc758 Why", "new": "\uc0c8\ub85c",
           "merged": "{m}\uc5d0 \ud569\ub958",
           "kinds": {"done": "\ub05d\ub0b8 \uc77c", "decision": "\uacb0\uc815"},
           "used": "{n}\uc77c \uc0ac\uc6a9", "closing": ""},
}


def _locale(root):
    conf = root / "config/agent.conf"
    value = (os.environ.get("DOGANY_LANG", "").strip()
             or conf_get("AGENT_LANG", conf) or conf_get("LANG", conf) or "en")
    return "ko" if re.split(r"[-_.]", value.lower(), 1)[0] == "ko" else "en"


def _short_label(text, width=38):
    """Keep only recorded words, wrapped into at most two short lines."""
    words = str(text or "").split()
    lines, pos = [], 0
    for line in range(2):
        part = []
        while pos < len(words):
            candidate = " ".join(part + [words[pos]])
            size = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
                       for c in candidate)
            reserve = int(line == 1 and pos + 1 < len(words))
            if size + reserve > width:
                break
            part.append(words[pos])
            pos += 1
        if not part:
            lines.append("\u2026")
            break
        lines.append(" ".join(part) + ("\u2026" if line == 1 and pos < len(words) else ""))
        if pos == len(words):
            break
    return "\n".join(lines) if words else ""


def _why_label(node, probes):
    quote = (node.get("confirm") or {}).get("quote")
    # Owner-worded nodes already carry their verbatim wording in text.
    text = node.get("text", "")
    # owner_words holds probe ids (or seed:<decision>), never generated prose.
    # Resolve read-only here without importing the Unix-only store writers.
    for key in reversed(node.get("owner_words") or []):
        words = text if key.startswith("seed:") else probes.get(key)
        if words:
            text = words
            break
    text = quote or text
    return _short_label(text)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _read_events(root: Path, name="events.jsonl"):
    path = root / ".telegram_bot/state/why-net" / name
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(dt.timezone.utc)


def _business_date(value):
    parsed = _instant(value)
    if parsed is None:
        return None
    # A Dogany day changes at 05:00 KST, not at civil midnight.
    return (parsed.astimezone(KST) - dt.timedelta(hours=5)).date()


def _frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    out = {}
    for line in text[4:end].splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
        if match:
            out[match.group(1).lower()] = match.group(2).strip(" '\"")
    return out, text[end + 4:]


def _literal_ids(value, pattern):
    return list(dict.fromkeys(x.lower() if x.lower().startswith("why-") else x.upper()
                              for x in pattern.findall(value or "")))


def _week(week_ending):
    end = dt.date.fromisoformat(week_ending)
    if end.weekday() != 6:
        raise ValueError("week-ending must be a Sunday")
    start = end - dt.timedelta(days=6)
    return start, end


def _day(date, start, end):
    return (date - start).days + 1 if date is not None and start <= date <= end else None


def _event_date(row):
    return _business_date(row.get("at"))


def _ticket_records(root, event_links):
    records = {}
    for path in sorted((root / "worklog").glob("*.md")):
        if path.name == "_DECISIONS.md":
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        front, body = _frontmatter(raw)
        ticket = (front.get("id") or next(iter(TICKET_RE.findall(path.stem)), "")).upper()
        if not ticket:
            continue
        explicit = " ".join(front.get(k, "") for k in ("why_id", "why", "restores"))
        whys = _literal_ids(explicit, WHY_RE) or list(event_links.get(ticket, ()))
        dates = []
        created = next(iter(DATE_RE.findall(front.get("created", ""))), None)
        if created:
            dates.append((dt.date.fromisoformat(created), "created"))
        for line in body.splitlines():
            found = DATE_RE.search(line)
            if found and DONE_RE.search(line):
                dates.append((dt.date.fromisoformat(found.group(1)), "done"))
        records[ticket] = {"id": ticket, "title": front.get("title") or path.stem,
                           "whys": whys, "dates": dates}
    return records


def _decision_records(root):
    try:
        text = (root / "worklog/_DECISIONS.md").read_text(encoding="utf-8")
    except OSError:
        return []
    headings = list(re.finditer(r"(?m)^#{1,6}\s+(.+)$", text))
    out = []
    for index, heading in enumerate(headings):
        title = heading.group(1).strip()
        block = text[heading.start():headings[index + 1].start() if index + 1 < len(headings) else len(text)]
        if "\ud655\uc815" not in title and "\ud655\uc815" not in block:
            continue
        date = next(iter(DATE_RE.findall(title)), None)
        if not date:
            date = next(iter(DATE_RE.findall(block)), None)
        out.append({"title": title, "date": dt.date.fromisoformat(date) if date else None,
                    "whys": _literal_ids(block, WHY_RE),
                    "tickets": _literal_ids(block, TICKET_RE)})
    return out


def _run_records(root):
    seen, out = set(), []
    base = root / ".telegram_bot/dispatch"
    for path in sorted(base.glob("*/status.json")):
        row = _read_json(path)
        if not isinstance(row, dict):
            continue
        run_id = str(row.get("id") or row.get("dispatch_id") or path.parent.name)
        if run_id in seen:
            continue
        seen.add(run_id)
        ticket = str(row.get("ticket") or "").upper()
        when = None
        for key in ("completed_at", "finished_at", "updated_at", "started_at", "created_at", "at"):
            when = _business_date(row.get(key))
            if when:
                break
        if when is None:
            match = re.search(r"(20\d{2})(\d{2})(\d{2})", path.parent.name)
            if match:
                when = dt.date(*(int(x) for x in match.groups()))
        out.append({"id": run_id, "ticket": ticket, "date": when})
    return out


def build_feed(root, week_ending):
    """Return ``(renderer_document, counts)`` for one Monday--Sunday week."""
    root, (start, end) = Path(root), _week(week_ending)
    all_events = _read_events(root)
    cutoff = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(5), KST).astimezone(dt.timezone.utc)
    events = [row for row in all_events if (_instant(row.get("at")) or cutoff) < cutoff]
    folded = why_nodes.fold(events)
    probes = {row["id"]: row.get("verbatim") for row in _read_events(root, "probes.jsonl")
              if row.get("id")}
    live = {wid: node for wid, node in folded.items()
            if node.get("status") not in ("retired", "pivoted")}

    event_links = {}
    for row in events:
        work = (row.get("data") or {}).get("work")
        for ticket in _literal_ids(str(work or ""), TICKET_RE):
            event_links.setdefault(ticket, []).append(str(row.get("why", "")).lower())
    tickets = _ticket_records(root, event_links)
    decisions, runs = _decision_records(root), _run_records(root)

    strands = []
    for index, (wid, node) in enumerate(sorted(live.items())):
        # Parents remain explicit and cycle-safe; the renderer can draw a
        # partial lineage even if a hand-edited ledger contains a cycle.
        parents = [p for p in node.get("parents", ()) if p in live and p != wid]
        strands.append({"id": wid, "label": _why_label(node, probes), "start": 1,
                        "rho": 0.55, "deg": (index * 47) % 360, "shade": index % 3,
                        "mark": index + 1,
                        "state": node.get("status"), "parents": parents})

    days, hows, unlinked = {}, [], 0
    how_ids, how_days, how_labels = set(), {}, {}
    what_count = 0

    def add_touch(day, wid, item):
        nonlocal what_count
        if day is None or wid not in live:
            return
        bucket = days.setdefault(str(day), [])
        touch = next((x for x in bucket if x["id"] == wid), None)
        if touch is None:
            touch = {"id": wid, "state": live[wid].get("status", "hypothesis"), "items": []}
            bucket.append(touch)
        if len(touch["items"]) < 9:
            touch["items"].append(item)
        else:
            touch["overflow"] = touch.get("overflow", 0) + 1
        what_count += 1

    how_counts = {}

    def add_how(base_id, wid, day, label, linked, **extra):
        if how_counts.get(wid, 0) >= 8:
            strand = next(x for x in strands if x["id"] == wid)
            strand["hows_overflow"] = strand.get("hows_overflow", 0) + 1
            return None
        # The renderer indexes Hows globally, even for multi-Why tickets.
        hid = base_id if len(linked) == 1 else base_id + ":" + wid
        while hid in how_ids:
            hid += ":" + wid
        how_ids.add(hid)
        hows.append({"id": hid, "why": wid, "day": day, "label": label, **extra})
        how_counts[wid] = how_counts.get(wid, 0) + 1
        return hid

    for ticket in tickets.values():
        weekly = [(date, kind) for date, kind in ticket["dates"] if _day(date, start, end)]
        if not weekly:
            continue
        valid = list(dict.fromkeys(wid for wid in ticket["whys"] if wid in live))
        if not valid:
            unlinked += 1
            continue
        used_days = {_day(date, start, end) for date, kind in weekly if kind == "done"}
        if used_days:
            how_days[ticket["id"]] = used_days
            how_labels[ticket["id"]] = ticket["title"]
        for wid in valid:
            hid = add_how(ticket["id"], wid, _day(min(date for date, _ in weekly), start, end),
                          ticket["title"], valid)
            for date, kind in weekly:
                if kind == "done":
                    item = {"kind": "done", "text": ticket["title"]}
                    if hid is not None:
                        item["how"] = hid
                    add_touch(_day(date, start, end), wid, item)

    for decision in decisions:
        day = _day(decision["date"], start, end)
        if day is None:
            continue
        linked = [wid for wid in decision["whys"] if wid in live]
        if not linked:
            for ticket in decision["tickets"]:
                linked.extend(wid for wid in tickets.get(ticket, {}).get("whys", ()) if wid in live)
        linked = list(dict.fromkeys(linked))
        if not linked:
            unlinked += 1
        for wid in linked:
            add_touch(day, wid, {"kind": "decision", "text": decision["title"]})

    for run in runs:
        day = _day(run["date"], start, end)
        if day is None:
            continue
        linked = list(dict.fromkeys(wid for wid in tickets.get(run["ticket"], {}).get("whys", ())
                                   if wid in live))
        if not linked:
            unlinked += 1
            continue
        for wid in linked:
            add_how(run["id"], wid, day, run["id"], linked, ticket=run["ticket"])

    lang = _locale(root)
    labels = dict(LABELS[lang], kinds=dict(LABELS[lang]["kinds"]))
    if how_days:
        winner = min(how_days, key=lambda hid: (-len(how_days[hid]), hid))
        label = _short_label(how_labels[winner]).replace("\n", " ")
        count = len(how_days[winner])
        template = ("\uac00\uc7a5 \uc790\uc8fc \uc4f4 How: {label} ({n}\uc77c)" if lang == "ko"
                    else "Most used How: {label} ({n} days)")
        labels["closing"] = template.format(label=label, n=count)
    doc = {"day": 7, "history": [],
           "labels": labels,
           "strands": {"source": "weekly-picture-feed", "ref_day": 7,
                       "list": strands, "days": days, "hows": hows}}
    return doc, {"whys": len(strands), "hows": len(hows),
                 "whats": what_count, "unlinked": unlinked}


def write_feed(root, week_ending, out):
    doc, counts = build_feed(root, week_ending)
    Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return counts
