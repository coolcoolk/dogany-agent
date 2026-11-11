#!/usr/bin/env python3
"""Replace only the pack-owned bootstrap directory, with rollback on failure."""
import os
from pathlib import Path
import shutil
import sys
import tempfile


def replace(source, destination):
    if destination.is_symlink():
        raise ValueError('bootstrap destination must not be a symlink')
    temp = Path(tempfile.mkdtemp(prefix='.bootstrap-', dir=destination.parent))
    old = temp / 'old'
    try:
        shutil.copytree(source, temp / 'new')
        if destination.exists():
            os.rename(destination, old)
        try:
            os.rename(temp / 'new', destination)
        except OSError:
            if old.exists():
                os.rename(old, destination)
            raise
    finally:
        # A failed rollback keeps its recovery copy for diagnosis.
        if destination.exists() or not old.exists():
            shutil.rmtree(temp)


if __name__ == '__main__':
    replace(Path(sys.argv[1]), Path(sys.argv[2]))
