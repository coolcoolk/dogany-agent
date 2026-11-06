#!/usr/bin/env python3
# test-lane: fast    # measured 0.04s (2026-09-16, DGN-1509 backup-tests dispatch)
"""Unit tests for minting_policy (DGN-238 decision 10 discipline).

Pure synthetic data -- no live files touched. Run: python3 test_policy.py

DGN-1267 note: this file matches the PYTEST-class glob (tests/test_*.py) but
carries no pytest test_* functions -- it is a standalone script (module-level
asserts + sys.exit), predating DGN-1267's structural discovery rule. Without
this marker, run-tests.sh would route the whole skills/dogany-mint-agent/tests/ dir
into PYTEST class, where pytest legitimately collects 0 items (QUIET-GREEN
FAIL, DGN-1154). The marker declares this file SUITE-class instead, so it
runs the way its docstring always said: `python3 test_policy.py`.
"""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "scripts"))
import minting_policy as pol  # noqa: E402

TODAY = dt.date(2026, 7, 12)
FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got} want={want}")
    if not ok:
        FAILS.append(name)


def ev(share=0.0, records=0, recs=(), declines=(), dedicated=None,
       silenced=False, today=TODAY):
    ds = {"recommendations": list(recs), "declines": list(declines),
          "dedicated_agent": dedicated}
    if silenced:
        ds["silenced"] = True
    return pol.evaluate_domain("health", {"share": share, "records": records},
                               ds, today)


# --- first recommendation thresholds (>= per spec) ---
check("fresh share 25% fires", ev(share=0.25)["decision"], "recommend")
check("fresh share exactly 20% fires (>=)", ev(share=0.20)["decision"], "recommend")
check("fresh records exactly 30 fires (>=)", ev(records=30)["decision"], "recommend")
check("fresh share 19% + records 29 skips",
      ev(share=0.19, records=29)["reason"], "below-threshold")
check("rec_no is 1 on first fire", ev(share=0.25)["rec_no"], 1)

# --- dedicated agent / silence ---
check("dedicated agent blocks", ev(share=0.9, dedicated="worg")["reason"],
      "dedicated-agent-exists")
check("two declines = permanent silence",
      ev(share=0.9, records=999, recs=["2026-05-01", "2026-06-15"],
         declines=["2026-05-02", "2026-06-16"])["reason"],
      "silenced-permanent")
check("explicit silence blocks", ev(share=0.9, silenced=True)["reason"],
      "silenced-explicit")

# --- cap: max 2 recommendations ever ---
check("2 recs, no declines = cap",
      ev(share=0.9, recs=["2026-05-01", "2026-06-10"])["reason"],
      "max-recommendations-reached")

# --- cooldown: 28 days from last event (decline or rec) ---
check("declined 14d ago = cooldown even at high share",
      ev(share=0.5, recs=["2026-06-28"], declines=["2026-06-28"])["reason"],
      "cooldown")
check("declined exactly 28d ago = eligible lane",
      ev(share=0.5, recs=["2026-06-14"], declines=["2026-06-14"])["decision"],
      "recommend")
check("no-response rec 7d ago = spaced (conservative default)",
      ev(share=0.5, recs=["2026-07-05"])["reason"], "cooldown")

# --- 2nd recommendation: strict > 1.5x threshold ---
old = {"recs": ["2026-06-01"], "declines": ["2026-06-02"]}
check("2nd at share 30% exactly = skip (strict >)",
      ev(share=0.30, recs=old["recs"], declines=old["declines"])["reason"],
      "below-escalated-threshold")
check("2nd at share 31% fires",
      ev(share=0.31, recs=old["recs"], declines=old["declines"])["decision"],
      "recommend")
check("2nd fire has rec_no 2",
      ev(share=0.31, recs=old["recs"], declines=old["declines"])["rec_no"], 2)
check("2nd at records 45 exactly = skip (strict >)",
      ev(records=45, recs=old["recs"], declines=old["declines"])["reason"],
      "below-escalated-threshold")
check("2nd at records 46 fires",
      ev(records=46, recs=old["recs"], declines=old["declines"])["decision"],
      "recommend")

# --- same-day refire (weekly review re-run) ---
check("same-day refire repeats without new slot",
      ev(share=0.5, recs=[TODAY.isoformat()])["reason"], "refire-same-day")

# --- top-1 across domains ---
state = pol.empty_state()
winner, decisions = pol.evaluate_all(
    {"health": {"share": 0.25, "records": 10},
     "finance": {"share": 0.40, "records": 0},
     "study": {"share": 0.05, "records": 0}},
    state, TODAY)
check("top-1 winner = highest share", winner["domain"], "finance")
check("runner-up deferred",
      [d["reason"] for d in decisions if d["domain"] == "health"][0],
      "deferred-top1")
check("below-threshold untouched",
      [d["reason"] for d in decisions if d["domain"] == "study"][0],
      "below-threshold")

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("RESULT: all policy tests passed")
