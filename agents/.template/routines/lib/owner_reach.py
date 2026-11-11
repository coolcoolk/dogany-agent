#!/usr/bin/env python3
"""Owner-reach: does an owner's verbatim words in a ticket reach code (DGN-1390).

WHY THIS EXISTS: owner words land in tickets, notes, SKILL text and code
comments -- places a person reads.  Whether they ever reach code was held by
one agent remembering to carry them.  Measured: of 9 owner complaints in one
session, 8 were already written down somewhere and never ran.  A rule that
says "carry owner words to code" is itself only text, so this module is the
counting machine behind it:

  (a) an owner quote in a ticket is an ITEM.  The item is met only by a
      `reach:` line naming a code coordinate (file:line) or an explicit
      `none:` reason (not an implementation item).
  (b) every coordinate is checked for reality: the file must exist under
      the instance root (or an OWNER_REACH_ROOTS root) and the line must be
      inside it.  A coordinate that names a missing file or line is PHANTOM.
  (c) unmet items and phantom coordinates are counted, per ticket and per
      worklog, so they show up instead of piling up silently.

Ticket syntax (body, outside fenced code):

    reach: q-3fa2c1 routines/x.py:42 routines/lib/y.py:10-20
    reach: q-77d0e4 none: owner opinion on tone, no code surface

The id "q-<6 hex>" is the sha256 prefix of the normalized quote text, so it
survives reordering; `owner-reach.py check <ticket>` prints the ids.  Since
the id derives from the verbatim text, editing a quote breaks its reach line
on purpose: verbatim is not edited.

Identification predicate (what counts as an owner quote) -- MEASURE FIRST:
  R1  a blockquote run (lines starting with ">") whose nearest preceding
      non-blank, non-quote line carries an owner cue word.
  R2  a double-quoted span ("..." or curly quotes, at least QUOTE_MIN_CHARS
      characters) in the same SENTENCE as an owner cue word; one item per
      span.  Table rows, `code spans`, <placeholders> and single-token
      ASCII spans never count (measured noise, see the DGN-1390 design).
  An explicit "<!-- owner-verbatim -->" line forces the next blockquote run
  to count (R1 without a cue); "<!-- not-owner -->" suppresses it.
Cue words: conf OWNER_REACH_CUES (comma list) or the default
("owner", the Korean word for owner).  Frontmatter and fenced code are
never scanned.  `owner-reach.py measure` prints every hit with context so
an instance can label hits/false positives on its own worklog before it
turns OWNER_REACH on.

Pure stdlib.  English/ASCII only (non-ASCII cue defaults are escaped).
"""

import hashlib
import os
import re

__all__ = [
    "DEFAULT_CUES", "QUOTE_MIN_CHARS", "split_frontmatter", "owner_quotes",
    "reach_lines", "check_coord", "ticket_report", "worklog_report",
    "quote_id", "frontmatter_status",
]

# "owner" + the Korean word for owner (escaped: this file stays ASCII).
DEFAULT_CUES = ("owner", "\uc624\ub108")
QUOTE_MIN_CHARS = 4

FORCE_MARK = "<!-- owner-verbatim -->"
SUPPRESS_MARK = "<!-- not-owner -->"

_QUOTED_RE = re.compile(
    r"[\"\u201c]([^\"\u201c\u201d\n]{%d,}?)[\"\u201d]" % QUOTE_MIN_CHARS)
_REACH_RE = re.compile(r"^\s*[-*]?\s*reach:\s*(.+?)\s*$", re.IGNORECASE)
_QID_RE = re.compile(r"\bq-([0-9a-f]{6})\b")
_NONE_RE = re.compile(r"\bnone:\s*(\S.*)$", re.IGNORECASE)
# path:line or path:start-end; the path must carry a "/" or a "." so a
# clock time ("06:46") is never read as a coordinate.
_COORD_RE = re.compile(r"(?<![\w/.-])([\w./-]+):(\d+)(?:-(\d+))?(?![\w])")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MARKUP_RE = re.compile(r"[*_`]+")
NONE_REASON_MIN = 8


def quote_id(text):
    """'q-' + first 6 hex of sha256 over the whitespace-collapsed quote."""
    norm = " ".join(text.split())
    return "q-" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:6]


