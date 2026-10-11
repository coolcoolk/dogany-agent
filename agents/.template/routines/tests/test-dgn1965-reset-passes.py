#!/usr/bin/env python3
# test-lane: fast
"""DGN-1965: expiring reset passes in the dispatch selector.

Offline: fixture usage values, a stub owner push and a temp stamp file -- no
workers, live quota reads or real notifications. Covers lib/reset_passes.py,
the headroom policy with passes, select_default_runtime() (decision line,
once-per-window prompt) and compute_dynamic_cap() (reserve / boost).
Follow-up 2 (dec-340): each pass carries a kind (full / 5h / weekly /
unknown) and is planned on the windows THAT kind covers.
"""
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
import reset_passes as rp  # noqa: E402
import runtime_headroom as rh  # noqa: E402

H = 3600
D = 86400


def iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(epoch))


def day(now, offset_days):
    """Local date string `offset_days` from now (the spec's expiry format)."""
    return time.strftime('%Y-%m-%d', time.localtime(now + offset_days * D))


def usage(u5, u7, now, r5=3 * H, r7=3 * D):
    return {'five_hour_util': u5, 'five_hour_resets_at': iso(now + r5),
            'seven_day_util': u7, 'seven_day_resets_at': iso(now + r7),
            'fetched_at': now}


