#!/usr/bin/env python3
# test-lane: manual devkit-owned suite; run against extracted-devkit/framework
"""Dispatch runtime choice by what would be lost (owner 2026-10-09 20:09).

"There is no fixed priority -- the priority of a resource depends on the
situation": every auto run ranks the pools that cleared their gates by the
capacity each would lose at the current burn rate (lib/runtime_loss.py) and
the same run lands on different pools as the state changes. Offline: fixture
usage, a temp ledger, stubbed quota/hook/key reads -- no workers, live reads
or network. The cloud-eligible half (codex vs cloud-first, cloud at cap)
lives in test-dgn1726-cloud-first.sh.
"""
import calendar
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

ROOT = Path(__file__).resolve().parents[2]
ROUTINES = ROOT / 'routines'
sys.path.insert(0, str(ROUTINES / 'lib'))
import runtime_loss as rl  # noqa: E402

H = 3600
D = 86400
END = calendar.timegm((2026, 10, 30, 12, 0, 0))    # last days of the month


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(epoch))


def day(now, offset_days):
    return time.strftime('%Y-%m-%d', time.localtime(now + offset_days * D))


def snapshot(u5, u7, now, r7=3 * D):
    return json.dumps({'usage': {'five_hour_util': u5, 'five_hour_resets_at': iso(now + 2 * H),
                                 'seven_day_util': u7, 'seven_day_resets_at': iso(now + r7),
                                 'fetched_at': now}, 'history': []})


class Rank(unittest.TestCase):
    def test_pick_tie_low_and_line(self):
        pick, verdict, line = rl.rank(
            {'codex': (31.0, '31% (3 passes, first 10-23)', 'cx'),
             'claude-code': (18.0, '18%', 'cl'), 'cloud': (9.0, '9%', 'cd'),
             'claude-api': (4.0, '$4', 'ap')}, [])
        self.assertEqual((pick, verdict), ('codex', 'loss_score_pick'))
        self.assertTrue(line.startswith(
            'loss: codex 31% (3 passes, first 10-23) > claude 18% > cloud 9% > api $4 -> codex'),
            line)
        pick, verdict, line = rl.rank({'codex': (31.0, '31%', ''),
                                       'claude-code': (28.0, '28%', '')}, [])
        self.assertEqual((pick, verdict), ('fallback', 'loss_tie'))
        self.assertIn('-> fallback (tie: codex 31% vs claude 28%)', line)
        pick, verdict, line = rl.rank({'codex': (6.0, '6%', ''), 'cloud': (None, 'n/a', '')}, [])
        self.assertEqual((pick, verdict), ('fallback', 'loss_low'))
        _, _, line = rl.rank({'claude-code': (40.0, '40%', '')},
                             [('claude-api', 'run needs connectors')])
        self.assertIn('-> claude; vetoed: api (run needs connectors)', line)

    def test_claude_usage_gate_vetoes(self):
        now = time.time()
        u = json.loads(snapshot(92, 40, now))['usage']
        self.assertIn('usage gate: 5h 92%', rl.claude_blocked(u, [], now))
        self.assertEqual(rl.claude_blocked(json.loads(snapshot(10, 40, now))['usage'], [], now), '')


