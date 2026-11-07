#!/usr/bin/env python3
# test-lane: fast
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-component: dispatch-archive
# test-new-component: regenerable archive index and reversible retention have no existing owner
# test-impact: agents/.template/routines/dispatch-archive.py
# promise: every run is indexed with routing, result, duration, commit outcome, sizes and bounded text, never stream contents
# promise: dry-run is read-only and selects strictly older than 30 days, including failed runs
# promise: apply moves intact runs to Trash and they remain findable on regeneration
# promise: pending-review, active and unreadable runs are kept
# promise: collisions refuse without overwriting and moves never fall back to copy/delete
# promise: CLI defaults to dry-run and rejects overlapping archive/Trash trees
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'dispatch-archive.py'
spec = importlib.util.spec_from_file_location('archive', SCRIPT)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
NOW = a.timestamp('2026-10-23T00:00:00Z')


class Archive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'archive'
        self.trash = self.root / 'Trash'
        self.archive.mkdir()
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(('GIT_CONFIG_', 'DOGANY_', 'DGN_'))}
        env = patch.dict(os.environ, clean, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def run_dir(self, name, age=31, **extra):
        path = self.archive / ('dsp-' + name)
        path.mkdir()
        status = dict(result='completed', runtime='codex', model='fixture',
                      started_at='2026-09-01T00:00:00Z',
                      ended_at=a.datetime.fromtimestamp(NOW - age * 86400, a.timezone.utc).isoformat(),
                      commits=0)
        status.update(extra)
        (path / 'status.json').write_text(json.dumps(status))
        (path / 'brief.md').write_text('fixture brief')
        (path / 'out.log').write_text('final report')
        (path / 'stream.jsonl').write_text('DO-NOT-COPY-RAW-STREAM')
        return path

    def test_index(self):
        path = self.run_dir('old', commits=2, head='abc')
        (path / 'out.log').write_text('x' * 5000 + 'final report')
        broken = self.run_dir('broken')
        (broken / 'status.json').write_text('{')
        (path / 'external').symlink_to(self.root)
        (self.archive / 'dsp-link').symlink_to(path)
        (broken / 'run.env').write_text("RUNTIME='fixture-runtime'\nMODEL='$(touch should-not-exist)'\n")
        rows = a.scan(self.archive, self.trash)
        self.assertEqual(len(rows), 2)
        fallback = next(r for r in rows if r['runid'] == 'dsp-broken')
        self.assertEqual(fallback['routing']['runtime'], 'fixture-runtime')
        self.assertEqual(fallback['routing']['model'], '$(touch should-not-exist)')
        row = next(r for r in rows if r['runid'] == 'dsp-old')
        self.assertEqual(row['routing']['runtime'], 'codex')
        self.assertEqual(row['result'], 'completed')
        self.assertEqual(row['duration_seconds'], 21 * 86400)
        self.assertEqual(row['commit_outcome']['head'], 'abc')
        self.assertEqual(row['brief'], 'fixture brief')
        self.assertTrue(row['final_report'].endswith('final report'))
        self.assertLessEqual(len(row['final_report']), a.TEXT_LIMIT)
        self.assertEqual(row['file_sizes']['stream.jsonl'], 22)
        self.assertNotIn('DO-NOT-COPY-RAW-STREAM', json.dumps(rows))
        self.assertNotIn('external', row['file_sizes'])

    def test_dry_run_boundary_and_failures(self):
        for name, age, result in [('old', 31, 'completed'), ('failed', 31, 'error'),
                                  ('boundary', 30, 'error'), ('new', 29, 'completed')]:
            self.run_dir(name, age, result=result)
        before = {str(p): p.read_bytes() for p in self.archive.rglob('*') if p.is_file()}
        actions = a.retain(self.archive, self.trash, NOW)
        self.assertEqual({r['runid'] for r in actions}, {'dsp-old', 'dsp-failed'})
        self.assertTrue(all(r['action'] == 'would-move' for r in actions))
        self.assertFalse(self.trash.exists())
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.archive.rglob('*') if p.is_file()})
        self.assertFalse((self.archive / '.dispatch-archive-retention.lock').exists())

    def test_apply_and_regenerate(self):
        old = self.run_dir('old')
        self.run_dir('new', 2)
        before = {p.name: p.read_bytes() for p in old.iterdir()}
        self.assertEqual(a.retain(self.archive, self.trash, NOW, True)[0]['action'], 'moved')
        self.assertFalse(old.exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.trash / old.name).iterdir()})
        rows = a.scan(self.archive, self.trash)
        self.assertEqual({r['runid'] for r in rows}, {'dsp-old', 'dsp-new'})
        self.assertEqual(next(r['location'] for r in rows if r['runid'] == old.name), 'trash')
        self.assertEqual(a.retain(self.archive, self.trash, NOW, True), [])

    def test_keep_review_active_unknown(self):
        for name, extra in [('flag', dict(pending_review=True, result='error')),
                            ('review', dict(review_state='pending-review')),
                            ('commits', dict(commits=1)),
                            ('active', dict(result='running')),
                            ('no-end', dict(ended_at='')),
                            ('bad-date', dict(ended_at='yesterday'))]:
            self.run_dir(name, **extra)
        broken = self.run_dir('broken')
        (broken / 'status.json').write_text('[]')
        self.assertEqual(a.retain(self.archive, self.trash, NOW, True), [])
        self.assertEqual(len(a.scan(self.archive, self.trash)), 7)
        self.run_dir('reviewed', commits=1, review_state='reviewed')
        self.assertEqual([r['runid'] for r in a.retain(self.archive, self.trash, NOW)], ['dsp-reviewed'])

    def test_collision_and_cross_device(self):
        self.run_dir('a')
        self.run_dir('b')
        self.trash.mkdir()
        (self.trash / 'dsp-b').mkdir()
        with self.assertRaises(ValueError):
            a.retain(self.archive, self.trash, NOW, True)
        self.assertTrue((self.archive / 'dsp-a').exists())
        (self.trash / 'dsp-b').rmdir()
        with patch.object(a.os, 'rename', side_effect=OSError('cross-device')):
            with self.assertRaises(OSError):
                a.retain(self.archive, self.trash, NOW, True)
        self.assertTrue((self.archive / 'dsp-a' / 'stream.jsonl').is_file())
        self.assertFalse((self.archive / '.dispatch-archive-retention.lock').exists())

    def test_cli(self):
        self.run_dir('old')
        args = ['retain', '--archive', str(self.archive), '--trash', str(self.trash),
                '--now', '2026-10-23T00:00:00Z']
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(a.main(args), 0)
        self.assertEqual(json.loads(out.getvalue())['action'], 'would-move')
        self.assertTrue((self.archive / 'dsp-old').exists())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            a.main(['retain', '--archive', str(self.archive), '--trash', str(self.archive / 'Trash')])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            a.main(['index', '--archive', str(self.archive), '--apply'])


if __name__ == '__main__':
    unittest.main()
