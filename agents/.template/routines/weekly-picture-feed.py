#!/usr/bin/env python3
"""Write the real-record weekly picture feed."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from weekly_picture_feed import write_feed  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--week-ending", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    counts = write_feed(args.root, args.week_ending, args.out)
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
