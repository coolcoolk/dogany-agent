"""Retired morning question entry and shared keyboard serialization.

FDGN-594 removes the sender. The CLI remains a no-send compatibility entry
with its existing switch/window diagnostics; the sender argument is unused.
"""

import json
import os
import sys

from . import ask as ask_mod
from . import budget

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import machine_signal  # noqa: E402

CALLBACK_MAX_BYTES = 64


def buttons_json(shown):
    """One button per row; callback 'opt:<label>' with 'opt:<i>' fallback,
    same contract as bridge build_option_keyboard (unnumbered labels)."""
    rows = []
    for s in shown:
        cb = "opt:%s" % s["label"]
        if len(cb.encode("utf-8")) > CALLBACK_MAX_BYTES or s["label"].strip().isdigit():
            cb = "opt:%d" % s["slot"]
        rows.append([{"text": s["label"], "callback_data": cb}])
    return json.dumps(rows, ensure_ascii=False)


def run(ctx, sender=None, out=None):
    out = out or sys.stdout
    if not ctx.enabled():
        return 0
    now = ctx.now
    import retro_slot
    if retro_slot.slot_on(ctx):    # DGN-1961: the slot owns the asks (no double ask)
        out.write(machine_signal.signal("WHY_REFUSED", reason="retro-slot") + "\n")
        return 0
    if (ctx.conf("WHY_MORNING_FALLBACK") or "off").strip().lower() != "on":
        out.write(machine_signal.signal("WHY_REFUSED", reason="fallback-off") + "\n")
        return 0
    if budget.in_window(now, ctx.conf("WHY_QUIET")) or not budget.in_window(
            now, ctx.conf("WHY_ASK_MORNING_WINDOW")):
        out.write(machine_signal.signal("WHY_REFUSED", reason="quiet-hours") + "\n")
        return ask_mod.EXIT_BUDGET
    # DGN-2028 AMEND-2 P3.4 (merged design 9): the morning fallback is gated
    # with the retro.  The retro is view-only now and frame asks are never
    # queued; queued concerns return inline on their topic (`gate`), so
    # with the retro off nothing is pushed.
    out.write(machine_signal.signal("WHY_REFUSED", reason="retro-off") + "\n")
    return 0
