#!/usr/bin/env python3
"""Connection files (DGN-1825 v1 + launch extension), framework side. dec-319.

Reader for the app-written com.moha.dogany.json, writer for the framework
companion com.moha.dogany.dogany.json, per-instance MCP registration.

Single writer per file: this module NEVER writes the app file or the host
status file. All paths derive from an explicit home so tests use temp HOME.

CLI:
  connection_files.py sync --home H --version V [--events k1,k2]
                           [--instance ROOT ...]
  connection_files.py ack [--home H]
      Production re-ack (DGN-1825): version from the checkout VERSION file,
      events from `dogany-event kinds` (declared union + framework kinds),
      instances from the dogany-event estate discovery; installs the
      dispatcher at the callback path when it differs; then sync. Writes
      only when the app file sha (or version / events / mcp state) moved
      since the last ack, so it is safe on every tick. Run by the
      health-observer poll (routines/health-observer.py, every 5 min).
Exit 0 ok, 1 error, 3 app file absent (nothing to do; companion untouched).
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader

APP_FILE = "com.moha.dogany.json"
COMPANION_FILE = "com.moha.dogany.dogany.json"
LOCK_FILE = COMPANION_FILE + ".lock"
HOST_STATUS_FILE = "com.moha.dogany.host-status.json"
EVENT_COMMAND_REL = ".dogany/bin/dogany-event"
REPO_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", ".."))


class ContractError(Exception):
    pass


def integrations_dir(home):
    return os.path.join(home, ".dogany", "integrations")


def _no_dup_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ContractError("duplicate key: %s" % k)
        out[k] = v
    return out


def parse_json_bytes(raw):
    """Strict parse: UTF-8 without BOM, no duplicate keys, object root."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ContractError("BOM not allowed")
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
    except (UnicodeDecodeError, ValueError) as e:
        raise ContractError("invalid JSON: %s" % e)
    if not isinstance(doc, dict):
        raise ContractError("root must be an object")
    return doc


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def read_app_file(home):
    """Return (doc, sha256_hex_of_exact_bytes) or None if absent.
    Unknown keys are tolerated. Raises ContractError when malformed."""
    path = os.path.join(integrations_dir(home), APP_FILE)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    doc = parse_json_bytes(raw)
    if not _is_int(doc.get("schema_version")):
        raise ContractError("schema_version must be an integer")
    mcp = doc.get("mcp")
    if mcp is not None:
        if not isinstance(mcp, dict) or not isinstance(mcp.get("name"), str) \
                or not mcp["name"] or not isinstance(mcp.get("command"), str):
            raise ContractError("mcp needs string name and command")
        if not isinstance(mcp.get("args", []), list) or \
                not all(isinstance(a, str) for a in mcp.get("args", [])):
            raise ContractError("mcp.args must be a list of strings")
        env = mcp.get("env", {})
        if not isinstance(env, dict) or \
                not all(isinstance(v, str) for v in env.values()):
            raise ContractError("mcp.env must be a string map")
    return doc, hashlib.sha256(raw).hexdigest()


def launch_host_info(app_doc):
    """launch_host capability (absent/unsupported -> None)."""
    lh = app_doc.get("launch_host")
    if isinstance(lh, dict) and lh.get("supported") is True:
        return lh
    return None


def atomic_write_json(path, doc):
    """JSON serializer only; UTF-8 no BOM; temp + fsync + rename."""
    data = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_companion(home):
    path = os.path.join(integrations_dir(home), COMPANION_FILE)
    try:
        with open(path, "rb") as f:
            return parse_json_bytes(f.read())
    except FileNotFoundError:
        return None
    except ContractError:
        return None  # corrupt companion is rewritten whole


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_companion(home, dogany_version, events, app_sha, mcp_registered,
                    read_at, launch=None, owners=None):
    doc = {
        "schema_version": 1,
        "dogany_version": dogany_version,
        "callback": {
            "transport": "exec",
            "command": os.path.join(home, EVENT_COMMAND_REL),
            "args": [],
            "timeout_s": 15,  # receiver exec timeout; dogany-event caps handlers below it
        },
        # The app's receiver reads the event list from the TOP level as a
        # list of kind names (checked by dogany-receiver --check-dogany-file).
        "events": sorted(set(events)),
        "ack": {
            "app_file_sha256": app_sha,
            "read_at": read_at,
            "mcp_registered": bool(mcp_registered),
            "dogany_version": dogany_version,
        },
    }
    if launch is not None:
        doc["launch"] = launch
    if owners is not None:
        doc["owners"] = owners
    return doc


