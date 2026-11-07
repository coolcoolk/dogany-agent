#!/usr/bin/env python3
"""Check that a command wrapper preserves a failing script's useful error."""

import argparse
import subprocess
import sys


def run(argv):
    return subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--producer", required=True)
    parser.add_argument("--caller", required=True)
    args = parser.parse_args()
    producer = run([args.producer])
    if producer.returncode == 0:
        parser.error("producer must fail")
    lines = [line for line in producer.stderr.splitlines() if line.strip()]
    remedies = [line for line in lines if line.startswith("REMEDY: ")]
    causes = [line for line in lines if not line.startswith("REMEDY: ")]
    if len(remedies) != 1 or not causes:
        print("producer must write cause and one REMEDY: line to stderr",
              file=sys.stderr)
        return 2
    caller = run([args.caller, args.producer])
    output = caller.stdout + caller.stderr
    missing = [line for line in causes + remedies if line not in output]
    if caller.returncode != producer.returncode or missing:
        print("caller collapses script failure: keep exit code, cause, and REMEDY",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