class Selection(unittest.TestCase):
    """Through select_default_runtime(), same harness as the DGN-1963 suite."""

    def assertVetoed(self, entry, detail):
        """`entry` is one item of the rank line's `vetoed:` list, wherever it
        sits (DGN-2014 names codex-cloud ahead of it when the run is not
        cloud-eligible)."""
        self.assertRegex(detail, r'vetoed: (?:[a-z-]+ \([^()]*\), )*' + re.escape(entry))

    def run_case(self, *, claude, codex_used=60, codex_reset=int(3.5 * D), claude_pass='',
                 budget='', spent=0.0, now_pin=None, hooks_rc=0, wt_slug='', extra_env=None,
                 codex_pass='', codex_rc=0, refused=False):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, 'ledger.jsonl')
            month = time.strftime('%Y-%m', time.gmtime(now_pin or time.time()))
            Path(ledger).write_text(json.dumps({'ts': 0, 'month': month, 'runid': 'r',
                                                'model': 'opus', 'cost_usd': spent}) + '\n')
            env = dict(os.environ, DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='', DOGANY_DISPATCH_MACHINE_PRESSURE='off',
                       TEST_ROOT=str(ROOT), TEST_TMP=tmp, HOME=tmp, PLAN='',
                       DOGANY_SECRET_BACKEND='file', DOGANY_SECRET_DIR=os.path.join(tmp, 'secrets'),
                       API_CREDIT_MONTHLY_USD=budget, RESET_PASSES_CLAUDE=claude_pass,
                       RESET_PASSES_CODEX=codex_pass)
            # A temp HOME alone does not isolate the macOS keychain.
            # The selector must never reconcile from a host credential.
            env.pop('ANTHROPIC_API_KEY_FILE', None)
            env.pop('ANTHROPIC_ADMIN_KEY_FILE', None)
            env.pop('DOGANY_DISPATCH_NEEDS_CONNECTORS', None)
            if now_pin:
                env['DOGANY_API_POOL_NOW'] = str(now_pin)
            env.update(extra_env or {})
            snap = "printf '%s' '" + claude + "'"
            script = f'''source "$TEST_ROOT/routines/dispatch-detached.sh"
DECISION_DIR="$TEST_TMP/decisions"
DISPATCH_DIR="$TEST_TMP/dispatch"
ACTIVE_COUNT_FILE="$TEST_TMP/dispatch/active-count"
API_CREDIT_LEDGER="{ledger}"
API_CREDIT_EXHAUSTED_MARKER="$TEST_TMP/exhausted.json"
API_CREDIT_RECONCILE_STATE="$TEST_TMP/reconcile.json"
API_CREDIT_INFLIGHT_DIR="$TEST_TMP/inflight"
RESET_PASS_PROMPT_STAMP="$TEST_TMP/stamp"
PUSH_SH=/usr/bin/true
LABEL=test
WT_REPO=/fixture/estate
MODEL=opus; BOTTLENECK=coverage; MODEL_SOURCE=explicit
REQUESTED_RUNTIME=auto
WT_SLUG={wt_slug}
api_key_present() {{ return 0; }}
# External balance evidence is payable; the fixture pins it available.
api_pool_probe() {{ API_POOL_PROBE_NOTE='api: balance available (fixture)'; return 0; }}
claude_quota_remaining() {{ echo '80 70'; }}
claude_usage_snapshot() {{ {snap}; }}
codex_quota_gate() {{ CODEX_USED_PERCENT={codex_used}; CODEX_RESETS_AT=$(( $(date +%s) + {codex_reset} )); CODEX_QUOTA_NOTE='codex fixture used={codex_used}'; return {codex_rc}; }}
codex_hook_gate() {{ CODEX_HOOK_NOTE='hooks-fixture@selection: rc {hooks_rc}'; return {hooks_rc}; }}
select_default_runtime
echo "MODEL=$MODEL CODEX_MODEL=$CODEX_MODEL"'''
            r = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
            if refused:
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                return None, r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            recs = list(Path(tmp, 'decisions').glob('*.json'))
            self.assertEqual(len(recs), 1, r.stdout + r.stderr)
            return json.loads(recs[0].read_text()), r.stdout

    def test_same_run_follows_the_claude_pass(self):
        now = time.time()
        # Claude 7d 40% with 3 days left (30 points unused), codex 60% due in
        # 10h (~36 unused): codex loses more -- the measured pick agrees.
        doc, _ = self.run_case(claude=snapshot(10, 40, now), codex_reset=10 * H)
        self.assertEqual(doc['selected_runtime'], 'codex', doc['selection_detail'])
        self.assertEqual(doc['selection_reason'], 'read_quota_verified')
        self.assertIn('loss: codex 36% > claude 30% -> codex', doc['selection_detail'])
        # Same run, Claude now holds a full pass expiring tomorrow that the
        # burn never reaches: the subscription would lose it whole.
        doc, out = self.run_case(claude=snapshot(10, 40, now), codex_reset=10 * H,
                                 claude_pass=f'1|{day(now, 1)}:full')
        self.assertEqual(doc['selected_runtime'], 'claude-code', doc['selection_detail'])
        self.assertEqual(doc['selection_reason'], 'loss_score_pick')
        self.assertIn('loss: claude 130% (1 pass, first ', doc['selection_detail'])
        self.assertIn('loss_over=codex/read_quota_verified', doc['selection_detail'])
        self.assertIn('MODEL=opus CODEX_MODEL=', out)    # the codex tier does not leak
        self.assertIn('[dispatch] runtime selection: claude-code (loss_score_pick;', out)

    def test_usage_gate_still_vetoes_the_pass(self):
        now = time.time()
        doc, _ = self.run_case(claude=snapshot(92, 40, now), codex_reset=10 * H,
                               claude_pass=f'1|{day(now, 1)}:full')
        self.assertEqual(doc['selected_runtime'], 'codex', doc['selection_detail'])
        self.assertVetoed('claude (usage gate: 5h 92% >= 90%)', doc['selection_detail'])

    def test_month_end_api_and_the_connector_veto(self):
        now = time.time()
        # Both subscriptions on pace (nothing lost); $60 of $100 spent with a
        # day and a half left -> ~$37 lost at the month end.
        doc, _ = self.run_case(claude=snapshot(10, 60, now), budget='100', spent=60.0,
                               now_pin=END)
        self.assertEqual(doc['selected_runtime'], 'claude-api', doc['selection_detail'])
        self.assertIn('loss: api $3', doc['selection_detail'])
        self.assertIn('-> api', doc['selection_detail'])
        # The same run naming a connector tool never goes to claude-api.
        doc, _ = self.run_case(claude=snapshot(10, 60, now), budget='100', spent=60.0,
                               now_pin=END, extra_env={'DOGANY_DISPATCH_NEEDS_CONNECTORS': '1'})
        self.assertNotEqual(doc['selected_runtime'], 'claude-api', doc['selection_detail'])
        self.assertVetoed('api (run needs connectors)', doc['selection_detail'])
        self.assertIn('api_pool=api_connectors_needed', doc['selection_detail'])

    def test_nothing_at_risk_falls_back(self):
        now = time.time()
        doc, _ = self.run_case(claude=snapshot(10, 60, now))
        self.assertEqual(doc['selection_reason'], 'claude_headroom_on_pace', doc['selection_detail'])
        self.assertIn('-> fallback (top 0% < 10%)', doc['selection_detail'])

    def test_dgn1991_claude_live_reserve_sends_workers_to_codex(self):
        # The 2026-10-11 10:35 line: claude 7d 92% with 4d0h to reset and one
        # pass (exp 10-23 = 12 days out); codex 77% with 3d8h left and three
        # passes (10-23 / 10-30 / 11-07 = 12 / 19 / 27 days out).
        now = time.time()
        doc, out = self.run_case(
            claude=snapshot(30, 92, now, r7=4 * D), codex_used=77, codex_reset=80 * H,
            claude_pass=f'1|{day(now, 12)}',
            codex_pass=f'3|{day(now, 12)},{day(now, 19)},{day(now, 27)}')
        detail = doc['selection_detail']
        self.assertEqual(doc['selected_runtime'], 'codex', detail)
        self.assertNotIn('claude-code', doc['selected_runtime'])
        # DGN-2293: worker pacing refuses this seat before the live-reserve
        # loss estimate. Held passes cannot waive a detached worker's pace.
        self.assertRegex(detail, r'vetoed: claude \(weekly pace -[0-9.]+\)')
        # The headroom fallback agrees: the pass no longer reads "claude open".
        self.assertIn('headroom=claude_tight_codex_open', detail)
        self.assertIn('pass held, live reserve short', detail)
        self.assertIn('[dispatch] runtime selection: codex (', out)
        # Same Claude window, one more pass in hand: the live reserve is
        # covered, the pass is spendable budget again (no veto).
        doc, _ = self.run_case(
            claude=snapshot(30, 92, now, r7=4 * D), codex_used=77, codex_reset=80 * H,
            claude_pass=f'2|{day(now, 12)},{day(now, 19)}',
            codex_pass=f'3|{day(now, 12)},{day(now, 19)},{day(now, 27)}')
        self.assertNotIn('Claude live reserve', doc['selection_detail'])

    def test_dgn1991_claude_only_install_unchanged(self):
        # No codex lane to take the run (quota read refused / not installed):
        # the held pass still plans Claude as before, no live-reserve veto.
        now = time.time()
        doc, _ = self.run_case(claude=snapshot(30, 92, now, r7=4 * D), codex_rc=1,
                               claude_pass=f'1|{day(now, 12)}')
        detail = doc['selection_detail']
        self.assertEqual(doc['selected_runtime'], 'claude-code', detail)
        self.assertNotIn('live reserve', detail)
        self.assertIn('[mode=near_spent]', detail)

    def test_write_run_codex_hooks_veto(self):
        now = time.time()
        # DGN-2180: hooks untrusted never moves to the other subscription either;
        # the selection refuses (rc 2, the caller queues) instead of picking claude.
        _, out = self.run_case(claude=snapshot(10, 60, now), codex_used=40, codex_reset=10 * H,
                               wt_slug='w', hooks_rc=2, refused=True)
        self.assertIn('safety_never_moves', out)


if __name__ == '__main__':
    unittest.main()
