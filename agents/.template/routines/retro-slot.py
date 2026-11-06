#!/usr/bin/env python3
"""retro-slot.py -- the framework daily retro slot (DGN-1961).

Verbs:
  tick              one scheduler beat: the evening message once per retro
                    day at/after RETRO_SLOT_TIME, the deferred part once the
                    next morning at/after RETRO_SLOT_MORNING_TIME.
  next [--delay S]  the interactive section's next item (one per turn),
                    started by the Why net's answer hook.

Default OFF (dec-338): with RETRO_SLOT off every verb sends nothing
(`signal::RETRO_SLOT off`) and exits 0.  Contract: lib/retro_slot.py.
"""

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(_HERE, "lib"))

import retro_slot  # noqa: E402
from why_net import budget  # noqa: E402


def main(argv=None, ctx=None, sender=None):
    p = argparse.ArgumentParser(prog="retro-slot.py")
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("tick")
    s = sub.add_parser("next")
    s.add_argument("--delay", type=float, default=0.0)
    try:
        a = p.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit:
        return 2
    if not a.verb:
        p.print_usage(sys.stderr)
        return 2
    ctx = ctx or budget.Ctx()
    if a.verb == "tick":
        retro_slot.tick(ctx, sender=sender)
        return 0
    if a.delay > 0:
        time.sleep(min(a.delay, 120))
    retro_slot.next_item(ctx, sender=sender)
    return 0


if __name__ == "__main__":
    sys.exit(main())
