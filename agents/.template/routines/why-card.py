#!/usr/bin/env python3
"""One helper verb for showing the non-blocking owner Why card."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "routines/lib"))
from why_net import owner_card


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--candidate", action="append", required=True)
    parser.add_argument("--now", required=True)
    args = parser.parse_args()
    value = owner_card.show(ROOT, args.session, args.work, args.candidate, args.now)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
