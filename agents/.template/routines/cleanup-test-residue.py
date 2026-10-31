#!/usr/bin/env python3
"""DRAFT: explicit test residue only; dry-run unless --apply. No age-only sweep."""
import argparse
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

# Deliberately no tmp*, dgn*, model-* or mig* wildcard.
PREFIXES = (
    'program-plan-test-', 'program-rehearsal-', 'bridge-test-',
    'model-state-test-', 'model-cmd-test-', 'dgn1141-vendors-',
    'dgn1141-root-', 'dgn1141-boot-', 'dgn1256-vendors-', 'dgn618-test-',
)


def eligible(path, cutoff):
    if not path.name.startswith(PREFIXES):
        return False
    # Check descendants too: never move a directory containing evidence,
    # a symlink, another owner's data, or a recently written file.
    pending = [path]
    while pending:
        item = pending.pop()
        if 'evidence' in item.name.lower() or item.name.startswith('com.apple.'):
            return False
        info = item.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mtime >= cutoff):
            return False
        if stat.S_ISDIR(info.st_mode):
            pending.extend(item.iterdir())
        elif not stat.S_ISREG(info.st_mode):
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument('--trash', type=Path, default=Path.home() / '.Trash')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    trash = args.trash.resolve()
    if root == Path('/') or root == trash or root in trash.parents:
        parser.error('unsafe root/trash relationship')
    cutoff = time.time() - 3 * 60 * 60
    for path in sorted(root.iterdir()):
        try:
            if not eligible(path, cutoff):
                continue
            if args.apply:
                trash.mkdir(mode=0o700, parents=True, exist_ok=True)
                # Recheck immediately before rename; never recursively delete.
                # Cross-device moves fail closed rather than copying/deleting.
                if not eligible(path, cutoff):
                    continue
                path.rename(trash / (path.name + '.' + uuid.uuid4().hex))
            print(('DRAFT en/ko: trashed / 휴지통 이동' if args.apply else 'DRAFT en/ko: would-trash / 휴지통 이동 예정') + '\t' + str(path))
        except OSError as exc:
            print('skip\t' + str(path) + '\t' + str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
