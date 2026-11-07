#!/usr/bin/env python3
"""Run-owned dispatch reclaim. No global temp sweep and no branch deletion.

DOGANY_DISPATCH_RECLAIM_HOURS (default 3), KEEP_MAX_HOURS (default 24), and
MIN_FREE_GB (default 10)
accept env overrides; config/agent.conf uses DISPATCH_RECLAIM_HOURS and
DISPATCH_MIN_FREE_GB.
A run/keep or scratch/.keep marker retains failed/blocked resources for at most
KEEP_MAX_HOURS. Completed scratch is reclaimed by the first sweep after finalize.
"""
import argparse
import fcntl
import functools
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from conf_reader import conf_get

MANIFEST = 'reclaim.json'
MARKER = '.dogany-test-scratch'
MARKER_VALUE = 'dogany-test-scratch-v1\n'


def number(workspace, name, default):
    value = os.environ.get('DOGANY_' + name)
    if value is None:
        value = conf_get(name, str(workspace / 'config/agent.conf')) or str(default)
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(name + ' must be a finite nonnegative number')
    return result


def real_dir(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('not an owned directory: ' + str(path))


def read_manifest(run):
    path = run / MANIFEST
    if path.is_symlink():
        raise ValueError('linked reclaim manifest')
    return json.loads(path.read_text()) if path.exists() else {}


def save_manifest(run, doc):
    # Unique temp names tolerate a killed writer; no stale fixed-name file
    # can block every later finalizer. Atomic replace keeps readers coherent.
    with tempfile.NamedTemporaryFile(mode='w', dir=run, prefix='.reclaim-', delete=False) as out:
        tmp = Path(out.name)
        try:
            json.dump(doc, out, sort_keys=True)
            out.write('\n')
            out.close()
            tmp.replace(run / MANIFEST)
        finally:
            tmp.unlink(missing_ok=True)


def locked_run(function):
    @functools.wraps(function)
    def call(run, *args, **kwargs):
        run = Path(run)
        real_dir(run)
        run = run.resolve()
        if function.__name__ == 'sweep_run' and not (args[1] if len(args) > 1 else kwargs['apply']):
            return function(run, *args, **kwargs)
        real_dir(run)
        fd = os.open(run / '.reclaim.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+') as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('invalid reclaim lock')
            fcntl.flock(lock, fcntl.LOCK_EX)
            return function(run, *args, **kwargs)
    return call




def scratch_access(root, retired=False):
    """Clear only user flags on owned inodes, without traversing symlinks.

    Flagged hard links are kept: clearing flags would affect another path.
    Ordinary hard links (including local Git clones) can be unlinked safely.
    Directory access is restored before descent so read-only fixtures work.
    """
    real_dir(root)
    marker = root / MARKER
    if marker.is_symlink() or (marker.exists() and marker.read_text() != MARKER_VALUE):
        raise ValueError('invalid scratch marker: ' + str(root))
    # A partial rmtree may already have removed the inner marker. Only the
    # protected, externally recorded reclaim receipt authorizes that retry.
    if not marker.exists() and not retired:
        raise ValueError('unmarked scratch: ' + str(root))
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if info.st_uid != os.getuid():
            raise ValueError('foreign scratch inode: ' + str(path))
        flags = getattr(info, 'st_flags', 0)
        user_flags = getattr(stat, 'UF_APPEND', 0) | getattr(stat, 'UF_IMMUTABLE', 0)
        if flags & user_flags:
            if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
                raise ValueError('flagged hard-linked scratch inode: ' + str(path))
            os.chflags(path, flags & ~user_flags, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            path.chmod(stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
            pending.extend(path.iterdir())


@locked_run
def retire(run, result):
    run = run.resolve()
    real_dir(run)
    reclaim = run / 'reclaim'
    reclaim.mkdir(mode=0o700, exist_ok=True)
    real_dir(reclaim)
    doc = read_manifest(run)
    # One timestamp per resource, written BEFORE the move: interrupted moves
    # can be retried without extending the evidence window on every sweep.
    source, dest = run / 'tmp', reclaim / 'scratch'
    if source.exists() or source.is_symlink():
        scratch_access(source)
        if 'scratch' not in doc:
            doc['scratch'] = dict(at=time.time(), result=result)
            save_manifest(run, doc)
        source.rename(dest)
    return 0


def keep_markers(run):
    return [p for p in (run / 'keep', run / 'tmp/.keep', run / 'reclaim/scratch/.keep')
            if p.exists() or p.is_symlink()]


@locked_run
def sweep_run(run, hours, apply, now=None, keep_max_hours=24):
    now = time.time() if now is None else now
    real_dir(run)
    doc = read_manifest(run)
    if not doc:
        return 0
    reclaim = run / 'reclaim'
    if reclaim.exists() or reclaim.is_symlink():
        real_dir(reclaim)
    markers = keep_markers(run)
    completed = any(item.get('result') == 'completed' for item in doc.values())
    active_markers = []
    for marker in markers:
        expired = now - marker.lstat().st_mtime >= keep_max_hours * 3600
        if completed:
            print('[dispatch] reclaim ignored keep marker (completed): ' + str(marker))
        elif expired:
            print('[dispatch] reclaim expired keep marker: ' + str(marker))
            if apply:
                marker.unlink()
        else:
            active_markers.append(marker)
    if active_markers:
        print('[dispatch] reclaim kept (evidence marker): ' + str(run))
        return 0
    for kind, item in doc.items():
        if (kind != 'scratch' or
                (item.get('result') != 'completed' and now - float(item['at']) < hours * 3600)):
            continue
        target = reclaim / kind
        # A finalizer interrupted between receipt and move is recoverable by
        # the next cancel/finalize. Never guess that a live source is retired.
        if not target.exists() and not target.is_symlink():
            if apply and (run / '.finalized/complete').is_file():
                if kind == 'scratch' and (run / 'tmp').exists():
                    scratch_access(run / 'tmp')
                    (run / 'tmp').rename(target)
            if not target.exists() and not target.is_symlink():
                continue
        real_dir(target)
        if not apply:
            print('[dispatch] would reclaim: ' + str(target))
            continue
        if active_markers:
            return 0
        if kind == 'scratch':
            scratch_access(target, retired=True)
            shutil.rmtree(target)
        print('[dispatch] reclaimed: ' + str(target))
    return 0


def sweep(workspace, apply):
    root = workspace / '.telegram_bot/dispatch'
    if not root.is_dir():
        return 0
    hours = number(workspace, 'DISPATCH_RECLAIM_HOURS', 3)
    keep_max = number(workspace, 'DISPATCH_KEEP_MAX_HOURS', 24)
    failed = False
    for run in sorted(root.glob('dsp-*')):
        try:
            real_dir(run)
            if (run / MANIFEST).exists():
                sweep_run(run, hours, apply, keep_max_hours=keep_max)
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            print('[dispatch] reclaim retained ' + str(run) + ': ' + str(exc), file=sys.stderr)
            failed = True
    return int(failed)


def disk_check(workspace, paths):
    floor = number(workspace, 'DISPATCH_MIN_FREE_GB', 10) * 1024 ** 3
    for path in paths:
        path = Path(path).absolute()
        while not path.exists() and path.parent != path:
            path = path.parent
        free = shutil.disk_usage(path).free
        if free < floor:
            print('[dispatch] REFUSED: local disk free %.2f GiB < floor %.2f GiB at %s (DOGANY_DISPATCH_MIN_FREE_GB / DISPATCH_MIN_FREE_GB)' %
                  (free / 1024 ** 3, floor / 1024 ** 3, path), file=sys.stderr)
            return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('retire')
    p.add_argument('--rundir', type=Path, required=True)
    p.add_argument('--result', required=True)
    p = sub.add_parser('sweep')
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--apply', action='store_true')
    p = sub.add_parser('disk-check')
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--path', action='append', required=True)
    a = parser.parse_args()
    try:
        if a.command == 'retire':
            return retire(a.rundir, a.result)
        if a.command == 'sweep':
            return sweep(a.workspace, a.apply)
        return disk_check(a.workspace, a.path)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print('[dispatch] storage refused: ' + str(exc).replace('\n', ' '), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
