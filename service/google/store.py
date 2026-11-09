"""Machine-wide store. Callers hold the one lock across every transaction."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .errors import GoogleAuthError, redact


def root():
    return Path.home() / '.dogany' / 'google'


def key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value):
        raise GoogleAuthError('auth_unknown')
    return value


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        raise GoogleAuthError('store_unwritable') from None


def setup():
    try:
        for name in ('', 'accounts', 'pending', 'paused', '_archive', 'log'):
            p = root() / name
            p.mkdir(parents=True, exist_ok=True, mode=0o700)
            p.chmod(0o700)
    except OSError:
        raise GoogleAuthError('store_unwritable') from None


@contextlib.contextmanager
def lock(timeout=20):
    setup()
    try:
        fd = os.open(root() / 'store.lock', os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
    except OSError:
        raise GoogleAuthError('store_unwritable') from None
    try:
        until = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise GoogleAuthError('store_busy')
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def write(path, value, exclusive=False, plain=False):
    data = value if plain else json.dumps(value, ensure_ascii=True, sort_keys=True)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            # O_EXCL reserves the name; readers share the store lock.
            try:
                reserve = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return False
            os.close(reserve)
        try:
            os.replace(temp, path)
        except OSError:
            if exclusive:
                path.unlink(missing_ok=True)
            raise
        return True
    except OSError:
        raise GoogleAuthError('store_unwritable') from None
    finally:
        if temp:
            Path(temp).unlink(missing_ok=True)


def default():
    try:
        return key((root() / 'default').read_text().strip())
    except FileNotFoundError:
        return None
    except OSError:
        raise GoogleAuthError('store_unwritable') from None


def record(account=None):
    sub = key(account) if account else default()
    return read(root() / 'accounts' / (sub + '.json')) if sub else None


def first_default(sub):
    write(root() / 'default', key(sub), exclusive=True, plain=True)


def log(message):
    """Caller holds lock. Logging failure must never become a fallback writer."""
    try:
        fd = os.open(root() / 'log/google.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(redact(message).replace('\n', ' ') + '\n')
    except OSError:
        pass


def probe():
    for name in ('pending', 'accounts'):
        try:
            fd, path = tempfile.mkstemp(prefix='.probe-', dir=root() / name)
            os.close(fd)
            Path(path).unlink()
        except OSError:
            raise GoogleAuthError('store_unwritable') from None
