#!/usr/bin/env python3
"""Versioned local/SSH-stdio authority endpoint; deployment supplies identity.

Install as a forced SSH command with a deployment-owned descriptor/principal.
No host discovery, network mutation, service or implicit local fallback.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from why_net.authority import Authority


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stdio', action='store_true', required=True)
    parser.add_argument('--descriptor', required=True)
    parser.add_argument('--principal', required=True)
    args = parser.parse_args()
    try:
        engine = Authority(json.loads(Path(args.descriptor).read_text()), args.principal)
        for line in sys.stdin:
            try:
                result = engine.transact(json.loads(line))
            except (ValueError, TypeError):
                result = {'status': 'invalid-proof'}
            print(json.dumps(result), flush=True)
    except (OSError, ValueError, KeyError):
        print(json.dumps({'status': 'unavailable'}), flush=True)


if __name__ == '__main__':
    main()
