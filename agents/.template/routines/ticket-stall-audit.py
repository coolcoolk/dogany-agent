#!/usr/bin/env python3
"""SessionStart adapter for the devkit-lane stall audit (DGN-2189).

Not wired by the framework template: the devkit declares its own hook. Emits
nothing unless the devkit is installed (ticket_stall_audit.session_start).
"""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from ticket_stall_audit import session_start


def main():
    out = session_start(json.load(sys.stdin))
    if out:
        print(json.dumps(out, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('[ticket-stall] audit unavailable: %s' % exc, file=sys.stderr)
