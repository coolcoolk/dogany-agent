#!/usr/bin/env python3
"""DGN-1981: the one framework secret store (get / set / delete / has).

Every framework reader of a stored key goes through here instead of its own
`security find-generic-password` line or key file:

  keychain  macOS login keychain via /usr/bin/security. Writes go through
            `security -i` on STDIN, so the value never enters an argument
            vector (`ps` shows argv to every local user). Reads of the value
            use `-w`; `has` asks for the item's attributes only, so a
            presence check never reads the secret.
  file      Linux (and any host without a keychain): one file per entry
            under $DOGANY_SECRET_DIR (default ~/.dogany/secrets), dir 0700,
            file 0600, owned by the current user. A file that is readable by
            group/other, or owned by someone else, is refused, not read.

Backend: $DOGANY_SECRET_BACKEND (keychain|file) when set, else keychain on
Darwin and file elsewhere. $DOGANY_SECURITY_BIN overrides the security binary
(tests use a stub).

Rules this module keeps: a value is never printed except by `get` to stdout,
never put in argv, never logged, never part of an exception message.
The Anthropic admin and API-credit slots read account dogany first, then
any account for the same service if absent. Writes and deletes stay exact.
Errors carry a short code (not_found, keychain_locked, insecure_perms,
invalid_value, invalid_name, store_failed) the callers map to owner copy.

CLI (exit 0 ok, 1 not found, 2 usage / invalid input, 3 store error):
  secret_store.py get    --service S [--account A]   value on stdout, no newline
  secret_store.py set    --service S --account A     value on STDIN
  secret_store.py delete --service S --account A
  secret_store.py has    --service S [--account A]
  secret_store.py backend
"""

import argparse
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# Names: what the framework uses plus the CLI's own "Claude Code-credentials".
_NAME_RE = re.compile(r"^[A-Za-z0-9._@ -]{1,128}$")
# Values that can be written: printable ASCII, no whitespace, no quote or
# backslash (they would need escaping inside `security -i`). API keys and
# tokens fit; anything else is refused before it reaches a store.
_VALUE_RE = re.compile(r"^[!#-&(-\[\]-~]{1,4096}$")
_SECURITY_TIMEOUT_S = 20
_ERR_NOT_FOUND = 44  # errSecItemNotFound exit status of security(1)
_ACCOUNT_FALLBACK_SERVICES = frozenset((
    "dogany-anthropic-admin", "dogany-anthropic-api-credit",
))


