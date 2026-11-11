#!/usr/bin/env python3
"""Regenerable dispatch JSONL index and reversible 30-day retention."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

DAYS = 30
TEXT_LIMIT = 4096
TERMINAL = {'completed', 'error', 'timed-out', 'canceled', 'incomplete',
            'needs-validation', 'replacing', 'replaced', 'pending-review'}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def regular(path):
    return path.is_file() and not path.is_symlink()


def excerpt(path, tail=False):
    if not regular(path):
        return ''
    with path.open('rb') as handle:
        if tail:
            handle.seek(max(0, path.stat().st_size - TEXT_LIMIT))
        return handle.read(TEXT_LIMIT).decode('utf-8', errors='replace')


def read_env(path):
    values = {}
    # Parse shell quoting only: never source archived executable text.
    for line in (path.read_text(errors='replace').splitlines() if regular(path) else []):
        key, eq, value = line.partition('=')
        if eq:
            try:
                parts = shlex.split(value)
                values[key] = parts[0] if parts else ''
            except ValueError:
                pass
    return values


def record(path, location):
    status, issue = {}, ''
    try:
        if not regular(path / 'status.json'):
            raise ValueError('missing status')
        status = json.loads((path / 'status.json').read_text())
        if not isinstance(status, dict):
            raise ValueError('status is not an object')
    except (OSError, ValueError) as exc:
        status, issue = {}, str(exc)
    env = read_env(path / 'run.env')
    started = status.get('started_at') or env.get('STARTED_AT', '')
    ended = status.get('ended_at', '')
    begin, end = timestamp(started), timestamp(ended)
    review = status.get('review_state', '')
    pending = (status.get('pending_review') is True or review == 'pending-review'
               or status.get('result') == 'pending-review'
               or (not review and bool(status.get('commits'))))
    sizes = {}
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(base) / d).is_symlink())
        for name in sorted(files):
            file = Path(base) / name
            if regular(file):
                sizes[str(file.relative_to(path))] = file.stat().st_size
    return dict(runid=path.name, path=str(path), location=location,
                routing={key: status.get(key, env.get(key.upper(), '')) for key in
                         ('runtime', 'model', 'tier', 'ticket', 'branch', 'selection_reason')},
                result=status.get('result', 'unknown'), started_at=started, ended_at=ended,
                duration_seconds=(end - begin if end is not None and begin is not None else None),
                commit_outcome={key: status.get(key) for key in
                                ('commits', 'head', 'preserve', 'artifact_landing')},
                pending_review=pending, status_issue=issue, file_sizes=sizes,
                total_bytes=sum(sizes.values()), brief=excerpt(path / 'brief.md'),
                final_report=excerpt(path / 'out.log', tail=True))


def scan(archive, trash):
    rows = []
    for root, location in ((archive, 'archive'), (trash, 'trash')):
        if root.exists():
            for path in sorted(root.glob('dsp-*')):
                if path.is_dir() and not path.is_symlink():
                    rows.append(record(path, location))
    return rows


def eligible(row, now):
    ended = timestamp(row['ended_at'])
    return (row['location'] == 'archive' and not row['status_issue']
            and row['result'] in TERMINAL and not row['pending_review']
            and ended is not None and now - ended > DAYS * 86400)


def retain(archive, trash, now, apply=False):
    # Apply is serialized; index/dry-run never writes even a lock file.
    lock = archive / '.dispatch-archive-retention.lock'
    if apply:
        lock.mkdir()
    try:
        candidates = [r for r in scan(archive, trash) if eligible(r, now)]
        if apply and candidates:
            trash.mkdir(parents=True, exist_ok=True)
            # Refuse the batch before moving anything on known collisions/EXDEV.
            for row in candidates:
                dest = trash / row['runid']
                if os.path.lexists(dest):
                    raise ValueError('Trash destination exists: ' + str(dest))
                if Path(row['path']).stat().st_dev != trash.stat().st_dev:
                    raise ValueError('Trash must be on the archive filesystem')
        actions = []
        for row in candidates:
            dest = trash / row['runid']
            if apply:
                # Re-read eligibility immediately before rename. No copy/delete fallback.
                if not eligible(record(Path(row['path']), 'archive'), now):
                    continue
                os.rename(row['path'], dest)
            actions.append(dict(runid=row['runid'], source=row['path'], destination=str(dest),
                                action='moved' if apply else 'would-move'))
        return actions
    finally:
        if apply:
            lock.rmdir()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['index', 'retain'])
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--trash', type=Path, help='dedicated Trash directory, same filesystem')
    parser.add_argument('--apply', action='store_true', help='retain only: move eligible runs')
    parser.add_argument('--now', help='timezone-aware ISO timestamp (fixture clock)')
    args = parser.parse_args(argv)
    archive = args.archive.resolve()
    namespace = hashlib.sha256(str(archive).encode()).hexdigest()[:16]
    trash = (args.trash or Path.home() / '.Trash' / 'dogany-dispatch' / namespace).resolve()
    if not archive.is_dir():
        parser.error('archive must be an existing directory')
    if archive == trash or archive in trash.parents or trash in archive.parents:
        parser.error('archive and Trash must be separate directory trees')
    if args.apply and args.command != 'retain':
        parser.error('--apply is only valid with retain')
    now = timestamp(args.now) if args.now else datetime.now(timezone.utc).timestamp()
    if now is None:
        parser.error('--now must include a timezone')
    try:
        rows = (scan(archive, trash) if args.command == 'index' else
                retain(archive, trash, now, args.apply))
        for row in rows:
            print(json.dumps(row, sort_keys=True))
    except (OSError, ValueError) as exc:
        print('dispatch-archive: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
