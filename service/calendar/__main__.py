"""python3 -m service.calendar status|resolve [--instance PATH] [--json]

Thin CLI for shell routines and preflight (locked spec 1). Prints one JSON
object; never a token, never an item body. Exit 0 when the command ran (the
JSON carries any unavailable class), 2 on usage error.

English/ASCII only.
"""

import argparse
import json
import sys

from . import resolve, status


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m service.calendar")
    ap.add_argument("cmd", choices=("status", "resolve"))
    ap.add_argument("--instance", default=None,
                    help="instance root (default: $PROJECT_ROOT)")
    ap.add_argument("--json", action="store_true",
                    help="JSON output (the only format)")
    ns = ap.parse_args(argv)
    out = status(ns.instance) if ns.cmd == "status" else resolve(ns.instance)
    sys.stdout.write(json.dumps(out, sort_keys=True, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