class Lib(unittest.TestCase):
    NOW = time.time()

    def test_parse(self):
        self.assertIsNone(rp.parse(''))
        self.assertEqual(rp.parse('3|')[0], 3)
        self.assertIsNone(rp.parse('3|')[1])
        self.assertEqual(rp.parse("'1|2026-10-23'")[0], 1)
        for bad in ('x|2026-10-23', '1/2026-10-23', '1|23-10-2026'):
            with self.assertRaises(ValueError):
                rp.parse(bad)

    def test_modes(self):
        far, soon = day(self.NOW, 14), day(self.NOW, 1)
        self.assertEqual(rp.mode('claude', '', 50, None, self.NOW)[0], 'none')
        self.assertEqual(rp.mode('claude', f'0|{far}', 50, None, self.NOW)[0], 'none')
        self.assertEqual(rp.mode('claude', f'1|{day(self.NOW, -2)}', 50, None, self.NOW)[0], 'none')
        self.assertEqual(rp.mode('claude', 'garbage', 50, None, self.NOW)[0], 'none')
        self.assertEqual(rp.mode('claude', f'1|{far}', 50, None, self.NOW)[0], 'held')
        self.assertEqual(rp.mode('codex', '3|', 50, None, self.NOW)[0], 'held')
        m, kind, key, _ = rp.mode('claude', f'1|{far}', 92, 7200 + 40, self.NOW)
        self.assertEqual((m, kind, key), ('near_spent', 'near_spent', 'claude|near_spent|7d|2'))
        m, kind, _, _ = rp.mode('codex', f'3|{soon}', 40, None, self.NOW)
        self.assertEqual((m, kind), ('expiry_boost', ''))      # 24-48h out: boost, no prompt yet
        m, kind, _, _ = rp.mode('codex', f'3|{day(self.NOW, 0)}', 40, None, self.NOW)
        self.assertEqual((m, kind), ('expiry_boost', 'expiring'))
        # Near spent beats the boost: the window is used, ask to redeem.
        self.assertEqual(rp.mode('codex', f'3|{soon}', 95, 1, self.NOW)[0], 'near_spent')

    def test_per_pass_expiries_legacy_forms(self):
        """Older forms (no kind) still read; every such pass is kind unknown."""
        a, b, c = day(self.NOW, 14), day(self.NOW, 21), day(self.NOW, 29)
        U = 'unknown'
        self.assertEqual(rp.parse_passes(f'3|{c},{a},{b}'), [(a, U), (b, U), (c, U)])
        self.assertEqual(rp.parse_passes(f'3|{a}'), [(a, U)] * 3)          # DGN-1965 form
        self.assertEqual(rp.parse_passes('3|'), [(U, U)] * 3)
        self.assertEqual(rp.parse_passes(f'3|{a},unknown'), [(a, U), (U, U), (U, U)])
        self.assertEqual(rp.parse_passes('2|2026-10-23T05:50,UNKNOWN'),
                         [('2026-10-23T05:50', U), (U, U)])
        self.assertEqual(rp.parse_passes('0|'), [])
        for bad in (f'1|{a},{b}', '2|2026-10-23 05:50', '1|soon', f'1|{a}:both', '1|full'):
            with self.assertRaises(ValueError):
                rp.parse_passes(bad)

    def test_kind_parse(self):
        a, b = day(self.NOW, 14), day(self.NOW, 21)
        self.assertEqual(rp.parse_entry(f'{a}:full'), (a, 'full'))
        self.assertEqual(rp.parse_entry(f'{a}:5H'), (a, '5h'))
        self.assertEqual(rp.parse_entry('2026-10-23T05:50:weekly'), ('2026-10-23T05:50', 'weekly'))
        self.assertEqual(rp.parse_entry('2026-10-23T05:50'), ('2026-10-23T05:50', 'unknown'))
        self.assertEqual(rp.parse_entry('unknown:full'), ('unknown', 'full'))
        self.assertEqual(rp.parse_entry(a), (a, 'unknown'))
        self.assertEqual(rp.parse_passes(f'3|{b}:5h,{a}:full,unknown:weekly'),
                         [(a, 'full'), (b, '5h'), ('unknown', 'weekly')])
        self.assertEqual(rp.parse_passes(f'2|{a}:full'), [(a, 'full')] * 2)  # shared entry
        # The count is derived on write, kinds always written.
        self.assertEqual(rp.format_spec([(b, '5h'), ('unknown', 'unknown'), (a, 'full')]),
                         f'3|{a}:full,{b}:5h,unknown:unknown')
        with self.assertRaises(ValueError):
            rp.parse_kind('both')

    def test_planner_per_kind(self):
        """full covers 5h + weekly, 5h only 5h, weekly only weekly, unknown = weekly."""
        far = day(self.NOW, 14)
        want = {'full': ('held', 'held'), '5h': ('held', 'none'),
                'weekly': ('none', 'held'), 'unknown': ('none', 'held')}
        for kind, (m5, m7) in want.items():
            spec = f'1|{far}:{kind}'
            got = (rp.mode('claude', spec, 50, None, self.NOW, '5h')[0],
                   rp.mode('claude', spec, 50, None, self.NOW, '7d')[0])
            self.assertEqual(got, (m5, m7), kind)
            self.assertEqual(rp.mode('codex', spec, 50, None, self.NOW, '5h')[0], m5, kind)
            self.assertEqual(rp.mode('codex', spec, 50, None, self.NOW, '7d')[0], m7, kind)
        # Legacy (no kind) = unknown = weekly: the 5h window is not covered.
        m, _, _, detail = rp.mode('codex', f'1|{far}', 50, None, self.NOW, '5h')
        self.assertEqual(m, 'none')
        self.assertIn('none covers the 5h window', detail)
        self.assertEqual(rp.window_name(300), '5h')
        self.assertEqual(rp.window_name(10080), '7d')
        self.assertEqual(rp.window_name(''), '7d')

    def test_soonest_covering_pass_per_window(self):
        soon, far = day(self.NOW, 1), day(self.NOW, 20)
        spec = f'2|{soon}:5h,{far}:full'
        # 5h window: the 5h pass expiring tomorrow is planned first -> boost.
        self.assertEqual(rp.mode('codex', spec, 40, None, self.NOW, '5h')[0], 'expiry_boost')
        # weekly window: only the full pass covers it, 20 days out -> held.
        self.assertEqual(rp.mode('codex', spec, 40, None, self.NOW, '7d')[0], 'held')

    def test_soonest_pass_planned_first(self):
        soon, far = day(self.NOW, 1), day(self.NOW, 20)
        st = rp.state(f'2|{far},{soon}', self.NOW)
        self.assertEqual(st['count'], 2)
        self.assertEqual(rp.mode('codex', f'2|{far},{soon}', 40, None, self.NOW)[0], 'expiry_boost')
        # The expired one no longer counts; the next one is planned.
        st = rp.state(f'2|{day(self.NOW, -3)},{far}', self.NOW)
        self.assertEqual((st['held'], st['count'], st['expired']), (True, 1, 1))
        self.assertEqual(rp.mode('codex', f'2|{day(self.NOW, -3)},{far}', 40, None, self.NOW)[0], 'held')
        # Exact local time: an expiry 2 hours out prompts, 30 hours out does not.
        hm = time.strftime('%Y-%m-%dT%H:%M', time.localtime(self.NOW + 2 * H))
        self.assertEqual(rp.mode('codex', f'1|{hm}', 40, None, self.NOW)[1], 'expiring')
        hm = time.strftime('%Y-%m-%dT%H:%M', time.localtime(self.NOW + 30 * H))
        self.assertEqual(rp.mode('codex', f'1|{hm}', 40, None, self.NOW)[:2], ('expiry_boost', ''))
        self.assertEqual(rp.mode('codex', f'1|{day(self.NOW, -1)}T00:00', 40, None, self.NOW)[0], 'none')

    def test_review_names_the_nearly_full_window(self):
        far = day(self.NOW, 14)
        u = usage(93, 40, self.NOW)
        # 5h nearly full, a full pass covers it -> one 5h line.
        _, prompts = rp.review(f'1|{far}:full', '', u, '', '', self.NOW)
        self.assertEqual([(p[0], p[1], p[4]) for p in prompts], [('claude', 'near_spent', '5h')])
        # A weekly pass does not restore the 5h window -> no line.
        self.assertEqual(rp.review(f'1|{far}:weekly', '', u, '', '', self.NOW)[1], [])
        # Both windows nearly full under a full pass -> one line, the weekly one.
        _, prompts = rp.review(f'1|{far}:full', '', usage(93, 95, self.NOW), '', '', self.NOW)
        self.assertEqual([(p[1], p[4]) for p in prompts], [('near_spent', '7d')])
        # Codex: the window the quota read reports.
        _, prompts = rp.review('', f'1|{far}:5h', None, 92, self.NOW + H, self.NOW, '5h')
        self.assertEqual([(p[0], p[4]) for p in prompts], [('codex', '5h')])
        self.assertEqual(rp.review('', f'1|{far}:5h', None, 92, self.NOW + H, self.NOW, '7d')[1], [])

    def test_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = os.path.join(tmp, 'sub', 'stamp')
            self.assertEqual(rp.once(stamp, 'claude|near_spent|1'), 0)
            self.assertEqual(rp.once(stamp, 'claude|near_spent|1'), 1)
            self.assertEqual(rp.once(stamp, 'claude|near_spent|2'), 0)


