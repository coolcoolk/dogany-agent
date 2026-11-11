#!/usr/bin/env python3
"""filing.py -- basekit filing mechanism (DGN-117 / dec-535).

One owner for files/docs/ and files/INDEX.md. The agent files a document
right after handling it; nobody files by hand and nothing waits for a
batch clean-up.

Contract (dec-535, locked 2026-10-23):
  - files/inbox/   = what the owner sent, unprocessed. Agent output never
                     lands there.
  - files/docs/    = filed documents. Name: "<YYYY-MM-DD> <title>.<ext>".
                     Folders are born lazily, never pre-made.
  - axis SLOT      = optional first-level folder (ASCII slug). A kit fills
                     it via config/filing-axis.conf (one slug per line,
                     optional " = display label"); lifekit ships the life
                     domains. No file -> no axis: flat files/docs/ + tags.
  - files/INDEX.md = manifest, one row per document: date, path, axis,
                     tags, title, sha256, state, group, candidates, since,
                     asked. Search, tag views, precedent lookups and the
                     console all read it.
  - dedup by hash  = a second copy of a known document is moved to
                     files/_archive/dup/<date>/ and the existing row
                     gains its tags.
  - ask-once       = an unclear placement is held (filed flat, state
                     pending, with its candidates) under a group; the agent
                     asks the owner once per group; the answer resolves the
                     whole group and becomes precedent for that group.
  - best-guess     = a pending row 30 days past its ask (or its hold, if
                     never asked) is placed in its first candidate with
                     state auto. Nothing is trashed, nothing rots.
  - sensitive      = files/docs/_private/...; hidden from search and views
                     unless include_private.
Paths are always reported relative to the agent root ("files/docs/...");
never an absolute path in chat.

CLI (JSON on stdout):
  filing.py --base ROOT file SRC --title T --date D [--axis A] [--tag X]... [--private]
  filing.py --base ROOT hold SRC --title T --date D --group G --candidate A [--candidate B]... [--tag X]...
  filing.py --base ROOT questions
  filing.py --base ROOT asked GROUP
  filing.py --base ROOT resolve GROUP AXIS
  filing.py --base ROOT precedent GROUP
  filing.py --base ROOT sweep [--dry-run]
  filing.py --base ROOT search QUERY... [--private]
  filing.py --base ROOT view TAG [--private]
Every write command takes --today YYYY-MM-DD (defaults to the local date).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata

INDEX_REL = "files/INDEX.md"
DOCS_REL = "files/docs"
PRIVATE = "_private"
AXIS_CONF_REL = "config/filing-axis.conf"
BEST_GUESS_DAYS = 30
COLUMNS = ("date", "path", "axis", "tags", "title", "sha256", "state",
           "group", "candidates", "since", "asked")
STATES = ("filed", "pending", "auto")
HEADER = (
    "# files INDEX\n\n"
    "Filing manifest, one row per document under files/docs/. Written only\n"
    "by routines/lib/filing.py (file / hold / resolve / sweep); do not\n"
    "hand-edit. state: filed | pending (asked once, unresolved) | auto\n"
    "(best-guess after %d days).\n\n" % BEST_GUESS_DAYS
)
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (.+)$")


class FilingError(ValueError):
    pass


def _nfc(s):
    return unicodedata.normalize("NFC", s or "")


def _cell(s):
    return _nfc(str(s)).replace("|", "/").replace("\n", " ").strip()


def _today(today):
    return today or dt.date.today().isoformat()


def _check_date(d):
    if not DATE_RE.match(d or ""):
        raise FilingError("date must be YYYY-MM-DD: %r" % (d,))
    dt.date.fromisoformat(d)
    return d


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- axis slot ----------------------------------------------------------

def axis_slot(base):
    """Kit-provided axis values {slug: label}, or None when no kit fills it."""
    p = os.path.join(base, AXIS_CONF_REL)
    if not os.path.isfile(p):
        return None
    slot = {}
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            slug, _, label = line.partition("=")
            slug = slug.strip()
            if SLUG_RE.match(slug):
                slot[slug] = _nfc(label.strip()) or slug
    return slot or None


def _check_axis(base, axis):
    if axis in (None, ""):
        return ""
    if axis_slot(base) is None:
        raise FilingError("no axis slot configured (flat files/docs/): drop --axis, use tags")
    if not SLUG_RE.match(axis):
        # Open set (a kit may grow e.g. "pet"), but folder names stay ASCII.
        raise FilingError("axis must be an ASCII slug: %r" % (axis,))
    return axis


# ---- INDEX --------------------------------------------------------------

def load(base):
    p = os.path.join(base, INDEX_REL)
    rows = []
    if not os.path.isfile(p):
        return rows
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.startswith("| ") or line.startswith("| date ") or line.startswith("|---"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) != len(COLUMNS):
                continue
            rows.append(dict(zip(COLUMNS, cells)))
    return rows


def save(base, rows):
    p = os.path.join(base, INDEX_REL)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["date"], r["path"]))
    out = [HEADER, "| " + " | ".join(COLUMNS) + " |",
           "|" + "|".join("---" for _ in COLUMNS) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(r.get(c, "")) for c in COLUMNS) + " |")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".INDEX.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, p)


def _split(s):
    return [x for x in (s or "").split(",") if x]


def _join(items):
    seen = []
    for x in items:
        x = _cell(x).replace(",", " ").strip()
        if x and x not in seen:
            seen.append(x)
    return ",".join(seen)


def _abs(base, rel):
    return os.path.join(base, rel)


def _rel(base, path):
    return os.path.relpath(path, base).replace(os.sep, "/")


def is_private(row):
    return row["path"].startswith("%s/%s/" % (DOCS_REL, PRIVATE))


# ---- placement ----------------------------------------------------------

def _safe_title(title):
    t = _nfc(title)
    t = re.sub(r"[\x00-\x1f/\\:|]", " ", t)
    t = re.sub(r"\s+", " ", t).strip().strip(".")
    if not t:
        raise FilingError("empty title")
    return t[:120]


def _dest(base, axis, private, date, title, ext):
    parts = [base, DOCS_REL] + ([PRIVATE] if private else []) + ([axis] if axis else [])
    d = os.path.join(*parts)
    stem = "%s %s" % (date, title)
    cand = os.path.join(d, stem + ext)
    n = 2
    while os.path.exists(cand):
        cand = os.path.join(d, "%s (%d)%s" % (stem, n, ext))
        n += 1
    return cand


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)  # lazy folder birth
    shutil.move(src, dst)


def _dedup(base, rows, src, h, tags, today):
    for r in rows:
        if r["sha256"] == h:
            r["tags"] = _join(_split(r["tags"]) + list(tags))
            if os.path.abspath(src) != os.path.abspath(_abs(base, r["path"])):
                dup = os.path.join(base, "files/_archive/dup", today.replace("-", ""),
                                   os.path.basename(src))
                n = 2
                while os.path.exists(dup):
                    root, ext = os.path.splitext(dup)
                    dup = "%s.%d%s" % (root, n, ext)
                    n += 1
                _move(src, dup)
            return r
    return None


def _place(base, src, title, date, axis, tags, private, state, group, candidates, today):
    today = _today(today)
    _check_date(date)
    if not os.path.isfile(src):
        raise FilingError("no such file: %s" % src)
    rows = load(base)
    h = sha256(src)
    hit = _dedup(base, rows, src, h, tags, today)
    if hit is not None:
        save(base, rows)
        return {"path": hit["path"], "dedup": True, "state": hit["state"]}
    title = _safe_title(title)
    ext = os.path.splitext(src)[1].lower()
    dst = _dest(base, axis, private, date, title, ext)
    _move(src, dst)
    row = {"date": date, "path": _rel(base, dst), "axis": axis, "tags": _join(tags),
           "title": title, "sha256": h, "state": state, "group": _cell(group or ""),
           "candidates": _join(candidates or ()), "since": today, "asked": ""}
    rows.append(row)
    save(base, rows)
    return {"path": row["path"], "dedup": False, "state": state}


def file_doc(base, src, title, date, axis=None, tags=(), private=False, group="", today=None):
    """Clear placement: file now, record the row, return the relative path."""
    axis = _check_axis(base, axis)
    return _place(base, src, title, date, axis, tags, private, "filed", group, (), today)


def hold(base, src, title, date, group, candidates, tags=(), private=False, today=None):
    """Unclear placement (2+ equal candidates): precedent first, else hold."""
    if not group:
        raise FilingError("hold needs a group")
    prev = precedent(base, group)
    if prev is not None:
        return file_doc(base, src, title, date, axis=prev, tags=tags,
                        private=private, group=group, today=today)
    cands = [_check_axis(base, c) for c in candidates]
    if len([c for c in cands if c]) < 2:
        raise FilingError("hold needs 2+ candidates; with one, file it")
    return _place(base, src, title, date, "", tags, private, "pending", group, cands, today)


def precedent(base, group):
    """Axis the owner already chose for this group, or None."""
    for r in load(base):
        if group and r["group"] == _cell(group) and r["state"] == "filed" and r["axis"]:
            return r["axis"]
    return None


def questions(base):
    """Pending groups never asked: [{group, candidates, items}] (ask each once)."""
    out = {}
    for r in load(base):
        if r["state"] == "pending" and not r["asked"]:
            g = out.setdefault(r["group"], {"group": r["group"], "candidates": [], "items": []})
            g["candidates"] = _split(_join(g["candidates"] + _split(r["candidates"])))
            g["items"].append(r["path"])
    return sorted(out.values(), key=lambda g: g["group"])


def mark_asked(base, group, today=None):
    rows = load(base)
    n = 0
    for r in rows:
        if r["state"] == "pending" and r["group"] == _cell(group) and not r["asked"]:
            r["asked"] = _today(today)
            n += 1
    save(base, rows)
    return n


def _relocate(base, r, axis, state):
    src = _abs(base, r["path"])
    private = is_private(r)
    name = os.path.basename(src)
    m = NAME_DATE_RE.match(os.path.splitext(name)[0])
    title = m.group(2) if m else r["title"]
    dst = _dest(base, axis, private, r["date"], title, os.path.splitext(name)[1])
    _move(src, dst)
    r.update(path=_rel(base, dst), axis=axis, state=state)


def resolve(base, group, axis, today=None):
    """The owner's answer: place every pending row of the group under axis."""
    axis = _check_axis(base, axis)
    rows = load(base)
    moved = []
    for r in rows:
        if r["state"] == "pending" and r["group"] == _cell(group):
            _relocate(base, r, axis, "filed")
            moved.append(r["path"])
    save(base, rows)
    return moved


