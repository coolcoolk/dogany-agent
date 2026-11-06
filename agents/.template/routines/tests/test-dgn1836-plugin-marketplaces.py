#!/usr/bin/env python3
# test-lane: fast
# test-covers: agents/.template/routines/lib/plugin_marketplaces.py agents/.template/routines/cli-updater.py agents/.template/config/i18n/*.json
"""Offline DGN-1836 acceptance tests; local Git origins, no actual owner push."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROUTINES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTINES / 'lib'))
import plugin_marketplaces as pm

spec = importlib.util.spec_from_file_location('cli_updater', ROUTINES / 'cli-updater.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class Alerts:
    def __init__(self):
        self.items = []

    def add(self, level, message, **kwargs):
        self.items.append((level, message))


def git(repo, *args):
    return subprocess.check_output(
        ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
         '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
         '-C', str(repo)] + list(args), stderr=subprocess.PIPE, text=True).strip()


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='dgn1836-')
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.config = self.base / 'claude'
        self.config.mkdir()
        self.settings = self.config / 'settings.json'
        self.state = self.base / 'state'
        self.state.mkdir()
        self.cfg = {'claude_config_dir': str(self.config), 'started': time.time(), 'budget': 1000}
        self.alerts = Alerts()
        self.owner = u.OwnerNotices(str(self.state), str(self.base), False)
        self.report = {'owner': self.owner}
        self.settings.write_text(json.dumps({'enabledPlugins': {'active@catalog': True,
                                                                 'inactive@catalog': False}}))
        self.origin = self.base / 'origin'
        self.origin.mkdir()
        git(self.origin, 'init', '-b', 'fixture')
        (self.origin / '.claude-plugin').mkdir()
        self.manifest = {'name': 'catalog', 'plugins': [
            {'name': 'active', 'source': './plugins/active'},
            {'name': 'inactive', 'source': './plugins/inactive'}]}
        (self.origin / '.claude-plugin/marketplace.json').write_text(json.dumps(self.manifest))
        for name in ('active', 'inactive'):
            p = self.origin / 'plugins' / name
            p.mkdir(parents=True)
            (p / 'content.txt').write_text('before')
        self.commit()
        self.repo = self.config / 'plugins/marketplaces/catalog-dir'
        self.repo.parent.mkdir(parents=True)
        git(self.base, 'clone', str(self.origin), str(self.repo))

    def commit(self, repo=None):
        repo = repo or self.origin
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'fixture')

    def advance(self, path='plugins/active/content.txt'):
        (self.origin / path).write_text('after')
        self.commit()

    def run_update(self, mode='install', roots=(), runner=None):
        return pm.update_marketplaces(self.cfg, mode, self.alerts, self.report,
                                      roots, runner or u.run_cmd)

    def hook_doc(self):
        notice = {'type': 'command', 'command': 'node "%s/scripts/gptaku-update-check.cjs"' % self.config}
        keep = {'type': 'command', 'command': 'node /elsewhere/update-check.cjs'}
        return {'enabledPlugins': {}, 'other': {'keep': 7}, 'hooks': {
            'SessionStart': [{'matcher': 'startup', 'timeout': 50, 'hooks': [notice, keep]},
                             {'hooks': [notice]}], 'Stop': [{'hooks': [notice]}]}}

    def test_enabled_content_and_single_owner_budget(self):
        original = self.settings.read_bytes()
        self.advance()
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), git(self.origin, 'rev-parse', 'HEAD'))
        self.assertEqual(self.owner.plugins, ['active@catalog'])
        self.assertEqual(self.settings.read_bytes(), original)
        self.owner.new_model('Fixture Model')
        self.owner.push_flag = True
        (self.state / 'owner-push-armed').touch()
        with patch.object(u, 'run_cmd', return_value=(0, '', '')) as push, patch.dict(os.environ, {'DOGANY_LANG': 'en'}):
            self.owner.flush(self.alerts, self.report, int(time.time()))
        self.assertEqual(push.call_count, 1)
        text = self.report['owner_notice']['text']
        self.assertIn('Plugin sources changed: active@catalog', text)
        self.assertIn('Fixture Model', text)
        self.assertEqual(len([line for line in text.splitlines() if 'Plugin' in line]), 1)
        self.owner.plugins.clear()
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.owner.plugins, [])

    def test_disabled_and_unrelated_changes_are_silent(self):
        self.advance('plugins/inactive/content.txt')
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.owner.compose([]), '')
        self.manifest['description'] = 'new catalog description'
        (self.origin / '.claude-plugin/marketplace.json').write_text(json.dumps(self.manifest))
        self.commit()
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.owner.plugins, [])

    def test_nothing_enabled_is_silent(self):
        self.settings.write_text('{}')
        self.advance()
        self.assertEqual(self.run_update(), 0)
        with patch.object(u, 'run_cmd') as push:
            self.owner.flush(self.alerts, self.report, int(time.time()))
        push.assert_not_called()
        self.assertNotIn('owner_notice', self.report)

    def test_backup_strip_mixed_groups_preserve_other_hooks(self):
        doc = self.hook_doc()
        self.settings.write_text(json.dumps(doc, separators=(',', ':')))
        self.settings.chmod(0o640)
        original = self.settings.read_bytes()
        self.assertEqual(self.run_update(), 0)
        backups = list(self.config.glob('settings.json.bak-marketplaces-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        after = json.loads(self.settings.read_text())
        self.assertEqual(after['enabledPlugins'], doc['enabledPlugins'])
        self.assertEqual(after['other'], doc['other'])
        self.assertEqual(after['hooks']['Stop'], doc['hooks']['Stop'])
        self.assertEqual(after['hooks']['SessionStart'], [dict(doc['hooks']['SessionStart'][0], hooks=[
            doc['hooks']['SessionStart'][0]['hooks'][1]])])
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(len(list(self.config.glob('settings.json.bak-marketplaces-*'))), 1)
        self.settings.write_bytes(original)
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(len(list(self.config.glob('settings.json.bak-marketplaces-*'))), 2)

    def test_observe_never_invokes_git_or_changes_settings(self):
        self.settings.write_text(json.dumps(self.hook_doc()))
        original = self.settings.read_bytes()
        with patch.object(u, 'run_cmd') as runner:
            self.assertEqual(self.run_update('observe', runner=runner), 0)
        runner.assert_not_called()
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse(list(self.config.glob('*.bak-*')))
        self.assertEqual(self.report['marketplace_hooks']['detected'], 2)

    def test_backup_failure_and_malformed_settings_fail_closed(self):
        self.settings.write_text(json.dumps(self.hook_doc()))
        original = self.settings.read_bytes()
        with patch.object(pm.tempfile, 'mkstemp', side_effect=OSError('backup denied')):
            self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.settings.read_bytes(), original)
        for value in ('{', '[]', '{"enabledPlugins":{"active@catalog":"true"}}'):
            self.settings.write_text(value)
            with patch.object(u, 'run_cmd') as runner:
                self.assertEqual(self.run_update(runner=runner), 1)
            runner.assert_not_called()
            self.assertEqual(self.settings.read_text(), value)

    def test_dirty_detached_and_divergence_preserved(self):
        self.advance()
        before = git(self.repo, 'rev-parse', 'HEAD')
        dirty = self.repo / 'untracked'
        dirty.write_text('keep')
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(dirty.read_text(), 'keep')
        dirty.unlink()
        git(self.repo, 'checkout', '--detach')
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), before)
        git(self.repo, 'checkout', 'fixture')
        dirty.write_text('local commit')
        self.commit(self.repo)
        before = git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.owner.plugins, [])
        self.assertTrue(all(level == 'FAIL' for level, _ in self.alerts.items))

    def test_failure_continues_to_next_marketplace_and_redacts_remote(self):
        bad = self.repo.parent / 'a-bad'
        bad.mkdir()
        self.advance()
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(self.owner.plugins, ['active@catalog'])
        def fail_pull(argv, *args, **kwargs):
            if 'pull' in argv:
                return 128, '', 'https://secret-token@example.invalid/repo'
            return u.run_cmd(argv, *args, **kwargs)
        self.assertEqual(self.run_update(runner=fail_pull), 1)
        self.assertNotIn('secret-token', str(self.alerts.items))

    def test_instance_settings_override_and_union(self):
        root = self.base / 'instance'
        (root / '.claude').mkdir(parents=True)
        (root / '.claude/settings.json').write_text(json.dumps({'enabledPlugins': {'inactive@catalog': True}}))
        (root / '.claude/settings.local.json').write_text(json.dumps({'enabledPlugins': {'inactive@catalog': False}}))
        self.advance('plugins/inactive/content.txt')
        self.assertEqual(self.run_update(roots=[str(root)]), 0)
        self.assertEqual(self.owner.plugins, [])
        (root / '.claude/settings.local.json').write_text('{}')
        self.assertIn('inactive@catalog', pm.enabled_plugins({}, [str(root)]))

    def test_symlink_settings_and_repository_are_refused(self):
        other = self.base / 'settings.json'
        other.write_text(json.dumps(self.hook_doc()))
        self.settings.unlink()
        self.settings.symlink_to(other)
        self.assertEqual(self.run_update(), 1)
        self.assertEqual(json.loads(other.read_text()), self.hook_doc())
        self.settings.unlink()
        self.settings.write_text('{}')
        link = self.repo.parent / 'linked'
        link.symlink_to(self.origin)
        self.assertEqual(self.run_update(), 1)

    def test_hook_matching_is_exact(self):
        for command in ('echo gptaku-update-check.cjs',
                        'node /elsewhere/gptaku-update-check.cjs',
                        'node "%s/scripts/gptaku-update-check.cjs" && other' % self.config):
            self.assertFalse(pm.notice_hook({'type': 'command', 'command': command}, str(self.config)))
        self.assertTrue(pm.notice_hook({'type': 'command', 'command':
                                       'node "$CLAUDE_CONFIG_DIR/scripts/gptaku-update-check.cjs"'}, str(self.config)))

    def test_remote_source_changes_are_not_claimed_as_content(self):
        self.manifest['plugins'][0]['source'] = {'source': 'url', 'url': 'https://example.invalid/plugin.git'}
        (self.origin / '.claude-plugin/marketplace.json').write_text(json.dumps(self.manifest))
        self.commit()
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.owner.plugins, [])
        self.advance('plugins/inactive/content.txt')
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.owner.plugins, [])
        self.assertTrue(any(level == 'watch' for level, _ in self.alerts.items))

    def test_gitlink_source_content_is_compared(self):
        first = git(self.origin, 'rev-parse', 'HEAD')
        git(self.origin, 'rm', '-r', 'plugins/active')
        git(self.origin, 'update-index', '--add', '--cacheinfo',
            '160000,' + first + ',plugins/active')
        git(self.origin, 'commit', '-m', 'gitlink source')
        self.assertEqual(self.run_update(), 0)
        self.owner.plugins.clear()
        second = git(self.origin, 'rev-parse', 'HEAD')
        git(self.origin, 'update-index', '--cacheinfo',
            '160000,' + second + ',plugins/active')
        git(self.origin, 'commit', '-m', 'advance gitlink')
        self.assertEqual(self.run_update(), 0)
        self.assertEqual(self.owner.plugins, ['active@catalog'])

    def test_exhausted_budget_does_not_start_git(self):
        self.cfg['started'] = time.time() - 1001
        with patch.object(u, 'run_cmd') as runner:
            self.assertEqual(self.run_update(runner=runner), 1)
        runner.assert_not_called()
        self.assertIn('budget exhausted', str(self.alerts.items))

    def test_main_arming_and_agents_mode(self):
        env = {'CLI_UPDATER_CLAUDE_CONFIG_DIR': str(self.config)}
        with patch.dict(os.environ, env), patch.object(u, 'check_agents'), \
                patch.object(u, 'derive_targets', return_value={}), patch.object(u, 'probe_models'), \
                patch.object(u, 'update_marketplaces', return_value=0) as update:
            for requested, effective in [('install', 'observe'), ('observe', 'observe')]:
                self.assertEqual(u.main(['--mode', requested, '--state-dir', str(self.state)]), 0)
                self.assertEqual(update.call_args.args[1], effective)
            (self.state / 'armed').touch()
            self.assertEqual(u.main(['--mode', 'install', '--state-dir', str(self.state)]), 0)
            self.assertEqual(update.call_args.args[1], 'install')
            update.reset_mock()
            self.assertEqual(u.main(['--mode', 'agents', '--state-dir', str(self.state)]), 0)
            update.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
