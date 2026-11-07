#!/usr/bin/env python3
"""Explicit .retired retention check; only --apply removes archives.

Bounds apply to complete receipts only. Protected receipts do not consume
slots. Age and ordering use receipt created_at, never filesystem mtime.
No automatic invocation from retirement or restoration.
"""
import argparse
import datetime as dt
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

import mint_retire

# Owner-approved DGN-1648 / dec-352 policy, declared in one place.
POLICY = {'keep': 3, 'max_age_days': 30}


def eligible(path):
    """Fail closed for missing, invalid, non-complete or linked receipts."""
    if path.is_symlink() or not path.is_dir():
        return None
    receipt = path / 'RECEIPT.json'
    if receipt.is_symlink():
        return None
    try:
        r = mint_retire.load_receipt(receipt)
        if r['status'] != 'complete' or not re.fullmatch(r'[A-Za-z0-9_-]+', r['name']):
            return None
        created = dt.datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))
        if created.tzinfo is None:
            return None
        return r['name'], created, receipt.read_bytes()
    except (mint_retire.Refusal, OSError, ValueError, KeyError, TypeError):
        return None


def plan(root, now):
    """Return over-bound complete archives, with evidence for apply readback."""
    groups = {}
    for path in sorted(root.iterdir()):
        record = eligible(path)
        if record:
            name, created, evidence = record
            groups.setdefault(name, []).append((created, path, evidence))
        else:
            print('PROTECTED ' + str(path))
    removals = []
    cutoff = now - dt.timedelta(days=POLICY['max_age_days'])
    for name in sorted(groups):
        archives = sorted(groups[name], key=lambda row: (row[0], row[1].name), reverse=True)
        for rank, (created, path, evidence) in enumerate(archives):
            if rank == 0:
                continue
            reasons = []
            if rank >= POLICY['keep']:
                reasons.append('count')
            if created < cutoff:
                reasons.append('age')
            if reasons:
                removals.append((path, evidence, '+'.join(reasons)))
    return removals


def remove(path, require_trash=False):
    """Use platform Trash; legacy retention alone permits a logged fallback."""
    if sys.platform == 'darwin':
        trash = Path.home() / '.Trash'
        trash.mkdir(mode=0o700, exist_ok=True)
        target = trash / (path.name + '-retention-' + uuid.uuid4().hex)
        shutil.move(str(path), str(target))
        print('TRASH ' + str(path) + ' -> ' + str(target))
    elif shutil.which('gio'):
        # Failure is not permission to bypass an available Trash.
        subprocess.run(['gio', 'trash', str(path)], check=True)
        print('TRASH ' + str(path))
    else:
        if require_trash:
            raise RuntimeError('platform Trash unavailable; refusing removal: ' + str(path))
        print('DELETE-NO-PLATFORM-TRASH ' + str(path), flush=True)
        shutil.rmtree(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-dir', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='dry check (default), exit 1 if over bound')
    mode.add_argument('--apply', action='store_true', help='remove over-bound complete archives')
    args = parser.parse_args(argv)
    root = args.archive_dir.absolute()
    if root.is_symlink() or not root.is_dir():
        parser.error('archive directory must be an existing physical directory')
    removals = plan(root, dt.datetime.now(dt.timezone.utc))
    for path, evidence, reason in removals:
        print('OVER-BOUND ' + str(path) + ' reason=' + reason)
        if args.apply:
            current = eligible(path)
            if current is None or current[2] != evidence:
                raise RuntimeError('receipt changed; refusing removal: ' + str(path))
            remove(path)
    print('retention: over-bound=' + str(len(removals)) + ' mode=' + ('apply' if args.apply else 'check'))
    return 0 if args.apply or not removals else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print('retention: ' + str(error), file=sys.stderr)
        sys.exit(2)
