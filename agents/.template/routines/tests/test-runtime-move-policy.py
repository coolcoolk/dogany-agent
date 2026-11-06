#!/usr/bin/env python3
# test-lane: fast
# test-component: runtime-selector
# test-tier: 0
# test-prereq: subprocess, tmp-files
# test-new-component: the move policy is a pure module (lib/runtime_policy.py) plus a mutation harness over a copied dispatcher tree; test-dgn1592-runtime-selection.py drives only select_default_runtime and cannot host the policy table or per-promise mutants
# promise: a refused run never moves to another subscription; it queues on its preferred pool
# promise: hooks-untrusted never moves anywhere, Claude included, on or off pace
# promise: a move into a paced main pool needs the account under pace
"""DGN-2180 S1: a refused run never moves to Claude without the pace check.

Owner 2026-10-19 (dec-497): Claude is the main chat model -- background work
may use it only under the elapsed-time pace line; the other account resets
often, so queue on it instead of waiting or walking away. Observed 08:07 and
11:18: a run whose preferred pool (codex) only had a queue ahead fell back to
claude-code while Claude was off pace.

Locks (each one is also mutated below -- the suite must go red):
  walk        slot_fallback_walk re-checks the move policy: 08:07 and 11:18
              replay rows queue on codex, 08:53 still picks codex-cloud;
  pair        the explicit-runtime pair never crosses subscriptions;
  quota       codex quota refusal does not switch to claude-code, and a Claude
              quota refusal does not fall back to codex (claude_to_codex_fallback);
  hooks       hooks-untrusted (safety) moves nowhere -- also at the initial
              selection, where it used to land on Claude while Claude was off pace;
  cloud seat  the same account's cloud seat needs the pace check once cloud
              credit is spent, and is exempt while credit remains;
  mirror      the same table with traits swapped (codex = main) mirrors.

Offline: fixture usage reads, stubbed slot/gate functions; no workers, no
network.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / 'routines'
TRAITS = ROUTINES / 'lib' / 'runtime-traits.tsv'
D = 86400
NOW = time.time()


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(epoch))


def resets_for(elapsed_pct):
    """A 7d window that is elapsed_pct through, as resets_at (epoch)."""
    return NOW + (1 - elapsed_pct / 100.0) * 7 * D


def load_policy(path):
    spec = importlib.util.spec_from_file_location('_rp', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def swapped_traits(text):
    """Codex becomes the main pool, Claude the background one."""
    out = []
    for line in text.splitlines():
        cols = line.split('\t')
        if len(cols) == 7 and cols[0] in ('claude-code', 'cloud', 'claude-api', 'codex', 'codex-cloud'):
            if cols[0] in ('codex', 'codex-cloud'):
                cols[3] = 'main'
            else:
                cols[3] = 'background'
            line = '\t'.join(cols)
        out.append(line)
    return '\n'.join(out) + '\n'


# --- pure policy ---------------------------------------------------------

def policy_table(rp, traits_text):
    """-> [(name, got, want)] over the pure decision."""
    t = rp.parse_traits(traits_text)
    main_acct = next(p for p, r in t.items() if r['role'] == 'main' and not r['credit_funded'])
    bg_acct = next(p for p, r in t.items() if r['role'] == 'background' and not r['credit_funded']
                   and r['account'] != t[main_acct]['account'])
    bg_cloud = next(p for p, r in t.items() if r['account'] == t[bg_acct]['account'] and r['seat'] == 'cloud')
    main_cloud = next(p for p, r in t.items() if r['account'] == t[main_acct]['account']
                      and r['seat'] == 'cloud')
    allowed = lambda *a, **k: rp.may_move(t, *a, **k)[0]
    return [
        # queue ahead / lane cap / machine / reservation: same verdict
        ('bg->main off pace', allowed(bg_acct, main_acct, 'capacity', pace=-12.0, elapsed=90.0), False),
        ('bg->main on pace (still another subscription)',
         allowed(bg_acct, main_acct, 'capacity', pace=5.0, elapsed=50.0), False),
        ('bg->main unknown pace', allowed(bg_acct, main_acct, 'quota', pace=None, elapsed=50.0), False),
        ('main->bg (another subscription)', allowed(main_acct, bg_acct, 'capacity', pace=0.0, elapsed=50.0), False),
        ('bg -> own cloud seat', allowed(bg_acct, bg_cloud, 'capacity', pace=-50.0, elapsed=50.0), True),
        ('safety -> own cloud seat', allowed(bg_acct, bg_cloud, 'safety', pace=0.0, elapsed=50.0), False),
        ('safety -> main', allowed(bg_acct, main_acct, 'safety', pace=50.0, elapsed=50.0), False),
        ('main -> own cloud seat, far off pace', allowed(main_acct, main_cloud, 'capacity', pace=-50.0, elapsed=50.0),
         t[main_cloud]['credit_funded']),
        ('credit spent, off pace', allowed(main_acct, main_cloud, 'capacity', pace=-12.0, elapsed=90.0,
                                            no_credit=(main_cloud,)), False),
        ('credit left, off pace', allowed(main_acct, main_cloud, 'capacity', pace=-12.0, elapsed=90.0),
         t[main_cloud]['credit_funded']),
        ('credit spent, unknown pace', allowed(main_acct, main_cloud, 'capacity', pace=None, elapsed=50.0,
                                                no_credit=(main_cloud,)), False),
        ('credit spent, under pace', allowed(main_acct, main_cloud, 'capacity', pace=-3.0, elapsed=50.0,
                                              no_credit=(main_cloud,)), True),
        ('api credit pool is exempt', allowed(bg_acct, 'claude-api', 'capacity') if 'claude-api' in t
         else True, True),
        ('unknown pool fails closed', allowed(bg_acct, 'nope', 'capacity'), False),
    ]


# --- dispatcher wiring ---------------------------------------------------

PRELUDE = r'''set --
source "$TEST_ROOT/routines/dispatch-detached.sh"
DISPATCH_DIR="$TEST_TMP/dispatch"; DECISION_DIR="$TEST_TMP/decisions"
mkdir -p "$DISPATCH_DIR" "$DECISION_DIR"
USAGE_GATE=/usr/bin/true
RUNTIME_TRAITS_TSV="${TRAITS_FILE:-$RUNTIME_TRAITS_TSV}"
record_runtime_decision() { return 0; }
read_active_count() { echo 0; }
active_count_incr() { return 0; }
active_count_decr() { return 0; }
slot_candidate_gate() { SLOT_CAND_NOTE=""; return 0; }   # hard limits are not under test
fallback_tier_eligible() { return 0; }
expiry_flag_active() { [[ -n "${STUB_CLOUD_EXHAUSTED:-}" ]]; }
claude_usage_snapshot() {
  printf '{"usage":{"five_hour_util":10,"five_hour_resets_at":"","seven_day_util":%s,"seven_day_resets_at":%s}}' \
    "$CLAUDE_USED" "$CLAUDE_RESETS"
}
CODEX_USED_PERCENT="${CODEX_USED:-40}"; CODEX_RESETS_AT="${CODEX_RESETS:-}"
MAX_ACTIVE=3; DYNAMIC_CAP_NOTE=fixture; DYNAMIC_CAP_VALUE=3
BOTTLENECK=coverage; MODEL=opus; REQUESTED_MODEL=opus; LOCAL_WHY=""
'''


def run_bash(root, body, env):
    full = dict(os.environ, DISPATCH_TEST='1', TEST_ROOT=str(root))
    full.update(env)
    return subprocess.run(['bash', '-c', PRELUDE + body], env=full, text=True,
                          capture_output=True, cwd=full['TEST_TMP'])


def walk_case(root, tmp, *, runtime, loss_order, used, elapsed, traits=None, extra_env=None,
              cloud_exhausted=False, body_extra=''):
    env = {'TEST_TMP': tmp, 'CLAUDE_USED': str(used), 'CLAUDE_RESETS': json.dumps(iso(resets_for(elapsed))),
           'CODEX_USED': str(used), 'CODEX_RESETS': str(int(resets_for(elapsed)))}
    if traits:
        env['TRAITS_FILE'] = traits
    if cloud_exhausted:
        env['STUB_CLOUD_EXHAUSTED'] = '1'
    env.update(extra_env or {})
    body = f'''
RUNTIME={runtime}; LOSS_ORDER="{loss_order}"; LOSS_CLOUD_OK=1; CFC_TOP=/x; RUNTIME_FALLBACK_DIRECTION=""
{body_extra}
rc=0; slot_fallback_walk || rc=$?
echo "RC=$rc RUNTIME=$RUNTIME"
echo "NOTE=$SLOT_WALK_NOTE"
'''
    r = run_bash(root, body, env)
    return r.returncode, r.stdout + r.stderr


def switch_case(root, tmp, *, cls, used, elapsed, traits=None):
    env = {'TEST_TMP': tmp, 'CLAUDE_USED': str(used), 'CLAUDE_RESETS': json.dumps(iso(resets_for(elapsed)))}
    if traits:
        env['TRAITS_FILE'] = traits
    body = f'''
RUNTIME=codex; RUNTIME_FALLBACK_DIRECTION=""
rc=0; codex_to_claude_switch fixture {cls} || rc=$?
echo "RC=$rc RUNTIME=$RUNTIME REASON=$FALLBACK_BLOCK_REASON"
'''
    r = run_bash(root, body, env)
    return r.stdout + r.stderr


def claude_refusal_case(root, tmp, *, used, elapsed):
    """Claude usage-gate refused (quota); codex healthy and hooks verified."""
    env = {'TEST_TMP': tmp, 'CLAUDE_USED': str(used), 'CLAUDE_RESETS': json.dumps(iso(resets_for(elapsed))),
           'CODEX_USED': '30', 'CODEX_RESETS': str(int(resets_for(elapsed)))}
    body = '''
RUNTIME=claude-code; RUNTIME_FALLBACK_DIRECTION=""; WT_SLUG=""
codex_quota_gate() { CODEX_QUOTA_NOTE=fixture; return 0; }
rc=0; claude_to_codex_fallback "fixture quota refusal" || rc=$?
echo "RC=$rc RUNTIME=$RUNTIME DIR=[$RUNTIME_FALLBACK_DIRECTION] WHY=$FALLBACK_BLOCK_REASON"
'''
    r = run_bash(root, body, env)
    return r.stdout + r.stderr


def hooks_selection_case(root, tmp, *, used, elapsed, codex_hooks):
    """Initial auto selection: codex quota open, write work, hook gate verdict."""
    env = {'TEST_TMP': tmp, 'CLAUDE_USED': str(used), 'CLAUDE_RESETS': json.dumps(iso(resets_for(elapsed))),
           'CODEX_USED': '30', 'CODEX_RESETS': str(int(resets_for(elapsed)))}
    body = f'''
REQUESTED_RUNTIME=auto; CLOUD_FIRST_SELECTED=0; WT_SLUG=slug; WT_REPO=/fixture/repo; LOSS_DONE=1
LOSS_PICK=fallback; LOSS_LINE=fixture; CLOUD_FIRST_NOTE=""; NEEDS_CONNECTORS=0
tier_binding_lookup() {{ echo opus; }}
codex_quota_gate() {{ CODEX_QUOTA_NOTE=fixture; return 0; }}
runtime_headroom_verdict() {{ printf 'codex\tcodex_open\tfixture\n'; }}
codex_hook_gate() {{ CODEX_HOOK_NOTE=fixture; return {0 if codex_hooks else 2}; }}
needs_connectors_infer() {{ :; }}
api_pool_verdict() {{ printf 'claude-code\tapi_off\tfixture\n'; }}
model_pin_gate() {{ return 0; }}
reset_pass_review() {{ :; }}
rc=0; select_default_runtime || rc=$?
echo "RC=$rc RUNTIME=$RUNTIME REASON=$RUNTIME_SELECTION_REASON"
'''
    r = run_bash(root, body, env)
    return r.stdout + r.stderr


def wiring_checks(root, policy_py=None):
    """-> [(name, ok)] for every dispatcher-side promise."""
    res = []
    with tempfile.TemporaryDirectory() as tmp:
        sw = os.path.join(tmp, 'swapped.tsv')
        Path(sw).write_text(swapped_traits(TRAITS.read_text()))
        # 08:07 -- codex 38% used, Claude 67% vs 55% elapsed, 4 tickets ahead
        _, o = walk_case(root, tmp, runtime='codex', loss_order='codex,claude-code', used=67, elapsed=55)
        res.append(('08:07 queues on codex', 'RC=1 RUNTIME=codex' in o))
        res.append(('08:07 records preferred wait', 'other_subscription:' in o))
        # 11:18 -- codex 47%, Claude 73% vs 57%, 1 ahead
        _, o = walk_case(root, tmp, runtime='codex', loss_order='codex,claude-code', used=73, elapsed=57)
        res.append(('11:18 queues on codex', 'RC=1 RUNTIME=codex' in o))
        # 08:53 -- codex-cloud eligible: still taken
        _, o = walk_case(root, tmp, runtime='codex', loss_order='codex,codex-cloud,claude-code',
                         used=67, elapsed=55)
        res.append(('08:53 picks codex-cloud', 'RC=0 RUNTIME=codex-cloud' in o))
        # Claude well under pace is still another subscription: queue on codex
        _, o = walk_case(root, tmp, runtime='codex', loss_order='codex,claude-code', used=20, elapsed=55)
        res.append(('under-pace claude still not taken from codex', 'RC=1 RUNTIME=codex' in o))
        # explicit-runtime pair (no loss ranking): neither direction crosses
        _, o = walk_case(root, tmp, runtime='codex', loss_order='', used=20, elapsed=55)
        res.append(('pair codex->claude-code stays', 'RC=1 RUNTIME=codex' in o))
        _, o = walk_case(root, tmp, runtime='claude-code', loss_order='', used=20, elapsed=55)
        res.append(('pair claude-code->codex stays', 'RC=1 RUNTIME=claude-code' in o))
        # codex quota refusal / hooks untrusted never switch to claude
        o = switch_case(root, tmp, cls='quota', used=20, elapsed=55)
        res.append(('quota refusal stays', 'RC=2 RUNTIME=codex' in o))
        o = switch_case(root, tmp, cls='safety', used=20, elapsed=55)
        res.append(('hooks untrusted never claude', 'RC=2 RUNTIME=codex' in o))
        # Claude quota refusal: codex is another subscription -- stays refused on Claude
        # (the caller defers), never falls back, on or off pace
        for used in (67, 20):
            o = claude_refusal_case(root, tmp, used=used, elapsed=55)
            res.append((f'claude quota refusal stays (used {used})',
                        'RC=2 RUNTIME=claude-code DIR=[] WHY=runtime_policy_other_subscription' in o))
        # codex hooks untrusted at the initial selection while Claude is off pace
        # (and on pace): never claude-code; the caller queues the run
        for used in (67, 20):
            o = hooks_selection_case(root, tmp, used=used, elapsed=55, codex_hooks=False)
            res.append((f'hooks untrusted selection queues (claude used {used})',
                        'RC=2' in o and 'safety_never_moves' in o))
        o = hooks_selection_case(root, tmp, used=67, elapsed=55, codex_hooks=True)
        res.append(('hooks verified still selects codex', 'RC=0 RUNTIME=codex' in o))
        # the same account's cloud seat: exempt while credit remains ...
        _, o = walk_case(root, tmp, runtime='claude-code', loss_order='claude-code,cloud', used=67, elapsed=55)
        res.append(('cloud seat with credit', 'RC=0 RUNTIME=cloud' in o))
        # ... paced once the cloud credit is spent
        _, o = walk_case(root, tmp, runtime='claude-code', loss_order='claude-code,cloud', used=70,
                         elapsed=55, cloud_exhausted=True)
        res.append(('cloud seat no credit off pace', 'RC=1 RUNTIME=claude-code' in o))
        _, o = walk_case(root, tmp, runtime='claude-code', loss_order='claude-code,cloud', used=40,
                         elapsed=55, cloud_exhausted=True)
        res.append(('cloud seat no credit on pace', 'RC=0 RUNTIME=cloud' in o))
        # mirrored: codex is main, claude the background pool
        _, o = walk_case(root, tmp, runtime='claude-code', loss_order='claude-code,codex', used=67,
                         elapsed=55, traits=sw)
        res.append(('mirror 08:07 queues on claude-code', 'RC=1 RUNTIME=claude-code' in o))
        _, o = walk_case(root, tmp, runtime='claude-code', loss_order='claude-code,codex', used=73,
                         elapsed=57, traits=sw)
        res.append(('mirror 11:18 queues on claude-code', 'RC=1 RUNTIME=claude-code' in o))
        _, o = walk_case(root, tmp, runtime='claude-code', loss_order='claude-code,cloud,codex',
                         used=67, elapsed=55, traits=sw)
        res.append(('mirror 08:53 picks the cloud seat', 'RC=0 RUNTIME=cloud' in o))
        # mirrored: the account whose traits say "main" is the one paced -- codex-cloud
        # (now main, no credit) from the claude background pool is another subscription
        _, o = walk_case(root, tmp, runtime='codex', loss_order='codex,codex-cloud', used=70,
                         elapsed=55, traits=sw)
        res.append(('mirror main cloud seat off pace', 'RC=1 RUNTIME=codex' in o))
        _, o = walk_case(root, tmp, runtime='codex', loss_order='codex,codex-cloud', used=40,
                         elapsed=55, traits=sw)
        res.append(('mirror main cloud seat on pace', 'RC=0 RUNTIME=codex-cloud' in o))
    return res


def make_mutant(tmp, edits, target='routines/dispatch-detached.sh'):
    """A copy of the repo's routines tree (symlinks except the mutated file)."""
    root = Path(tmp) / 'mut'
    (root / 'routines').mkdir(parents=True)
    for p in ROUTINES.iterdir():
        dst = root / 'routines' / p.name
        if p.name in ('dispatch-detached.sh', 'lib'):
            continue
        dst.symlink_to(p)
    lib = root / 'routines' / 'lib'
    lib.mkdir()
    for p in (ROUTINES / 'lib').iterdir():
        (lib / p.name).symlink_to(p)
    for rel, old, new in edits:
        f = root / rel
        src = ROOT / rel
        text = src.read_text() if not f.exists() or f.is_symlink() else f.read_text()
        assert old in text, (rel, old)
        if f.is_symlink() or f.exists():
            f.unlink()
        f.write_text(text.replace(old, new, 1))
    if not (root / 'routines' / 'dispatch-detached.sh').exists():
        (root / 'routines' / 'dispatch-detached.sh').symlink_to(ROUTINES / 'dispatch-detached.sh')
    return root


