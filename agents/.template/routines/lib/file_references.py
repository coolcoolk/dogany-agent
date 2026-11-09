"""Shared literal-reference predicate (DGN-1580 / enforcement-claim-lint).

The caller owns the search scope. The lint keeps its repository-only scope;
retirement adds installed and loaded units and treats scan errors as holds.
"""
import json
import os
from pathlib import Path
import plistlib


def references(text, name):
    return name in text


def unit_texts(directory, recursive=False, ignore_errors=False, dropins=False):
    root = Path(directory)
    if not root.exists():
        return
    def walk_error(error):
        if not ignore_errors:
            raise error

    def descendants():
        visited = set()
        for base, dirs, files in os.walk(root, followlinks=True, onerror=walk_error):
            real = Path(base).resolve()
            if real in visited:
                dirs[:] = []
                continue
            visited.add(real)
            for name in files:
                yield Path(base) / name

    paths = descendants() if recursive else root.iterdir()
    for path in paths:
        if (path.suffix in (".plist", ".timer", ".service") or
                (dropins and path.suffix == ".conf")) and path.is_file():
            try:
                data = path.read_bytes()
                text = (json.dumps(plistlib.loads(data)) if data.startswith(b"bplist")
                        else data.decode("utf-8", errors="replace"))
                yield path, text
            except (OSError, ValueError):
                if not ignore_errors:
                    raise
