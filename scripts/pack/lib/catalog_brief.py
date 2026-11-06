#!/usr/bin/env python3
"""catalog_brief.py -- the mint flow's catalog branch as MACHINERY (DGN-1575).

Measured defect (2026-09-18 16:08, live owner session): the owner said
"헬스 트레이너 에이전트가 필요해", packs/catalog.json carried pack_health with
the literal domain entry '헬스', SKILL.md ordered the catalog branch FIRST --
and the flow went straight to the BotFather walkthrough without ever naming
the official pack. The branch lived only as prose the model had to remember
to execute; prose reminders do not survive contact with association-driven
execution (standing estate finding, 2026-08-19, and four sibling tickets the
same day). This file is the forcing point: the presentation becomes DATA the
machinery prints, and the mint machinery refuses to build until the stamp
this file writes proves the branch ran.

Two verbs, ONE matcher -- the occupancy-gate doctrine (DGN-1543): the
flow-level presentation and the machine backstop share one predicate so they
can never disagree.

  brief --catalog <file> --query <owner words> [--stamp <file>]
        [--user-label <label>]   (accepted, unused since DGN-1702)
      rc 0: the owner-facing block on stdout -- the caller renders it
            VERBATIM (its own message, every line kept). The stamp records
            the matched pack ids for the gate.
      rc 3: no catalog match, stdout EMPTY -- the flow enters its no-copy
            agent terminal. The stamp still records that the branch RAN.
      rc 4: the composed copy violates a mechanical owner ruling (em-dash /
            '비서') -- REFUSED LOUD, never rendered, never degraded to
            silence: a silent skip here IS the DGN-1575 incident.
      rc 1: usage / unreadable catalog.

  gate --catalog <file> --stamp <file> --ttl <seconds>
       {--pack <id> | --haystack <text>}
      rc 0:  PASS. Either no eligible pack claims this mint (verdict
             NO-MATCH -- a packless operator mint is never blocked), or the
             stamp is present, fresh, and covers every claimed pack id
             (verdict COVERED).
      rc 20: REFUSE. An official pack claims this mint and the stamp is
             absent, stale, or does not name it -- the owner was never
             shown the catalog choice. The caller stops at the
             registered gate (mint.catalog-brief, gates.json).
      rc 1:  usage / unreadable catalog (a data error is the caller's plain
             ERROR, not the planned gate).

Copy rules OWNED HERE IN CODE (the ticket's argument for generating the
block -- the rules stop being prose too):
  * tagline_ko and EVERY includes_ko line verbatim -- by construction: the
    values are copied, never summarized, never merged (owner ruling
    2026-09-17 18:11 "왜케 길어 뭘해주는지 깔끔히 설명해야지").
  * em-dash FORBIDDEN. Catalog option captions use the house middle-dot
    separator (owner decision dec-176); the ASCII ' -- ' spelling remains
    forbidden.
  * the [[OPTIONS]] marker is LABELED -- by construction; a bare marker
    harvests the numbered body lines as button labels and every button
    overflows the ~31-width budget (keyboard degrades to bare numbers).
  * '비서' never appears; the estate says 담당 에이전트.
  * no row the owner cannot actually get: the custom line is gone (dec-183),
    and a choice screen with zero options is a caller bug, never a render.
"""

import argparse
import json
import os
import sys
import time

STAMP_SCHEMA = 1

# A body-wide refusal set, not a style guess: the owner ruled each of these
# on a live screen. Refusal is LOUD (rc 4) -- silence here would be the
# incident this file exists to prevent.
FORBIDDEN_SUBSTRINGS = [
    ("—", "em-dash is forbidden in owner-facing copy; use the house middle dot"),
    ("–", "en-dash is the same invented separator; use the house middle dot"),
    (" -- ", "the ASCII em-dash spelling is the same separator by another "
             "name; use the house middle dot"),
    ("비서", "the estate says 담당 에이전트, never 비서"),
]

OPTIONS_WIDTH_BUDGET = 31  # Telegram keyboard label budget (SKILL.md step 1b)


def _fail(msg, rc=1):
    sys.stderr.write("[catalog-brief] ERROR: %s\n" % msg)
    sys.exit(rc)


def _note(msg):
    sys.stderr.write("[catalog-brief] %s\n" % msg)


