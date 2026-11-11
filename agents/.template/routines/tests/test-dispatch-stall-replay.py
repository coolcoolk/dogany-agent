#!/usr/bin/env python3
# test-lane: fast
# test-component: dispatch-stall
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-new-component: replay boundary spans shell admission, queue persistence, ticket reconciliation and SessionStart injection
# test-impact: agents/.template/routines/dispatch-detached.sh agents/.template/routines/lib/host_dispatcher.py agents/.template/routines/lib/ticket_stall_audit.py agents/.template/routines/ticket-stall-audit.py agents/.template/routines/health-observer.py agents/.template/routines/lib/output_gate.py agents/.template/routines/output-gate-stop.py agents/.template/.claude/settings.json
# promise: refused cloud/base work is queued with its brief, expired/error work is surfaced, and duplicate ticket workers are refused with the live runid (DGN-2164); the stall audit injects only on devkit installs, scoped and capped (DGN-2189)
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

ROUTINES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTINES / 'lib'))
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
        (self.ws / '.instance.conf').write_text('DOGANY_PACKS=kit_dev@0.1.0\n')
        self.now = time.time()
        self.env = patch.dict(os.environ, {'HOME': str(self.ws / 'home'),
                              'DOGANY_SHARED_HOME': str(self.shared), 'DISPATCH_TEST': '1',
                              'DOGANY_DISPATCH_NOW': str(self.now), 'DOGANY_DISPATCH_MONO': '1000',
                              'DOGANY_DISPATCH_MACHINE': '1 8 32000 0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(patch.stopall)

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

    def test_error_without_followup_surfaces_and_followup_clears(self):
        self.ticket()
        self.run_record('dsp-error', 'error', self.now - 300)
        self.assertIn('dsp-error ended error', audit.findings(self.ws, self.shared, self.now)[0])
        self.run_record('dsp-followup', 'done', self.now - 100)
        self.assertEqual(audit.findings(self.ws, self.shared, self.now), [])

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
                                env={k: v for k, v in os.environ.items() if k == 'DOGANY_RESOURCE_SETTINGS_DIR' or (k != 'DOGANY_DISPATCH_WORKER')})
        injected = json.loads(result.stdout)['hookSpecificOutput']
        self.assertEqual(injected['hookEventName'], 'SessionStart')
        self.assertIn('DGN-1961', injected['additionalContext'])
        self.assertIn('DGN-2164', injected['additionalContext'])

    def test_fresh_date_only_ticket_and_template_are_not_stalls(self):
        path = self.ticket()
        today = time.strftime('%Y-%m-%d', time.localtime(self.now))
        path.write_text('---\nid: DGN-2164\nstatus: open\ncreated: %s\n---\n' % today)
        (self.ws / 'worklog/_TEMPLATE.md').write_text('---\nid:\ntitle:\nstatus: open | done\n---\n')
        self.assertEqual(audit.findings(self.ws, self.shared, self.now), [])

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

    def test_landed_ticket_without_dispatch_is_not_reported(self):
        cases = {
            'DGN-6101': '## Landed\n- 2026-11-07 5db23f0fa on dsp-base/v271-candidate; shipped.\n',
            'DGN-6102': '## Landed\n- 2026-11-07 19:05 on dsp/c4-defects (3.0 line).\n',
        }
        for ticket, landed in cases.items():
            path = self.ticket(ticket)
            path.write_text(path.read_text() + landed)
        self.assertEqual(audit.findings(self.ws, self.shared, self.now), [])

    def test_missing_or_empty_landed_section_is_still_reported(self):
        self.ticket('DGN-6103')
        path = self.ticket('DGN-6104')
        path.write_text(path.read_text() + '## Landed\n\n## Notes\n- Not landed yet.\n')
        lines = audit.findings(self.ws, self.shared, self.now)
        self.assertEqual([line.split()[1] for line in lines], ['DGN-6103:', 'DGN-6104:'])
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
                                env={k: v for k, v in os.environ.items() if k == 'DOGANY_RESOURCE_SETTINGS_DIR' or (k != 'DOGANY_DISPATCH_WORKER')})
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
