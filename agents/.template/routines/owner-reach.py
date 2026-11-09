#!/usr/bin/env python3
"""owner-reach CLI: count owner words that have not reached code (DGN-1390).

Verbs:
  check <ticket.md>      list owner quotes (id, rule, line, text), the reach
                         state of each, phantom coordinates and orphan ids.
                         Exit 0 = every quote met and no phantom; 1 = not.
  count [<worklog-dir>]  one line: unmet quotes, unmet in DONE tickets,
                         phantom coordinates (default dir: <root>/worklog).
                         Exit 0 always.  This is the number (c) surfaces.
  measure <path>...      print every predicate hit with two lines of
                         context, tab-separated, for hand labelling hit /
                         false positive.  Run on an instance's own worklog
                         BEFORE turning OWNER_REACH on (DGN-1390 pre-build
                         judgment 4: no wiring before the predicate is
                         measured).

The library (routines/lib/owner_reach.py) holds the predicate, the reach
syntax and the coordinate check; this file only resolves config and prints.

Config (config/agent.conf, or env DOGANY_AGENT_CONF for its path):
  OWNER_REACH        off (default) | warn | block -- read by the ticket lint
                     and the session-start count; this CLI ignores it.
  OWNER_REACH_CUES   comma list of owner cue words (default: owner + the
                     Korean word for owner).
  OWNER_REACH_ROOTS  colon list of extra roots coordinates may resolve
                     under (relative = instance root).  The instance root
                     (parent of worklog/) is always first.

Pure stdlib.  English/ASCII only.
"""
import fnmatch
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
import owner_reach as orx  # noqa: E402

try:
    from conf_reader import conf_get
except Exception:  # pragma: no cover
    def conf_get(key, conf_path=None):
        return ""


def instance_root_for(path):
    """Parent of the worklog/ dir holding `path`, else the code root."""
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.basename(d) == "worklog":
            return os.path.dirname(d)
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.dirname(HERE)
        d = parent


def conf(key, root):
    path = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(
        root, "config", "agent.conf")
    try:
        return conf_get(key, path) or ""
    except Exception:
        return ""


def mode(root):
    v = (os.environ.get("DOGANY_OWNER_REACH") or conf("OWNER_REACH", root)
         or "off").strip().lower()
    return v if v in ("warn", "block") else "off"


def cues(root):
    raw = conf("OWNER_REACH_CUES", root)
    got = tuple(c.strip() for c in raw.split(",") if c.strip())
    return got or orx.DEFAULT_CUES


def roots(root):
    out = [root]
    for r in conf("OWNER_REACH_ROOTS", root).split(":"):
        r = r.strip()
        if r:
            out.append(r if os.path.isabs(r) else os.path.join(root, r))
    return out


def ticket_glob(root):
    g = os.environ.get("TICKET_FILE_GLOB") or conf("TICKET_FILE_GLOB", root)
    if g:
        return g
    prefix = os.environ.get("TICKET_PREFIX") or conf("TICKET_PREFIX", root)
    return "%s-*.md" % prefix if prefix else "*.md"


def report_for(ticket):
    root = instance_root_for(ticket)
    return orx.ticket_report(ticket, roots(root), cues(root))


def format_problems(rep, limit=6):
    """Short lines naming what blocks closure: unmet quotes + phantoms."""
    lines = []
    for q in rep["unmet"][:limit]:
        t = q["text"] if len(q["text"]) <= 60 else q["text"][:57] + "..."
        lines.append("  unmet %s (line %d): \"%s\"" % (q["id"], q["line"], t))
    if len(rep["unmet"]) > limit:
        lines.append("  ... %d more unmet" % (len(rep["unmet"]) - limit))
    for p in rep["phantoms"][:limit]:
        lines.append("  phantom %s (reach line %d): %s" % (
            p["coord"], p["line"], p["why"]))
    for o in rep["orphans"][:limit]:
        lines.append("  orphan reach id %s (line %d): matches no owner quote"
                     % (o["id"], o["line"]))
    return lines


def count_line(worklog_dir):
    """(text, totals).  Text is "" when nothing is unmet or phantom."""
    root = os.path.dirname(os.path.abspath(worklog_dir))
    g = ticket_glob(root)
    tot = orx.worklog_report(worklog_dir, roots(root), cues(root),
                             match=lambda n: fnmatch.fnmatch(n, g))
    if not tot["unmet"] and not tot["phantoms"]:
        return "", tot
    text = ("owner-reach: %d owner quote(s) not reached to code "
            "(%d in done tickets), %d phantom coordinate(s), %d ticket(s)"
            % (tot["unmet"], tot["closed_unmet"], tot["phantoms"],
               tot["tickets"]))
    return text, tot


def cmd_check(path):
    rep = report_for(path)
    print("%s  status=%s  quotes=%d  met=%d  unmet=%d  phantoms=%d" % (
        os.path.basename(path), rep["status"], len(rep["quotes"]),
        len(rep["met"]), len(rep["unmet"]), len(rep["phantoms"])))
    for q in rep["quotes"]:
        state = "met" if q["id"] in rep["met"] else "UNMET"
        print("  %s %s %s line %d: %s" % (q["id"], state, q["rule"],
                                          q["line"], q["text"]))
    for line in format_problems(rep, limit=1000):
        if not line.lstrip().startswith("unmet"):
            print(line)
    return 0 if not rep["unmet"] and not rep["phantoms"] else 1


def cmd_count(worklog_dir):
    text, tot = count_line(worklog_dir)
    print(text or "owner-reach: 0 unmet, 0 phantom (%d quote(s) in %d ticket(s))"
          % (tot["quotes"], tot["tickets"]))
    return 0


def cmd_measure(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for dp, _, fs in os.walk(p):
                files += [os.path.join(dp, f) for f in sorted(fs)
                          if f.endswith(".md")]
        elif os.path.isfile(p):
            files.append(p)
    hits = 0
    for f in files:
        root = instance_root_for(f)
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        lines = text.splitlines()
        for q in orx.owner_quotes(text, cues(root)):
            hits += 1
            ctx = " | ".join(x.strip() for x in
                             lines[max(0, q["line"] - 3):q["line"]])
            print("%s:%d\t%s\t%s\t%s\t%s" % (f, q["line"], q["rule"], q["id"],
                                             q["text"][:80], ctx[:160]))
    print("# measure: %d hit(s) in %d file(s); label each hit/fp" % (
        hits, len(files)), file=sys.stderr)
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(argv) >= 2 else 2
    verb, args = argv[1], argv[2:]
    if verb == "check" and len(args) == 1:
        return cmd_check(args[0])
    if verb == "count" and len(args) <= 1:
        d = args[0] if args else os.path.join(os.getcwd(), "worklog")
        return cmd_count(d)
    if verb == "measure" and args:
        return cmd_measure(args)
    print("usage: owner-reach.py check <ticket> | count [<worklog>] | "
          "measure <path>...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
