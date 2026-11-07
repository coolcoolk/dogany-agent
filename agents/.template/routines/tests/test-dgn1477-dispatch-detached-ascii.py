#!/usr/bin/env python3
# test-lane: fast
"""DGN-1477 regression: dispatch-detached.sh follows the framework rule
"Code = English/ASCII only (comments + string literals)" where it is not
owner-facing copy, and names no instance.

Before the fix the file carried 59+ non-ASCII comment lines (owner quotes
pasted verbatim, a Korean design note) and the dispatch-return directive
prefix was a hardcoded instance name plus Korean (`D = "<Name> ..."`), so
every agent minted from the template stamped another agent's name on its
own session directives.

Checks:
  1. no comment line (shell or embedded Python, full-line `#`) is non-ASCII;
  2. the directive prefix D is the neutral ASCII session address;
  3. the dispatch-return directive block (D assignment through the inbox
     write) is ASCII end to end.

Owner-facing notice copy, ledger-note parse contracts and the Korean
usage-claim regex are out of scope (they are runtime text, not comments).

Run: python3 routines/tests/test-dgn1477-dispatch-detached-ascii.py
Python 3 stdlib only.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "dispatch-detached.sh"))

_failures = []


def check(name, cond, detail=""):
    if not cond:
        _failures.append("%s %s" % (name, detail))


def non_ascii(s):
    return any(ord(c) > 127 for c in s)


with open(SCRIPT, encoding="utf-8") as f:
    lines = f.read().split("\n")

bad = [(i + 1, ln) for i, ln in enumerate(lines)
       if ln.lstrip().startswith("#") and non_ascii(ln)]
check("T1 comments ASCII-only", not bad,
      "; ".join("L%d" % n for n, _ in bad[:10]) + (" (+%d)" % (len(bad) - 10) if len(bad) > 10 else ""))

d_idx = [i for i, ln in enumerate(lines) if re.match(r'^D = "', ln)]
check("T2 one D assignment", len(d_idx) == 1, str(len(d_idx)))
if d_idx:
    d_line = lines[d_idx[0]]
    check("T2 D neutral", d_line == 'D = "Session directive: "', repr(d_line))
    end = next((i for i in range(d_idx[0], len(lines))
                if lines[i].lstrip().startswith("os.makedirs(inbox")), None)
    check("T3 block end found", end is not None)
    if end is not None:
        blk = [(i + 1, lines[i]) for i in range(d_idx[0], end) if non_ascii(lines[i])]
        check("T3 directive block ASCII-only", not blk,
              "; ".join("L%d" % n for n, _ in blk[:10]))

if _failures:
    for f in _failures:
        print("FAIL", f)
    sys.exit(1)
print("PASS test-dgn1477-dispatch-detached-ascii")
