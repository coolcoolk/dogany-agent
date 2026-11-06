#!/usr/bin/env python3
"""Bounded payload pool; the calling shell owns all policy and results.

FD protocol: source resolution begins locked. A capable self-update releases
it only for a capable private payload; update.sh re-acquires before shared
checkout finalization. Unknown/older scripts retain the lock for their whole
run. The descriptor stays open in this supervisor until the child exits.
"""
import concurrent.futures
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def main():
    batch, tmp, width = sys.argv[1:]
    jobs = [line.split('\t') for line in Path(batch).read_text().splitlines()]
    stopped = threading.Event()
    guard = threading.Lock()
    children = set()
    resources = {}

    def interrupt(signum, frame):
        stopped.set()
        with guard:
            for child in children:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)

    def run(job):
        n, name, root, target, script, now, canon = job
        prefix = Path(tmp) / ('fw.%s.%s' % (n, name))
        rc = 125
        may_have_started = False
        # Consume checkouts are normally leader-owned. If configured as the
        # same path (or aliased), serialize those entire writers as well.
        conf = dict(line.split('=', 1) for line in
                    (Path(root) / '.instance.conf').read_text().splitlines() if '=' in line)
        consume = conf.get('DOGANY_CONSUME_ROOT', '')
        with guard:
            resource = resources.setdefault(os.path.realpath(consume) if consume else canon,
                                            threading.Lock())
        try:
            with resource, open(str(prefix) + '.out', 'wb') as out, \
                    open(Path(tmp) / 'fw.shared.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if not stopped.is_set():
                    env = dict(os.environ, DOGANY_UPDATE_STATE_FILE=str(prefix) + '.state',
                               DOGANY_UPDATE_REASON_FILE=str(prefix) + '.reason',
                               DOGANY_GATE_STATE_FILE=str(prefix) + '.gate',
                               DOGANY_DRIVE_DEFER_TO_START='1', DOGANY_UPDATE_DOOR='apply',
                               DOGANY_UPDATE_PIN='v' + target,
                               DOGANY_UPDATE_SHARED_FD=str(lock.fileno()))
                    with guard:
                        if stopped.is_set():
                            return
                        may_have_started = True
                        started = time.monotonic()
                        out.write(('[apply-fw-timing] member=%s event=start wall_s=%.3f\n' %
                                   (name, time.time())).encode())
                        out.flush()
                        child = subprocess.Popen(['bash', script, '--no-restart'], env=env,
                                                 stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                                 pass_fds=(lock.fileno(),), start_new_session=True)
                        children.add(child)
                    try:
                        cancelled_at = None
                        while True:
                            try:
                                rc = child.wait(timeout=.2)
                                break
                            except subprocess.TimeoutExpired:
                                if stopped.is_set():
                                    cancelled_at = cancelled_at or time.monotonic()
                                    if time.monotonic() - cancelled_at > 5:
                                        try:
                                            os.killpg(child.pid, signal.SIGKILL)
                                        except ProcessLookupError:
                                            pass
                    finally:
                        # A shell may exit on TERM before a stubborn grandchild
                        # closes the inherited descriptor. Reap its whole group
                        # before another source-phase waiter can remain stuck.
                        if stopped.is_set():
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        with guard:
                            children.discard(child)
                        out.write(('\n[apply-fw-timing] member=%s event=end wall_s=%.3f elapsed_s=%.3f\n' %
                                   (name, time.time(), time.monotonic() - started)).encode())
                # Close releases a legacy child's lock or a capable child's
                # finalization lock. No worker restarts or mutates parent state.
        except Exception as exc:
            with open(str(prefix) + '.out', 'ab') as out:
                out.write(('worker error: %s\n' % exc).encode())
        finally:
            if not may_have_started:
                Path(str(prefix) + '.not-started').touch()
            # Published atomically: the parent collects each member the moment
            # its status file exists, while sibling payloads are still running.
            rc_tmp = Path(str(prefix) + '.rc.tmp')
            rc_tmp.write_text(str(rc if rc >= 0 else 128 - rc) + '\n')
            os.replace(rc_tmp, str(prefix) + '.rc')

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(width)) as pool:
        for future in [pool.submit(run, job) for job in jobs]:
            future.result()
    return 130 if stopped.is_set() else 0


if __name__ == '__main__':
    sys.exit(main())
