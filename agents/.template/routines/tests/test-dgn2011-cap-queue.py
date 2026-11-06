#!/usr/bin/env python3
# test-lane: fast
# test-impact: agents/.template/routines/dispatch-detached.sh agents/.template/routines/lib/host_dispatcher.py
"""DGN-2011: full pooled cap queues every caller and release starts the replay.

Offline, foreground only: actual shell client and host state in a temporary
home; the spawn seam captures the normal replay command instead of a worker.
No notification, runtime CLI, network, or live dispatcher state is used.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROUTINES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTINES / 'lib'))
import host_dispatcher as hd


class CapQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dgn2011-')
        self.root = Path(self.tmp.name).resolve()  # /var -> /private/var symlink; host records realpaths
        self.env = patch.dict(os.environ, {
            'HOME': str(self.root / 'home'),
            'DOGANY_SHARED_HOME': str(self.root / 'shared'),
            # One young build lease fills build capacity, across runtimes.
            'DOGANY_DISPATCH_MACHINE': '11 8 32000 0',
            'DOGANY_HOST_DISPATCH': '0',
            'CODEX_HOME': str(self.root / 'home' / '.codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'home' / '.claude'),
            'DISPATCH_TEST': '1',
        })
        self.env.start()
        (self.root / 'home').mkdir()
        self.spawned = []
        self.real_spawn = hd.spawn
        self.spawn_patch = patch.object(hd, 'spawn', side_effect=lambda lease: self.spawned.append(lease))
        self.spawn_patch.start()
        self.task = self.root / 'brief with spaces.md'
        self.task.write_text('Keep this original brief: $literal; `text`.\n')
        self.wrapper = ROUTINES / 'dispatch-detached.sh'

    def tearDown(self):
        self.spawn_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hd.main([str(x) for x in args])
        self.assertEqual(rc, 0, out.getvalue())
        return out.getvalue().strip().split('\t')

    def occupy(self, runtime='codex'):
        out = self.call('acquire', '--agent', 'holder', '--runtime', runtime,
                        '--cap', 10, '--pid', os.getpid())
        self.assertEqual(out[0], 'granted', out)
        return out[1]

    def dispatch(self, agent='caller', *extra, kind='build'):
        workspace = self.root / agent
        workspace.mkdir(exist_ok=True)
        body = r'''
source "$WRAPPER"
WORKSPACE="$PWD"
DISPATCH_DIR="$PWD/dispatch"; DECISION_DIR="$PWD/decisions"
ACTIVE_COUNT_FILE="$DISPATCH_DIR/active-count"
ACTIVE_COUNT_FILE_CODEX="$DISPATCH_DIR/active-count.codex"
ACTIVE_COUNT_FILE_CLOUD="$DISPATCH_DIR/active-count.cloud"
ACTIVE_COUNT_FILE_API="$DISPATCH_DIR/active-count.claude-api"
COUNT_LOCK_DIR="$DISPATCH_DIR/.count.lock"
USAGE_GATE=/usr/bin/true
RUNTIME=claude-code; MODEL=sonnet; LABEL=queue-fixture
BOTTLENECK=accuracy; BOTTLENECK_WHY='Verify the fixed acceptance cases'
# The original selection found no eligible fallback. Keep real capacity,
# queue, priority and persistence code; replace external quota reads only.
RUNTIME_FALLBACK_DIRECTION='codex->claude-code'
claude_quota_remaining() { echo '80 80'; }
claude_hours_left() { return 1; }
# DGN-2293: a detached worker needs a measured weekly pace; the production
# unknown-usage refusal stays. The fixture supplies a low, fresh reading.
claude_usage_snapshot() {
  python3 -c 'import json, datetime as d
n = d.datetime.now(d.timezone.utc)
print(json.dumps({"usage": {"seven_day_util": 10, "five_hour_util": 10,
  "seven_day_resets_at": (n + d.timedelta(days=2)).isoformat(),
  "five_hour_resets_at": (n + d.timedelta(hours=2)).isoformat()}}))'
}
reset_pass_mode() { printf 'none\t-\t-\tfixture\n'; }
machine_snapshot() { echo '19 8 4000'; }
rc=0; run_gates || rc=$?
echo "RC=$rc KIND=$(host_run_kind)"
'''
        env = dict(os.environ, WRAPPER=str(self.wrapper))
        for key in ('DOGANY_DISPATCH_MAX_ACTIVE', 'DOGANY_HOST_LEASE', 'DOGANY_DISPATCH_RUNTIME'):
            env.pop(key, None)
        # These queue fixtures require build capacity. No worktree otherwise
        # means llm since DGN-2138; None tests that default explicitly.
        env.pop('DOGANY_DISPATCH_RUN_KIND', None)
        if kind is not None:
            env['DOGANY_DISPATCH_RUN_KIND'] = kind
        result = subprocess.run(['bash', '-c', body, 'fixture', '--task', str(self.task),
                                 '--notify', 'silent', '--label', 'literal $value', *extra],
                                cwd=workspace, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def queue(self, agent='caller', *extra):
        result = self.dispatch(agent, *extra)
        self.assertIn('RC=3 KIND=build', result.stdout, result.stdout + result.stderr)
        line = next(s for s in result.stdout.splitlines() if s.startswith('[dispatch] queued '))
        ticket = line.split('(ticket ', 1)[1].split(')', 1)[0]
        doc = json.loads((Path(hd.root()) / 'queue' / (ticket + '.json')).read_text())
        self.assertEqual(doc['kind'], 'build')
        return line, doc

    def test_no_worktree_llm_runs_beside_build_holder_under_load(self):
        self.occupy()
        result = self.dispatch(kind=None)
        self.assertIn('RC=0 KIND=llm', result.stdout, result.stdout + result.stderr)
        self.assertNotIn('[dispatch] queued ', result.stdout)
        leases = list(hd.load_state()['leases'].values())
        self.assertEqual(len(leases), 2)
        self.assertEqual({(hd.lane_runtime(l['lane']), l['kind']) for l in leases},
                         {('codex', 'build'), ('claude-code', 'llm')})
        self.assertEqual(self.spawned, [])

    def test_full_machine_queues_legacy_caller_and_release_replays_snapshot(self):
        lease = self.occupy()
        line, ticket = self.queue('caller', '--no-queue')
        self.assertIn('queued #1 (ticket ', line)
        self.assertNotIn('runtime', line)
        self.assertNotIn('Bump:', line)
        self.assertEqual(self.spawned, [])
        argv = ticket['argv']
        snap = Path(argv[argv.index('--task') + 1])
        brief = self.task.read_text()
        self.task.unlink()
        self.assertEqual(snap.read_text(), brief)
        self.assertEqual(argv[2:], ['--notify', 'silent', '--label', 'literal $value', '--no-queue'])
        # The local machine cap was zero; the queued lane retains quota,
        # while the host freshly measures machine room on the release.
        lanes = json.loads((Path(hd.root()) / 'lanes.json').read_text())
        self.assertGreater(lanes[ticket['lane']]['cap'], 0)
        self.call('release', '--lease', lease)
        self.assertEqual(len(self.spawned), 1)
        replay = self.spawned[0]
        self.assertEqual(replay['ticket_doc']['argv'], argv)
        self.assertEqual(replay['ticket_doc']['workspace'], str(self.root / 'caller'))
        self.assertEqual(replay['ticket_doc']['script'], str(self.wrapper))
        with patch.object(hd.subprocess, 'Popen') as process:
            self.assertTrue(self.real_spawn(replay))
            args, kwargs = process.call_args
            self.assertEqual(args[0], [str(self.wrapper)] + argv)
            self.assertEqual(kwargs['cwd'], str(self.root / 'caller'))
            self.assertEqual(kwargs['env']['DOGANY_HOST_LEASE'], replay['id'])
            self.assertNotIn('DOGANY_DISPATCH_WORKER', kwargs['env'])
        self.assertFalse(list((Path(hd.root()) / 'queue').glob('*.json')))
        self.assertEqual(hd.replace_outcome(0, line), ('queued', ticket['id'], '-'))
        # Normal selection on replay may now choose Codex. Its lease must
        # move to that lane, still consuming just one pooled machine slot.
        adopted = self.call('acquire', '--agent', ticket['agent'], '--runtime', 'codex',
                            '--cap', 10, '--pid', os.getpid(), '--lease', replay['id'])
        self.assertEqual(adopted[0], 'granted')
        state = hd.load_state()
        self.assertEqual(len(state['leases']), 1)
        self.assertEqual(state['leases'][adopted[1]]['lane'], hd.lane_key('codex', 'unknown'))
        denied = self.call('acquire', '--agent', 'other', '--runtime', 'claude-api',
                           '--cap', 10, '--pid', os.getpid())
        self.assertEqual(denied[0], 'full')

    def test_position_uses_priority_and_aging_and_release_picks_best(self):
        lease = self.occupy()
        first_line, first = self.queue('background-a')
        second_line, second = self.queue('background-b')
        owner_line, owner = self.queue('owner', '--owner-turn', 'turn-1')
        self.assertIn('queued #1 ', first_line)
        self.assertIn('queued #2 ', second_line)
        self.assertIn('queued #1 ', owner_line)
        self.call('release', '--lease', lease)
        self.assertEqual([x['ticket'] for x in self.spawned], [owner['id']])
        self.call('release', '--lease', self.spawned[-1]['id'])
        self.assertEqual(self.spawned[-1]['ticket'], first['id'])
        self.call('release', '--lease', self.spawned[-1]['id'])
        self.assertEqual(self.spawned[-1]['ticket'], second['id'])

    def test_reselected_full_lane_cannot_adopt_old_reservation(self):
        lease = self.occupy()
        _, ticket = self.queue()
        self.call('release', '--lease', lease)
        pending = self.spawned[0]
        refused = self.call('acquire', '--agent', ticket['agent'], '--runtime', 'codex',
                            '--cap', 0, '--pid', os.getpid(), '--lease', pending['id'])
        self.assertEqual(refused[0], 'full')
        self.assertNotIn(pending['id'], hd.load_state()['leases'])


if __name__ == '__main__':
    unittest.main()
