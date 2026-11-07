#!/usr/bin/env python3
# kit_db_mode.py -- DGN-2056: does a kit have a crew DB lane?
#
# The crew registry readers (update_seed.sh membership predicate,
# update_plan.sh 3-source cross-check) need the answer pack_install.sh
# computes as KIT_DB_CREW (pack_install.sh capabilities block, lockstep with
# compat-lint C1):
#   capabilities absent / db_lane absent|true, db_derived absent|false -> crew
#   capabilities.db_lane=false                                         -> derived
#   capabilities.db_derived=true                                       -> derived
# "crew": members share <crews home>/<kit>/<kit>.db through a
# database/<kit>.db symlink -- the symlink is the membership ground truth.
# "derived": no per-HOME / crew DB exists (the kit rebuilds its index from
# its own files); a member is an instance that mounts the kit, nothing else.
#
# CLI: kit_db_mode.py <pack-manifest.json | ->
#   prints "crew" or "derived", exit 0
#   exit 1 (nothing printed): unreadable / not JSON / malformed capabilities
#   -- callers treat that as unknown and keep the crew (symlink) predicate,
#   which is the fail-closed side.
import json
import sys


def mode(d):
    if not isinstance(d, dict):
        return None
    caps = d.get("capabilities")
    if caps is None:
        return "crew"
    if not isinstance(caps, dict):
        return None
    if set(caps) - {"db_lane", "schedules", "db_derived"}:
        return None
    lane, derived = caps.get("db_lane"), caps.get("db_derived")
    for v in (lane, derived, caps.get("schedules")):
        if v is not None and not isinstance(v, bool):
            return None
    if lane is False or derived is True:
        return "derived"
    return "crew"


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: kit_db_mode.py <pack-manifest.json | ->\n")
        return 2
    try:
        if argv[1] == "-":
            d = json.load(sys.stdin)
        else:
            with open(argv[1], encoding="utf-8") as fh:
                d = json.load(fh)
    except Exception:
        return 1
    m = mode(d)
    if m is None:
        return 1
    print(m)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
