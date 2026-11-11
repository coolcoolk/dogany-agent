#!/usr/bin/env python3
# test-lane: fast    # measured 0.04s (2026-09-16, DGN-1509 backup-tests dispatch)
"""Unit tests for weekly_eval.agent_exists / reap_orphans (DGN-1480, existence
check rewritten by DGN-1505: recorded agent_path, not a sibling-dir guess).

Pure synthetic data, tmp dirs only -- no live files touched. Run:
python3 test_reap.py

DGN-1267 note: same shape as test_policy.py in this dir -- see its marker
comment. Standalone script, no pytest test_* functions, needs the SUITE-class
marker or the whole tests/ dir mis-routes to PYTEST class and collects 0.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "scripts"))
import weekly_eval as we  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got} want={want}")
    if not ok:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as sandbox:
    # Two DIFFERENT roots, like the live estate (~/.dogany/agents/ vs
    # a second operator-chosen root) -- proves agent_exists no longer assumes the
    # minted agent sits next to this instance's own directory.
    root_a = os.path.join(sandbox, "roots-a")
    root_b = os.path.join(sandbox, "roots-b")
    real_in_a = os.path.join(root_a, "worg")
    real_in_b = os.path.join(root_b, "kkari")
    os.makedirs(real_in_a)
    os.makedirs(real_in_b)

    check("agent_exists true for a recorded path that is present",
          we.agent_exists({"dedicated_agent": "worg", "agent_path": real_in_a}), True)
    check("agent_exists true for a recorded path under a DIFFERENT root "
          "(DGN-1505: no sibling assumption)",
          we.agent_exists({"dedicated_agent": "kkari", "agent_path": real_in_b}), True)
    check("agent_exists false for a recorded path that is gone",
          we.agent_exists({"dedicated_agent": "ghost",
                            "agent_path": os.path.join(root_a, "ghost")}), False)
    check("agent_exists true (never reaped) for a legacy record with no agent_path",
          we.agent_exists({"dedicated_agent": "legacy"}), True)

    state = {
        "domains": {
            "health": {"recommendations": [], "declines": [],
                       "dedicated_agent": "worg", "minted_at": "2026-09-01",
                       "agent_path": real_in_a},
            "study_": {"recommendations": [], "declines": [],
                       "dedicated_agent": "kkari", "minted_at": "2026-09-02",
                       "agent_path": real_in_b},
            "finance": {"recommendations": ["2026-08-01"], "declines": [],
                        "dedicated_agent": "ghost", "minted_at": "2026-08-01",
                        "agent_path": os.path.join(root_a, "ghost")},
            "travel": {"recommendations": [], "declines": [],
                       "dedicated_agent": "legacy-no-path", "minted_at": "2026-01-01"},
            "study": {"recommendations": [], "declines": [], "dedicated_agent": None},
        }
    }
    healed = we.reap_orphans(state)
    check("only the recorded-and-gone path is healed",
          sorted(healed), [("finance", "ghost")])
    check("live agent under root A survives",
          state["domains"]["health"]["dedicated_agent"], "worg")
    check("live agent under root B (different root) survives",
          state["domains"]["study_"]["dedicated_agent"], "kkari")
    check("live agent's minted_at survives",
          state["domains"]["health"].get("minted_at"), "2026-09-01")
    check("legacy no-path record is NOT reaped (safe side, DGN-1505)",
          state["domains"]["travel"]["dedicated_agent"], "legacy-no-path")
    check("legacy no-path record's minted_at survives",
          state["domains"]["travel"].get("minted_at"), "2026-01-01")
    check("orphan's dedicated_agent cleared",
          state["domains"]["finance"]["dedicated_agent"], None)
    check("orphan's minted_at key removed",
          "minted_at" in state["domains"]["finance"], False)
    check("orphan's agent_path key removed",
          "agent_path" in state["domains"]["finance"], False)
    check("orphan's recommendations history untouched",
          state["domains"]["finance"]["recommendations"], ["2026-08-01"])
    check("domain with no dedicated_agent is untouched",
          state["domains"]["study"],
          {"recommendations": [], "declines": [], "dedicated_agent": None})

    # Second pass over an already-healed state is a no-op (idempotent).
    healed2 = we.reap_orphans(state)
    check("re-running reap on healed state finds nothing new", healed2, [])

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("RESULT: all reap tests passed")
