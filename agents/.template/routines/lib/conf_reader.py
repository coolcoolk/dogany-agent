#!/usr/bin/env python3
"""Single conf parser for KEY=value conf files (config/agent.conf and kin).

Root fix for DGN-916: agent.conf is sourced by bash (cron-guard,
generic-brief, secret-sweep, ...), so values containing spaces MUST be
quoted (DGN-899).  Bash strips those quotes automatically; python readers
must strip them too.  Before this module every python consumer parsed the
file with its own inline `split("=", 1)[1].strip()` variant -- some
stripped quotes, some did not, and the mismatch leaked quotes into
user-facing output (status-footer live label bug).  This module is the one
place that owns KEY=value lookup + normalization; python consumers call
conf_get() and never hand-roll the parse again.

Contract:
  - Lookup: first line that starts with "<key>=" wins (after whitespace
    strip); comment/blank lines never match because they cannot start
    with the key.
  - Normalization: value is whitespace-stripped, then surrounding double
    or single quotes are stripped (same normalization the bash `source`
    contract applies).
  - Fail-open: missing file, unreadable file, or missing key -> "".
    Never raises for filesystem trouble; hook callers must not crash.

Promoted from routines/output-gate-stop.py _read_agent_role /
status-footer.py _conf_get (the pre-existing quote-aware parsers).
Pure stdlib.  English/ASCII only.
"""

import os
import tempfile

__all__ = ["conf_get", "conf_remove_if_value", "conf_set_if_absent"]


def _validated_assignment(key, value):
    """Return the encoded assignment after rejecting line injection."""
    if "\n" in key or "\r" in key or "=" in key:
        raise ValueError("conf key must not contain a newline or equals sign")
    if "\n" in value or "\r" in value:
        raise ValueError("conf value must not contain a newline")
    return key.encode("utf-8") + b"=", value.encode("utf-8")


def _atomic_write(path, content, mode=None):
    """Replace path atomically with content, flushing it before publication."""
    path = os.fspath(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agent.conf.", dir=parent)
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_for_update(path):
    try:
        stat = os.stat(path)
        with open(path, "rb") as fh:
            return fh.read(), stat.st_mode & 0o7777
    except FileNotFoundError:
        return b"", None


def conf_set_if_absent(path, key, value):
    """Atomically append KEY=value only when no KEY line exists."""
    prefix, encoded_value = _validated_assignment(key, value)
    content, mode = _read_for_update(path)
    if any(line.startswith(prefix) for line in content.splitlines()):
        return
    separator = b"" if not content or content.endswith((b"\n", b"\r")) else b"\n"
    _atomic_write(path, content + separator + prefix + encoded_value + b"\n", mode)


def conf_remove_if_value(path, key, value):
    """Atomically remove KEY only when its complete value equals value."""
    prefix, encoded_value = _validated_assignment(key, value)
    content, mode = _read_for_update(path)
    lines = content.splitlines(keepends=True)
    kept = []
    removed = False
    for line in lines:
        body = line.rstrip(b"\r\n")
        if not removed and body.startswith(prefix) and body[len(prefix):] == encoded_value:
            removed = True
            continue
        kept.append(line)
    if removed:
        _atomic_write(path, b"".join(kept), mode)


def conf_get(key, conf_path=None):
    """Return the normalized value for `key` from a KEY=value conf file.

    Args:
        key: conf key name (e.g. "PLAN", "DASHBOARD_LIVE_LABEL").
        conf_path: absolute path to the conf file.  None -> resolve
            <cwd>/config/agent.conf (matches the historical default of
            the hook consumers).

    Returns:
        Normalized value string, or "" when the file or key is absent
        (fail-open -- callers chain their own defaults via `or`).
    """
    path = conf_path or os.path.join(os.getcwd(), "config", "agent.conf")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""
