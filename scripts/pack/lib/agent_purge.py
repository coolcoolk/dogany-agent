#!/usr/bin/env python3
"""Purge one over-bound complete retirement archive and its transaction backups.

Reuses dec-352 retention eligibility, bounds and Trash. Dry-run is the default.
Apply requires --confirm TRANSACTION_ID printed by the dry-run. Missing,
linked, foreign, changed and non-complete receipts are protected. Shared DBs
and other transactions' backups are never purge targets. No service commands.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

import mint_retire
import retired_retention as retention


def physical(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise ValueError('physical absolute path required: ' + str(path))
    return path


def fingerprint(path):
    s = path.lstat()
    return [s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns]


def backups(receipt):
    """Only the exact transaction namespace beside recorded crew/ledger files."""
    tx = receipt['transaction_id']
    if not isinstance(tx, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', tx):
        raise ValueError('invalid transaction ID')
    found = set()
    for crew in receipt['crew']:
        registry = physical(crew['registry'])
        prefix = registry.name + '.bak-leave-retire-' + tx + '-'
        recorded = {physical(p) for p in crew.get('backups', [])}
        if any(p.parent != registry.parent or not p.name.startswith(prefix) for p in recorded):
            raise ValueError('crew backup outside transaction namespace')
        found.update(recorded)
        found.update(registry.parent.glob(prefix + '*'))
    ledger = receipt.get('minting_ledger') or {}
    if ledger.get('path'):
        source = physical(ledger['path'])
        expected = source.with_name(source.name + '.bak-retire-' + tx)
        if ledger.get('backup') and physical(ledger['backup']) != expected:
            raise ValueError('ledger backup outside transaction namespace')
        found.add(expected)
    result = []
    for path in sorted(found):
        if not os.path.lexists(path):
            continue
        physical(path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('backup must be a physical regular file: ' + str(path))
        result.append(path)
    return result


def purge(args):
    path = physical(Path(os.path.abspath(args.archive)))
    record = retention.eligible(path)
    if record is None:
        raise ValueError('PROTECTED: requires a valid complete local retirement receipt')
    receipt = mint_retire.load_receipt(path / 'RECEIPT.json')
    # Preserve the approved keep-3/30-day bounds and the newest generation.
    with contextlib.redirect_stdout(io.StringIO()):
        removals = retention.plan(path.parent, dt.datetime.now(dt.timezone.utc))
    if not any(p == path and evidence == record[2] for p, evidence, _ in removals):
        raise ValueError('PROTECTED: archive is within retention bounds or is the newest generation')
    targets = backups(receipt) + [path]  # archive last; its receipt survives a backup failure
    result = {'schema': 'agent.purge/1', 'mode': 'apply' if args.apply else 'dry-run',
              'archive': str(path), 'transaction_id': receipt['transaction_id'],
              'targets': [str(p) for p in targets], 'moved': [], 'rc': 0, 'errors': []}
    if not args.apply:
        return result
    if args.confirm != receipt['transaction_id']:
        raise ValueError('apply requires --confirm ' + receipt['transaction_id'])
    if sys.platform != 'darwin' and not shutil.which('gio'):
        raise ValueError('platform Trash unavailable; refusing purge')
    stamps = {p: fingerprint(p) for p in targets}
    try:
        for target in targets:
            current = retention.eligible(path)
            if current is None or current[2] != record[2] or fingerprint(target) != stamps[target]:
                raise RuntimeError('purge target changed: ' + str(target))
            with contextlib.redirect_stdout(io.StringIO()):
                retention.remove(target, require_trash=True)
            if os.path.lexists(target):
                raise RuntimeError('Trash readback failed: ' + str(target))
            result['moved'].append(str(target))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        result.update(rc=6, errors=[str(error)])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', help='one retirement archive directory')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm', help='exact transaction ID from the dry-run')
    parser.add_argument('--json', action='store_true', help='JSON output (also the default)')
    args = parser.parse_args(argv)
    try:
        result = purge(args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, mint_retire.Refusal) as error:
        result = {'schema': 'agent.purge/1', 'rc': 2, 'errors': [str(error)]}
    print(json.dumps(result, indent=2))
    return result['rc']


if __name__ == '__main__':
    sys.exit(main())