class Slack(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_slack_counts_passes_minus_the_live_reserve(self):
        spec = f'1|{day(self.NOW, 12)}'
        s = rp.slack(spec, 92, self.NOW + 96 * H, self.NOW, '7d', 1.2)
        self.assertAlmostEqual(s['slack'], 8 + 100 - 115.2, places=3)
        self.assertTrue(s['short'])
        self.assertEqual(s['passes'], 1)
        # No pass: never "short" (the no-pass rules stay as they are).
        s = rp.slack('', 92, self.NOW + 96 * H, self.NOW, '7d', 1.2)
        self.assertFalse(s['short'])
        self.assertLess(s['slack'], 0)
        # Worker-only pool: live burn 0, passes count in full.
        s = rp.slack(f'3|{day(self.NOW, 12)},{day(self.NOW, 19)},{day(self.NOW, 27)}',
                     77, self.NOW + 80 * H, self.NOW)
        self.assertEqual((s['slack'], s['text']), (323.0, '23% left + 3 passes -> slack 323%'))
        # Unknown reset: a full window of live burn (the owner's side).
        s = rp.slack(spec, 0, '', self.NOW, '7d', 1.2)
        self.assertAlmostEqual(s['reserve'], 1.2 * 168, places=3)
        self.assertIsNone(rp.slack(spec, '', '', self.NOW))


class Settings(unittest.TestCase):
    """list / add / remove: the user's setting surface writes through these."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name, 'agent.conf')
        self.conf.write_text("PLAN=max\n# RESET_PASSES_CLAUDE='1|2026-10-23'\n"
                             "RESET_PASSES_CODEX='1|2099-01-02'\nAGENT_LNG=ko\n")
        self.now = time.time()

    def cli(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('RESET_PASSES_')}
        return subprocess.run([sys.executable, str(ROUTINES / 'lib' / 'reset_passes.py'), *args,
                               '--conf', str(self.conf)], env=env, text=True, capture_output=True)

    def test_add_remove_keep_the_rest_of_the_file(self):
        r = self.cli('add', '--runtime', 'codex', '--expiry', '2099-01-01T05:50', '--kind', 'full')
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = json.loads(r.stdout)
        self.assertEqual([(p['expiry'], p['kind']) for p in inv['codex']['passes']],
                         [('2099-01-01T05:50', 'full'), ('2099-01-02', 'unknown')])
        self.assertEqual([p['next'] for p in inv['codex']['passes']], [True, False])
        self.assertNotIn('restores', inv['codex'])
        text = self.conf.read_text()
        self.assertIn("RESET_PASSES_CODEX='2|2099-01-01T05:50:full,2099-01-02:unknown'\nAGENT_LNG=ko",
                      text)
        self.assertIn("# RESET_PASSES_CLAUDE='1|2026-10-23'", text)
        r = self.cli('add', '--runtime', 'claude', '--expiry', 'unknown', '--kind', '5h')
        self.assertIn("RESET_PASSES_CLAUDE='1|unknown:5h'", self.conf.read_text())
        r = self.cli('remove', '--runtime', 'claude', '--expiry', 'unknown', '--kind', 'weekly')
        self.assertEqual(r.returncode, 2)                     # kind must match when given
        r = self.cli('remove', '--runtime', 'claude', '--expiry', 'unknown', '--kind', '5h')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("\nRESET_PASSES_CLAUDE=", self.conf.read_text())
        r = self.cli('remove', '--runtime', 'claude', '--expiry', 'unknown')
        self.assertEqual(r.returncode, 2)

    def test_refusals(self):
        self.assertEqual(self.cli('add', '--runtime', 'codex', '--expiry', '2001-01-01').returncode, 2)
        self.assertEqual(self.cli('add', '--runtime', 'codex', '--expiry', '10/23').returncode, 2)
        self.assertEqual(self.cli('add', '--runtime', 'gemini', '--expiry', 'unknown').returncode, 2)
        self.assertEqual(self.cli('add', '--runtime', 'codex', '--expiry', '2099-01-03',
                                  '--kind', 'both').returncode, 2)
        self.assertIn("RESET_PASSES_CODEX='1|2099-01-02'", self.conf.read_text())

    def test_malformed_line_is_reported_and_replaced_by_add(self):
        self.conf.write_text("RESET_PASSES_CODEX='lots'\n")
        inv = json.loads(self.cli('list').stdout)
        self.assertFalse(inv['codex']['valid'])
        self.cli('add', '--runtime', 'codex', '--expiry', '2099-03-04')
        self.assertEqual(self.conf.read_text(), "RESET_PASSES_CODEX='1|2099-03-04:unknown'\n")

    def test_env_override_reported(self):
        inv = rp.inventory(str(self.conf), self.now, env={'RESET_PASSES_CLAUDE': '2|'})
        self.assertTrue(inv['claude']['env_override'])
        self.assertEqual(len(inv['claude']['passes']), 2)
        self.assertEqual(inv['claude']['spec'], '')


class Headroom(unittest.TestCase):
    NOW = 1_800_000_000.0

    def decide(self, u5, u7, codex_used=40, codex_reset=4 * D, **passes):
        return rh.decide(usage(u5, u7, self.NOW), [], codex_used,
                         self.NOW + codex_reset, 10080, self.NOW, **passes)

    def test_no_pass_is_todays_behaviour(self):
        self.assertEqual(self.decide(10, 88)[:2], ('codex', 'claude_tight_codex_open'))

    def test_claude_pass_makes_7d_refillable(self):
        rt, verdict, detail = self.decide(10, 88, claude_pass=f'1|{day(self.NOW, 14)}')
        self.assertEqual((rt, verdict), ('claude-code', 'claude_headroom_on_pace'))
        self.assertIn('pass=held (refillable)', detail)

    def test_dgn1991_live_reserve_short_paces_the_7d(self):
        # Claude 7d 92% / 4d to reset, one pass: 8 + 100 - 1.2 x 96 < 0 ->
        # the pass no longer frees the 7d while codex (40%, no pass) has slack.
        u = usage(10, 92, self.NOW, r7=4 * D)
        rt, verdict, detail = rh.decide(u, [], 40, self.NOW + 4 * D, 10080, self.NOW,
                                        claude_pass=f'1|{day(self.NOW, 12)}')
        self.assertEqual((rt, verdict), ('codex', 'claude_tight_codex_open'))
        self.assertIn('pass held, live reserve short (8% left + 1 pass - live 1.2%/h x '
                      '96h to reset = 115% -> slack -7%) -> paced', detail)
        # Codex with no slack (100% used, no pass): the Claude pass governs as before.
        rt, _, detail = rh.decide(u, [], 100, self.NOW + 4 * D, 10080, self.NOW,
                                  claude_pass=f'1|{day(self.NOW, 12)}')
        self.assertNotIn('live reserve', detail)
        self.assertIn('pass=near_spent (refillable)', detail)
        # A smaller live burn (an install whose live use is light) leaves slack.
        _, _, detail = rh.decide(u, [], 40, self.NOW + 4 * D, 10080, self.NOW,
                                 claude_pass=f'1|{day(self.NOW, 12)}', claude_live_burn=0.5)
        self.assertNotIn('live reserve', detail)

    def test_codex_pass_counts_codex_open(self):
        # Claude tight on 5h, codex far behind pace: without a pass both tight.
        self.assertEqual(self.decide(88, 40, codex_used=84, codex_reset=5 * D)[1],
                         'both_tight_less_tight')
        rt, verdict, _ = self.decide(88, 40, codex_used=84, codex_reset=5 * D,
                                     codex_pass='3|')
        self.assertEqual((rt, verdict), ('codex', 'claude_tight_codex_open'))

    def test_codex_pass_counts_on_a_5h_window_read(self):
        args = (usage(88, 40, self.NOW), [], 90, self.NOW + 4.5 * H, 300, self.NOW)
        self.assertEqual(rh.decide(*args)[1], 'both_tight_less_tight')
        self.assertEqual(rh.decide(*args, codex_pass=f'1|{day(self.NOW, 14)}:full')[:2],
                         ('codex', 'claude_tight_codex_open'))
        self.assertEqual(rh.decide(*args, codex_pass=f'1|{day(self.NOW, 14)}:5h')[:2],
                         ('codex', 'claude_tight_codex_open'))
        # A weekly (or unknown-kind) pass does not restore the 5h window.
        for kind in ('weekly', 'unknown'):
            self.assertEqual(rh.decide(*args, codex_pass=f'1|{day(self.NOW, 14)}:{kind}')[1],
                             'both_tight_less_tight', kind)

    def test_claude_pass_kind_decides_which_window_is_refillable(self):
        far = day(self.NOW, 14)
        # 5h tight (88%), 7d open: only a pass covering 5h keeps claude.
        self.assertEqual(self.decide(88, 40)[0], 'codex')
        for kind, rt in (('full', 'claude-code'), ('5h', 'claude-code'),
                         ('weekly', 'codex'), ('unknown', 'codex')):
            got = self.decide(88, 40, codex_used=84, claude_pass=f'1|{far}:{kind}')
            self.assertEqual(got[0], rt, kind)
        # 7d tight (88%), 5h open: only a pass covering 7d keeps claude.
        for kind, rt in (('full', 'claude-code'), ('weekly', 'claude-code'),
                         ('unknown', 'claude-code'), ('5h', 'codex')):
            got = self.decide(10, 88, claude_pass=f'1|{far}:{kind}')
            self.assertEqual(got[0], rt, kind)
        detail = self.decide(88, 88, claude_pass=f'1|{far}:full')[2]
        self.assertEqual(detail.count('pass=held (refillable)'), 2)
        self.assertIn('claude_eff_pace=+0', detail)

    def test_expiring_passes_pick_their_lane(self):
        soon = day(self.NOW, 1)
        self.assertEqual(self.decide(30, 40, codex_pass=f'3|{soon}')[:2],
                         ('codex', 'codex_pass_expiring'))
        self.assertEqual(self.decide(10, 88, claude_pass=f'1|{soon}')[:2],
                         ('claude-code', 'claude_pass_expiring'))
        # 5h nearly used: the expiring claude pass does not override the spill.
        self.assertNotEqual(self.decide(90, 50, claude_pass=f'1|{soon}')[1],
                            'claude_pass_expiring')


class Selection(unittest.TestCase):
    """select_default_runtime() with fixture reads and a stub owner push."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pushed = Path(self.tmp.name, 'pushed.txt')
        self.push = Path(self.tmp.name, 'push.sh')
        self.push.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$PUSH_LOG"\n')
        self.push.chmod(0o755)
        self.stamp = Path(self.tmp.name, 'stamp')
        self.now = time.time()

    def run_select(self, usage_pair, codex_used=40, codex_reset=4 * D, **passes):
        snap = json.dumps({'usage': usage(*usage_pair, self.now), 'history': []})
        dec = tempfile.mkdtemp(dir=self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith('RESET_PASSES_')}
        env.update(DISPATCH_TEST='1', DOGANY_DISPATCH_RUNTIME='', TEST_ROOT=str(ROOT),
                   TEST_SNAP=snap, PUSH_LOG=str(self.pushed),
                   RESET_PASSES_CLAUDE=passes.get('claude', ''),
                   RESET_PASSES_CODEX=passes.get('codex', ''))
        script = f'''source "$TEST_ROOT/routines/dispatch-detached.sh"
DECISION_DIR="{dec}"
PUSH_SH="{self.push}"
RESET_PASS_PROMPT_STAMP="{self.stamp}"
LABEL=test; MODEL=opus; BOTTLENECK=coverage; WT_REPO=/fixture/estate; WT_SLUG=
REQUESTED_RUNTIME=auto
claude_usage_snapshot() {{ printf '%s' "$TEST_SNAP"; }}
codex_quota_gate() {{ CODEX_USED_PERCENT={codex_used}; CODEX_RESETS_AT={int(self.now + codex_reset)}; CODEX_WINDOW_MINS=10080; CODEX_QUOTA_NOTE='codex fixture used={codex_used}'; return 0; }}
select_default_runtime
'''
        r = subprocess.run(['bash', '-c', script], env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        doc = json.loads(next(Path(dec).glob('*.json')).read_text())
        self.assertIn(doc['selection_detail'], r.stdout)   # decision line == record
        return doc

    def pushes(self):
        return self.pushed.read_text().splitlines() if self.pushed.exists() else []

    def test_no_pass_is_todays_behaviour(self):
        doc = self.run_select((10, 88))
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('headroom=claude_tight_codex_open', doc['selection_detail'])
        self.assertIn('; passes=none', doc['selection_detail'])
        self.assertEqual(self.pushes(), [])

    def test_pass_in_hand_spends_claude_weekly(self):
        doc = self.run_select((10, 88), claude=f'1|{day(self.now, 14)}')
        self.assertEqual(doc['selected_runtime'], 'claude-code')
        self.assertEqual(doc['selection_reason'], 'claude_headroom_on_pace')
        self.assertIn('passes: claude pass x1 exp=', doc['selection_detail'])
        self.assertIn('[mode=held]', doc['selection_detail'])
        self.assertEqual(self.pushes(), [])

    def test_near_spent_prompts_once_per_window(self):
        spec = f'1|{day(self.now, 14)}'
        doc = self.run_select((10, 93), claude=spec)
        self.assertEqual(doc['selected_runtime'], 'claude-code')   # not throttled
        self.assertIn('[mode=near_spent]', doc['selection_detail'])
        self.assertIn('pass_prompt=near_spent:claude sent', doc['selection_detail'])
        sent = self.pushes()
        self.assertEqual(len(sent), 1)
        self.assertIn('--audience owner', sent[0])
        self.assertIn('Claude', sent[0])
        # Same weekly window again -> no second line.
        doc = self.run_select((10, 94), claude=spec)
        self.assertNotIn('pass_prompt=', doc['selection_detail'])
        self.assertEqual(len(self.pushes()), 1)

    def test_near_expiry_boost_and_redeem_prompt(self):
        doc = self.run_select((30, 40), codex=f'3|{day(self.now, 1)}')
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('headroom=codex_pass_expiring', doc['selection_detail'])
        self.assertIn('[mode=expiry_boost]', doc['selection_detail'])
        self.assertEqual(self.pushes(), [])
        doc = self.run_select((30, 40), codex=f'3|{day(self.now, 0)}')
        self.assertIn('pass_prompt=expiring:codex sent', doc['selection_detail'])
        self.assertEqual(len(self.pushes()), 1)
        self.assertIn('Codex', self.pushes()[0])

    def test_three_codex_passes_plan_the_soonest(self):
        spec = f'3|{day(self.now, 30)},{day(self.now, 0)},{day(self.now, 14)}'
        doc = self.run_select((30, 40), codex=spec)
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('headroom=codex_pass_expiring', doc['selection_detail'])
        self.assertIn('codex pass x3 exp=', doc['selection_detail'])
        self.assertIn('pass_prompt=expiring:codex sent', doc['selection_detail'])
        self.assertIn(day(self.now, 0), self.pushes()[0])
        self.assertIn("Use up what's left first", self.pushes()[0])

    def test_near_spent_line_names_the_5h_window(self):
        doc = self.run_select((93, 40), claude=f'1|{day(self.now, 14)}:full')
        self.assertIn('pass_prompt=near_spent:claude sent', doc['selection_detail'])
        self.assertEqual(len(self.pushes()), 1)
        self.assertIn('Claude 5-hour usage is nearly full', self.pushes()[0])

    def test_near_spent_line_names_the_weekly_window(self):
        self.run_select((10, 93), claude=f'1|{day(self.now, 14)}:weekly')
        self.assertIn('Claude weekly usage is nearly full', self.pushes()[0])

    def test_invalid_spec_is_no_pass_and_says_so(self):
        doc = self.run_select((10, 88), claude='one pass')
        self.assertEqual(doc['selected_runtime'], 'codex')
        self.assertIn('invalid', doc['selection_detail'])


class Cap(unittest.TestCase):
    """compute_dynamic_cap(): opus, 5h 90% / 7d 15% remaining, horizon 0.4h,
    roomy machine. 5h -> 30; 7d no pass floor((15-10-.48)/.44) = 10, held
    (reserve 0) floor((15-.48)/.44) = 33 -> 30. Codex 31% remaining: no pass
    floor((31-2)/6) = 4, held floor(31/6) = 5, expiring -> boost 8."""

    def cap(self, runtime, **passes):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('RESET_PASSES_', 'DOGANY_', 'USAGE_GATE'))}
        env.update(DISPATCH_TEST='1', TEST_ROOT=str(ROOT), USAGE_GATE_7D='full',
                   RESET_PASSES_CLAUDE=passes.get('claude', ''),
                   RESET_PASSES_CODEX=passes.get('codex', ''), TEST_RUNTIME=runtime)
        script = '''source "$TEST_ROOT/routines/dispatch-detached.sh"
claude_quota_remaining() { echo "90 15"; }
codex_quota_remaining() { echo 31; }
claude_hours_left() { return 1; }
claude_weekly_cap_waived() { return 1; }
machine_snapshot() { echo "0.5 64 64000"; }
read_active_count() { echo 0; }
MODEL=opus
compute_dynamic_cap "$TEST_RUNTIME"
printf '%s\\n%s\\n' "$DYNAMIC_CAP_VALUE" "$DYNAMIC_CAP_NOTE"
'''
        r = subprocess.run(['bash', '-c', script], env=env,
                           text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        value, note = r.stdout.split('\n', 1)
        return int(value), note

    def test_claude(self):
        now = time.time()
        self.assertEqual(self.cap('claude-code')[0], 10)
        value, note = self.cap('claude-code', claude=f'1|{day(now, 14)}')
        self.assertEqual(value, 30)
        self.assertIn('reserve=0%', note)
        value, note = self.cap('claude-code', claude=f'1|{day(now, 1)}')
        self.assertEqual(value, 30)
        self.assertIn('dropped', note)

    def test_claude_per_kind(self):
        """full drops both reserve terms, 5h only the 5h one, weekly / unknown
        only the weekly one."""
        now = time.time()
        far, soon = day(now, 14), day(now, 1)
        base = self.cap('claude-code')[1]
        self.assertIn('5h remaining=90% reserve=10%', base)
        for kind, r5, r7 in (('full', True, True), ('5h', True, False),
                             ('weekly', False, True), ('unknown', False, True)):
            _, note = self.cap('claude-code', claude=f'1|{far}:{kind}')
            self.assertEqual('5h remaining=90% reserve=0%' in note, r5, (kind, note))
            self.assertEqual('7d remaining=15% reserve=0%' in note, r7, (kind, note))
        self.assertEqual(self.cap('claude-code', claude=f'1|{far}:5h')[0], 10)   # 7d still bounds
        # Expiring full pass: both terms dropped -> boost, machine still bounds.
        value, note = self.cap('claude-code', claude=f'1|{soon}:full')
        self.assertIn('both windows dropped -> boost 8', note)
        self.assertEqual(value, 8)
        # Expiring 5h pass: the 5h term dropped, the 7d term still bounds.
        value, note = self.cap('claude-code', claude=f'1|{soon}:5h')
        self.assertIn('5h remaining=90% dropped', note)
        self.assertEqual(value, 10)

    def test_codex(self):
        now = time.time()
        self.assertEqual(self.cap('codex')[0], 4)
        self.assertEqual(self.cap('codex', codex='3|')[0], 5)
        value, note = self.cap('codex', codex=f'3|{day(now, 1)}')
        self.assertEqual(value, 8)
        self.assertIn('boost', note)



class WindowLoss(unittest.TestCase):
    """Owner 2026-10-09 20:09: no fixed order -- what each window would lose."""
    NOW = time.time()

    def loss(self, spec, used, reset_in, window='7d'):
        return rp.window_loss(spec, used, self.NOW + reset_in, self.NOW, window)

    def test_window_only(self):
        # 40% used over 4 of 7 days = 10%/d; 3 days left -> 30 points unused.
        w = self.loss('', 40, 3 * D)
        self.assertAlmostEqual(w['loss'], 30.0, places=3)
        self.assertEqual(w['passes'], 0)
        # On pace or past it: nothing lost.
        self.assertEqual(self.loss('', 60, 3 * D)['loss'], 0.0)
        # Unread quota: no estimate, never a claim.
        self.assertIsNone(rp.window_loss('', '', '', self.NOW)['loss'])

    def test_young_window_assumes_pace(self):
        # 6h into the week: the burn says nothing yet -> on pace, next to
        # no loss (the pace line itself ends the week spent).
        self.assertLess(self.loss('', 0, 7 * D - 6 * H)['loss'], 5.0)

    def test_pass_expiring_tomorrow_is_lost_whole(self):
        n = self.NOW
        w = self.loss(f'1|{day(n, 1)}:full', 40, 3 * D)
        # window 30 + the pass the burn never reaches by tomorrow (100).
        self.assertAlmostEqual(w['loss'], 130.0, places=0)
        self.assertEqual(w['passes'], 1)
        self.assertIn('pass ', w['text'])

    def test_far_pass_weighs_less_and_unknown_never(self):
        n = self.NOW
        near = self.loss(f'1|{day(n, 1)}:full', 40, 3 * D)['loss']
        far = self.loss(f'1|{day(n, 28)}:full', 40, 3 * D)['loss']
        self.assertLess(far, near)
        self.assertGreater(far, 30.0)
        self.assertAlmostEqual(self.loss('1|unknown:full', 40, 3 * D)['loss'], 30.0, places=3)
        # A 5h-only pass does not cover the weekly window.
        self.assertAlmostEqual(self.loss(f'1|{day(n, 1)}:5h', 40, 3 * D)['loss'], 30.0, places=3)

    def test_burn_that_reaches_the_pass_loses_nothing_of_it(self):
        n = self.NOW
        # 95% used in 4 days: the window fills within hours, the pass is
        # redeemed then; what is lost is the refilled window's remainder at
        # the reset (100 - 23.75%/d x ~2.8d), not the pass.
        w = self.loss(f'1|{day(n, 6)}:full', 95, 3 * D)
        self.assertNotIn('unredeemed', w['text'])
        self.assertAlmostEqual(w['loss'], 100 - 95 / 4 * (3 - 5 / 23.75), places=0)

    def test_cli(self):
        n = self.NOW
        out = subprocess.run(
            [sys.executable, str(ROUTINES / 'lib' / 'reset_passes.py'), 'loss',
             '--runtime', 'codex', '--spec', f'1|{day(n, 1)}:full', '--used', '40',
             '--resets-at', str(n + 3 * D), '--now', str(n)],
            capture_output=True, text=True, check=True).stdout
        self.assertTrue(out.startswith('130.0\tcodex used=40%'), out)


if __name__ == '__main__':
    unittest.main()
