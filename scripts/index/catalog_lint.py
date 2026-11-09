#!/usr/bin/env python3
"""catalog_lint.py -- the catalog repo's row lint (DGN-1883 design v2 sec 2.3 #4/#5).

One predicate, two callers: the index resolver (index.py) runs it on every
fetched catalog snapshot BEFORE it may become `current`, and the publish side
(WP5) runs it before a catalog commit. A snapshot that fails is rejected
whole -- there is no per-row partial accept.

Checks (all reported, not first-only):
  schema    top-level `version` is an int <= SUPPORTED_CATALOG_VERSION
  shape     `packs` is a list of objects
  id        every row has a non-empty string `id`; ids are unique
  required  REQUIRED_FIELDS present on every row (today's CONTRACT rows
            minus package_dir)
  local     no `package_dir` key: a local-only coordinate never ships in
            the repo (it stays in the legacy framework catalog /
            pack-sources.conf)
  url       `source_url` under the allowlist prefix; empty/missing only when
            the row is `menu_visible: false` AND the channel is dev; on
            stable an empty `source_url` is a reject for every INSTALLABLE
            row. A retired row (`status: retired`, e.g. pack_ledger) is
            exempt from "source_url required" on both channels: it is not
            installable, it only carries the retire declaration
            update_plan reads. A retired row that does carry a source_url
            is still held to the allowlist.

Usage: catalog_lint.py <catalog.json> --channel dev|stable --allow-prefix <p>
Exit:  0 clean, 1 violations (one per line on stdout), 2 usage.
"""
import argparse
import json
import sys

SUPPORTED_CATALOG_VERSION = 1
REQUIRED_FIELDS = ("id", "status", "name_ko", "name_en")


def lint_catalog(data, channel, allow_prefix):
    """Return (violations, schema_too_new). data = parsed JSON (or None)."""
    errs = []
    if not isinstance(data, dict):
        return ["shape: top level is not a JSON object"], False
    ver = data.get("version")
    if not isinstance(ver, int) or isinstance(ver, bool):
        errs.append("schema: top-level 'version' missing or not an integer")
    elif ver > SUPPORTED_CATALOG_VERSION:
        return ["schema: catalog version %d > supported %d (framework update needed)"
                % (ver, SUPPORTED_CATALOG_VERSION)], True
    packs = data.get("packs")
    if not isinstance(packs, list):
        errs.append("shape: 'packs' missing or not a list")
        return errs, False
    seen = set()
    for i, row in enumerate(packs):
        if not isinstance(row, dict):
            errs.append("shape: packs[%d] is not an object" % i)
            continue
        rid = row.get("id")
        label = rid if isinstance(rid, str) and rid else "packs[%d]" % i
        if not isinstance(rid, str) or not rid:
            errs.append("id: packs[%d] has no string id" % i)
        elif rid in seen:
            errs.append("id: duplicate id '%s'" % rid)
        else:
            seen.add(rid)
        for f in REQUIRED_FIELDS:
            if f not in row:
                errs.append("required: %s lacks '%s'" % (label, f))
        if "package_dir" in row:
            errs.append("local: %s carries package_dir (local-only field)" % label)
        url = row.get("source_url") or ""
        if not isinstance(url, str):
            errs.append("url: %s source_url is not a string" % label)
        elif not url:
            if row.get("status") == "retired":
                pass
            elif channel == "stable":
                errs.append("url: %s has empty source_url on stable" % label)
            elif row.get("menu_visible", True) is not False:
                errs.append("url: %s has empty source_url but is menu-visible" % label)
        elif not url.startswith(allow_prefix):
            errs.append("url: %s source_url '%s' is off the allowlist (%s)"
                        % (label, url, allow_prefix))
    return errs, False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="catalog_lint.py")
    ap.add_argument("catalog")
    ap.add_argument("--channel", required=True, choices=("dev", "stable"))
    ap.add_argument("--allow-prefix", required=True)
    a = ap.parse_args(argv)
    try:
        with open(a.catalog, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        print("parse: %s" % e)
        return 1
    errs, _ = lint_catalog(data, a.channel, a.allow_prefix)
    for e in errs:
        print(e)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
