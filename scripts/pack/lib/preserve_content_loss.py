#!/usr/bin/env python3
"""Preserve accepted pack loss before any overwrite; no automatic pruning.

Snapshot the complete live scan surfaces (a superset of drifted files), their
symlink targets, and the admission evidence. This is forensic preservation,
not a transactionally consistent database backup or an automatic rollback.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile


def preserve(root, pack_id, version, specs, verdict):
    root = Path(os.path.abspath(root))
    if not root.is_dir():
        raise ValueError("instance root is missing")
    dest = root / 'files/_archive/pack-content-loss'
    # Do not redirect the receipt through a symlink outside the instance.
    for parent in [root / 'files', root / 'files/_archive', dest]:
        if parent.is_symlink():
            raise ValueError('archive directory is a symlink: ' + str(parent))
        parent.mkdir(mode=0o700, exist_ok=True)
    paths = {root / '.instance.conf'}
    for spec in specs:
        live = Path(spec.split('\t')[2])
        live.relative_to(root)
        if live.exists() or live.is_symlink():
            paths.add(live)
    fd, tmp = tempfile.mkstemp(prefix='.pending-', dir=dest)
    final = dest / (Path(tmp).name.replace('.pending-', 'loss-') + '.tar.gz')
    receipt = dict(pack_id=pack_id, version=version, verdict=verdict,
                   specs=specs, sha256={}, link_targets={})
    try:
        with os.fdopen(fd, 'wb') as raw:
            with tarfile.open(fileobj=raw, mode='w:gz', dereference=False) as archive:
                def add(path, name):
                    if path.is_symlink():
                        if not path.is_file():
                            raise ValueError('cannot preserve non-file symlink: ' + str(path))
                        archive.add(path, arcname=name, recursive=False)
                        target_name = 'link-targets/' + name
                        receipt['link_targets'][name] = target_name
                        add(path.resolve(strict=True), target_name)
                    elif path.is_dir():
                        archive.add(path, arcname=name, recursive=False)
                        for child in sorted(path.iterdir()):
                            add(child, name + '/' + child.name)
                    elif path.is_file():
                        before = path.stat()
                        info = archive.gettarinfo(str(path), arcname=name)
                        data = path.read_bytes()
                        after = path.stat()
                        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                            raise ValueError('file changed during preservation: ' + str(path))
                        archive.addfile(info, io.BytesIO(data))
                        receipt['sha256'][name] = hashlib.sha256(data).hexdigest()
                    else:
                        raise ValueError('cannot preserve special or missing file: ' + str(path))
                for path in sorted(paths):
                    # A parent symlink would hide the original directory topology.
                    if any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
                        raise ValueError('live surface has a symlink parent: ' + str(path))
                    add(path, 'original/' + str(path.relative_to(root)))
                data = json.dumps(receipt, sort_keys=True, indent=2).encode()
                info = tarfile.TarInfo('receipt.json')
                info.size = len(data)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(data))
            raw.flush()
            os.fsync(raw.fileno())
        with tarfile.open(tmp, 'r:gz') as archive:
            for name, expected in receipt['sha256'].items():
                if hashlib.sha256(archive.extractfile(name).read()).hexdigest() != expected:
                    raise ValueError('archive verification failed: ' + name)
        os.replace(tmp, final)
        dirfd = os.open(dest, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return final
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


if __name__ == '__main__':
    try:
        print(preserve(*sys.argv[1:4], sys.argv[4:], sys.stdin.read()))
    except Exception as exc:
        print('content-loss preservation failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
