#!/usr/bin/env python3
"""Idempotently import the installed legacy Why history; --dry-run writes nothing."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from why_net.migration import migrate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--source-state")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate(args.root, dry_run=args.dry_run, source_state=args.source_state)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status="held", reason=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)))
        return 3


if __name__ == "__main__":
    sys.exit(main())
