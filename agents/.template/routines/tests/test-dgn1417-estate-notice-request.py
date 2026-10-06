#!/usr/bin/env python3
# test-lane: fast
"""DGN-1417 round 3: the session-start hook on an estate host.

Owner rehearsal 2026-09-26 section D item 17: the session-start notice covers
framework + kits + packs in ONE message, fed by the planner /update uses (no
second measurement), replacing the framework-only notice.  On a host whose
DOGANY_REPO_ROOT carries scripts/pack/update_surface.py this hook therefore:

  (a) leaves the request for the bridge (source session-start, session id)
      and injects NO framework-only notice, even with a newer dev tag
  (b) supersedes a pending framework-only notice (never both)
  (c) a non-owner session leaves no request (DGN-1675 gate unchanged)
  (d) a host without the machinery keeps the framework-only notice
  (e) check 3 (landed but not running) still fires on an estate host
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "vc_dgn1417", os.path.join(HERE, "..", "version-check.py"))
vc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vc)

FAILS = []


def check(name, cond, detail=""):
    print("{} ({}){}".format("PASS" if cond else "FAIL", name, "" if cond else ": " + detail))
    if not cond:
        FAILS.append(name)


def git(*args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git"] + list(args), check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fixture(estate):
    tmp = tempfile.mkdtemp(prefix="dgn1417vc-")
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "scripts", "pack"))
    if estate:
        open(os.path.join(repo, "scripts", "pack", "update_surface.py"), "w").close()
    open(os.path.join(repo, "VERSION"), "w").write("2.6.0\n")
    git("init", "-q", repo)
    git("-C", repo, "add", "-A")
    git("-C", repo, "commit", "-q", "-m", "x")
    git("-C", repo, "tag", "v2.6.0-dev.53")
    root = os.path.join(tmp, "inst")
    os.makedirs(os.path.join(root, ".telegram_bot", "state"))
    open(os.path.join(root, ".instance.conf"), "w").write(
        "DOGANY_FW_VERSION=2.6.0\nDOGANY_FW_TAG=v2.6.0-dev.52\n"
        "DOGANY_UPDATE_CHANNEL=dev\nDOGANY_REPO_ROOT={}\n".format(repo))
    return tmp, root


def run(root, owner=True, session="s-1"):
    vc.__file__ = os.path.join(root, "routines", "version-check.py")
    old = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"session_id": session}))
    buf = io.StringIO()
    env = {"DOGANY_BRIDGE_SESSION": "1"} if owner else {"DOGANY_BRIDGE_SESSION": ""}
    try:
        with patch.dict(os.environ, env), contextlib.redirect_stdout(buf):
            try:
                vc.main()
            except SystemExit:
                pass
    finally:
        sys.stdin = old
    return buf.getvalue()


def request(root):
    path = os.path.join(root, ".telegram_bot", "state", "update-offer-request")
    if not os.path.isfile(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def spool(root):
    path = os.path.join(root, ".telegram_bot", "state", "notice-spool.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main():
    tmp, root = fixture(estate=True)
    try:
        out = run(root)
        req = request(root)
        check("a-request-left", req is not None and req["source"] == "session-start"
              and req["session_id"] == "s-1", repr(req))
        check("a-no-framework-only-notice", "framework update available" not in out
              and "업데이트 있어요" not in out, out)
        check("a-no-fw-update-record", not [r for r in spool(root) if r.get("kind") == "fw-update"], repr(spool(root)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    tmp, root = fixture(estate=True)
    try:
        path = os.path.join(root, ".telegram_bot", "state", "notice-spool.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"id": "ntc-1", "kind": "fw-update", "version": "2.6.0-dev.53",
                                 "status": "pending", "attempts": 0, "body_fold": "도가니 업데이트 있어요"}) + "\n")
        run(root)
        recs = spool(root)
        check("b-pending-fw-only-superseded", recs and recs[0]["status"] == "expired"
              and recs[0]["expired_reason"] == "superseded", repr(recs))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    tmp, root = fixture(estate=True)
    try:
        run(root, owner=False)
        check("c-non-owner-no-request", request(root) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    tmp, root = fixture(estate=False)
    try:
        out = run(root)
        check("d-public-tree-keeps-framework-notice", request(root) is None
              and "framework update available" in out, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    tmp, root = fixture(estate=True)
    try:
        conf = os.path.join(root, ".instance.conf")
        text = open(conf).read()
        open(conf, "w").write(text.replace("DOGANY_FW_TAG=v2.6.0-dev.52", "DOGANY_FW_TAG=v2.6.0-dev.53")
                              .replace("DOGANY_FW_VERSION=2.6.0", "DOGANY_FW_VERSION=2.6.1"))
        with open(os.path.join(root, ".telegram_bot", "runtime-snapshot.json"), "w") as fh:
            json.dump({"schema": 1, "pid": os.getpid(), "fw_version": "2.6.0"}, fh)
        out = run(root)
        check("e-check3-still-runs", "2.6.1" in out and request(root) is not None, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILS:
        print("FAILED: " + ", ".join(FAILS))
        return 1
    print("All DGN-1417 estate notice-request tests PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
