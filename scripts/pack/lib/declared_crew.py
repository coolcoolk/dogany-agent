#!/usr/bin/env python3
"""Stage a derived kit's declared crew under the shared crew.conf lock.

Active member= rows are mount-validated. Unmounted declarations remain in
crew.declared-pending; subsequent seeds promote them after an explicit mount.
Writing pending before active and retiring the marker last makes an interrupted
conversion retryable without losing declarations. Readers refuse the marker
until the active registry is ready.
"""
import fcntl
import os
from pathlib import Path
import re
import sys
import tempfile
import time


def atomic(path, body):
    if path.exists() and path.read_text() == body:
        return
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def members(path):
    rows = []
    for line in path.read_text().splitlines() if path.exists() else []:
        if not line or line.startswith('#'):
            continue
        if not line.startswith('member=/'):
            raise ValueError('unparseable member registry %s: %s' % (path, line))
        root = line[len('member='):]
        if root not in rows:
            rows.append(root)
    return rows


def mounted(root, pid):
    # An unreadable instance is uncertainty, never evidence of an unmount.
    lines = (Path(root) / '.instance.conf').read_text().splitlines()
    values = [ln[len('DOGANY_PACKS='):] for ln in lines if ln.startswith('DOGANY_PACKS=')]
    if len(values) > 1:
        raise ValueError('duplicate DOGANY_PACKS at %s' % root)
    packs = values[0].split(',') if values and values[0] else []
    if any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}@[0-9][0-9A-Za-z.+-]*', p) for p in packs):
        raise ValueError('invalid DOGANY_PACKS at %s' % root)
    return any(p.split('@')[0] == pid for p in packs)


def convert(crew, pid, dry=False):
    conf, mark, pending = (crew / name for name in
                           ('crew.conf', 'crew.declared', 'crew.declared-pending'))
    with open(str(conf) + '.lock', 'a') as lock:
        deadline = time.monotonic() + float(os.environ.get('DOGANY_CONF_LOCK_TIMEOUT', '30'))
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('crew.conf lock timeout')
                time.sleep(.05)
        if (crew / '.lease').exists():
            raise ValueError('lease present; declared crew not converted')
        if not mark.exists() and not pending.exists():
            return
        if mark.exists() and ('kit=' + pid) not in mark.read_text().splitlines():
            raise ValueError('declared marker kit mismatch')
        active_before = members(conf)
        declared = list(dict.fromkeys(active_before + members(pending)))
        active, waiting = [], []
        for root in declared:
            (active if mounted(root, pid) else waiting).append(root)
        if not mark.exists() and any(root in waiting for root in active_before):
            raise ValueError('active crew member no longer mounts kit; not demoted')
        if not active:
            raise ValueError('no declared member mounts %s; crew stays declared' % pid)
        if not mark.exists() and active == active_before and members(pending) == waiting:
            return
        action = 'would convert' if dry else 'converted'
        print('[seed] crew %s: declared crew %s to a kit crew (%d mounted member(s), %d declared-pending)' %
              (pid, action, len(active), len(waiting)))
        if dry:
            return
        # Keep a durable union until the active registry is committed. A crash
        # between these writes can only leave duplicate pending entries; retry
        # deduplicates and classifies them from fresh instance evidence.
        if active != active_before and (waiting or pending.exists()):
            atomic(pending, ''.join('member=%s\n' % root for root in declared))
        atomic(conf, ''.join('member=%s\n' % root for root in active))
        if pending.exists():
            atomic(pending, ''.join('member=%s\n' % root for root in waiting))
        if mark.exists():
            dest = crew / ('crew.declared.converted-%s' % time.time_ns())
            os.replace(mark, dest)
            fd = os.open(str(crew), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


if __name__ == '__main__':
    try:
        convert(Path(sys.argv[1]), sys.argv[2], sys.argv[3] == '1')
    except (OSError, ValueError) as exc:
        print('[seed] FAIL: crew %s -- %s -- fail-closed' % (sys.argv[2], exc))
        sys.exit(1)