def split_frontmatter(text):
    """(frontmatter_lines, body_lines_with_numbers).  Body numbers are
    1-based file line numbers."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[1:i], list(enumerate(lines[i + 1:], start=i + 2))
    return [], list(enumerate(lines, start=1))


def frontmatter_status(text):
    fm, _ = split_frontmatter(text)
    for line in fm:
        if line.startswith("status:"):
            return line[len("status:"):].strip()
    return None


def _unfenced(body):
    out, fenced = [], False
    for n, line in body:
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append((n, line))
    return out


def _cue_res(cues):
    """ASCII cues match as a whole word, not inside an identifier
    ("worded_by=owner", "deploy_owner"); other scripts match as a substring
    (Korean particles attach to the noun)."""
    out = []
    for c in cues:
        c = c.strip().lower()
        if not c:
            continue
        if c.isascii():
            out.append(re.compile(r"(?<![\w=_-])%s(?![\w=_-])" % re.escape(c)))
        else:
            out.append(re.compile(re.escape(c)))
    return out


def _has_cue(line, cue_res):
    low = line.lower()
    return any(r.search(low) for r in cue_res)


_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _r2_spans(line, cue_res):
    """Quoted spans that share a sentence with an owner cue.  Table rows,
    code spans and <placeholder> / single-token ASCII spans never count."""
    if line.lstrip().startswith("|"):
        return []
    out = []
    for sent in _SENTENCE_SPLIT_RE.split(_CODE_SPAN_RE.sub(" ", line)):
        if not _has_cue(sent, cue_res):
            continue
        for m in _QUOTED_RE.finditer(sent):
            t = m.group(1).strip()
            if t.startswith("<") and t.endswith(">"):
                continue
            if t.isascii() and " " not in t:
                continue
            out.append(t)
    return out


def _clean(s):
    return " ".join(_MARKUP_RE.sub("", s).split()).strip(" \"\u201c\u201d")


def owner_quotes(text, cues=DEFAULT_CUES):
    """List of {"id","line","text","rule"} for owner quotes in a ticket."""
    _, body = split_frontmatter(text)
    body = _unfenced(body)
    cue_res = _cue_res(cues)
    items, seen = [], set()

    def add(n, raw, rule):
        t = _clean(raw)
        if len(t) < QUOTE_MIN_CHARS:
            return
        qid = quote_id(t)
        if qid in seen:
            return
        seen.add(qid)
        items.append({"id": qid, "line": n, "text": t, "rule": rule})

    i = 0
    prev = ""          # nearest preceding non-blank, non-quote line
    while i < len(body):
        n, line = body[i]
        s = line.strip()
        if s.startswith(">"):
            run = []
            start = n
            while i < len(body) and body[i][1].strip().startswith(">"):
                run.append(body[i][1].strip().lstrip(">").strip())
                i += 1
            p = prev.strip()
            if p != SUPPRESS_MARK and (p == FORCE_MARK or _has_cue(p, cue_res)):
                add(start, " ".join(r for r in run if r), "R1")
            continue
        if s:
            if not _REACH_RE.match(line):
                for t in _r2_spans(line, cue_res):
                    add(n, t, "R2")
            prev = line
        i += 1
    return items


def reach_lines(text):
    """List of {"line","ids","coords":[(path,start,end)],"none":reason|None}."""
    _, body = split_frontmatter(text)
    out = []
    for n, line in _unfenced(body):
        m = _REACH_RE.match(line)
        if not m:
            continue
        rest = m.group(1)
        ids = ["q-" + h for h in _QID_RE.findall(rest)]
        nm = _NONE_RE.search(rest)
        reason = nm.group(1).strip() if nm else None
        coord_part = rest[:nm.start()] if nm else rest
        coord_part = _QID_RE.sub(" ", coord_part)
        coords = []
        for cm in _COORD_RE.finditer(coord_part):
            if "/" not in cm.group(1) and "." not in cm.group(1):
                continue
            start = int(cm.group(2))
            end = int(cm.group(3)) if cm.group(3) else start
            coords.append((cm.group(1), start, end))
        out.append({"line": n, "ids": ids, "coords": coords, "none": reason})
    return out


def check_coord(path, start, end, roots):
    """None if path:start-end is real under one of `roots`, else a reason."""
    if start < 1 or end < start:
        return "bad line range"
    cands = [path] if os.path.isabs(path) else [
        os.path.join(r, path) for r in roots]
    for c in cands:
        if os.path.isfile(c):
            try:
                with open(c, "rb") as fh:
                    count = sum(1 for _ in fh)
            except OSError:
                return "unreadable"
            if end > count:
                return "line %d past end (%d lines)" % (end, count)
            return None
    return "no such file"


def ticket_report(path, roots, cues=DEFAULT_CUES):
    """Per-ticket result: quotes, met ids, unmet quotes, phantom coords,
    reach lines naming ids that match no quote (orphans)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    quotes = owner_quotes(text, cues)
    qids = {q["id"] for q in quotes}
    met, phantoms, orphans = set(), [], []
    for r in reach_lines(text):
        bad = []
        for (p, s, e) in r["coords"]:
            why = check_coord(p, s, e, roots)
            if why:
                bad.append({"line": r["line"], "coord": "%s:%d%s" % (
                    p, s, "-%d" % e if e != s else ""), "why": why})
        phantoms.extend(bad)
        ok = (r["coords"] and not bad) or (
            not r["coords"] and r["none"]
            and len(r["none"]) >= NONE_REASON_MIN)
        for qid in r["ids"]:
            if qid not in qids:
                orphans.append({"line": r["line"], "id": qid})
            elif ok:
                met.add(qid)
    unmet = [q for q in quotes if q["id"] not in met]
    return {"path": path, "status": frontmatter_status(text),
            "quotes": quotes, "met": sorted(met), "unmet": unmet,
            "phantoms": phantoms, "orphans": orphans}


def worklog_report(worklog_dir, roots, cues=DEFAULT_CUES, match=None):
    """Aggregate over worklog tickets (status != dismissed).  `match` is an
    optional basename predicate (the instance ticket glob)."""
    totals = {"tickets": 0, "quotes": 0, "unmet": 0, "closed_unmet": 0,
              "phantoms": 0, "rows": []}
    try:
        names = sorted(os.listdir(worklog_dir))
    except OSError:
        return totals
    for name in names:
        if name.startswith("_") or not name.endswith(".md"):
            continue
        if match is not None and not match(name):
            continue
        p = os.path.join(worklog_dir, name)
        try:
            rep = ticket_report(p, roots, cues)
        except OSError:
            continue
        if rep["status"] == "dismissed" or not rep["quotes"] and not rep["phantoms"]:
            continue
        totals["tickets"] += 1
        totals["quotes"] += len(rep["quotes"])
        totals["unmet"] += len(rep["unmet"])
        totals["phantoms"] += len(rep["phantoms"])
        if rep["status"] == "done":
            totals["closed_unmet"] += len(rep["unmet"])
        totals["rows"].append(rep)
    return totals