class PolicyTable(unittest.TestCase):
    def test_weekly_front_wide_band_checkpoints(self):
        rp = load_policy(ROUTINES / 'lib' / 'runtime_policy.py')
        for elapsed, allowed_used in ((14.3, 40.0), (42.9, 60.0), (85.7, 90.0)):
            limit = elapsed + rp.BAND_START * (1 - elapsed / 100.0)
            self.assertTrue(rp.under_pace(elapsed - min(allowed_used, limit), elapsed))
            self.assertTrue(rp.under_pace(elapsed - limit, elapsed))
            self.assertFalse(rp.under_pace(elapsed - limit - 0.1, elapsed))
        self.assertTrue(rp.under_pace(6.8 - 8.0, 6.8))
        self.assertFalse(rp.under_pace(90.0 - 97.0, 90.0))
        self.assertFalse(rp.under_pace(None, 50.0))
        self.assertFalse(rp.under_pace(-1.0, None))

    def test_real_traits(self):
        rp = load_policy(ROUTINES / 'lib' / 'runtime_policy.py')
        for name, got, want in policy_table(rp, TRAITS.read_text()):
            self.assertEqual(got, want, name)

    def test_swapped_traits(self):
        rp = load_policy(ROUTINES / 'lib' / 'runtime_policy.py')
        for name, got, want in policy_table(rp, swapped_traits(TRAITS.read_text())):
            self.assertEqual(got, want, name)

    def test_traits_file_shape(self):
        rp = load_policy(ROUTINES / 'lib' / 'runtime_policy.py')
        t = rp.parse_traits(TRAITS.read_text())
        for pool in ('claude-code', 'cloud', 'claude-api', 'codex', 'codex-cloud'):
            self.assertIn(pool, t)
        self.assertEqual(t['claude-code']['role'], 'main')
        self.assertEqual(t['codex']['role'], 'background')
        self.assertEqual(t['claude-code']['account'], t['cloud']['account'])
        self.assertNotEqual(t['claude-code']['account'], t['codex']['account'])

    def test_policy_names_no_pool(self):
        src = (ROUTINES / 'lib' / 'runtime_policy.py').read_text()
        code = src.split('"""', 2)[2]          # past the docstring
        for word in ('claude', 'codex', 'anthropic', 'openai', 'gpt'):
            self.assertNotIn(word, code.lower())

    def test_pace_cli(self):
        r = subprocess.run([sys.executable, str(ROUTINES / 'lib' / 'runtime_policy.py'), 'pace',
                            '--traits', str(TRAITS), '--pool', 'claude-code', '--used', '67',
                            '--resets-at', str(resets_for(55)), '--now', str(NOW)],
                           text=True, capture_output=True)
        self.assertAlmostEqual(float(r.stdout), -12.0, delta=0.1)


