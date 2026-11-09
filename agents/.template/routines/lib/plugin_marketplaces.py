"""DGN-1836: dawn-owned marketplace pulls and user-global notice hygiene.

No plugin activation or cache install. Only clean Git checkouts fast-forward.
An enabled local plugin's Git tree (including gitlinks) is the content signal;
remote-source plugins cannot be verified by a marketplace pull alone.
"""
import copy
import json
import os
import posixpath
import shlex
import stat
import tempfile
import time


class MarketplaceError(Exception):
    pass


def read_settings(path):
    try:
        with open(path, 'rb') as stream:
            raw = stream.read()
    except FileNotFoundError:
        return {}, None
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError('settings must be an object')
    enabled = doc.get('enabledPlugins', {})
    if not isinstance(enabled, dict) or any(type(v) is not bool for v in enabled.values()):
        raise ValueError('enabledPlugins must map plugin IDs to booleans')
    return doc, raw


def notice_hook(hook, config_dir):
    """Recognize the measured notifier invocation, never substring-match a shell."""
    if not isinstance(hook, dict) or hook.get('type') != 'command':
        return False
    try:
        words = shlex.split(hook.get('command', ''))
    except (ValueError, TypeError):
        return False
    if len(words) != 2 or os.path.basename(words[0]) != 'node':
        return False
    script = words[1]
    for prefix in ('${HOME}/', '$HOME/', '~/'):
        if script.startswith(prefix):
            script = os.path.join(os.path.expanduser('~'), script[len(prefix):])
            break
    for prefix in ('${CLAUDE_CONFIG_DIR}/', '$CLAUDE_CONFIG_DIR/'):
        if script.startswith(prefix):
            script = os.path.join(config_dir, script[len(prefix):])
            break
    return os.path.abspath(script) == os.path.join(config_dir, 'scripts',
                                                 'gptaku-update-check.cjs')


