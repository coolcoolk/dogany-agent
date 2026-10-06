"""DGN-1675: the update notice belongs to OWNER conversation sessions only.

Live evidence (2026-09-24): three fw-update notices for the SAME version, keyed
to three different SessionStart session ids -- the owner bridge session, a
headless `claude -p` cron session and a dispatch worker session -- each
delivered on the next owner turn. The spool creation predicate is
session-scoped by design (DGN-735), so every session that reached it counted.

Discriminator: DOGANY_BRIDGE_SESSION=1, injected by sdk_bridge.py into its SDK
child env and unset by dispatch-detached.sh; cron/launchd never carries it.

Runs the REAL hook against a scratch instance (subprocess, as SessionStart
does). Network-free: dev channel served by a scratch local git repo.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _find_repo_root():
    for cand in Path(__file__).resolve().parents:
        if (cand / "routines" / "version-check.py").is_file():
            return cand
    return None


REPO_ROOT = _find_repo_root()
if REPO_ROOT is None:
    pytest.skip(
        "routines/version-check.py not present in this tree (staged "
        "harness) -- producer tests run in the canonical repo only",
        allow_module_level=True,
    )
SCRIPT_SRC = REPO_ROOT / "routines" / "version-check.py"
SPOOL_REL = Path(".telegram_bot") / "state" / "notice-spool.jsonl"
LOG_REL = Path(".telegram_bot") / "state" / "version-check.log"
VERSION = "2.6.0-dev.40"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                   capture_output=True, text=True)


def _mk_instance(tmp_path):
    repo = tmp_path / "canonical"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "VERSION").write_text("2.6.0\n", encoding="utf-8")
    _git(repo, "add", "VERSION")
    _git(repo, "commit", "-qm", "seed")
    _git(repo, "tag", "v2.6.0-dev.33")
    _git(repo, "tag", "v" + VERSION)
    root = tmp_path / "instance"
    (root / "routines").mkdir(parents=True)
    (root / ".telegram_bot" / "state").mkdir(parents=True)
    shutil.copy2(SCRIPT_SRC, root / "routines" / "version-check.py")
    (root / ".instance.conf").write_text(
        "DOGANY_FW_VERSION=2.6.0\nDOGANY_REPO_ROOT={}\n"
        "DOGANY_UPDATE_CHANNEL=dev\nDOGANY_FW_TAG=v2.6.0-dev.33\n".format(repo),
        encoding="utf-8")
    (root / ".telegram_bot" / ".env").write_text(
        "DOGANY_VERSION_CHECK=0\n", encoding="utf-8")
    return root


def _run_hook(root, session_id, owner):
    env = {k: v for k, v in os.environ.items()
           if k not in ("DOGANY_BRIDGE_SESSION", "DOGANY_LANG",
                        "DOGANY_UPDATE_CHANNEL")}
    env["DOGANY_LANG"] = "en"
    if owner:
        env["DOGANY_BRIDGE_SESSION"] = "1"
    proc = subprocess.run(
        [sys.executable, str(root / "routines" / "version-check.py")],
        input=json.dumps({"session_id": session_id}), capture_output=True,
        text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _spool(root):
    path = root / SPOOL_REL
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(
        encoding="utf-8").splitlines() if l.strip()]


def _fw(root):
    return [r for r in _spool(root) if r.get("kind") == "fw-update"]


def test_three_non_owner_sessions_plus_one_owner_session_one_notice(tmp_path):
    root = _mk_instance(tmp_path)
    # headless cron session, dispatch worker, a second worker: no notice,
    # no injection, no spool record.
    for sid in ("cron-headless", "dispatch-worker-1", "dispatch-worker-2"):
        assert _run_hook(root, sid, owner=False).strip() == ""
    assert _fw(root) == []
    # the owner bridge session gets exactly one.
    out = _run_hook(root, "owner-a", owner=True)
    assert VERSION in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    recs = _fw(root)
    assert len(recs) == 1
    assert recs[0]["version"] == VERSION
    assert recs[0]["status"] == "pending"
    assert recs[0]["context_injected_session_id"] == "owner-a"
    assert "skip(non-owner)" in (root / LOG_REL).read_text(encoding="utf-8")


def test_non_owner_sessions_after_owner_do_not_add_or_rekey(tmp_path):
    root = _mk_instance(tmp_path)
    _run_hook(root, "owner-a", owner=True)
    for sid in ("cron-headless", "dispatch-worker-1", "dispatch-worker-2"):
        assert _run_hook(root, sid, owner=False).strip() == ""
    recs = _fw(root)
    assert len(recs) == 1
    # a headless session must not overwrite the owner's injection marker
    assert recs[0]["context_injected_session_id"] == "owner-a"


def test_delivered_notice_is_not_reopened_by_non_owner_session(tmp_path):
    root = _mk_instance(tmp_path)
    _run_hook(root, "owner-a", owner=True)
    path = root / SPOOL_REL
    recs = _spool(root)
    [r for r in recs if r.get("kind") == "fw-update"][0].update(
        status="delivered", delivered_ts=1.0, message_id=7, chat_id=1)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs),
                    encoding="utf-8")
    assert _run_hook(root, "cron-headless", owner=False).strip() == ""
    assert [r["status"] for r in _fw(root)] == ["delivered"]


def test_owner_session_semantics_are_preserved(tmp_path):
    """DGN-735 intent: once per OWNER session. A second owner session
    re-uses the pending record (no duplicate); a later owner session after
    delivery opens a new round, its own id blocks a repeat."""
    root = _mk_instance(tmp_path)
    _run_hook(root, "owner-a", owner=True)
    out = _run_hook(root, "owner-b", owner=True)
    assert VERSION in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert len(_fw(root)) == 1
    # same owner session id never gets a second injection
    assert _run_hook(root, "owner-b", owner=True).strip() == ""
