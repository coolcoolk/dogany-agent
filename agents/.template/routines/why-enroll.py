#!/usr/bin/env python3
"""Enroll this installed instance using its existing owner access identity."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from why_net.enrollment import enroll

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(enroll(args.root), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status="held", reason=str(exc))))
        sys.exit(3)