def strip_notice(path, config_dir, doc, raw, mode):
    """Backup exact bytes before atomic replacement; refuse concurrent edits."""
    updated = copy.deepcopy(doc)
    hooks = updated.get('hooks', {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get('SessionStart', []), list):
        raise ValueError('invalid SessionStart hooks')
    groups, removed = [], 0
    for group in hooks.get('SessionStart', []):
        if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
            raise ValueError('invalid SessionStart hook group')
        keep = [h for h in group['hooks'] if not notice_hook(h, config_dir)]
        count = len(group['hooks']) - len(keep)
        removed += count
        if keep or not count:
            group['hooks'] = keep
            groups.append(group)
    result = {'removed': 0, 'detected': removed}
    if not removed or mode != 'install':
        return result
    if os.path.islink(path) or not stat.S_ISREG(os.stat(path).st_mode):
        raise ValueError('refusing non-regular settings file')
    updated['hooks']['SessionStart'] = groups
    fd, backup = tempfile.mkstemp(prefix='settings.json.bak-marketplaces-',
                                  dir=config_dir)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    result['backup'] = backup
    fd, temporary = tempfile.mkstemp(prefix='.settings-marketplaces-', dir=config_dir)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(os.stat(path).st_mode))
            stream.write((json.dumps(updated, indent=2, ensure_ascii=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        with open(path, 'rb') as stream:
            if stream.read() != raw or os.path.islink(path):
                raise ValueError('settings changed during cleanup; backup retained')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    result['removed'] = removed
    return result


def enabled_plugins(global_doc, instance_roots):
    """Union of effective enabled IDs across global and registered instance scopes."""
    global_enabled = global_doc.get('enabledPlugins', {})
    enabled = {key for key, value in global_enabled.items() if value is True}
    for root in instance_roots:
        effective = dict(global_enabled)
        for name in ('settings.json', 'settings.local.json'):
            doc, _ = read_settings(os.path.join(root, '.claude', name))
            effective.update(doc.get('enabledPlugins', {}))
        enabled.update(key for key, value in effective.items() if value is True)
    return enabled


def update_marketplaces(cfg, mode, alerts, report, instance_roots, run_cmd):
    config_dir = os.path.abspath(cfg['claude_config_dir'])
    records = report['marketplaces'] = []
    settings = os.path.join(config_dir, 'settings.json')
    rc = 0
    try:
        doc, raw = read_settings(settings)
        report['marketplace_hooks'] = strip_notice(settings, config_dir, doc, raw, mode)
        enabled = enabled_plugins(doc, instance_roots)
    except (OSError, ValueError) as exc:
        alerts.add('FAIL', 'marketplace settings: %s' % exc)
        # Do not pull if we cannot establish which enabled content changed.
        return 1
    base = os.path.join(config_dir, 'plugins', 'marketplaces')
    if not os.path.exists(base):
        return 0
    deadline = min(cfg['started'] + cfg['budget'], time.time() + 120)

    def git(repo, *args):
        remaining = deadline - time.time()
        if remaining <= 0:
            raise MarketplaceError('marketplace time budget exhausted')
        status, out, _ = run_cmd(
            ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'submodule.recurse=false',
             '-c', 'fetch.recurseSubmodules=false', '-c', 'merge.autoStash=false',
             '-c', 'rebase.autoStash=false', '-C', repo] + list(args),
            min(30, remaining), env_extra={'GIT_TERMINAL_PROMPT': '0'})
        if status:
            # Git stderr can include authenticated remote URLs. Keep it out of reports.
            raise MarketplaceError('git %s failed (rc=%d)' % (args[0], status))
        return out.strip()

    def snapshots(repo, revision):
        manifest = json.loads(git(repo, 'show', revision + ':.claude-plugin/marketplace.json'))
        name, plugins = manifest['name'], manifest['plugins']
        if not isinstance(name, str) or not isinstance(plugins, list):
            raise ValueError('invalid marketplace manifest')
        found, unsupported = {}, []
        for plugin in plugins:
            key = '%s@%s' % (plugin['name'], name)
            if key not in enabled:
                continue
            source = plugin.get('source')
            if not isinstance(source, str) or not source.startswith('./'):
                unsupported.append(key)
                continue
            path = posixpath.normpath(source)
            if path == '..' or path.startswith('../') or ':' in path:
                raise ValueError('unsafe plugin source path')
            # rev-parse returns the tree/blob/gitlink object ID, independent of
            # unrelated marketplace commits and manifest description/version edits.
            found[key] = git(repo, 'rev-parse', '--verify', revision + ':' + ('' if path == '.' else path))
        return found, unsupported

    try:
        entries = sorted(os.scandir(base), key=lambda entry: entry.name)
    except OSError as exc:
        alerts.add('FAIL', 'marketplace discovery: %s' % exc)
        return 1
    for entry in entries:
        rec = {'directory': entry.name, 'action': 'observe' if mode != 'install' else 'pending'}
        records.append(rec)
        if mode != 'install':
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise MarketplaceError('not a regular marketplace directory')
            repo = entry.path
            if os.path.realpath(git(repo, 'rev-parse', '--show-toplevel')) != os.path.realpath(repo):
                raise MarketplaceError('not an independent Git checkout')
            git(repo, 'symbolic-ref', '--quiet', 'HEAD')
            if git(repo, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none'):
                raise MarketplaceError('dirty checkout; not pulled')
            before = rec['before'] = git(repo, 'rev-parse', 'HEAD')
            old, unsupported = snapshots(repo, before)
            git(repo, 'pull', '--ff-only', '--no-rebase', '--no-recurse-submodules')
            after = rec['after'] = git(repo, 'rev-parse', 'HEAD')
            new, other = snapshots(repo, after)
            rec['action'] = 'updated' if before != after else 'unchanged'
            unresolved = sorted(set(unsupported + other))
            rec['changed_plugins'] = sorted(key for key in old.keys() | new.keys()
                                            if key not in unresolved
                                            and old.get(key) != new.get(key))
            for key in rec['changed_plugins']:
                report['owner'].plugin_changed(key)
            if unresolved:
                alerts.add('watch', 'marketplace enabled remote sources not content-verified: %s'
                           % ', '.join(unresolved))
        except (OSError, ValueError, KeyError, TypeError, MarketplaceError) as exc:
            rec['action'], rec['error'] = 'failed', str(exc)
            alerts.add('FAIL', 'marketplace %s: %s' % (entry.name, exc))
            rc = 1
    return rc