# ---- sweep --------------------------------------------------------------

def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def sweep(base, today=None, dry_run=False):
    """Self-heal INDEX against disk, then best-guess stale pending rows."""
    today = _today(today)
    rows = load(base)
    report = {"dropped": [], "adopted": [], "best_guess": []}
    kept = []
    for r in rows:
        if os.path.isfile(_abs(base, r["path"])):
            kept.append(r)
        else:
            report["dropped"].append(r["path"])
    known = {r["path"] for r in kept}
    docs = os.path.join(base, DOCS_REL)
    for root, dirs, files in os.walk(docs):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            p = os.path.join(root, fn)
            rel = _rel(base, p)
            if rel in known:
                continue
            parts = rel.split("/")[2:-1]
            if parts and parts[0] == PRIVATE:
                parts = parts[1:]
            stem = _nfc(os.path.splitext(fn)[0])
            m = NAME_DATE_RE.match(stem)
            date = m.group(1) if m else dt.date.fromtimestamp(os.path.getmtime(p)).isoformat()
            kept.append({"date": date, "path": rel, "axis": parts[0] if parts else "",
                         "tags": "", "title": m.group(2) if m else stem, "sha256": sha256(p),
                         "state": "filed", "group": "", "candidates": "", "since": today,
                         "asked": ""})
            report["adopted"].append(rel)
    for r in kept:
        if r["state"] != "pending":
            continue
        start = r["asked"] or r["since"]
        if start and _days(start, today) >= BEST_GUESS_DAYS:
            cands = _split(r["candidates"])
            axis = cands[0] if cands and axis_slot(base) is not None else ""
            if dry_run:
                report["best_guess"].append(r["path"])
                continue
            _relocate(base, r, axis, "auto")
            report["best_guess"].append(r["path"])
    if not dry_run and (rows or kept):
        save(base, kept)
    return report


