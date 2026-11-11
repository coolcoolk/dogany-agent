"""Single-instance guard for the bridge (v2.7 T6 P1, owner dec-319).

One bridge per instance root. At startup `python -m bridge` takes an exclusive
non-blocking flock on <root>/.telegram_bot/state/bridge.lock BEFORE any
Telegram polling, then records {"pid","host","started_at"} in it. The fd stays
open for the process lifetime (the kernel drops the lock when the process
dies, so a crash never leaves a stale lock).

If the lock is held, the caller logs one line naming the holder and exits 75
(EX_TEMPFAIL). Only the restart path (self_restart.sh / watchdog) waits for
the old process to release the lock: it drops a short-lived marker file
(bridge.restart_wait) right before it kills the old bridge, and the new
process polls up to RESTART_WAIT_S for the lock when it sees a fresh marker.
Cold start never waits.
"""

import fcntl
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

EX_TEMPFAIL = 75
RESTART_WAIT_S = 10.0
POLL_S = 0.2
MARKER_MAX_AGE_S = 60.0
HOSTS = ("launchd", "app", "manual")
HOST_ENV = "BRIDGE_HOST_CHILD"

LOCK_REL = os.path.join(".telegram_bot", "state", "bridge.lock")
MARKER_NAME = "bridge.restart_wait"


class LockHeld(Exception):
    """Another bridge holds the instance lock."""

    def __init__(self, holder: Optional[Dict[str, Any]]):
        super().__init__("bridge lock held")
        self.holder = holder or {}


def lock_path(root) -> Path:
    return Path(root) / LOCK_REL


def marker_path(root) -> Path:
    return lock_path(root).parent / MARKER_NAME


def normalize_host(value: Optional[str]) -> str:
    value = (value or "").strip().lower()
    return value if value in HOSTS else "manual"


def read_holder(root) -> Optional[Dict[str, Any]]:
    """Return the recorded holder dict, or None if absent/unreadable."""
    try:
        data = json.loads(lock_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def describe_holder(holder: Optional[Dict[str, Any]]) -> str:
    holder = holder or {}
    return "pid=%s host=%s" % (holder.get("pid", "?"), holder.get("host", "?"))


def restart_wait_requested(root, now: Optional[float] = None) -> bool:
    """True when a restart path left a fresh marker for this root."""
    try:
        age = (time.time() if now is None else now) - marker_path(root).stat().st_mtime
    except OSError:
        return False
    return 0 <= age <= MARKER_MAX_AGE_S


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def acquire(root, host: str = "manual", wait_s: float = 0.0,
            poll_s: float = POLL_S) -> int:
    """Take the instance lock and record the holder; return the open fd.

    The caller must keep the fd open for the process lifetime. Raises
    LockHeld when the lock is still held after waiting up to `wait_s`.
    The file is only truncated AFTER the lock is won, so a losing instance
    never clobbers the holder's record.
    """
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0.0, wait_s)
    while not _try_lock(fd):
        if time.monotonic() >= deadline:
            holder = read_holder(root)
            os.close(fd)
            raise LockHeld(holder)
        time.sleep(poll_s)
    record = {
        "pid": os.getpid(),
        "host": normalize_host(host),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "node": socket.gethostname(),
    }
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps(record).encode("utf-8"))
    os.fsync(fd)
    try:
        marker_path(root).unlink()
    except OSError:
        pass
    return fd


def guard(root, host: Optional[str] = None, logger=None) -> int:
    """Startup entry: acquire or exit 75. Returns the lock fd on success."""
    host = normalize_host(host if host is not None else os.environ.get(HOST_ENV))
    wait_s = RESTART_WAIT_S if restart_wait_requested(root) else 0.0
    try:
        return acquire(root, host=host, wait_s=wait_s)
    except LockHeld as e:
        msg = "bridge already running (%s); exiting %d without polling" % (
            describe_holder(e.holder), EX_TEMPFAIL)
        if logger is not None:
            logger.error(msg)
        else:
            print(msg, flush=True)
        raise SystemExit(EX_TEMPFAIL)


def health_segment(root) -> Optional[str]:
    """One /health line fragment: host + lock holder pid, or None."""
    holder = read_holder(root)
    if not holder or "pid" not in holder:
        return None
    return "host %s, lock pid %s" % (holder.get("host", "?"), holder["pid"])