def load_eligible(catalog_path):
    """Catalog rows that can be OFFERED as a domain-agent mint: published,
    not kind=kit (kits are activated post-mint, never minted -- install.sh
    MINOR-2 filter precedent), and claiming at least one domain."""
    try:
        with open(catalog_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        _fail("catalog unreadable (%s): %s" % (catalog_path, e))
    rows = data.get("packs")
    if not isinstance(rows, list):
        _fail("catalog carries no packs list: %s" % catalog_path)
    eligible = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") == "kit":
            continue
        if row.get("status") != "published":
            continue
        domains = row.get("domains")
        if not isinstance(domains, list) or not domains:
            continue
        if not isinstance(row.get("id"), str) or not row["id"]:
            continue
        eligible.append(row)
    return eligible


def _norm(s):
    return " ".join(str(s).split()).lower()


def _key_long_enough(key):
    # ASCII fragments shorter than 3 chars match everything; Hangul carries
    # meaning at 2. Current catalog data satisfies both floors.
    if all(ord(c) < 128 for c in key):
        return len(key) >= 3
    return len(key) >= 2


def _match_keys(row, with_names):
    keys = [d for d in row.get("domains", []) if isinstance(d, str)]
    keys += [row["id"], row["id"].replace("_", "-")]
    if with_names:
        for f in ("name_ko", "name_en"):
            v = row.get(f)
            if isinstance(v, str) and v:
                keys.append(v)
    return [k for k in (_norm(k) for k in keys) if k and _key_long_enough(k)]


def match_query(row, query):
    """brief matcher: the owner's own words vs a pack. SUBSTRING BOTH WAYS,
    case-insensitive (SKILL.md step 1a contract): '헬스' ⊂ '헬스 트레이너'
    and '헬스 트레이너' ⊂ 'dogany 헬스 트레이너' must both hit."""
    q = _norm(query)
    if not q:
        return False
    # The reverse direction gets the same key-length floor: a 2-letter ASCII
    # query ("ai") sits inside any long key ("trainer") -- grill DGN-1702 #9.
    q_ok = _key_long_enough(q)
    return any(k in q or (q_ok and q in k) for k in _match_keys(row, with_names=True))


def match_haystack(row, haystack):
    """gate matcher: the mint's slug+role vs a pack. ONE DIRECTION only
    (pack key ⊂ haystack) -- a whole role prose inside a keyword is
    impossible, and the reverse direction would let short junk slugs claim
    domains they never named."""
    h = _norm(haystack)
    if not h:
        return False
    return any(k in h for k in _match_keys(row, with_names=False))


def domain_term(matched, query):
    """Choose the display-domain word for the selected official pack.
    The word is ALWAYS one the packs declare in `domains`: the query only
    picks WHICH declared term (the longest one the owner's words contain),
    else the first declared term. The query text itself is never returned
    (DGN-1702: raw request text rendered as the domain word). Returns ""
    when no row declares a usable domain."""
    q = _norm(query)
    best = ""
    first = ""
    for row in matched:
        for d in row.get("domains", []):
            if not isinstance(d, str) or not d.strip():
                continue
            nd = _norm(d)
            if not first:
                first = d
            if q and nd in q and len(nd) > len(_norm(best)):
                best = d
    return best or first


def compose(matched, numbered=True):
    """The step-1b screen, byte-for-byte the SKILL.md owner-approved shape.
    tagline_ko and includes_ko are COPIED verbatim -- this function owns no
    prose about what a pack does. Empty `matched` raises ValueError: a no-fit
    is the caller's terminal (DGN-1702), never an optionless screen.
    numbered=False drops the "N. " in front of the card title (DGN-1702
    round 3: a single option carries no number, owner rehearsal A3)."""
    if not matched:
        raise ValueError("compose() needs at least one matched pack")
    lines = []
    names = []
    n = 0
    for row in matched:
        n += 1
        name = row.get("name_ko") or row.get("name_en") or row["id"]
        names.append(name)
        tagline = row.get("tagline_ko")
        caption = ("**%d. %s**" % (n, name)) if numbered else ("**%s**" % name)
        # `>!` is the bridge's expandable-quote grammar. The caption must be
        # first so it remains visible when Telegram folds the includes body.
        lines.append(">! %s" % caption)
        # Owner ruling (2026-08-06, re-stated 2026-10-05): title and
        # description sit on separate lines, never joined on one line.
        if isinstance(tagline, str) and tagline.strip():
            lines.append("> %s" % tagline.strip())
        includes = row.get("includes_ko")
        if isinstance(includes, list) and includes:
            for item in includes:
                lines.append("> - %s" % item)
        lines.append("")
    lines.append("[[OPTIONS: %s]]" % " | ".join(names))
    return "\n".join(lines)


def _judge(body):
    """Mechanical owner rulings, refusal loud. Returns list of findings."""
    findings = []
    for token, rule in FORBIDDEN_SUBSTRINGS:
        if token in body:
            findings.append("forbidden %r: %s" % (token, rule))
    return findings


def write_stamp(stamp_path, query, matched_ids):
    stamp = {
        "schema": STAMP_SCHEMA,
        "query": query,
        "matched": matched_ids,
        "ts": int(time.time()),
    }
    stamp_dir = os.path.dirname(stamp_path)
    if stamp_dir:
        os.makedirs(stamp_dir, exist_ok=True)
    tmp = stamp_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(stamp, fh, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, stamp_path)


def cmd_brief(args):
    eligible = load_eligible(args.catalog)
    matched = [r for r in eligible if match_query(r, args.query)]
    matched_ids = [r["id"] for r in matched]
    if args.stamp:
        write_stamp(args.stamp, args.query, matched_ids)
        _note("stamp written: %s (matched: %s)"
              % (args.stamp, ",".join(matched_ids) or "none"))
    else:
        _note("stamp SKIPPED (no --stamp): the step-6 gate has nothing to "
              "read -- instance-resident state needs --instance-root "
              "(DGN-366 L1)")
    if not matched:
        _note("verdict NO-MATCH (query %r claims no official pack) -> "
              "agent terminal" % args.query)
        sys.exit(3)
    body = compose(matched)
    findings = _judge(body)
    if findings:
        for f in findings:
            _note("copy REFUSED: %s" % f)
        _note("the composed screen violates an owner ruling -- NOT rendered,"
              " NOT silently skipped (a silent skip is the DGN-1575 "
              "incident). Fix the catalog copy, then re-run")
        sys.exit(4)
    for name in [r.get("name_ko") or r.get("name_en") or r["id"]
                 for r in matched]:
        if len(name) > OPTIONS_WIDTH_BUDGET:
            _note("WARN option label exceeds the ~%d-width keyboard budget:"
                  " %r" % (OPTIONS_WIDTH_BUDGET, name))
    sys.stdout.write(body + "\n")
    sys.exit(0)


def cmd_gate(args):
    eligible = load_eligible(args.catalog)
    if args.pack:
        needed = [r["id"] for r in eligible if r["id"] == args.pack]
    else:
        needed = [r["id"] for r in eligible
                  if match_haystack(r, args.haystack)]
    if not needed:
        print("verdict NO-MATCH (no official pack claims this mint) -- "
              "gate open")
        sys.exit(0)
    claim = ",".join(needed)
    if not os.path.isfile(args.stamp):
        print("verdict REFUSE: official pack(s) %s claim this mint but no "
              "catalog-brief stamp exists (%s) -- the owner was never shown "
              "the catalog choice" % (claim, args.stamp))
        sys.exit(20)
    try:
        with open(args.stamp, encoding="utf-8") as fh:
            stamp = json.load(fh)
        ts = int(stamp.get("ts", 0))
        covered = stamp.get("matched", [])
        assert isinstance(covered, list)
    except (OSError, ValueError, AssertionError):
        print("verdict REFUSE: catalog-brief stamp unreadable (%s) -- "
              "treat as never shown" % args.stamp)
        sys.exit(20)
    age = int(time.time()) - ts
    if age > args.ttl or age < 0:
        print("verdict REFUSE: catalog-brief stamp stale (age %ss > ttl "
              "%ss) -- the choice screen must be shown again" %
              (age, args.ttl))
        sys.exit(20)
    missing = [i for i in needed if i not in covered]
    if missing:
        print("verdict REFUSE: stamp (query %r, matched %s) does not name "
              "pack(s) %s -- the owner never saw THIS pack's choice screen"
              % (stamp.get("query"), ",".join(covered) or "none",
                 ",".join(missing)))
        sys.exit(20)
    print("verdict COVERED: stamp (query %r, age %ss) names %s -- the "
          "choice was shown" % (stamp.get("query"), age, claim))
    sys.exit(0)


def main():
    ap = argparse.ArgumentParser(prog="catalog_brief.py")
    sub = ap.add_subparsers(dest="verb", required=True)
    b = sub.add_parser("brief")
    b.add_argument("--catalog", required=True)
    b.add_argument("--query", required=True)
    b.add_argument("--stamp", default="")
    b.add_argument("--user-label", default="님")
    g = sub.add_parser("gate")
    g.add_argument("--catalog", required=True)
    g.add_argument("--stamp", required=True)
    g.add_argument("--ttl", type=int, default=21600)
    mx = g.add_mutually_exclusive_group(required=True)
    mx.add_argument("--pack", default="")
    mx.add_argument("--haystack", default="")
    args = ap.parse_args()
    if args.verb == "brief":
        if not args.query.strip():
            _fail("--query is empty")
        cmd_brief(args)
    else:
        cmd_gate(args)


if __name__ == "__main__":
    main()