# ---- read side ----------------------------------------------------------

MONTH_RE = re.compile(r"^(\d{1,2})\uc6d4$")   # "10<month>"
YEAR_RE = re.compile(r"^(\d{4})\ub144$")      # "2026<year>"


def _token_hit(tok, r, hay):
    m = MONTH_RE.match(tok)
    if m and int(r["date"][5:7]) == int(m.group(1)):
        return True
    m = YEAR_RE.match(tok)
    if m and r["date"][:4] == m.group(1):
        return True
    return tok in hay


def search(base, query, include_private=False):
    toks = [_nfc(t).lower() for t in _nfc(query).split() if t]
    hits = []
    for r in load(base):
        if is_private(r) and not include_private:
            continue
        hay = " ".join(_nfc(r[c]).lower() for c in ("date", "path", "axis", "tags", "title"))
        if toks and all(_token_hit(t, r, hay) for t in toks):
            hits.append(r)
    return sorted(hits, key=lambda r: r["date"], reverse=True)


def view(base, tag, include_private=False):
    tag = _cell(tag)
    return [r for r in load(base)
            if tag in _split(r["tags"]) and (include_private or not is_private(r))]


# ---- CLI ----------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="filing.py")
    ap.add_argument("--base", required=True)
    ap.add_argument("--today")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("file", "hold"):
        s = sub.add_parser(name)
        s.add_argument("src")
        s.add_argument("--title", required=True)
        s.add_argument("--date", required=True)
        s.add_argument("--tag", action="append", default=[])
        s.add_argument("--private", action="store_true")
        s.add_argument("--group", default="", required=(name == "hold"))
        if name == "file":
            s.add_argument("--axis")
        else:
            s.add_argument("--candidate", action="append", default=[])
    sub.add_parser("questions")
    s = sub.add_parser("asked"); s.add_argument("group")
    s = sub.add_parser("resolve"); s.add_argument("group"); s.add_argument("axis")
    s = sub.add_parser("precedent"); s.add_argument("group")
    s = sub.add_parser("sweep"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("search"); s.add_argument("query", nargs="+"); s.add_argument("--private", action="store_true")
    s = sub.add_parser("view"); s.add_argument("tag"); s.add_argument("--private", action="store_true")
    a = ap.parse_args(argv)
    base = os.path.abspath(a.base)
    try:
        if a.cmd == "file":
            out = file_doc(base, a.src, a.title, a.date, a.axis, a.tag, a.private, a.group, a.today)
        elif a.cmd == "hold":
            out = hold(base, a.src, a.title, a.date, a.group, a.candidate, a.tag, a.private, a.today)
        elif a.cmd == "questions":
            out = questions(base)
        elif a.cmd == "asked":
            out = {"asked": mark_asked(base, a.group, a.today)}
        elif a.cmd == "resolve":
            out = {"moved": resolve(base, a.group, a.axis, a.today)}
        elif a.cmd == "precedent":
            out = {"axis": precedent(base, a.group)}
        elif a.cmd == "sweep":
            out = sweep(base, a.today, a.dry_run)
        elif a.cmd == "search":
            out = search(base, " ".join(a.query), a.private)
        else:
            out = view(base, a.tag, a.private)
    except FilingError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
