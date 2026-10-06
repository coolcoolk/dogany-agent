#!/usr/bin/env python3
"""DGN-1786: update <-> retire exclusion, shared by mint_retire.py and update_apply.sh.

Observed 2026-09-28: an /update run treated an agent that had been stopped on
purpose and was mid-retirement as a normal member, re-bootstrapped all of its
units and restarted its bot; the retirement then ended retire.partial. Neither
side knew about the other.

The exclusion is two records, each written before the other is read:
  - a retirement writes <parent>/.retiring/<name>.json (this module) BEFORE it
    looks for an update in flight, and keeps it while the retirement is in
    progress (prepared run, retiring, partial, restoring, restore-partial);
  - an update apply run holds its estate lock (update_apply.sh
    $SHARED_HOME/update-apply.lock) BEFORE it reads a unit's markers, and reads
    them before it stops anything.
So whichever starts second sees the first: the update skips that agent, or the
retirement refuses up front as retire.busy instead of ending partial.

The marker lives beside the instance root (root.parent/.retiring/), never
inside it: retire hashes the body tree and moves it whole, so a marker inside
the body would itself read as "Original body changed".

CLI (update_apply.sh and operator use):
  retire_marker.py check ROOT   rc 0 + "retiring<TAB>name<TAB>receipt" when a
                                retirement holds ROOT; rc 1 when none (or stale)
  retire_marker.py clear ROOT   remove ROOT's marker (operator recovery for a
                                retirement that was abandoned, see DGN-1786)
"""
import json
import os
from pathlib import Path
import sys
import tempfile

def marker_path(root):
    root = Path(root).resolve()
    return root.parent / '.retiring' / (root.name + '.json')


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError, OverflowError):
        return False
    return True


def write(root, **fields):
    path = marker_path(root)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    value = dict(fields, root=str(Path(root).resolve()), pid=os.getpid())
    fd, tmp = tempfile.mkstemp(prefix='.retiring-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    return path


def clear(root, pid=None):
    """Remove ROOT's marker; with pid, only when that process wrote it."""
    path = marker_path(root)
    if pid is not None:
        try:
            if json.loads(path.read_text()).get('pid') != pid:
                return False
        except (OSError, ValueError, AttributeError):
            return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    try:
        path.parent.rmdir()   # leave no trace when this was the only marker
    except OSError:
        pass
    return True


def state(root):
    """None when no retirement holds ROOT, else the marker dict.

    Fail-closed: an unreadable marker holds the root. A marker is stale only
    when its receipt reads 'restored', or when it has no receipt and the
    process that wrote it is gone (killed before its first receipt write --
    nothing was moved, the body is untouched).
    """
    path = marker_path(root)
    if not os.path.lexists(path):
        return None
    try:
        marker = json.loads(path.read_text())
        assert isinstance(marker, dict)
    except Exception:
        return {'root': str(root), 'name': Path(root).name, 'receipt': None,
                'why': 'unreadable marker ' + str(path)}
    receipt = marker.get('receipt')
    status = None
    if receipt:
        try:
            status = json.loads(Path(receipt).read_text()).get('status')
        except Exception:
            status = None   # receipt unreadable: the retirement still holds the root
    if status == 'restored':
        return None
    if not receipt and not _alive(marker.get('pid')):
        return None
    marker['status'] = status
    return marker


def apply_lock_path():
    """The update apply run's estate lock (update_apply.sh, same expression)."""
    if os.environ.get('DOGANY_APPLY_LOCK'):
        return Path(os.environ['DOGANY_APPLY_LOCK'])
    shared = os.environ.get('DOGANY_SHARED_HOME') or str(Path.home() / '.dogany')
    return Path(shared) / 'update-apply.lock'


def update_in_flight():
    """Holder string of a live update apply run, else None.

    Mirrors update_apply.sh _apply_lock_take: a symlink whose target is
    "<pid>:<epoch>:<plan source>"; a holder whose pid is gone is a stale lock
    (update reclaims it), a non-symlink file at the lock path is held.
    """
    lock = apply_lock_path()
    try:
        held = os.readlink(lock)
    except FileNotFoundError:
        return None
    except OSError:
        return '(not a lock symlink: %s)' % lock if os.path.lexists(lock) else None
    pid = held.split(':', 1)[0]
    if pid.isdigit() and _alive(pid):
        return held
    return None


def main(argv):
    if len(argv) != 2 or argv[0] not in ('check', 'clear'):
        print('usage: retire_marker.py check|clear ROOT', file=sys.stderr)
        return 2
    if argv[0] == 'clear':
        print('cleared' if clear(argv[1]) else 'no marker')
        return 0
    marker = state(argv[1])
    if marker is None:
        return 1
    print('retiring\t%s\t%s' % (marker.get('name') or Path(argv[1]).name, marker.get('receipt') or ''))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
