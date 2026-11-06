#!/usr/bin/env python3
"""Activation receipts for temporary update stops, never retirement policy."""
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys


def receipt_path(root):
    return Path(root) / '.telegram_bot/update-defer.json'


def read_receipt(root):
    try:
        data = json.loads(receipt_path(root).read_text())
    except FileNotFoundError:
        return {'active': [], 'pending': False, 'deferred': []}
    if (data.get('root') != str(Path(root).resolve())
            or not isinstance(data.get('pending'), bool)
            or not isinstance(data.get('active'), list)
            or any(not isinstance(x, str) for x in data['active'])
            or not isinstance(data.get('deferred', []), list)
            or any(not isinstance(x, str) for x in data.get('deferred', []))):
        raise ValueError('invalid activation receipt')
    return data


def retired_labels(root, include_observer=True):
    """Read owner records, optionally including observer-only retirements."""
    result = set()
    try:
        for line in (Path(root) / '.telegram_bot/retired-routines.tsv').read_text().splitlines():
            if line.strip():
                fields = line.split('\t')
                if len(fields) < 2:
                    raise ValueError('invalid retirement record')
                result.add(fields[0])
    except FileNotFoundError:
        pass
    if not include_observer:
        return result
    state = Path(os.environ.get('DOGANY_HEALTH_OBSERVER_STATE_DIR',
                               str(Path.home() / '.dogany/health-observer'))) / 'jobs.json'
    try:
        jobs = json.loads(state.read_text())['jobs']
        result.update(label for label, entry in jobs.items() if entry.get('retired'))
    except FileNotFoundError:
        pass
    return result


def definitions(root):
    agent = Path(root).name
    try:
        for line in (Path(root) / '.instance.conf').read_text().splitlines():
            if line.startswith('DOGANY_AGENT_NAME='):
                agent = line.split('=', 1)[1]
                break
    except FileNotFoundError:
        pass
    result = {}
    deny = deferred(root)
    for path in sorted((Path(root) / 'routines').glob('*.plist')):
        with path.open('rb') as stream:
            label = plistlib.load(stream)['Label']
        if not re.fullmatch(re.escape('com.telegram-skill-bot.' + agent + '.') + r'[a-z0-9-]+', label):
            if path.name in deny:
                raise ValueError('foreign or invalid routine Label: ' + label)
            continue
        if label in result:
            raise ValueError('duplicate routine Label: ' + label)
        result[label] = path
    return result


def deferred(root):
    try:
        lines = (Path(root) / 'routines/plists.defer').read_text().splitlines()
    except FileNotFoundError:
        return set()
    names = {''.join(line.split('#', 1)[0].split()) for line in lines} - {''}
    if any('/' in name or not name.endswith('.plist') for name in names):
        raise ValueError('invalid deferred plist basename')
    return names


def launchctl(*args):
    return subprocess.run([os.environ.get('ROUTINE_CTL_LAUNCHCTL', 'launchctl'), *args],
                          capture_output=True, text=True, check=False)


def loaded_labels():
    proc = launchctl('list')
    if proc.returncode:
        raise ValueError('cannot read service-manager registrations')
    labels = set()
    for line in proc.stdout.splitlines():
        fields = line.split()
        if fields == ['PID', 'Status', 'Label']:
            continue
        if len(fields) != 3 or not re.fullmatch(r'-|[0-9]+', fields[0]):
            raise ValueError('invalid service-manager registration list')
        labels.add(fields[2])
    return labels


def write_receipt(root, active, pending, deferred_active=()):
    path = receipt_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps({'root': str(Path(root).resolve()),
                               'active': sorted(active), 'pending': pending,
                               'deferred': sorted(deferred_active)}) + '\n')
    tmp.replace(path)


def snapshot(root):
    # Capture ALL active definitions: the incoming manifest may defer a unit
    # that the old manifest did not mention. Retain debt after an interrupted
    # payload; a second stop must not forget registrations already stopped.
    units = definitions(root)
    prior = read_receipt(root)
    active = loaded_labels() & units.keys()
    if prior['pending']:
        active.update(prior['active'])
    active.difference_update(retired_labels(root))
    deny = deferred(root)
    deferred_active = {label for label, path in units.items() if path.name in deny}
    if prior['pending']:
        deferred_active.update(prior.get('deferred', []))
    write_receipt(root, active, True, active & deferred_active)


def restore(root):
    receipt = read_receipt(root)
    if not receipt['pending']:
        return
    units, deny, retired = definitions(root), deferred(root), retired_labels(root)
    active = set(receipt['active']) - retired
    loaded = loaded_labels()
    la = Path(os.environ.get('DOGANY_LAUNCH_AGENTS_DIR',
                            os.environ.get('ROUTINE_CTL_LA_DIR', str(Path.home() / 'Library/LaunchAgents'))))
    for label in sorted(active):
        path = units.get(label)
        if path is None:
            if label in receipt.get('deferred', []) or label + '.plist' in deny:
                raise ValueError('activated definition missing: ' + label)
            # Framework removal of an ordinary routine is not activation debt.
            active.discard(label)
            continue
        if path.name not in deny or label in loaded:
            continue
        la.mkdir(parents=True, exist_ok=True)
        target = la / path.name
        shutil.copyfile(path, target)
        if (launchctl('bootstrap', 'gui/' + str(os.getuid()), str(target)).returncode
                or launchctl('print', 'gui/' + str(os.getuid()) + '/' + label).returncode):
            raise ValueError('activation restore failed: ' + label)
        print('[update-defer] restored activated unit: ' + label)
    write_receipt(root, active, False,
                  {label for label in active if units[label].name in deny})


def plan(root):
    deny = deferred(root)
    for label, path in definitions(root).items():
        if path.name in deny:
            print('[update-defer] preserve existing activation; no disable: ' + label)


def inspect(root):
    deny = deferred(root)
    units = definitions(root)
    retired = retired_labels(root)
    loaded = loaded_labels()
    for label, path in units.items():
        if path.name not in deny:
            continue
        if label in retired and label in loaded:
            state = 'ambiguous (loaded and retired); unchanged'
        elif label in retired:
            state = 'retired; unchanged'
        elif label in loaded:
            state = 'activated; preserve'
        else:
            state = 'inactive; unchanged'
        print('[update-defer] ' + label + ': ' + state)


def main():
    action, root = sys.argv[1:3]
    try:
        if action == 'needed':
            return 0 if definitions(root) or receipt_path(root).exists() else 3
        if action == 'active':
            return 0 if sys.argv[3] in read_receipt(root)['active'] else 1
        {'inspect': inspect, 'plan': plan, 'snapshot': snapshot, 'restore': restore}[action](root)
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, plistlib.InvalidFileException) as exc:
        message = ('restore incomplete; activation receipt retained: ' if action == 'restore'
                   else 'ambiguous state; unchanged: ')
        print('[update-defer] ' + message + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
