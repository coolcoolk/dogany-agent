#!/usr/bin/env python3
"""DGN-1751: does a dispatch brief pin its Why and its What before build?

A brief that is only a How task list leaves the intent (Why) and the
artifact the owner will receive (What) implicit, so the worker fills both
by guess. This reads a brief and reports whether it carries:

  Why:  <one line: the owner's intent, in owner words when available>
  What: <one line: the concrete artifact + how the owner will judge it>
  Sample: <optional: where the owner-confirmed render/sample lives, for
           owner-facing output>

Labels are matched at line start, case-insensitive, after optional markdown
bullets/heading/bold ("- **Why:**", "## What (artifact): ..."). A line counts
only with >= MIN_CHARS of content after the colon, so "Why: x" or a bare
"What:" heading does not pin anything. Only the first match of each label
is used.

ADVISORY ONLY. The v2.7 Why-net LOCKED spec (5.3, dec-317) says dispatch
has no refusal; whether this becomes a refusing gate is an owner decision
(see docs/DISPATCH-BRIEF-CONTRACT.md). Exit code is 0 for any readable
brief, 2 for an unreadable one.

Output (one line, tab-separated, newline/tab-free fields):
  <verdict>\t<why>\t<what>\t<sample>
verdict: ok | missing:why | missing:what | missing:why,what

Usage: brief_why_what.py <brief-file>
"""

import re
import sys

MIN_CHARS = 10
FIELD_MAX = 200

_LABEL_RE = re.compile(
    r"^\s*(?:[-*+]\s+|#{1,6}\s+|>\s*)*"   # bullet / heading / quote
    r"(?:\*\*|__)?"                        # optional bold open
    r"(why|what|sample)"
    r"(?:\s*\([^)\n]{0,40}\))?"            # optional "(intent)" gloss
    r"\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?"  # colon, bold close either side
    r"\s*(.*)$",
    re.IGNORECASE,
)


def _clean(text):
    text = re.sub(r"[\t\r\n]+", " ", text).strip()
    text = re.sub(r"(\*\*|__)$", "", text).strip()
    return text[:FIELD_MAX]


def scan(text):
    """Return dict why/what/sample -> first qualifying line content ('')."""
    found = {"why": "", "what": "", "sample": ""}
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _LABEL_RE.match(line)
        if not m:
            continue
        key = m.group(1).lower()
        body = _clean(m.group(2))
        if found[key] or len(body) < MIN_CHARS:
            continue
        found[key] = body
    return found


def verdict(found):
    missing = [k for k in ("why", "what") if not found[k]]
    return "ok" if not missing else "missing:" + ",".join(missing)


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: brief_why_what.py <brief-file>\n")
        return 2
    try:
        with open(argv[1], encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        sys.stderr.write("brief_why_what: cannot read %s: %s\n" % (argv[1], exc))
        return 2
    found = scan(text)
    sys.stdout.write("\t".join([verdict(found), found["why"], found["what"],
                                found["sample"]]) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
