#!/usr/bin/env python3
# test-lane: fast
# test-component: runtime-pace-pick
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-new-component: pace-based runtime pick (runtime_headroom.pace_pick_runtime) has no suite of its own; test-dgn1592-runtime-selection covers the owner-policy pin, a different boundary
"""DGN-2297 (dec-563): the runtime pick weighs cloud credit, Codex cloud and
Codex local by pace; a freshly reset Codex weekly is not starved by credit-first.

Offline fixtures: usage snapshots as arguments, every gate stubbed. The shell
half sources the real select_default_runtime/pace_pick_runtime.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / 'routines'
sys.path.insert(0, str(ROUTINES / 'lib'))
import runtime_headroom as rh  # noqa: E402

NOW = 1_800_000_000.0
D = 86400


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d')


def pick(active=None, pools='cloud-credit,codex-cloud,codex', codex_used=1, codex_elapsed=0,
         credit_used_pct=0, credit_elapsed=0):
    """Pure pace_pick: elapsed/used given in % of each window."""
    cx_reset = NOW + (100 - codex_elapsed) / 100 * 7 * D
    until = NOW + (100 - credit_elapsed) / 100 * 30 * D
    return rh.pace_pick(pools.split(','), active or {}, codex_used, cx_reset, 10080,
                        250 - 250 * credit_used_pct / 100, 250, until, NOW)


class Pure(unittest.TestCase):
    def test_a_fresh_codex_and_credit_on_pace_goes_codex(self):
        rt, verdict, detail = pick(codex_used=1, codex_elapsed=0, credit_used_pct=40, credit_elapsed=40)
        self.assertEqual(rt, 'codex-cloud', detail)
        self.assertIn('tie within', detail)

    def test_b_codex_on_pace_credit_behind_goes_credit(self):
        rt, _, detail = pick(codex_used=50, codex_elapsed=50, credit_used_pct=15, credit_elapsed=63)
        self.assertEqual(rt, 'cloud-credit', detail)

    def test_detail_names_both_pools_pace(self):
        _, _, detail = pick(codex_used=1, credit_used_pct=15, credit_elapsed=63)
        for frag in ('credit used=15%', 'pace=+48', 'codex 7d used=1%', 'pace=-1'):
            self.assertIn(frag, detail)

    def test_c_burst_of_six_uses_all_three_pools(self):
        act = {'cloud-credit': 0, 'codex-cloud': 0, 'codex': 0}
        for _ in range(6):
            rt, _, _ = pick(act, codex_used=10, codex_elapsed=60, credit_used_pct=10, credit_elapsed=60)
            act[rt] += 1
        self.assertTrue(all(v >= 1 for v in act.values()), act)

    def test_codex_local_not_offered_means_next_pool(self):
        rt, _, _ = pick(pools='cloud-credit,codex-cloud', codex_used=1)
        self.assertEqual(rt, 'codex-cloud')
        rt, _, _ = pick(pools='cloud-credit', codex_used=1)
        self.assertEqual(rt, 'cloud-credit')

    def test_cli_round_trip(self):
        out = subprocess.run([sys.executable, str(ROUTINES / 'lib/runtime_headroom.py'), 'pace',
                              '--pools', 'cloud-credit,codex', '--codex-used', '1', '--now', str(NOW),
                              '--codex-resets-at', str(NOW + 7 * D), '--credit-remaining', '125',
                              '--credit-limit', '250', '--credit-until', iso(NOW + 15 * D)],
                             input='', text=True, capture_output=True, check=True).stdout
        self.assertTrue(out.startswith('codex\tpace_codex\t'), out)
        self.assertIn('credit used=50%', out)


def run_shell(runs, codex_used, codex_elapsed, credit_used_pct, credit_elapsed, cloud_ok=1,
              hooks_ok=1, brief='', write=1, credit_usable='0'):
    """Run `runs` auto picks through the real shell selection; active counts persist."""
    now = time.time()
    cx_reset = int(now + (100 - codex_elapsed) / 100 * 7 * D)
    until = iso(now + (100 - credit_elapsed) / 100 * 30 * D)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, 'brief.md').write_text(brief or 'do the thing\n')
        env = {'PATH': os.environ['PATH'], 'HOME': tmp, 'TMPD': tmp,
               'WRAPPER': str(ROUTINES / 'dispatch-detached.sh')}
        code = r'''set --; DISPATCH_TEST=1 source "$WRAPPER"
WORKSPACE="$TMPD"; WT_SLUG=%(slug)s; WT_REPO=/fixture/repo; REPO_CONTAINED=1; LOCAL_WHY=""
REQUESTED_RUNTIME=auto; BOTTLENECK=accuracy; MODEL=sonnet; MODEL_SOURCE=binding
TASK_FILE="$TMPD/brief.md"; ATTACH_BRIEF_FILE=""; CLOUD_FIRST_SELECTED=0; CREDIT_FIRST_SELECTED=1
CREDIT_FIRST_NOTE="cloud_credit_first: fixture"
CREDIT_BALANCE_REMAINING=%(rem)s; CREDIT_BALANCE_LIMIT=250
cloud_credit_until() { printf '%%s' "%(until)s"; }
# Owner 2026-11-02: the credit is offered only while switched on with cap > 0.
# Fixtures model a funded, enabled pool unless credit_usable says otherwise.
cloud_credit_usable() { case "%(cu)s" in 0) return 0 ;; esac; CLOUD_CREDIT_SKIP_NOTE="%(cu)s"; return 1; }
auto_codex_model() { printf 'gpt-fixture\tbinding\n'; }
fallback_tier_eligible() { return 0; }
codex_quota_gate() { CODEX_USED_PERCENT=%(used)s; CODEX_RESETS_AT=%(reset)s; CODEX_WINDOW_MINS=10080; CODEX_QUOTA_NOTE="fixture"; return 0; }
codex_cloud_configured() { return 0; }
codex_cloud_candidate() { [[ %(cloud)s == 1 ]]; }
codex_cloud_convert() { :; }; codex_cloud_validate() { return 0; }
codex_hook_gate() { if [[ %(hooks)s == 1 ]]; then CODEX_HOOK_NOTE=hooks-verified@selection; return 0; fi; CODEX_HOOK_NOTE="hooks-unverified@selection: fixture"; return 1; }
claude_usage_snapshot() { return 1; }
record_runtime_decision() { :; }; model_pin_gate() { return 0; }; reset_pass_review() { :; }
# No associative arrays: macOS /bin/bash is 3.2 (DGN-2297). One var per pool.
ACT_cloud_credit=0; ACT_codex_cloud=0; ACT_codex=0
read_active_count() { local k="ACT_${1:-$RUNTIME}"; k="${k//-/_}"; echo "${!k:-0}"; }
for i in $(seq 1 %(runs)d); do
  RUNTIME=""; RUNTIME_SELECTION_REASON=""; RUNTIME_SELECTION_DETAIL=""; MODEL=sonnet
  select_default_runtime >/dev/null 2>&1
  echo "PICK $RUNTIME $RUNTIME_SELECTION_REASON"
  _k="ACT_${RUNTIME//-/_}"; printf -v "$_k" '%%s' $(( ${!_k:-0} + 1 ))
done
echo "DETAIL $RUNTIME_SELECTION_DETAIL"
''' % dict(slug='slug' if write else '""', rem=250 - 250 * credit_used_pct / 100, until=until,
           used=codex_used, reset=cx_reset, cloud=cloud_ok, hooks=hooks_ok, runs=runs,
           cu=credit_usable)
        p = subprocess.run(['bash', '-c', code], env=env, text=True, capture_output=True, timeout=60)
    picks = [l.split()[1:] for l in p.stdout.splitlines() if l.startswith('PICK ')]
    detail = next((l for l in p.stdout.splitlines() if l.startswith('DETAIL ')), '')
    return picks, detail, p


class Shell(unittest.TestCase):
    def test_a_codex_fresh_credit_on_pace_goes_codex(self):
        picks, detail, p = run_shell(1, 1, 0, 40, 40)
        self.assertEqual(picks[0][0], 'codex-cloud', p.stdout + p.stderr)  # Codex family; cloud spares the Mac
        self.assertIn('pace=', detail)
        self.assertIn('codex 7d used=1%', detail)
        self.assertIn('credit used=40%', detail)

    def test_b_codex_on_pace_credit_behind_goes_credit(self):
        picks, _, p = run_shell(1, 50, 50, 15, 63)
        self.assertEqual(picks[0], ['cloud-credit', 'cloud_credit_first'], p.stdout + p.stderr)

    def test_c_burst_of_six_all_behind_uses_three_pools_suite_never_local(self):
        picks, _, p = run_shell(6, 10, 60, 10, 60)
        used = {x[0] for x in picks}
        self.assertEqual(used, {'cloud-credit', 'codex-cloud', 'codex'}, picks)
        picks, _, p = run_shell(6, 10, 60, 10, 60,
                                brief='Then run bash git-hooks/run-tests.sh for the whole suite.\n')
        self.assertNotIn('codex', {x[0] for x in picks}, picks)
        self.assertTrue({x[0] for x in picks} >= {'cloud-credit', 'codex-cloud'}, picks)

    def test_d_missing_hook_evidence_on_write_work_is_not_codex_local(self):
        picks, detail, _ = run_shell(4, 1, 0, 40, 40, cloud_ok=0, hooks_ok=0)
        self.assertEqual({x[0] for x in picks}, {'cloud-credit'}, picks)
        self.assertIn('hooks-unverified', detail)
        # read-only needs no hook proof
        picks, _, _ = run_shell(1, 1, 0, 40, 40, cloud_ok=0, hooks_ok=0, write=0)
        self.assertEqual(picks[0][0], 'codex', picks)

    def test_e_credit_cap_zero_moves_to_codex_cloud_never_credit(self):
        # Replay of incident rtd-20261102-100436 / -100701: credit $1.67/$250,
        # affordable=0 cap=0, codex 7d 90% (ceiling 98%).
        note = 'cap=0, unaffordable (remaining=1.67/250 affordable=0 cap=0)'
        picks, detail, p = run_shell(3, 90, 70, 99, 95, credit_usable=note)
        self.assertEqual({x[0] for x in picks} - {'codex'}, {'codex-cloud'}, p.stdout + p.stderr)
        self.assertNotIn('cloud-credit', {x[0] for x in picks})
        self.assertIn('cloud-credit not offered: cap=0', detail)

    def test_f_credit_off_and_no_codex_goes_subscription(self):
        picks, detail, p = run_shell(2, 1, 0, 40, 40, cloud_ok=0, hooks_ok=0,
                                     credit_usable='pool off (CLOUD_CREDIT_POOL=off)')
        self.assertEqual(picks, [['claude-code', 'no_cloud_pool_usable']] * 2, p.stdout + p.stderr)
        self.assertIn('CLOUD_CREDIT_POOL=off', detail)


if __name__ == '__main__':
    unittest.main()
