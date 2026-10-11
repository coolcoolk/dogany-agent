#!/usr/bin/env python3
"""plist-comment-lint.py -- python-parser plist lint (DGN-1442).

An XML comment containing ``--`` is illegal per the XML spec. ``plutil -lint``
passes it and launchd loads it, but python's ``plistlib`` (expat) raises
ExpatError -- so every python tool that reads the plist (health-observer's
label scan) silently loses that unit. This lint closes the authoring side:
it fails a plist that plistlib cannot parse and points at the offending
comment line.

Usage
  plist-comment-lint.py [FILE_OR_DIR ...]
    No args = every *.plist and *.plist.tpl next to this script (routines/).
    A directory = its plist files (recursive, suffix exact).
  Exit 0 = all parse, 1 = any failure (file, line, reason on stdout), 2 = usage.

English/ASCII only. Stdlib only. Read-only.
"""

import os
import plistlib
import re
import sys

PLIST_SUFFIXES = (".plist", ".plist.tpl")
COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


def comment_findings(text):
    """[(line, why)] for every XML comment whose body breaks the ``--`` rule.

    The body is what sits between ``<!--`` and ``-->``; ``--`` anywhere in it,
    or a trailing ``-`` (which makes the terminator ``--->``), is illegal.
    """
    found = []
    for m in COMMENT_RE.finditer(text):
        body = m.group(1)
        if "--" in body or body.endswith("-"):
            found.append((text.count("\n", 0, m.start()) + 1,
                          "'--' inside XML comment"))
    return found


def lint_file(path):
    """-> list of (line, message); empty = clean."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        return [(0, "unreadable: %s" % exc)]
    problems = comment_findings(raw.decode("utf-8", "replace"))
    parse_raw = raw
    if path.endswith(".plist.tpl"):
        # The bundled template deliberately leaves these activation values
        # unrendered. Substitute parser-safe stand-ins so XML/plist syntax is
        # still checked without treating the template itself as a bad plist.
        parse_raw = parse_raw.replace(b"__WEEKDAY_ENTRY__", b"")
        parse_raw = parse_raw.replace(b"__HOUR__", b"0")
        parse_raw = parse_raw.replace(b"__MINUTE__", b"0")
        parse_raw = parse_raw.replace(b"__PERIOD__", b"0")   # DGN-1655 drive
        parse_raw = re.sub(rb"__[A-Z_]+__", b"value", parse_raw)
    try:
        plistlib.loads(parse_raw)
    except Exception as exc:
        if not problems:
            problems.append((0, "plistlib parse failure: %s: %s"
                             % (type(exc).__name__, exc)))
    return problems


def expand(targets):
    files = []
    for target in targets:
        if os.path.isdir(target):
            for directory, dirnames, names in os.walk(target):
                dirnames.sort()
                files.extend(os.path.join(directory, name)
                             for name in sorted(names)
                             if name.endswith(PLIST_SUFFIXES))
        else:
            files.append(target)
    return files


def main(argv):
    if any(arg in ("-h", "--help") for arg in argv):
        sys.stdout.write(__doc__)
        return 0
    targets = argv or [os.path.dirname(os.path.abspath(__file__))]
    bad = 0
    for path in expand(targets):
        for line, why in lint_file(path):
            bad += 1
            print("%s:%d: %s" % (path, line, why))
    if bad:
        print("plist-comment-lint: %d problem(s)" % bad)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
