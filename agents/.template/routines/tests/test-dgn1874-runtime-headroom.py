#!/usr/bin/env python3
# test-lane: fast
"""DGN-1874 (dec-254): Claude-vs-Codex headroom runtime selection.

Offline: fixture usage values only -- no workers, notifications, live quota
RPCs or claude-usage.sh calls. Covers every policy branch both as a pure
function (lib/runtime_headroom.py) and through select_default_runtime().
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / 'routines'
sys.path.insert(0, str(ROUTINES / 'lib'))
import runtime_headroom as rh  # noqa: E402

NOW = 1_800_000_000.0
H = 3600
D = 86400


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(epoch))


def usage(u5, u7, r5=3 * H, r7=3 * D, now=NOW):
    return {'five_hour_util': u5, 'five_hour_resets_at': iso(now + r5),
            'seven_day_util': u7, 'seven_day_resets_at': iso(now + r7),
            'fetched_at': now}


def sample(age, u5, r5=3 * H, now=NOW):
    """A recorded history sample `age` seconds old, same 5h window as usage()."""
    return {'fetched_at': now - age, 'five_hour_util': u5,
            'five_hour_resets_at': iso(now + r5)}


def decide(claude, codex_used=40, codex_reset=4 * D, history=(), now=NOW):
    return rh.decide(claude, list(history), codex_used, now + codex_reset, 10080, now)


class Policy(unittest.TestCase):
    def test_claude_default_when_open_and_on_pace(self):
        rt, verdict, detail = decide(usage(30, 40))
        self.assertEqual((rt, verdict), ('claude-code', 'claude_headroom_on_pace'))
        for frag in ('5h used=30%', 'elapsed=40%', 'pace=+10', '7d used=40%',
                     'codex 7d used=40%', 'claude_eff_pace=+10'):
            self.assertIn(frag, detail)

    def test_effective_claude_is_tighter_window(self):
        # 5h comfortably on pace, 7d 30 points behind -> 7d decides.
        rt, verdict, detail = decide(usage(10, 70, r7=4 * D))
        self.assertEqual((rt, verdict), ('codex', 'claude_tight_codex_open'))
        self.assertIn('claude_eff_pace=-27', detail)
        self.assertIn('off pace', detail)

    def test_velocity_spill_at_half_used(self):
        # Owner case: 5h only 50% used, but 15 points burned in the last 30 min
        # with 3h to reset -> projected 140% -> spill to codex.
        rt, verdict, detail = decide(usage(50, 40), history=[sample(1800, 35)])
        self.assertEqual((rt, verdict), ('codex', 'claude_5h_velocity_spill'))
        self.assertIn('burn=0.50%/min proj5h=140%', detail)
        self.assertIn('5h projection 140% > 95%', detail)

    def test_slow_burn_stays_on_claude(self):
        rt, verdict, detail = decide(usage(50, 40), history=[sample(1800, 48)])
        self.assertEqual((rt, verdict), ('claude-code', 'claude_headroom_on_pace'))
        self.assertIn('proj5h=', detail)

    def test_no_rate_data_falls_back_to_used_percent_and_says_so(self):
        rt, verdict, detail = decide(usage(50, 40))
        self.assertEqual(rt, 'claude-code')
        self.assertIn('burn=n/a', detail)
        self.assertIn('used% only', detail)

    def test_burn_ignores_other_window_and_too_fresh_or_stale_samples(self):
        hist = [sample(1800, 0, r5=-2 * H),   # previous 5h window
                sample(120, 30),              # < 10 min: too fresh
                sample(4 * H, 0)]             # > 90 min: not recent
        self.assertIsNone(rh.burn_rate(usage(50, 40), hist, NOW))
        rt, _, detail = decide(usage(50, 40), history=hist)
        self.assertEqual(rt, 'claude-code')
        self.assertIn('burn=n/a', detail)

    def test_codex_use_it_or_lose_it(self):
        rt, verdict, detail = decide(usage(10, 20), codex_used=60, codex_reset=10 * H)
        self.assertEqual((rt, verdict), ('codex', 'codex_use_it_or_lose_it'))
        self.assertIn('codex 7d resets in 10h0m with 40% unused', detail)

    def test_lose_it_needs_unused_budget(self):
        rt, verdict, _ = decide(usage(10, 20), codex_used=90, codex_reset=10 * H)
        self.assertEqual((rt, verdict), ('claude-code', 'claude_headroom_on_pace'))

    def test_lose_it_needs_near_reset(self):
        rt, _, _ = decide(usage(10, 20), codex_used=60, codex_reset=30 * H)
        self.assertEqual(rt, 'claude-code')

    def test_claude_window_at_85_is_not_open(self):
        rt, verdict, detail = decide(usage(10, 86, r7=12 * H))
        self.assertEqual((rt, verdict), ('codex', 'claude_tight_codex_open'))
        self.assertIn('window >= 85%', detail)

    def test_both_tight_picks_less_tight(self):
        # Claude 5h 40 points behind; codex only 15 behind -> codex.
        rt, verdict, detail = decide(usage(60, 30, r5=4 * H), codex_used=60,
                                     codex_reset=4 * D)
        self.assertEqual((rt, verdict), ('codex', 'both_tight_less_tight'))
        self.assertIn('score claude=-40 codex=-17 -> codex', detail)
        # Reverse: codex far behind pace, Claude only mildly -> claude.
        rt, verdict, detail = decide(usage(40, 30, r5=4 * H), codex_used=84,
                                     codex_reset=5 * D)
        self.assertEqual((rt, verdict), ('claude-code', 'both_tight_less_tight'))
        self.assertIn('-> claude-code', detail)

    def test_both_tight_claude_at_gate_line_goes_codex(self):
        rt, verdict, _ = decide(usage(92, 60, r5=1 * H), codex_used=88, codex_reset=5 * D)
        self.assertEqual((rt, verdict), ('codex', 'both_tight_claude_blocked'))

    def test_claude_unreadable_is_not_permission(self):
        rt, verdict, detail = decide(None)
        self.assertEqual((rt, verdict), ('codex', 'claude_usage_unavailable'))
        self.assertIn('codex 7d used=40%', detail)

    def test_unknown_reset_is_conservative(self):
        u = usage(40, 40)
        u['five_hour_resets_at'] = ''
        rt, _, detail = decide(u)
        self.assertIn('elapsed=n/a', detail)
        self.assertEqual(rt, 'codex')   # 5h pace = -40 -> off pace

    def test_cli_contract(self):
        def cli(stdin, *extra):
            r = subprocess.run([sys.executable, str(ROUTINES / 'lib/runtime_headroom.py'),
                                '--codex-used', '40', '--codex-resets-at',
                                str(int(NOW + 4 * D)), '--now', str(NOW), *extra],
                               input=stdin, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.rstrip('\n').split('\t')
        out = cli(json.dumps({'usage': usage(30, 40), 'history': []}))
        self.assertEqual(out[:2], ['claude-code', 'claude_headroom_on_pace'])
        self.assertEqual(len(out), 3)
        for bad in ('', 'not json', json.dumps({'usage': {'five_hour_util': 140,
                                                           'seven_day_util': 1}})):
            self.assertEqual(cli(bad)[:2], ['codex', 'claude_usage_unavailable'])


class Selection(unittest.TestCase):
    """select_default_runtime() end to end with fixture reads."""

    def run_select(self, claude, *, codex_used=40, codex_reset=4 * D, qrc=0,
                   write=False, hooks=True, history=(), request='auto',
                   args=(), extra=''):
        now = time.time()
        snap = None
        if claude is not None:
            u = usage(*claude, now=now)
            hist = [sample(age, u5, now=now) for age, u5 in history]
            snap = json.dumps({'usage': u, 'history': hist})
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='',
                       TEST_ROOT=str(ROOT), TEST_TMP=tmp, TEST_SNAP=snap or '')
            script = f'''source "$TEST_ROOT/routines/dispatch-detached.sh"
DECISION_DIR="$TEST_TMP/decisions"
LABEL=test; MODEL=opus; BOTTLENECK=coverage; WT_REPO=/fixture/estate
WT_SLUG={'fixture' if write else ''}
{'' if args else 'REQUESTED_RUNTIME=' + request}
claude_usage_snapshot() {{ [[ -n "$TEST_SNAP" ]] || return 1; printf '%s' "$TEST_SNAP"; }}
codex_quota_gate() {{ CODEX_USED_PERCENT={codex_used}; CODEX_RESETS_AT={int(now + codex_reset)}; CODEX_WINDOW_MINS=10080; CODEX_QUOTA_NOTE='codex fixture used={codex_used}'; return {qrc}; }}
codex_hook_gate() {{ CODEX_HOOK_NOTE='hooks={hooks}'; return {0 if hooks else 2}; }}
select_default_runtime
{extra}
'''
            r = subprocess.run(['bash', '-c', script, 'test', *args], env=env,
                               text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            doc = json.loads(next(Path(tmp, 'decisions').glob('*.json')).read_text())
            return doc

    def check(self, doc, runtime, reason, *frags):
        self.assertEqual(doc['selected_runtime'], runtime, doc['selection_detail'])
        self.assertEqual(doc['selection_reason'], reason, doc['selection_detail'])
        for f in frags:
            self.assertIn(f, doc['selection_detail'])

    def test_claude_default(self):
        doc = self.run_select((30, 40))
        self.check(doc, 'claude-code', 'claude_headroom_on_pace',
                   'headroom=claude_headroom_on_pace', '5h used=30%', 'codex 7d used=40%')
        self.assertEqual(doc['model'], 'opus')

    def test_velocity_spill_read_and_write(self):
        doc = self.run_select((50, 40), history=[(1800, 35)])
        self.check(doc, 'codex', 'read_quota_verified',
                   'headroom=claude_5h_velocity_spill', 'proj5h=140%')
        self.assertEqual(doc['model'], 'fable')
        doc = self.run_select((50, 40), history=[(1800, 35)], write=True)
        self.check(doc, 'codex', 'write_hooks_and_quota_verified',
                   'headroom=claude_5h_velocity_spill', 'hooks=True')

    def test_spill_still_needs_write_hooks(self):
        doc = self.run_select((50, 40), history=[(1800, 35)], write=True, hooks=False)
        self.check(doc, 'claude-code', 'codex_hooks_unavailable',
                   'headroom=claude_5h_velocity_spill')
        self.assertEqual(doc['model'], 'opus')

    def test_no_history_reason_says_used_only(self):
        doc = self.run_select((50, 40))
        self.check(doc, 'claude-code', 'claude_headroom_on_pace', 'used% only')

    def test_use_it_or_lose_it(self):
        doc = self.run_select((10, 20), codex_used=60, codex_reset=10 * H)
        self.check(doc, 'codex', 'read_quota_verified', 'headroom=codex_use_it_or_lose_it')

    def test_claude_tight_codex_open(self):
        doc = self.run_select((10, 88), codex_used=40)
        self.check(doc, 'codex', 'read_quota_verified', 'headroom=claude_tight_codex_open')

    def test_both_tight_less_tight(self):
        doc = self.run_select((60, 30), codex_used=84, codex_reset=5 * D)
        self.check(doc, 'claude-code', 'both_tight_less_tight', 'score claude=-20 codex=-55')

    def test_claude_unreadable_goes_codex(self):
        doc = self.run_select(None)
        self.check(doc, 'codex', 'read_quota_verified', 'headroom=claude_usage_unavailable')

    def test_both_blocked_keeps_existing_refusal_path(self):
        # Codex over its owner ceiling (gate rc 2): no headroom verdict, the
        # pre-existing claude lane (usage-gate -> defer/refuse) owns it.
        doc = self.run_select((95, 97), qrc=2)
        self.check(doc, 'claude-code', 'codex_quota_unavailable')
        self.assertNotIn('headroom=', doc['selection_detail'])

    def test_explicit_runtime_wins_without_probes(self):
        doc = self.run_select((10, 10), args=('--runtime', 'codex'), qrc=99)
        self.check(doc, 'codex', 'explicit_runtime')
        self.assertEqual(doc['selection_source'], 'flag')
        self.assertNotIn('headroom=', doc['selection_detail'])

    def test_policy_crash_falls_back_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='',
                       TEST_ROOT=str(ROOT), TEST_TMP=tmp)
            r = subprocess.run(['bash', '-c', '''source "$TEST_ROOT/routines/dispatch-detached.sh"
RUNTIME_HEADROOM_PY=/nonexistent/runtime_headroom.py
CODEX_USED_PERCENT=40
claude_usage_snapshot() { return 1; }
runtime_headroom_verdict'''], env=env, text=True, capture_output=True)
            self.assertEqual(r.stdout.split('\t')[:2], ['codex', 'headroom_policy_unavailable'])


class Readers(unittest.TestCase):
    def load_gate(self, state_dir):
        spec = importlib.util.spec_from_file_location('_ug_test', ROUTINES / 'usage-gate.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.STATE_DIR = state_dir
        return mod

    def test_get_usage_records_bounded_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = self.load_gate(tmp)
            self.assertEqual(mod.usage_history(), [])
            stale = {'fetched_at': time.time() - 7 * H, 'five_hour_util': 1}
            Path(tmp, mod.HISTORY_NAME).write_text(json.dumps(stale) + '\n')
            vals = iter([10.0, 25.0])

            def fake_fetch():
                return {'five_hour_util': next(vals), 'seven_day_util': 5.0,
                        'five_hour_resets_at': 'r5', 'seven_day_resets_at': 'r7',
                        'fable_percent': None, 'fetched_at': time.time()}
            mod.fetch_usage = fake_fetch
            mod.get_usage()
            os.utime(Path(tmp, mod.CACHE_NAME), (0, 0))   # expire the TTL cache
            time.sleep(0.01)
            mod.get_usage()
            hist = mod.usage_history()
            self.assertEqual([s['five_hour_util'] for s in hist], [10.0, 25.0])
            self.assertEqual(set(hist[0]), {'fetched_at', 'five_hour_util',
                                            'five_hour_resets_at', 'seven_day_util',
                                            'seven_day_resets_at'})

    def test_snapshot_reads_get_usage_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp, 'probe.py')
            probe.write_text("def get_usage():\n    return {'five_hour_util': 12.0, 'seven_day_util': 3.0}\n"
                             "def usage_history():\n    return [{'fetched_at': 1}]\n")
            env = dict(os.environ, DISPATCH_TEST='1', TEST_PROBE=str(probe))
            r = subprocess.run(['bash', '-c', 'source routines/dispatch-detached.sh; '
                                'USAGE_GATE_PY="$TEST_PROBE"; claude_usage_snapshot'],
                               cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            doc = json.loads(r.stdout)
            self.assertEqual(doc['usage']['five_hour_util'], 12.0)
            self.assertEqual(doc['history'], [{'fetched_at': 1}])
            probe.write_text("def get_usage():\n    return None\n")
            r = subprocess.run(['bash', '-c', 'source routines/dispatch-detached.sh; '
                                'USAGE_GATE_PY="$TEST_PROBE"; claude_usage_snapshot'],
                               cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(r.returncode, 1)

    def test_codex_gate_keeps_reset_and_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp, 'probe.py')
            probe.write_text('print(' + repr(json.dumps({
                'ok': True, 'used_percent': 40, 'resets_at': 1800000000,
                'window_duration_mins': 10080})) + ')')
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='',
                       TEST_PROBE=str(probe))
            r = subprocess.run(['bash', '-c', 'source routines/dispatch-detached.sh; '
                                'CODEX_RATELIMIT_SCRIPT="$TEST_PROBE"; codex_quota_gate >/dev/null; '
                                'echo "$CODEX_USED_PERCENT $CODEX_RESETS_AT $CODEX_WINDOW_MINS"'],
                               cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(r.stdout.strip(), '40 1800000000 10080', r.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
