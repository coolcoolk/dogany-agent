#!/usr/bin/env python3
# test-lane: manual    # measurement harness, not a test -- asserts nothing
"""DGN-1331 step 3 -- run the predicates over the REAL handoff corpus.

The ticket forbids any effect claim before measurement, so this prints raw
counts on real files and computes no hit-rate. It also splits BLOCKs by cause
-- pre-existing DGN-1323 shapes vs the DGN-1331 pairing -- because attributing
an old block to the new predicate would be the very error the ticket is about.

The gate runs at send time on the BODY only, so the YAML frontmatter is stripped
here to match what the handoff send path actually feeds it.

Usage:
  python3 routines/tests/measure-dgn1331-handoff-corpus.py [--all] [dir ...]
    default dirs: every inbox/ and done/ under the handoff root that exists
    handoff root: $DOGANY_HANDOFF_ROOT, else ~/.dogany/handoff
    default scope: today's files (name prefix YYYYMMDD from the newest file)
    --all: every file in those dirs

READ-ONLY. Opens files, writes nothing.
  exit 0 = measured
  exit 2 = SCAN-FAIL (no handoff dirs, or no .md files in them)

FLEET NOTE (ported to agents/.template 2026-09-08)
  The original hardcoded two named instance mailboxes (e.g. "peer/inbox",
  "self/done"), so on any other instance the harness exited SCAN-FAIL on a
  path that was never going to exist. The dirs are now DISCOVERED under
  the handoff root, which means the same file measures whatever mailboxes
  an instance actually has --
  including none, which it reports as a scan failure rather than a zero.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))

import attrib_gate as gate                                     # noqa: E402
from claim_source_table import RULES                           # noqa: E402

HANDOFF_ROOT = os.environ.get(
    "DOGANY_HANDOFF_ROOT", os.path.expanduser("~/.dogany/handoff"))

# The mailbox subdirs worth measuring: what arrived and what was sent. `tmp`
# and `dead` are transport state, not documents that passed the gate.
MAILBOXES = ("inbox", "done")


def discover_dirs(root=HANDOFF_ROOT):
    """Every <root>/<agent>/<mailbox> that exists, sorted. [] = none."""
    out = []
    if not os.path.isdir(root):
        return out
    try:
        agents = sorted(os.listdir(root))
    except OSError:
        return out
    for agent in agents:
        for box in MAILBOXES:
            d = os.path.join(root, agent, box)
            if os.path.isdir(d):
                out.append(d)
    return out


def body_of(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5:]
    return text


def classify(raw):
    """Replicates main()'s order so the cause is attributed correctly."""
    stripped = gate._strip_quoted(raw)
    if gate.SUMMARY.search(stripped) and not (
            gate.SEPARATED.search(stripped) and gate.READING.search(stripped)):
        return "BLOCK", "DGN-1323/SUMMARY", []
    pairs = gate.pair_violations(stripped, raw)
    if pairs:
        return "BLOCK", "DGN-1331/PAIR", [(r.role, t) for r, t in pairs]
    hits = [m.group(0).strip() for m in gate.CLAIM.finditer(stripped)
            if not gate.CONFESS.search(gate._line_of(stripped, m.start()))]
    if hits and not (gate.TIME.search(stripped) and gate.UTTER.search(stripped)):
        return "BLOCK", "DGN-1323/CLAIM", []
    return "PASS", "-", []


def claim_types_present(raw):
    """Which claim types appear at all -- independent of block outcome."""
    stripped = gate._strip_quoted(raw)
    return [r.role for r in RULES if r.claim.search(stripped)]


def main(argv):
    show_all = "--all" in argv
    explicit = [a for a in argv if not a.startswith("--")]
    if explicit:
        dirs = explicit
        for d in dirs:
            if not os.path.isdir(d):
                print("SCAN-FAIL: no such dir %s" % d)
                return 2
    else:
        dirs = discover_dirs()
        if not dirs:
            print("SCAN-FAIL: no handoff mailboxes under %s" % HANDOFF_ROOT)
            print("           set DOGANY_HANDOFF_ROOT or pass dirs explicitly.")
            return 2

    files = []
    for d in dirs:
        for n in sorted(os.listdir(d)):
            if n.endswith(".md"):
                files.append(os.path.join(d, n))
    if not files:
        print("SCAN-FAIL: 0 documents in %d mailbox(es) under %s"
              % (len(dirs), HANDOFF_ROOT))
        for d in dirs:
            print("  dir: %s" % d)
        return 2

    if not show_all:
        day = max(os.path.basename(f)[:8] for f in files)
        files = [f for f in files if os.path.basename(f).startswith(day)]
        print("scope: %s only (%d files). --all for the full corpus." %
              (day, len(files)))
    else:
        print("scope: full corpus (%d files)" % len(files))
    for d in dirs:
        print("  dir: %s" % d)
    print("-" * 74)

    tally = {"PASS": 0, "BLOCK": 0}
    causes = {}
    role_tally = {}
    pair_rows = []
    for f in files:
        raw = body_of(f)
        verdict, cause, pairs = classify(raw)
        tally[verdict] += 1
        causes[cause] = causes.get(cause, 0) + 1
        for role in claim_types_present(raw):
            role_tally[role] = role_tally.get(role, 0) + 1
        if cause == "DGN-1331/PAIR":
            pair_rows.append((os.path.basename(f), pairs))

    print("BLOCK %d   PASS %d   (total %d)" %
          (tally["BLOCK"], tally["PASS"], len(files)))
    print("\nBLOCK by cause:")
    for k in sorted(causes):
        if k != "-":
            print("  %-18s %d" % (k, causes[k]))
    print("\nfiles blocked by the DGN-1331 pairing predicate:")
    if pair_rows:
        for name, pairs in pair_rows:
            for role, text in pairs:
                print("  %-42s role=%-14s claim=%s" % (name, role, text))
    else:
        print("  none")
    print("\nclaim types PRESENT in the corpus (detection only, "
          "block needs the source columns too):")
    for role in sorted(role_tally, key=lambda r: -role_tally[r]):
        print("  %-16s %d file(s)" % (role, role_tally[role]))
    for role in [r.role for r in RULES]:
        if role not in role_tally:
            print("  %-16s 0 file(s)" % role)
    print("\nNo hit-rate is computed. Raw counts only "
          "(DGN-1331: no effect claim before measurement).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
