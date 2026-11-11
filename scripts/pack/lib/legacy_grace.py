#!/usr/bin/env python3
"""legacy_grace.py -- explicit legacy-grace allowlist predicate (DGN-1498).

compat-lint.sh used to grant legacy-grace (C4/C5/C6 skipped, exit 0) to ANY
manifest that simply omitted contract_version (kind != kit). That made the
status-quo protection for old packs an opt-out-by-omission for new ones.
Grace is now granted only to a manifest whose legacy identity is listed in
scripts/pack/legacy-grace-allowlist.tsv.

Identity = (name, reference_slug) -- the two fields every pre-v2 manifest
carries (the old shape has no `id`). A missing reference_slug is spelled
"-" in the list. Matching is exact string equality.

CLI:
  legacy_grace.py match <manifest> <allowlist>
    rc 0 + stdout "legacy-allowlist name=<n> reference_slug=<s>"  listed
    rc 1 + stdout <reason>                                          refused
Fail closed: unreadable manifest/list, malformed list row, or a non-string
identity field is a refusal, never a grace. The printed values are
JSON-quoted so the record is always one line (it is copied into the install
receipt verbatim).
"""
import json
import os
import re
import sys

# A list cell: non-empty, no control characters (TAB is the column separator).
_CELL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_ABSENT = "-"


def _identity(manifest):
    d = json.load(open(manifest))
    if not isinstance(d, dict):
        raise ValueError("manifest top-level JSON is not an object")
    out = []
    for key in ("name", "reference_slug"):
        v = d.get(key)
        if v is None or v == "":
            out.append(_ABSENT)
            continue
        if not isinstance(v, str):
            raise ValueError("%s is %s, not a string" % (key, type(v).__name__))
        out.append(v)
    if out[0] == _ABSENT:
        raise ValueError("manifest has no name -- a legacy identity needs one")
    return tuple(out)


def _load(allowlist):
    rows = set()
    with open(allowlist) as fh:
        for n, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 2:
                raise ValueError("allowlist line %d: expected name<TAB>reference_slug[<TAB>note]" % n)
            name, slug = cols[0], cols[1]
            if not _CELL_RE.match(name) or not _CELL_RE.match(slug):
                raise ValueError("allowlist line %d: empty or control-character cell" % n)
            rows.add((name, slug))
    return rows


def match(manifest, allowlist):
    try:
        ident = _identity(manifest)
    except Exception as e:
        print("legacy identity unreadable: %s" % e)
        return 1
    try:
        rows = _load(allowlist)
        # DGN-1998: canonical-only sibling (never exported), read when present.
        sib = allowlist[:-4] + ".canonical.tsv" if allowlist.endswith(".tsv") else None
        if sib and os.path.isfile(sib):
            rows |= _load(sib)
    except Exception as e:
        print("legacy-grace allowlist unusable (%s): %s" % (allowlist, e))
        return 1
    if ident not in rows:
        print("identity name=%s reference_slug=%s not listed in %s"
              % (json.dumps(ident[0]), json.dumps(ident[1]), allowlist))
        return 1
    print("legacy-allowlist name=%s reference_slug=%s"
          % (json.dumps(ident[0]), json.dumps(ident[1])))
    return 0


def main(argv):
    if len(argv) != 4 or argv[1] != "match":
        print("usage: legacy_grace.py match <manifest> <allowlist>")
        return 2
    return match(argv[2], argv[3])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