class Wiring(unittest.TestCase):
    def test_replay_rows(self):
        for name, ok in wiring_checks(ROOT):
            with self.subTest(name):
                self.assertTrue(ok, name)


class Mutations(unittest.TestCase):
    """One mutation per promise: each must turn the matching check red."""

    def red_wiring(self, edits):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_mutant(tmp, edits)
            return {n for n, ok in wiring_checks(root) if not ok}

    def red_policy(self, old, new):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'runtime_policy.py'
            src = (ROUTINES / 'lib' / 'runtime_policy.py').read_text()
            self.assertIn(old, src)
            f.write_text(src.replace(old, new, 1))
            rp = load_policy(f)
            return {n for n, got, want in policy_table(rp, TRAITS.read_text()) if got != want}

    def test_walk_without_policy(self):
        red = self.red_wiring([('routines/dispatch-detached.sh',
                                'if ! runtime_move_allowed "$from" "$cand" capacity; then',
                                'if false; then')])
        self.assertTrue({'08:07 queues on codex', '11:18 queues on codex'} <= red, red)

    def test_quota_switch_without_policy(self):
        red = self.red_wiring([('routines/dispatch-detached.sh',
                                'if ! runtime_move_allowed codex claude-code "${2:-quota}"; then',
                                'if false; then')])
        self.assertTrue({'quota refusal stays', 'hooks untrusted never claude'} <= red, red)

    def test_claude_refusal_without_policy(self):
        red = self.red_wiring([('routines/dispatch-detached.sh',
                                'if ! runtime_move_allowed claude-code codex quota; then',
                                'if false; then')])
        self.assertTrue({'claude quota refusal stays (used 67)',
                         'claude quota refusal stays (used 20)'} <= red, red)

    def test_hooks_selection_without_policy(self):
        red = self.red_wiring([('routines/dispatch-detached.sh',
                                'if [[ "$hooks_refused" == 1 ]] && ! runtime_move_allowed codex "$RUNTIME" safety; then',
                                'if false; then')])
        self.assertTrue({'hooks untrusted selection queues (claude used 67)',
                         'hooks untrusted selection queues (claude used 20)'} <= red, red)

    def test_hooks_call_site_class(self):
        # the hooks call site must say "safety": that is what keeps a hooks refusal
        # from moving even where the account rule would allow it
        src = (ROUTINES / 'dispatch-detached.sh').read_text()
        self.assertIn('(repo stage)" safety ||', src)

    def test_cross_subscription_rule(self):
        red = self.red_policy('    if a["account"] != b["account"]:', '    if False:')
        self.assertTrue({'bg->main on pace (still another subscription)',
                         'main->bg (another subscription)'} <= red, red)

    def test_safety_rule(self):
        red = self.red_policy('    if reason_class == SAFETY:', '    if False:')
        self.assertTrue({'safety -> own cloud seat', 'safety -> main'} & red, red)

    def test_pace_rule(self):
        red = self.red_policy('    if under_pace(pace, elapsed, tol):', '    if True:')
        self.assertTrue({'credit spent, off pace', 'credit spent, unknown pace'} <= red, red)

    def test_credit_exemption(self):
        red = self.red_policy('    if b["credit_funded"] and dst not in no_credit:',
                              '    if b["credit_funded"]:')
        self.assertTrue({'credit spent, off pace'} <= red or 'credit spent, unknown pace' in red, red)

    def test_unreadable_account_reads_unknown(self):
        # pace attaches to the shared account read; an unreadable account is
        # "unknown" (never under pace). Mutated to 0 it would pass as on pace.
        mut = ('routines/dispatch-detached.sh',
               '  [[ -n "$used" && -n "$resets" ]] || { echo unknown; return 0; }',
               '  [[ -n "$used" && -n "$resets" ]] || { echo 0; return 0; }')
        probe = ('claude_usage_snapshot() { return 1; }\n'
                 'echo "PACE=$(runtime_account_pace claude-code)"')
        env = {'CLAUDE_USED': '0', 'CLAUDE_RESETS': '""'}
        for edits, mutated in (([], False), ([mut], True)):
            with tempfile.TemporaryDirectory() as tmp:
                root = make_mutant(tmp, edits)
                r = run_bash(root, probe, dict(env, TEST_TMP=tmp))
                self.assertEqual('PACE=0' in r.stdout, mutated, r.stdout + r.stderr)
                self.assertEqual('PACE=unknown' in r.stdout, not mutated, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