def register_mcp(instance_root, mcp):
    """Idempotent per-instance write to .claude/settings.local.json.
    Returns True when the entry is verified present after the write."""
    path = os.path.join(instance_root, ".claude", "settings.local.json")
    try:
        with open(path, "rb") as f:
            settings = parse_json_bytes(f.read())
    except FileNotFoundError:
        settings = {}
    servers = settings.get("mcpServers")
    if servers is None:
        servers = settings["mcpServers"] = {}
    if not isinstance(servers, dict):
        raise ContractError("mcpServers is not an object in %s" % path)
    entry = {"command": mcp["command"], "args": list(mcp.get("args", []))}
    if mcp.get("env"):
        entry["env"] = dict(mcp["env"])
    if servers.get(mcp["name"]) != entry:
        servers[mcp["name"]] = entry
        atomic_write_json(path, settings)
    with open(path, "rb") as f:
        return parse_json_bytes(f.read()).get("mcpServers", {}).get(mcp["name"]) == entry


def sync(home, dogany_version, events, instances, launch=None, now=None):
    """Read app file, register MCP in every instance, write companion.
    Returns a status string: 'absent', 'unchanged', 'written'.
    Cheap sha compare: rewrite only when the ack sha, version, events,
    mcp state or launch block changed (read_at alone never forces a write)."""
    app = read_app_file(home)
    if app is None:
        return "absent"
    app_doc, sha = app
    mcp = app_doc.get("mcp")
    mcp_ok = False
    if mcp is not None and instances:
        mcp_ok = True
        for root in instances:
            try:
                if not register_mcp(root, mcp):
                    mcp_ok = False
            except (ContractError, OSError):
                mcp_ok = False
    idir = integrations_dir(home)
    os.makedirs(idir, exist_ok=True)
    with open(os.path.join(idir, LOCK_FILE), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            cur = read_companion(home)
            if launch is None and cur is not None:
                launch = cur.get("launch")  # keep registry owned by later pieces
            owners = cur.get("owners") if cur is not None else None
            if not isinstance(owners, dict):
                owners = None  # kind ownership is written by dogany-event claim
            new = build_companion(home, dogany_version, events, sha, mcp_ok,
                                  (now or _now()), launch, owners)
            if cur is not None:
                a = json.loads(json.dumps(cur))
                b = json.loads(json.dumps(new))
                a.get("ack", {}).pop("read_at", None)
                b["ack"].pop("read_at", None)
                if a == b:
                    return "unchanged"
            atomic_write_json(os.path.join(idir, COMPANION_FILE), new)
            return "written"
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def load_dispatcher(repo=REPO_ROOT):
    """The checkout's scripts/dogany-event as a module (one source for the
    callback kinds and the estate discovery rule)."""
    return SourceFileLoader("dogany_event",
                            os.path.join(repo, "scripts", "dogany-event")).load_module()


def checkout_version(repo=REPO_ROOT):
    with open(os.path.join(repo, "VERSION"), encoding="utf-8") as f:
        v = f.read().strip()
    if not v:
        raise ContractError("empty VERSION in %s" % repo)
    return v


def ack(home, repo=REPO_ROOT, now=None):
    """Re-read the app file and refresh the ack when it moved. Returns the
    sync status ('absent', 'unchanged', 'written')."""
    if read_app_file(home) is None:  # cheap no-op tick: no install, no scan
        return "absent"
    de = load_dispatcher(repo)
    with contextlib.redirect_stdout(sys.stderr):
        de.install(home, os.path.join(repo, "scripts", "dogany-event"))
    return sync(home, checkout_version(repo), de.callback_kinds(home),
                de.estate_roots(home), now=now)


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--home", required=True)
    s.add_argument("--version", required=True)
    s.add_argument("--events", default="")
    s.add_argument("--instance", action="append", default=[])
    k = sub.add_parser("ack")
    k.add_argument("--home", default=os.environ.get("HOME") or os.path.expanduser("~"))
    a = p.parse_args(argv)
    try:
        if a.cmd == "ack":
            r = ack(a.home)
        else:
            r = sync(a.home, a.version, [e for e in a.events.split(",") if e],
                     a.instance)
    except (ContractError, OSError) as e:
        print("connection_files: %s" % e, file=sys.stderr)
        return 1
    print(r)
    return 3 if r == "absent" else 0


if __name__ == "__main__":
    sys.exit(main())
