#!/usr/bin/env python3
# test-lane: fast
# test-component: dispatch-stall
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-new-component: replay boundary spans shell admission, queue persistence, ticket reconciliation and SessionStart injection
# test-impact: agents/.template/routines/dispatch-detached.sh agents/.template/routines/lib/host_dispatcher.py agents/.template/routines/lib/ticket_stall_audit.py agents/.template/routines/ticket-stall-audit.py agents/.template/routines/health-observer.py agents/.template/routines/lib/output_gate.py agents/.template/routines/output-gate-stop.py agents/.template/.claude/settings.json
# promise: refused cloud/base work is queued with its brief, expired/error work is surfaced, and duplicate ticket workers are refused with the live runid (DGN-2164); the stall audit injects only on devkit installs, scoped and capped (DGN-2189)
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROUTINES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTINES / 'lib'))
import host_dispatcher as hd
import output_gate as og
import ticket_stall_audit as audit


class Replay(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dispatch-stall-')
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name).resolve() / 'ws'
        self.shared = self.ws.parent / 'shared'
        (self.ws / 'worklog').mkdir(parents=True)
        (self.ws / 'home').mkdir()
        # The host's eligibility probe executes the registered dispatcher,
        # so its script root must also be the disposable instance.
        routines = self.ws / 'routines'
        routines.mkdir()
        (routines / 'lib').symlink_to(ROUTINES / 'lib')
        self.wrapper = routines / 'dispatch-detached.sh'
        shutil.copy(ROUTINES / 'dispatch-detached.sh', self.wrapper)
        shutil.copy(ROUTINES / 'stage-judge.py', routines)

        (self.ws / '.instance.conf').write_text('DOGANY_PACKS=kit_dev@0.1.0\n')
        self.now = time.time()
        self.env = patch.dict(os.environ, {'HOME': str(self.ws / 'home'),
                              'DOGANY_SHARED_HOME': str(self.shared), 'DISPATCH_TEST': '1',
                              'DOGANY_DISPATCH_NOW': str(self.now), 'DOGANY_DISPATCH_MONO': '1000',
                              'DOGANY_DISPATCH_MACHINE': '1 8 32000 0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.spawn = patch.object(hd, 'spawn', return_value=True).start()
        self.addCleanup(patch.stopall)

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = hd.main(list(map(str, args)))
        self.assertEqual(rc, 0, output.getvalue())
        return output.getvalue().strip().split('\t')

    def ticket(self, ticket='DGN-2164', **extra):
        data = dict(id=ticket, status='open', created_at=self.now - 10800,
                    updated_at=self.now, **extra)
        # Fixtures live only in this disposable workspace.
        path = self.ws / 'worklog' / (ticket + '.md')
        path.write_text('---\n' + '\n'.join('%s: %s' % item for item in data.items()) + '\n---\n')
        return path

    def run_record(self, runid, result, at):
        path = self.ws / '.telegram_bot' / 'dispatch' / runid
        path.mkdir(parents=True)
        (path / 'status.json').write_text(json.dumps(dict(ticket='DGN-2164', runid=runid,
                                                          result=result, started_at=at)))

    def test_refused_cloud_first_is_queued_with_brief_and_delayed_replay(self):
        subprocess.run([sys.executable, str(ROUTINES / 'tests/fixtures/stage-why.py'), str(self.ws)], check=True)
        brief = self.ws / 'brief.md'
        brief.write_text('Why: stop silent stalls\nWhat: replay today\n')
        body = r'''
source "$WRAPPER"
WORKSPACE="$PWD"; SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"; DISPATCH_DIR="$PWD/.telegram_bot/dispatch"; DECISION_DIR="$DISPATCH_DIR/decisions"
RUNTIME=claude-code; MODEL=sonnet; LABEL=replay; TICKET=DGN-2164
validate_access_mode() { return 0; }
validate_spawn_args() { return 0; }
pin_why_gate() { return 0; }
attach_inline() { return 0; }
explicit_runtime_cloud_gate() { return 0; }
validate_live_targets() { return 0; }
# Actual do_spawn refusal handling and host enqueue remain intact. No cloud
# API, remote publication, sandbox provisioning or worker can be reached.
cloud_first_select() { CLOUD_FIRST_NOTE="$REFUSAL"; return 2; }
compute_dynamic_cap() { HOST_CAP_VALUE=10; DYNAMIC_CAP_NOTE=fixture; }
do_spawn
'''
        for reason in ('unpushed_commits: dev ahead', 'base_not_on_origin: dsp/work', 'task_touches_live: /live'):
            with self.subTest(reason=reason):
                result = subprocess.run(['bash', '-c', body, 'replay', '--why', 'why-9999', '--task', str(brief), '--ticket', 'DGN-2164'],
                                        cwd=self.ws, env=dict(os.environ, WRAPPER=str(self.wrapper),
                                                             REFUSAL=reason), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('queued #1', result.stdout)
                rows = list((self.shared / 'dispatcher' / 'queue').glob('*.json'))
                self.assertEqual(len(rows), 1)
                row = json.loads(rows[0].read_text())
                self.assertEqual(row['product_ticket'], 'DGN-2164')
                self.assertIn(reason, row['why'])
                self.assertGreater(row['not_before'], self.now)
                snap = Path(row['argv'][row['argv'].index('--task') + 1])
                self.assertEqual(snap.read_text(), brief.read_text())
                self.assertEqual(hd.replace_outcome(0, result.stdout)[:2], ('queued', row['id']))
                self.assertFalse(hd.load_state()['leases'])
                rows[0].unlink()

    def test_duplicate_ticket_is_atomic_across_runtime_lanes(self):
        args = ('--agent', 'A', '--workspace', self.ws, '--product-ticket', 'DGN-2164', '--cap', 8, '--pid', os.getpid())
        first = self.call('acquire', '--runtime', 'codex', '--runid', 'dsp-live-2164', *args)
        self.assertEqual(first[0], 'granted')
        duplicate = self.call('acquire', '--runtime', 'claude-code', '--runid', 'dsp-unwanted', *args)
        self.assertEqual(duplicate[:2], ['duplicate', 'dsp-live-2164'])
        other = self.call('acquire', '--agent', 'B', '--workspace', self.ws.parent / 'other',
                          '--runtime', 'cloud', '--product-ticket', 'DGN-2164', '--cap', 8,
                          '--pid', os.getpid(), '--runid', 'dsp-other-agent')
        self.assertEqual(other[:2], ['duplicate', 'dsp-live-2164'])
        self.assertEqual(len(hd.load_state()['leases']), 1)
        self.call('release', '--lease', first[1])
        self.assertEqual(self.call('acquire', '--runtime', 'claude-code', '--runid', 'dsp-followup', *args)[0], 'granted')

    def test_shell_duplicate_refuses_with_live_runid(self):
        self.call('acquire', '--agent', 'first', '--runtime', 'codex', '--cap', 8, '--pid', os.getpid(),
                  '--workspace', self.ws, '--product-ticket', 'DGN-2164', '--runid', 'dsp-live')
        body = r'''
source "$WRAPPER"
WORKSPACE="$PWD"; SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"; TICKET=DGN-2164; RUNID=dsp-unwanted
RUNTIME=claude-code; MODEL=sonnet; LABEL=replay
host_acquire claude-code 8
'''
        result = subprocess.run(['bash', '-c', body], cwd=self.ws,
                                env=dict(os.environ, WRAPPER=str(self.wrapper)),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('live runid=dsp-live', result.stderr)

    def test_error_without_followup_surfaces_and_followup_clears(self):
        self.ticket()
        self.run_record('dsp-error', 'error', self.now - 300)
        self.assertIn('dsp-error ended error', audit.findings(self.ws, self.shared, self.now)[0])
        self.run_record('dsp-followup', 'done', self.now - 100)
        self.assertEqual(audit.findings(self.ws, self.shared, self.now), [])

    def test_expired_queue_row_surfaces_product_id_and_survives_failed_inbox(self):
        self.ticket()
        argv = self.ws / 'argv.json'
        argv.write_text(json.dumps({'argv': ['--ticket', 'DGN-2164']}))
        with patch.object(hd, 'notify_inbox', return_value=False):
            queued = self.call('enqueue', '--agent', 'A', '--runtime', 'codex', '--cap', 0,
                               '--workspace', self.ws, '--argv-file', argv, '--product-ticket', 'DGN-2164')
            with patch.dict(os.environ, {'DOGANY_DISPATCH_NOW': str(self.now + hd.TICKET_DEADLINE_S + 1),
                                         'DOGANY_DISPATCH_MONO': str(1000 + hd.TICKET_DEADLINE_S + 1)}):
                self.call('drain')
        lines = audit.findings(self.ws, self.shared, self.now + hd.TICKET_DEADLINE_S + 1)
        self.assertIn('DGN-2164', lines[0])
        self.assertIn('ended expired', lines[0])
        self.assertIn(queued[1], lines[0])

    def test_old_ticket_and_park_with_new_owner_ask_are_injected_every_30min(self):
        self.ticket()
        self.ticket('DGN-1961', status_note='owner renewed ask')
        path = self.ws / 'worklog' / 'DGN-1961.md'
        path.write_text(path.read_text().replace('status: open', 'status: parked').replace('---\n',
                        '---\nparked_at: %s\nowner_ask_at: %s\n' % (self.now - 5 * 86400, self.now - 60), 1))
        lines = audit.periodic(self.ws, self.shared, self.now)
        self.assertEqual(len(lines), 2)
        self.assertTrue(any('owner ask newer' in x for x in lines))
        self.assertTrue(any('opened >2h' in x for x in lines))
        inbox = self.ws / '.telegram_bot/session-inbox/ticket-stall-audit.md'
        inbox.unlink()
        self.assertEqual(audit.periodic(self.ws, self.shared, self.now + 1799), [])
        self.assertFalse(inbox.exists())
        self.assertEqual(len(audit.periodic(self.ws, self.shared, self.now + 1800)), 2)
        result = subprocess.run([sys.executable, str(ROUTINES / 'ticket-stall-audit.py')],
                                input=json.dumps({'cwd': str(self.ws)}), text=True, capture_output=True,
                                env={k: v for k, v in os.environ.items() if k != 'DOGANY_DISPATCH_WORKER'})
        injected = json.loads(result.stdout)['hookSpecificOutput']
        self.assertEqual(injected['hookEventName'], 'SessionStart')
        self.assertIn('DGN-1961', injected['additionalContext'])
        self.assertIn('DGN-2164', injected['additionalContext'])

    def test_early_refusal_freezes_attachments_once(self):
        subprocess.run(['git', 'init', '-q', '-b', 'fixture', str(self.ws)], check=True)
        brief, attached = self.ws / 'brief.md', self.ws / 'context.md'
        instructions = 'Why: preserve queued inputs\nWhat: brief and attachments\n'
        evidence = 'transient attachment content'
        brief.write_text(instructions)
        attached.write_text(evidence)
        body = r'''
source "$WRAPPER"
WORKSPACE="$PWD"; SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"; DISPATCH_DIR="$PWD/.telegram_bot/dispatch"; DECISION_DIR="$DISPATCH_DIR/decisions"
RUNTIME=cloud; MODEL=sonnet; LABEL=attachments; TICKET=DGN-2164
REPO_PATH="$PWD"; WT_REPO="$PWD"
compute_dynamic_cap() { HOST_CAP_VALUE=10; DYNAMIC_CAP_NOTE=fixture; }
queue_refusal 'cloud environment unavailable'
'''
        result = subprocess.run(['bash', '-c', body, 'fixture', '--task', str(brief),
                                 '--attach', str(attached), '--ticket', 'DGN-2164'],
                                cwd=self.ws, env=dict(os.environ, WRAPPER=str(self.wrapper)),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = next(iter(hd.load_state()['tickets'].values()))
        # DGN-2197 keeps instructions separate from evidence on replay. Both
        # inputs must be frozen; an attachment path must never name the caller's
        # temporary original or become part of the instruction-only brief.
        self.assertEqual(row['argv'].count('--attach'), 1)
        snapshot = Path(row['argv'][row['argv'].index('--task') + 1])
        frozen_attachment = Path(row['argv'][row['argv'].index('--attach') + 1])
        self.assertNotEqual(snapshot, brief)
        self.assertNotEqual(frozen_attachment, attached)
        self.assertEqual(frozen_attachment.name, attached.name)
        brief.unlink()
        attached.unlink()
        self.assertEqual(snapshot.read_text(), instructions)
        self.assertEqual(frozen_attachment.read_text(), evidence)
        self.assertEqual(hd.prompt_digest(row['argv']), row['prompt_digest'])

        # Render and refuse the frozen request again through the real shell.
        # This must still render evidence once and return the same ticket,
        # even after the original inputs and the launcher's merged temp vanish.
        row['not_before'] = self.now - 1
        hd.save_ticket(row)
        self.call('drain')
        lease = next(iter(hd.load_state()['leases']))
        replay_body = body.replace("queue_refusal 'cloud environment unavailable'", r'''
attach_inline || exit 1
cat "$TASK_FILE"
queue_refusal 'cloud environment unavailable'
''')
        replay = subprocess.run(['bash', '-c', replay_body, 'fixture', *row['argv']],
                                cwd=self.ws, env=dict(os.environ, WRAPPER=str(self.wrapper),
                                                     DOGANY_HOST_LEASE=lease),
                                text=True, capture_output=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.assertEqual(replay.stdout.count(evidence), 1)
        tickets = hd.load_state()['tickets']
        self.assertEqual(list(tickets), [row['id']])
        self.assertFalse(hd.load_state()['leases'])
        replay_argv = tickets[row['id']]['argv']
        self.assertEqual(replay_argv.count('--attach'), 1)
        self.assertEqual(Path(replay_argv[replay_argv.index('--task') + 1]).read_text(), instructions)
        self.assertEqual(Path(replay_argv[replay_argv.index('--attach') + 1]).read_text(), evidence)
        self.assertEqual(tickets[row['id']]['prompt_digest'], row['prompt_digest'])
        self.assertEqual(snapshot.read_text(), instructions)
        self.assertEqual(frozen_attachment.read_text(), evidence)

    def test_fresh_date_only_ticket_and_template_are_not_stalls(self):
        path = self.ticket()
        today = time.strftime('%Y-%m-%d', time.localtime(self.now))
        path.write_text('---\nid: DGN-2164\nstatus: open\ncreated: %s\n---\n' % today)
        (self.ws / 'worklog/_TEMPLATE.md').write_text('---\nid:\ntitle:\nstatus: open | done\n---\n')
        self.assertEqual(audit.findings(self.ws, self.shared, self.now), [])

    def test_delayed_refusal_leaves_capacity_for_runnable_work(self):
        argv = self.ws / 'blocked.json'
        argv.write_text(json.dumps({'argv': ['--ticket', 'DGN-2164']}))
        queued = self.call('enqueue', '--agent', 'blocked', '--runtime', 'codex', '--cap', 2,
                           '--workspace', self.ws, '--argv-file', argv, '--product-ticket', 'DGN-2164',
                           '--not-before', self.now + 1800)
        ticket = hd.load_state()['tickets'][queued[1]]
        ticket['reserved'] = True
        hd.save_ticket(ticket)
        acquired = self.call('acquire', '--agent', 'ready', '--runtime', 'codex', '--cap', 2,
                             '--pid', os.getpid(), '--workspace', self.ws, '--product-ticket', 'DGN-2165')
        self.assertEqual(acquired[0], 'granted')

    def test_requeued_preflight_releases_old_reservation_without_resetting_age(self):
        argv = self.ws / 'retry.json'
        argv.write_text(json.dumps({'argv': ['--ticket', 'DGN-2164']}))
        args = ('--agent', 'A', '--runtime', 'codex', '--cap', 1, '--workspace', self.ws,
                '--argv-file', argv, '--product-ticket', 'DGN-2164')
        queued = self.call('enqueue', *args)
        state = hd.load_state()
        lease = next(iter(state['leases'].values()))
        self.assertEqual(lease['ticket'], queued[1])
        submitted = lease['ticket_doc']['submitted_awake']
        with patch.dict(os.environ, {'DOGANY_HOST_LEASE': lease['id']}):
            retried = self.call('enqueue', *args, '--not-before', self.now + 1800)
        state = hd.load_state()
        self.assertEqual(retried[1], queued[1])
        self.assertFalse(state['leases'])
        self.assertEqual(len(state['tickets']), 1)
        self.assertEqual(state['tickets'][queued[1]]['submitted_awake'], submitted)

    def test_health_tick_calls_periodic_audit_and_session_hook_is_not_shipped(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('stall_observer', ROUTINES / 'health-observer.py')
        observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observer)
        with patch.object(observer, 'observe_machine_metrics', return_value=[]), \
             patch.object(observer, 'refresh_connection_ack'), \
             patch.object(observer, 'host_dispatch_drain'), \
             patch.object(observer.sys, 'platform', 'linux'), \
             patch.object(audit, 'periodic') as periodic:
            self.assertEqual(observer.main([]), 0)
        periodic.assert_called_once()
        # DGN-2189: the framework template wires no ticket semantics.
        config = json.loads((ROUTINES.parent / '.claude/settings.json').read_text())
        self.assertFalse(any('ticket-stall' in h['command'] for rows in config['hooks'].values()
                             for row in rows for h in row['hooks']))
        self.assertNotIn('ticket-stall-audit', (ROUTINES / 'wiring-diff-check.py').read_text())

    def write(self, ticket, **front):
        path = self.ws / 'worklog' / (ticket + '.md')
        path.write_text('---\n' + ''.join('%s: %s\n' % kv for kv in dict(id=ticket, **front).items())
                        + '---\n')
        return path

    def flood(self):
        """Replay of the 2026-10-22 08:05 shape: ~500 open tickets."""
        day = 86400
        for n in range(1, 481):  # old P2/P3 backlog: never listed
            self.write('DGN-%d' % (3000 + n), status='open', priority='P%d' % (2 + n % 2),
                       created_at=self.now - (30 + n % 300) * day)
        for n in range(1, 31):  # 4-33h-old open tickets: capped
            self.write('DGN-%d' % (4000 + n), status='open', priority='P1' if n % 3 == 0 else 'P2',
                       created_at=self.now - (3 + n) * 3600)
        for n in range(1, 6):  # backlog-intent: never counts
            self.write('DGN-%d' % (5000 + n), status='open', priority='P0', intent='backlog',
                       created_at=self.now - day)
        asks = ['DGN-088', 'DGN-116', 'DGN-1387', 'DGN-1733', 'DGN-2072', 'DGN-2075', 'DGN-936']
        for ticket in asks:
            self.write(ticket, status='parked', priority='P3', created_at=self.now - 200 * day,
                       parked_at=self.now - 10 * day, owner_ask_at=self.now - day)
        self.write('DGN-2195', status='open', priority='P2', created='2026-10-20')
        record = self.ws / '.telegram_bot' / 'dispatch' / 'dsp-2195'
        record.mkdir(parents=True)
        (record / 'status.json').write_text(json.dumps(dict(ticket='DGN-2195', runid='dsp-2195',
                                                            result='error', started_at=self.now - 3600)))
        return asks

    def test_flood_replay_lists_actionable_first_then_capped_recent(self):
        asks = self.flood()
        lines = audit.findings(self.ws, self.shared, self.now)
        if os.environ.get('STALL_REPLAY_PRINT'):
            sys.stderr.write('\n'.join(lines) + '\n')
        self.assertEqual(len(lines), 20)
        self.assertEqual(lines[-1], '[ticket-stall] +19 more (see ticket list)')  # 38 findings, 19 shown
        head = [x.split()[1].rstrip(':') for x in lines[:8]]
        self.assertEqual(head[0], 'DGN-2195')  # P2 before the P3 parks
        self.assertEqual(sorted(head[1:]), sorted(asks))
        listed = ' '.join(lines)
        self.assertNotIn('DGN-3', listed)
        self.assertNotIn('DGN-5', listed)
        rest = [x.split()[1].rstrip(':') for x in lines[8:-1]]
        self.assertTrue(all(t.startswith('DGN-40') for t in rest))
        # No-run tickets follow newest first, regardless of their priority.
        self.assertEqual(rest, ['DGN-40%02d' % n for n in range(1, 12)])

    def test_old_never_run_tickets_are_backlog_even_when_urgent_or_wip(self):
        now = 1800000000
        for n, extra in enumerate(({}, {'priority': 'P0'}, {'priority': 'P1'}, {'status': 'wip'})):
            front = dict(status='open', created_at=now - 72 * 3600 - 1)
            front.update(extra)
            self.write('DGN-%d' % (6000 + n), **front)
        self.assertEqual(audit.findings(self.ws, self.shared, now), [])

    def test_three_hour_never_run_ticket_is_reported(self):
        now = 1800000000
        self.write('DGN-6001', status='open', created_at=now - 3 * 3600)
        self.write('DGN-6002', status='open', created_at=now - 72 * 3600)
        self.write('DGN-6003', status='open', created_at=now - 3600)
        lines = audit.findings(self.ws, self.shared, now)
        self.assertEqual([line.split()[1] for line in lines], ['DGN-6001:', 'DGN-6002:'])
        self.assertTrue(all('opened >2h ago; no run' in line for line in lines))

    def test_cap_includes_overflow_line_and_counts_every_hidden_ticket(self):
        now = 1800000000
        for n in range(20):
            self.write('DGN-%d' % (6000 + n), status='open', created_at=now - (3 + n) * 3600)
        lines = audit.findings(self.ws, self.shared, now)
        self.assertEqual(len(lines), 20)
        self.assertTrue(all('opened >2h ago; no run' in line for line in lines))
        self.write('DGN-6020', status='open', created_at=now - 23 * 3600)
        lines = audit.findings(self.ws, self.shared, now)
        self.assertEqual(len(lines), 20)
        self.assertEqual(lines[-1], '[ticket-stall] +2 more (see ticket list)')
        self.assertEqual([line.split()[1] for line in lines[:-1]],
                         ['DGN-%d:' % (6000 + n) for n in range(19)])

    def test_failed_run_and_owner_ask_precede_newest_no_run(self):
        now = 1800000000
        self.write('DGN-2164', status='open', priority='P3', created_at=now - 200 * 86400,
                   updated_at=now - 100)
        self.run_record('dsp-error', 'error', now - 300)
        self.write('DGN-6000', status='parked', priority='P3', created_at=now - 100 * 86400,
                   parked_at=now - 10 * 86400, owner_ask_at=now - 5 * 86400)
        self.write('DGN-6001', status='open', priority='P0', created_at=now - 6 * 3600)
        self.write('DGN-6002', status='open', priority='P3', created_at=now - 3 * 3600)
        lines = audit.findings(self.ws, self.shared, now)
        self.assertEqual([line.split()[1] for line in lines],
                         ['DGN-2164:', 'DGN-6000:', 'DGN-6002:', 'DGN-6001:'])
        self.assertIn('dsp-error ended error', lines[0])
        self.assertIn('owner ask newer than park', lines[1])

    def decision(self, number, text):
        path = self.ws / 'worklog/decisions' / ('dec-%s.md' % number)
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return path

    def metal_owner_ask_replay(self):
        """The seven parked tickets from the live 2026-10-22 audit."""
        self.now = audit.stamp('2026-10-22T22:00:00+09:00')
        bodies = {
            'DGN-1387': '- 2026-10-19 12:22 autopilot park: merge review REFUSED -- '
                        'Measure real chronological owner transcripts with negatives.\n',
            'DGN-116': '## Consumer (2026-09-02, owner)\n'
                       'Owner parked the knowledge warehouse until v2.1.\n',
            'DGN-088': '## Transfer (2026-08-10, owner approved)\n'
                       '- owner primary -> peer; gate: DGN-087.\n',
            'DGN-936': '- 2026-08-20 11:31 owner interest: Graphiti.\n'
                       'gate_deps: DGN-937; STAY-PARKED.\n',
            'DGN-1733': '- 2026-09-26 21:40 PARKED -- retention needs an owner decision.\n'
                        '- 2026-10-01 17:24 owner (dec-232 d): retention 30 days, then trash.\n',
            'DGN-2072': '- 2026-10-13 15:00 autopilot park: waits owner copy confirm dec-420\n'
                        '- 2026-10-19 12:22 needs owner copy confirmation before merge '
                        '(asked one at a time after dec-503). Attach confirmation for dec-420.\n',
            'DGN-2075': '- 2026-10-14 00:3x Copy keys retro.slot.missed.* pending owner confirm.\n'
                        '- 2026-10-19 12:22 needs owner copy confirmation before merge '
                        '(asked one at a time after dec-503).\n',
        }
        for ticket, body in bodies.items():
            path = self.write(ticket, status='parked', priority='P3', created='2026-07-01',
                              parked_at=audit.stamp('2026-07-08'), updated='2026-10-19')
            path.write_text(path.read_text() + body)
        self.decision('232', '# dec-232 -- ticket sweep\nStatus: confirmed 2026-10-01 17:30\n')
        self.decision('420', '# dec-420 -- notice copy (2026-10-13)\n- Status: pending\n')
        self.decision('503', '# dec-503 -- header\n- Status: confirmed 2026-10-19 21:31\n')
        # A genuine user message can list all seven IDs without asking any
        # new question (e.g. a ticket audit request). It must not reopen them.
        transcript = self.ws / 'owner.jsonl'
        transcript.write_text(json.dumps({'type': 'user', 'timestamp': self.now - 60,
                                         'message': {'content': 'Audit ' + ' '.join(bodies)}}) + '\n')
        bot = self.ws / '.telegram_bot'
        bot.mkdir(exist_ok=True)
        (bot / 'sessions.json').write_text(json.dumps({'main': {'transcript_path': str(transcript)}}))

    def test_metal_replay_only_two_unanswered_owner_questions_surface(self):
        self.metal_owner_ask_replay()
        lines = audit.findings(self.ws, self.shared, self.now)
        self.assertEqual([line.split()[1] for line in lines], ['DGN-2072:', 'DGN-2075:'])
        self.assertTrue(all('owner ask newer than park' in line for line in lines))

    def test_answered_decision_clears_stale_pending_ticket_copy(self):
        self.metal_owner_ask_replay()
        for answer in ('Status: confirmed 2026-10-22 22:21', '- Status: decided',
                       '---\nstatus: resolved\n---',
                       '- Status: pending\n- Decision (2026-10-22): approved'):
            with self.subTest(answer=answer):
                self.decision('420', '# dec-420 -- notice copy (pending)\n' + answer + '\n')
                lines = audit.findings(self.ws, self.shared, self.now)
                self.assertEqual([line.split()[1] for line in lines], ['DGN-2075:'])

    def test_linked_open_decision_and_explicit_marker_without_owner_mentions(self):
        self.decision('700', '# dec-700 -- choose copy\nStatus: open\n')
        path = self.write('DGN-700', status='parked', parked_at=self.now - 86400,
                          updated_at=self.now - 60)
        path.write_text(path.read_text() + 'Question: dec-700\n')
        path = self.write('DGN-701', status='parked', parked_at=self.now - 86400,
                          updated_at=self.now - 60)
        date = time.strftime('%Y-%m-%d %H:%M', time.localtime(self.now - 60))
        path.write_text(path.read_text() + '- %s [owner-ask] Choose copy A or B.\n' % date)
        self.assertEqual([line.split()[1] for line in audit.findings(self.ws, self.shared, self.now)],
                         ['DGN-700:', 'DGN-701:'])
        self.decision('700', '# dec-700\nStatus: answered\n')
        path.write_text(path.read_text().replace('status: parked',
                                               'status: parked\nowner_ask_status: answered'))
        self.assertEqual(audit.findings(self.ws, self.shared, self.now), [])

    def test_copy_confirmation_clears_explicit_pending_ask_without_decision(self):
        path = self.write('DGN-2075', status='parked', parked_at=audit.stamp('2026-10-13'),
                          updated='2026-10-22')
        pending = '- 2026-10-19 12:22 needs owner copy confirmation before merge.\n'
        text = path.read_text() + pending
        path.write_text(text)
        self.assertEqual(len(audit.findings(self.ws, self.shared, self.now)), 1)
        for answer in ('owner copy confirmed', 'copy confirmation received', '[owner-answer] A'):
            with self.subTest(answer=answer):
                path.write_text(text + '- 2026-10-22 22:21 ' + answer + '\n')
                self.assertEqual(audit.findings(self.ws, self.shared, self.now), [])
                path.write_text(path.read_text() + '- 2026-10-23 08:00 [owner-ask] Pick revised copy.\n')
                self.assertEqual(len(audit.findings(self.ws, self.shared, self.now)), 1)

    def test_pending_decision_with_empty_answer_placeholder_is_still_open(self):
        path = self.write('DGN-700', status='parked', parked_at=self.now - 86400,
                          updated_at=self.now - 60)
        path.write_text(path.read_text() + 'Question: dec-700\n')
        for answer in ('', 'pending', 'null', 'TBD', '-'):
            with self.subTest(answer=answer):
                self.decision('700', '# dec-700\nStatus: pending\nAnswer: ' + answer + '\n')
                self.assertEqual(len(audit.findings(self.ws, self.shared, self.now)), 1)

    def test_owner_mentions_missing_decision_and_prepark_asks_stay_quiet(self):
        for n, body in enumerate((
                '- 2026-10-21 owner approved the park.\n',
                '- 2026-10-21 dec-999 (missing decision).\n',
                '- 2026-10-01 [owner-ask] Choose A or B.\n',
                '- 2026-10-21 ' + '\uD615\uB2D8' + ' approved.\n',
                '- 2026-10-21 ' + '\uC624\uB108' + ' approved.\n')):
            path = self.write('DGN-%d' % (800 + n), status='parked',
                              parked_at=audit.stamp('2026-10-20'),
                              owner='primary', created='2026-09-01',
                              source='2026-10-21 owner filed this ticket')
            path.write_text(path.read_text() + body)
        self.assertEqual(audit.findings(self.ws, self.shared, self.now), [])

    def test_non_devkit_install_injects_nothing_from_either_path(self):
        self.flood()
        (self.ws / '.instance.conf').write_text('DOGANY_PACKS=kit_life@1.2.0,agent_dev@0.3.0\n')
        inbox = self.ws / '.telegram_bot/session-inbox/ticket-stall-audit.md'
        inbox.parent.mkdir(parents=True)
        inbox.write_text('[ticket-stall] DGN-088: left over from an earlier build\n')
        self.assertEqual(audit.periodic(self.ws, self.shared, self.now), [])
        self.assertFalse(inbox.exists())
        result = subprocess.run([sys.executable, str(ROUTINES / 'ticket-stall-audit.py')],
                                input=json.dumps({'cwd': str(self.ws)}), text=True, capture_output=True,
                                env={k: v for k, v in os.environ.items() if k != 'DOGANY_DISPATCH_WORKER'})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        (self.ws / '.instance.conf').unlink()
        self.assertEqual(audit.periodic(self.ws, self.shared, self.now + 3600), [])

    def test_bare_date_created_uses_ulid_time_not_midnight(self):
        # Created 40 min ago but dated today: must not read as midnight.
        stamp_ms = int((self.now - 2400) * 1000)
        ulid = ''
        for _ in range(10):
            ulid = audit.ULID[stamp_ms % 32] + ulid
            stamp_ms //= 32
        today = time.strftime('%Y-%m-%d', time.localtime(self.now))
        self.write('DGN-2194', status='open', created=today, ulid=ulid + '0' * 16)
        self.assertAlmostEqual(audit.ulid_time(ulid + '0' * 16), self.now - 2400, delta=1)
        self.assertEqual(audit.findings(self.ws, self.shared, self.now), [])
        self.assertEqual(len(audit.findings(self.ws, self.shared, self.now + 2 * 3600)), 1)

    def test_conditional_ticket_promise_keeps_earlier_bound_carrier(self):
        text = 'DGN-2164 결과 오면 정리하겠습니다.'
        self.assertIsNone(og.idle_promise_block_verdict(text, '',
                          {'armed': True, 'ticket_ids': ['DGN-2164']}))
        self.assertIsNotNone(og.idle_promise_block_verdict(text, '',
                             {'armed': True, 'ticket_ids': ['DGN-1']}))
        self.assertIsNotNone(og.idle_promise_block_verdict('DGN-2164 수정하겠습니다.', '',
                             {'armed': True, 'ticket_ids': ['DGN-2164']}))

    def test_fix_promise_requires_the_same_ticket_carrier(self):
        text = 'DGN-2164 수정하겠습니다.'
        for blob in ('TOOL Write /x/worklog/DGN-2164.md', 'CMD git commit -m fix',
                     'CMD bash dispatch-detached.sh --ticket DGN-1'):
            self.assertIsNotNone(og.idle_promise_block_verdict(text, blob), blob)
        for blob in ('CMD bash dispatch-detached.sh --ticket DGN-2164',
                     'BG Bash DGN-2164', 'TOOL ScheduleWakeup DGN-2164'):
            self.assertIsNone(og.idle_promise_block_verdict(text, blob), blob)
        self.assertIsNone(og.idle_promise_block_verdict('DGN-2164 티켓에 기록했습니다. 아직 시작하지 않았습니다.',
                                                      'TOOL Write /x/worklog/DGN-2164.md'))


if __name__ == '__main__':
    unittest.main()
