#!/usr/bin/env python3
"""DGN-1646: retire sweeps the family; mint checks only the workspace read key."""
import os
from pathlib import Path
import re
import shutil
import sys


def read_key(workspace):
    # Match the injection lanes' workspace-path encoding; siblings are NOT
    # read keys. Do not turn their harmless residue into a mint refusal.
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(workspace))


def projects():
    return Path.home() / '.claude/projects'


def family(workspace):
    key = read_key(workspace)
    return sorted(p for p in projects().iterdir()
                  if p.name == key or p.name.startswith(key + '-')) if projects().exists() else []


def assert_empty(workspace):
    path = residue(workspace)
    if path:
        raise ValueError('Transcript read key is not empty: ' + str(path) +
                         '; retire/archive the predecessor before minting')


def residue(workspace):
    """The read-key predicate used by both occupancy and mint preflight."""
    path = projects() / read_key(workspace)
    return path if path.exists() and (not path.is_dir() or any(path.iterdir())) else None


def archive(workspace, destination):
    """Move the whole workspace transcript family, reversibly, to destination."""
    destination = Path(destination)
    items = family(workspace)
    if not items:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    for item in items:
        target = destination / item.name
        if target.exists():
            raise ValueError('Transcript archive destination is occupied: ' + str(target))
    for item in items:
        shutil.move(str(item), str(destination / item.name))
    return [item.name for item in items]


def restore(destination):
    """Restore a previously archived family, refusing conflicts before moving."""
    destination = Path(destination)
    items = sorted(destination.iterdir()) if destination.is_dir() else []
    target = projects()
    for item in items:
        if (target / item.name).exists():
            raise ValueError('Transcript restore destination is occupied: ' + str(target / item.name))
    if items:
        target.mkdir(parents=True, exist_ok=True)
    for item in items:
        shutil.move(str(item), str(target / item.name))
    return [item.name for item in items]


def check_restore(destination):
    destination = Path(destination)
    for item in (sorted(destination.iterdir()) if destination.is_dir() else []):
        if (projects() / item.name).exists():
            raise ValueError('Transcript restore destination is occupied: ' + str(projects() / item.name))


if __name__ == '__main__':
    try:
        if len(sys.argv) == 3 and sys.argv[1] == 'list':
            for item in family(sys.argv[2]):
                print(item.name)
        elif len(sys.argv) == 4 and sys.argv[1] == 'archive':
            for name in archive(sys.argv[2], sys.argv[3]):
                print(name)
        elif len(sys.argv) == 3 and sys.argv[1] == 'check-restore':
            check_restore(sys.argv[2])
        elif len(sys.argv) == 3 and sys.argv[1] == 'restore':
            for name in restore(sys.argv[2]):
                print(name)
        else:
            assert_empty(sys.argv[1])
    except (OSError, ValueError) as exc:
        print('mint.transcript-dirty: ' + str(exc), file=sys.stderr)
        sys.exit(1)
