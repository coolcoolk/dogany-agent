"""Shared durable storage and validation for opt-in session routing."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time


class Refusal(Exception):
    def __init__(self, rc, message):
        super().__init__(message)
        self.rc = rc


def now_ms():
    return time.time_ns() // 1000000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def integer(value, minimum=None):
    return type(value) is int and (minimum is None or value >= minimum)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal(40, "unreadable durable record: " + str(path)) from exc


def atomic_bytes(path, data, mode=0o600):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    except OSError as exc:
        raise Refusal(40, "durable storage unavailable: " + str(path)) from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise Refusal(40, "durable write failed: " + str(path)) from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    atomic_bytes(path, canonical(value) + b"\n")


@contextmanager
def locked(path, blocking=True, inherited=False):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            os.chmod(path, 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise Refusal(24, "original invocation is still running") from exc
            try:
                yield stream
            finally:
                if not inherited:
                    fcntl.flock(stream, fcntl.LOCK_UN)
    except OSError as exc:
        raise Refusal(40, "durable lock failed: " + str(path)) from exc


def data_dir(root):
    return Path(root) / ".telegram_bot/live-surface"


def chat_lock(root, chat_id):
    if not integer(chat_id):
        raise Refusal(2, "chat id must be an integer")
    return locked(data_dir(root) / (str(chat_id) + ".lock"))


def relative(value):
    if (not isinstance(value, str) or not value or value.startswith("/")
            or any(c in value for c in ("\\", "\x00", "\n", "\r"))
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise Refusal(2, "expected a root-relative path")
    return value


def contained(root, value, exists=True):
    root = Path(root).resolve()
    path = root / relative(value)
    if root not in path.resolve().parents or (exists and not path.is_file()):
        raise Refusal(2, "path missing or outside instance")
    return path


def argv_valid(argv):
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(a, str) or not a or "\x00" in a for a in argv)):
        raise Refusal(2, "expected a nonempty JSON argv")
    return argv


def pack_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_\-]{0,40}", value):
        raise Refusal(2, "invalid pack id")
    return value


def adapter_config(value):
    if (not isinstance(value, dict) or sorted(value) != ["identities", "schema", "toast_copy"]
            or value["schema"] != "live_adapter/1"
            or not isinstance(value["identities"], list)
            or not isinstance(value["toast_copy"], list)):
        raise Refusal(16, "invalid adapter configuration")
    identities = []
    for row in value["identities"]:
        if (not isinstance(row, dict)
                or sorted(row) != ["actor", "chat_id", "member_id", "user_id"]
                or not integer(row["chat_id"]) or not integer(row["user_id"])
                or any(not isinstance(row[k], str) or not row[k] for k in ("actor", "member_id"))
                or (row["chat_id"], row["user_id"]) in identities):
            raise Refusal(16, "invalid or ambiguous identity mapping")
        identities.append((row["chat_id"], row["user_id"]))
    copies = []
    for row in value["toast_copy"]:
        if (not isinstance(row, dict)
                or sorted(row) != ["alert", "diagnostic_key", "status", "text"]
                or row["status"] not in ("ACCEPTED", "DELIVERY_PENDING", "MOVED", "INVALID", "UNAVAILABLE")
                or (row["diagnostic_key"] is not None and not isinstance(row["diagnostic_key"], str))
                or not isinstance(row["text"], str) or len(row["text"]) > 200
                or type(row["alert"]) is not bool
                or (row["status"] in ("ACCEPTED", "DELIVERY_PENDING") and row["alert"])
                or (row["status"], row["diagnostic_key"]) in copies):
            raise Refusal(16, "invalid or ambiguous callback copy")
        copies.append((row["status"], row["diagnostic_key"]))
    return value
