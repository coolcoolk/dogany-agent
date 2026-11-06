#!/usr/bin/env python3
"""Generic host test-slot API v1, sharing legacy paths and liveness rules.

This is nonblocking exclusion, not PO admission/scheduling. No new queue or
semaphore. Receipt tokens bind one request for its lifetime, including replay
following release. Legacy heavy-turn/heavy-done/release-marker verbs stay in
host_dispatcher.py; callers keep applying their existing admission policy.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

import host_dispatcher as _host

API = "framework-host-budget/1"
RESOURCES = ("heavy-test", "worker-test")
__all__ = ("API", "RESOURCES", "acquire", "release", "status")


def _paths(resource, token):
    if resource not in RESOURCES:
        raise ValueError("unsupported host resource")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", token):
        raise ValueError("invalid immutable request token")
    return (Path(_host.shared_home()) / "cron-queue" / resource,
            Path(_host.root()) / "budget-receipts" / (token + ".json"))


def _read(path):
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid host budget receipt")
    return value


def _live(slot):
    try:
        pid = (slot / "pid").read_text().strip()
    except FileNotFoundError:
        # The old runner grants 60 seconds for an owner to publish its PID.
        return time.time() - slot.stat().st_mtime <= 60
    return not pid.isdigit() or _host.pid_alive(pid)


def _width(resource):
    if resource == "heavy-test":
        return 1
    width = os.environ.get("DGN_WORKER_TEST_SLOTS", "")
    width = int(width) if width.isdigit() and int(width) > 0 else max(1, (os.cpu_count() or 1) // 4)
    heavy = Path(_host.shared_home()) / "cron-queue/heavy-test/slot-1"
    if heavy.exists() and _live(heavy):
        width = 1
    return width


def _request(resource, token, path, pid=None):
    prior = _read(path)
    if prior is not None:
        identity = dict(resource=resource, token=token, pid=prior.get("pid"))
        if (prior.get("api") != API or prior.get("resource") != resource
                or prior.get("token") != token or type(prior.get("pid")) is not int
                or prior["pid"] <= 0 or prior.get("request") != identity
                or prior.get("state") not in ("busy", "claiming", "held", "released")
                or pid is not None and prior["pid"] != pid):
            raise ValueError("immutable host request conflict or malformed receipt")
        if prior["state"] in ("claiming", "held"):
            directory, _ = _paths(resource, token)
            slot = Path(prior.get("slot", ""))
            if slot.parent != directory or not re.fullmatch(r"slot-[1-9][0-9]*", slot.name):
                raise ValueError("invalid host budget slot receipt")
    return prior


def _owns(slot, record):
    owner = _read(slot / "request.json")
    try:
        pid = (slot / "pid").read_text().strip()
    except FileNotFoundError:
        return False
    return owner == record["request"] and pid == str(record["pid"])


def status(resource, token):
    """Read a snapshot without creating host state or mutating custody."""
    _, path = _paths(resource, token)
    prior = _request(resource, token, path)
    if not prior:
        return dict(api=API, resource=resource, token=token, state="unknown")
    result = dict(prior)
    if prior["state"] in ("held", "claiming"):
        result["state"] = "held" if _owns(Path(prior["slot"]), prior) else "lost"
    return result


def acquire(resource, token, pid):
    """Try once. Busy retains the identical request; terminal replay never grants."""
    if type(pid) is not int or pid <= 0 or not _host.pid_alive(pid):
        raise ValueError("host resource requires a live positive holder PID")
    directory, path = _paths(resource, token)
    with _host.host_lock():
        prior = _request(resource, token, path, pid)
        if prior and prior["state"] != "busy":
            return status(resource, token)
        record = prior or dict(api=API, resource=resource, token=token, pid=pid,
                               request=dict(resource=resource, token=token, pid=pid), state="busy")
        # Freeze intent before touching a slot; this ledger is not a queue.
        _host.write_json(str(path), record)
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(1, _width(resource) + 1):
            slot = directory / ("slot-%d" % index)
            record.update(state="claiming", slot=str(slot))
            _host.write_json(str(path), record)
            try:
                slot.mkdir()
            except FileExistsError:
                try:
                    inode = slot.stat().st_ino
                    old_pid = (slot / "pid").read_text() if (slot / "pid").exists() else None
                    if _live(slot):
                        continue
                    current_pid = (slot / "pid").read_text() if (slot / "pid").exists() else None
                    if slot.stat().st_ino != inode or current_pid != old_pid:
                        continue
                    stale = directory / (".reclaim.%d.%s" % (os.getpid(), uuid.uuid4().hex))
                    slot.rename(stale)
                    shutil.rmtree(stale)
                    slot.mkdir()
                except FileNotFoundError:
                    continue
                except FileExistsError:
                    continue
            try:
                # Match the legacy pid/who layout; leave no PID-less grant.
                (slot / "pid").write_text(str(pid) + "\n")
                (slot / "who").write_text("%d resource=%s token=%s\n" % (pid, resource, token))
                _host.write_json(str(slot / "request.json"), record["request"])
                record.update(state="held", slot=str(slot))
                _host.write_json(str(path), record)
            except OSError:
                # Retain partial ownership for recovery; never report a grant.
                raise
            return record
        record["state"] = "busy"
        record.pop("slot", None)
        _host.write_json(str(path), record)
        return record


def release(resource, token):
    """Idempotent token-bound release; never remove another/legacy holder."""
    _, path = _paths(resource, token)
    with _host.host_lock():
        record = _request(resource, token, path)
        if not record:
            return dict(api=API, resource=resource, token=token, state="unknown")
        if record["state"] in ("held", "claiming"):
            slot = Path(record["slot"])
            if not _owns(slot, record):
                return dict(record, state="lost")
            shutil.rmtree(slot)
        if record["state"] in ("held", "claiming", "busy"):
            record["state"] = "released"
            _host.write_json(str(path), record)
        return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, choices=[API])
    parser.add_argument("verb", choices=("acquire", "release", "status"))
    parser.add_argument("--resource", required=True, choices=RESOURCES)
    parser.add_argument("--token", required=True)
    parser.add_argument("--pid", type=int)
    args = parser.parse_args(argv)
    try:
        result = (acquire(args.resource, args.token, args.pid) if args.verb == "acquire"
                  else globals()[args.verb](args.resource, args.token))
        print(json.dumps(result, sort_keys=True))
        return 1 if result["state"] in ("busy", "lost") else 0
    except (OSError, ValueError, _host.Unavailable) as error:
        print("host budget unavailable: %s" % error, file=sys.stderr)
        return 77


if __name__ == "__main__":
    sys.exit(main())