class SecretStoreError(Exception):
    """A store failure. str() is a fixed sentence; it never holds a value."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def backend_name() -> str:
    forced = os.environ.get("DOGANY_SECRET_BACKEND", "").strip().lower()
    if forced in ("keychain", "file"):
        return forced
    return "keychain" if platform.system() == "Darwin" else "file"


def valid_value(value: str) -> bool:
    return bool(_VALUE_RE.match(value or ""))


def _check_name(kind: str, name: str, allow_empty: bool = False) -> None:
    if allow_empty and name == "":
        return
    if not _NAME_RE.match(name or ""):
        raise SecretStoreError("invalid_name", f"invalid {kind} name")


# ---------------------------------------------------------------------------
# keychain backend
# ---------------------------------------------------------------------------

def _security_bin() -> str:
    return os.environ.get("DOGANY_SECURITY_BIN") or "security"


def _classify_security(rc: int, stderr: str) -> SecretStoreError:
    low = (stderr or "").lower()
    if "interaction is not allowed" in low or "-25308" in low or rc == 36:
        return SecretStoreError(
            "keychain_locked",
            "the keychain is locked or cannot ask for permission from this "
            "process (run it from the logged-in user session)")
    if rc == _ERR_NOT_FOUND or "could not be found" in low:
        return SecretStoreError("not_found", "no such entry")
    return SecretStoreError("store_failed", f"keychain call failed (exit {rc})")


def _security(args, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [_security_bin()] + list(args), input=stdin, capture_output=True,
            text=True, timeout=_SECURITY_TIMEOUT_S)
    except FileNotFoundError:
        raise SecretStoreError("store_failed", "the security tool is not available") from None
    except subprocess.TimeoutExpired:
        raise SecretStoreError("keychain_locked", "the keychain did not answer in time") from None


def _id_args(service: str, account: str):
    out = ["-s", service]
    if account:
        out += ["-a", account]
    return out


def _kc_has(service: str, account: str) -> bool:
    p = _security(["find-generic-password"] + _id_args(service, account))
    if p.returncode == 0:
        return True
    err = _classify_security(p.returncode, p.stderr)
    if err.code == "not_found":
        return False
    raise err


def _kc_get(service: str, account: str) -> Optional[str]:
    p = _security(["find-generic-password"] + _id_args(service, account) + ["-w"])
    if p.returncode == 0:
        value = p.stdout.rstrip("\n")
        return value or None
    err = _classify_security(p.returncode, p.stderr)
    if err.code == "not_found":
        return None
    raise err


def _kc_set(service: str, account: str, value: str) -> None:
    # One interactive-mode command on stdin: the value never enters argv.
    cmd = 'add-generic-password -U -s "%s" -a "%s" -w "%s"\n' % (service, account, value)
    p = _security(["-i"], stdin=cmd)
    del cmd
    # `security -i` can exit 0 after a failed command: confirm the item exists.
    if p.returncode != 0 or not _kc_has(service, account):
        err = _classify_security(p.returncode, p.stderr)
        if err.code == "not_found":
            err = SecretStoreError("store_failed", "the keychain did not keep the entry")
        raise err


def _kc_delete(service: str, account: str) -> bool:
    p = _security(["delete-generic-password"] + _id_args(service, account))
    if p.returncode == 0:
        return True
    err = _classify_security(p.returncode, p.stderr)
    if err.code == "not_found":
        return False
    raise err


# ---------------------------------------------------------------------------
# file backend
# ---------------------------------------------------------------------------

def secret_dir() -> Path:
    override = os.environ.get("DOGANY_SECRET_DIR", "").strip()
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".dogany" / "secrets"


def _entry_path(service: str, account: str) -> Path:
    return secret_dir() / (quote(service, safe="") + "@" + quote(account, safe=""))


def _check_private(path: Path, want_dir: bool) -> None:
    st = os.lstat(path)
    kind_ok = stat.S_ISDIR(st.st_mode) if want_dir else stat.S_ISREG(st.st_mode)
    if not kind_ok or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise SecretStoreError(
            "insecure_perms",
            f"{path} must be owned by this user and mode {'0700' if want_dir else '0600'}")


def _file_get(service: str, account: str) -> Optional[str]:
    path = _entry_path(service, account)
    if not account:
        # Service-only lookup, like security without -a. Sort for stable
        # selection when several accounts have an entry for this service.
        if not path.parent.exists():
            return None
        _check_private(path.parent, want_dir=True)
        prefix = quote(service, safe="") + "@"
        matches = sorted(p for p in path.parent.iterdir() if p.name.startswith(prefix))
        if not matches:
            return None
        path = matches[0]
    if not os.path.lexists(path):
        return None
    _check_private(path.parent, want_dir=True)
    _check_private(path, want_dir=False)
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        raise SecretStoreError("store_failed", "the secret file could not be read") from None
    return value or None


def _file_set(service: str, account: str, value: str) -> None:
    d = secret_dir()
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        raise SecretStoreError("store_failed", "the secret directory could not be created") from None
    _check_private(d, want_dir=True)
    path = _entry_path(service, account)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(d))  # mkstemp: 0600
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise SecretStoreError("store_failed", "the secret file could not be written") from None


def _file_delete(service: str, account: str) -> bool:
    try:
        _entry_path(service, account).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise SecretStoreError("store_failed", "the secret file could not be removed") from None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def _pick(backend: Optional[str]) -> str:
    b = (backend or backend_name()).lower()
    if b not in ("keychain", "file"):
        raise SecretStoreError("invalid_name", "unknown backend")
    return b


def get(service: str, account: str = "", backend: Optional[str] = None) -> Optional[str]:
    """The stored value, or None when there is no entry."""
    _check_name("service", service)
    _check_name("account", account, allow_empty=True)
    read = _kc_get if _pick(backend) == "keychain" else _file_get
    value = read(service, account)
    if value is None and _account_fallback(service, account):
        value = read(service, "")
    return value


def _account_fallback(service: str, account: str) -> bool:
    return account == "dogany" and service in _ACCOUNT_FALLBACK_SERVICES


def has(service: str, account: str = "", backend: Optional[str] = None) -> bool:
    """Presence only. On the keychain this never reads the value."""
    _check_name("service", service)
    _check_name("account", account, allow_empty=True)
    if _pick(backend) == "keychain":
        return (_kc_has(service, account) or
                (_account_fallback(service, account) and _kc_has(service, "")))
    return get(service, account, backend="file") is not None


def set(service: str, account: str, value: str, backend: Optional[str] = None) -> None:  # noqa: A001
    _check_name("service", service)
    _check_name("account", account)
    if not valid_value(value):
        raise SecretStoreError("invalid_value", "the value has characters a key never has")
    if _pick(backend) == "keychain":
        _kc_set(service, account, value)
    else:
        _file_set(service, account, value)


def delete(service: str, account: str, backend: Optional[str] = None) -> bool:
    """True when an entry was removed, False when there was none."""
    _check_name("service", service)
    _check_name("account", account)
    if _pick(backend) == "keychain":
        return _kc_delete(service, account)
    return _file_delete(service, account)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="secret_store.py", add_help=True)
    ap.add_argument("op", choices=("get", "set", "delete", "has", "backend"))
    ap.add_argument("--service", default="")
    ap.add_argument("--account", default="")
    ap.add_argument("--backend", default=None, choices=("keychain", "file"))
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    if a.op == "backend":
        print(backend_name())
        return 0
    try:
        if a.op == "get":
            value = get(a.service, a.account, a.backend)
            if value is None:
                return 1
            sys.stdout.write(value)
            return 0
        if a.op == "has":
            return 0 if has(a.service, a.account, a.backend) else 1
        if a.op == "delete":
            return 0 if delete(a.service, a.account, a.backend) else 1
        value = sys.stdin.read().strip()
        set(a.service, a.account, value, a.backend)
        del value
        return 0
    except SecretStoreError as e:
        print(f"secret_store: {e.code}: {e}", file=sys.stderr)
        return 2 if e.code in ("invalid_name", "invalid_value") else 3


if __name__ == "__main__":
    sys.exit(main())
