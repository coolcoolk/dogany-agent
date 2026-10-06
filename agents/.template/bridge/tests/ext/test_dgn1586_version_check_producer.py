"""DGN-1586 producer side: routines/version-check.py spool creation.

Runs the REAL hook script against a scratch instance root (subprocess, the
same way SessionStart runs it) plus unit-level calls via importlib. Covers:
  - spec 3.4 creation idempotency (parallel SessionStarts -> ONE pending)
    and the multi-version supersede as one locked change.
  - spec 3.2 kind split at the producer (check 3 -> kind=pending-restart,
    resolution only on snapshot_fw == built).
  - spec 3.3 surface: bridge marker -> machine-delivery framing (no
    VERBATIM relay), limited by the SessionStart payload session_id.
    DGN-1675: a non-bridge session produces no notice at all (see
    test_dgn1675_update_notice_owner_sessions.py).
  - spec 4 Q4 migration: `unconsumed:` prefix -> kind discriminator,
    legacy-assumed delivered compat record, idempotent importer.
  - session-scoped announcement: a new SessionStart payload id and a
    still-live detection condition -> a NEW pending round.
  - the automatic-turn rail (push.sh) has no spool coupling at all.

Network-free by construction: no DOGANY_REPO_ROOT (check 1 skipped) and
DOGANY_VERSION_CHECK=0 (check 2 disabled), so only check 3 and the spool
machinery run.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _find_repo_root():
    """Walk up from this file until a dir carrying routines/version-check.py
    appears. In the extract-bridge gate-3 staging the bridge tree is copied
    OUT of the repo, so the script is absent there -- these producer tests
    then skip (the canonical suite is their required surface)."""
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
SHOWN_REL = Path(".telegram_bot") / "state" / "version-notice-shown"


def _mk_instance(tmp_path: Path, built="2.6.0", snapshot_fw="2.5.0") -> Path:
    root = tmp_path / "instance"
    (root / "routines").mkdir(parents=True)
    (root / ".telegram_bot" / "state").mkdir(parents=True)
    shutil.copy2(SCRIPT_SRC, root / "routines" / "version-check.py")
    (root / ".instance.conf").write_text(
        "DOGANY_FW_VERSION={}\n".format(built), encoding="utf-8")
    (root / ".telegram_bot" / ".env").write_text(
        "DOGANY_VERSION_CHECK=0\n", encoding="utf-8")
    if snapshot_fw is not None:
        (root / ".telegram_bot" / "runtime-snapshot.json").write_text(
            json.dumps({"schema": 1, "pid": os.getpid(),
                        "fw_version": snapshot_fw}),
            encoding="utf-8")
    return root


def _run_hook(root: Path, bridge=True, session_id="session-a") -> str:
    # DGN-1675: the notice path is owner-session only, so the default caller
    # is the bridge; pass bridge=False for a headless/worker/CLI session.
    env = {k: v for k, v in os.environ.items()
           if k not in ("DOGANY_BRIDGE_SESSION", "DOGANY_LANG")}
    env["DOGANY_LANG"] = "en"
    if bridge:
        env["DOGANY_BRIDGE_SESSION"] = "1"
    proc = subprocess.run(
        [sys.executable, str(root / "routines" / "version-check.py")],
        input=json.dumps({"session_id": session_id}), capture_output=True,
        text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _spool(root: Path):
    path = root / SPOOL_REL
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _pendings(root: Path, kind=None):
    return [r for r in _spool(root)
            if r.get("status") == "pending"
            and (kind is None or r.get("kind") == kind)]


def _seed_spool(root: Path, records):
    path = root / SPOOL_REL
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _mgmt():
    return {"id": "mgmt:legacy-import", "kind": "management",
            "migrated_ts": time.time()}


def _rec(kind, version, status="pending", **over):
    rec = {
        "id": "ntc-seed-{}-{}".format(kind, version),
        "kind": kind, "version": version, "audience": "owner",
        "first_pending_ts": time.time() - 60, "attempts": 0,
        "status": status, "expired_reason": None, "delivered_ts": None,
        "chat_id": None, "message_id": None,
        "body_oneline": "seed body", "body_fold": "",
        "context_injected_ts": None, "origin": "version-check",
    }
    rec.update(over)
    return rec


# ---------------------------------------------------------------------------
# check 3 -> pending-restart creation, idempotency, injection throttle
# ---------------------------------------------------------------------------

def test_check3_creates_one_pending_idempotently(tmp_path):
    root = _mk_instance(tmp_path)
    out1 = _run_hook(root)
    out2 = _run_hook(root)
    pend = _pendings(root, "pending-restart")
    assert len(pend) == 1                      # verification: idempotency
    assert pend[0]["version"] == "2.6.0"
    assert pend[0]["attempts"] == 0
    # Owner (bridge) surface: injected once per SessionStart session_id.
    assert "additionalContext" in out1
    assert "DELIVERY IS MACHINE-OWNED" in out1
    assert out2.strip() == ""
    assert pend[0]["context_injected_ts"] is not None


def test_bridge_surface_gets_machine_delivery_framing(tmp_path):
    root = _mk_instance(tmp_path)
    out1 = _run_hook(root, bridge=True, session_id="session-a")
    out2 = _run_hook(root, bridge=True, session_id="session-b")
    assert "DELIVERY IS MACHINE-OWNED" in out1
    assert "VERBATIM" not in out1
    assert "Do NOT relay" in out1
    # Bridge injection is informational context, once per session; the spool
    # still holds exactly one pending.
    assert "DELIVERY IS MACHINE-OWNED" in out2
    assert len(_pendings(root, "pending-restart")) == 1
    assert _pendings(root)[0]["context_injected_session_id"] == "session-b"


def test_snapshot_equal_resolves_pending(tmp_path):
    # Verification step 5: kind=pending-restart dies ONLY on
    # snapshot_fw == built.
    root = _mk_instance(tmp_path, built="2.6.0", snapshot_fw="2.6.0")
    _seed_spool(root, [_mgmt(), _rec("pending-restart", "2.6.0")])
    out = _run_hook(root)
    assert out.strip() == ""
    recs = [r for r in _spool(root) if r.get("kind") == "pending-restart"]
    assert recs[0]["status"] == "expired"
    assert recs[0]["expired_reason"] == "resolved"


def test_missing_snapshot_preserves_pending(tmp_path):
    root = _mk_instance(tmp_path, built="2.6.0", snapshot_fw=None)
    _seed_spool(root, [_mgmt(), _rec("pending-restart", "2.6.0")])
    _run_hook(root)
    assert len(_pendings(root, "pending-restart")) == 1


def test_fw_update_pending_resolves_when_built_catches_up(tmp_path):
    # Producer half of self-invalidation: the owner updated; the stale
    # fw-update pending expires on the next hook run.
    root = _mk_instance(tmp_path, built="2.6.0", snapshot_fw="2.6.0")
    _seed_spool(root, [_mgmt(), _rec("fw-update", "2.6.0")])
    _run_hook(root)
    recs = [r for r in _spool(root) if r.get("kind") == "fw-update"]
    assert recs[0]["status"] == "expired"
    assert recs[0]["expired_reason"] == "resolved"


# ---------------------------------------------------------------------------
# session-scoped re-announce + exhaustion block
# ---------------------------------------------------------------------------

def test_new_session_reannounces_after_delivery(tmp_path):
    root = _mk_instance(tmp_path)  # built 2.6.0, snapshot 2.5.0 -> live
    delivered = _rec("pending-restart", "2.6.0", status="delivered",
                     delivered_ts=time.time() - 1,
                     context_injected_session_id="session-a",
                     message_id=42, chat_id=111)
    _seed_spool(root, [_mgmt(), delivered])
    _run_hook(root, session_id="session-b")
    pend = _pendings(root, "pending-restart")
    assert len(pend) == 1                       # new round, new id
    assert pend[0]["id"] != delivered["id"]
    assert pend[0]["attempts"] == 0
    # The delivered receipt of the old round is untouched.
    old = [r for r in _spool(root) if r["id"] == delivered["id"]][0]
    assert old["status"] == "delivered" and old["message_id"] == 42


def test_delivered_same_session_blocks_new_round(tmp_path):
    root = _mk_instance(tmp_path)
    _seed_spool(root, [_mgmt(), _rec(
        "pending-restart", "2.6.0", status="delivered",
            delivered_ts=time.time() - 24 * 3600,
            context_injected_session_id="session-a")])
    out = _run_hook(root, session_id="session-a")
    assert out.strip() == ""
    assert _pendings(root) == []


def test_attempts_exhausted_blocks_auto_recreation(tmp_path):
    root = _mk_instance(tmp_path)
    _seed_spool(root, [_mgmt(), _rec(
        "pending-restart", "2.6.0", status="expired",
        expired_reason="attempts_exhausted", attempts=3)])
    out = _run_hook(root)
    assert out.strip() == ""
    assert _pendings(root) == []


# ---------------------------------------------------------------------------
# migration (spec 4 Q4 / MINOR-2)
# ---------------------------------------------------------------------------

def test_migration_unconsumed_prefix_maps_to_pending_restart(tmp_path):
    root = _mk_instance(tmp_path)
    legacy_ts = time.time() - 2 * 24 * 3600
    (root / SHOWN_REL).write_text(
        "unconsumed:2.5.0 {}\n".format(legacy_ts), encoding="utf-8")
    _run_hook(root)
    legacy = [r for r in _spool(root) if r.get("origin") == "legacy-assumed"]
    assert len(legacy) == 1
    assert legacy[0]["kind"] == "pending-restart"
    assert legacy[0]["version"] == "2.5.0"      # prefix stripped EXACTLY once
    assert legacy[0]["status"] == "delivered"
    assert legacy[0]["message_id"] is None      # compat value, not a receipt
    assert abs(legacy[0]["delivered_ts"] - legacy_ts) < 1
    # Importer is idempotent: a second run adds no second legacy record.
    _run_hook(root)
    legacy = [r for r in _spool(root) if r.get("origin") == "legacy-assumed"]
    assert len(legacy) == 1
    mgmt = [r for r in _spool(root) if r.get("id") == "mgmt:legacy-import"]
    assert len(mgmt) == 1


def test_migration_plain_key_maps_to_fw_update_and_suppresses(tmp_path):
    # A legacy record has no SessionStart id, so it remains a receipt but
    # cannot suppress a new session's first announcement.
    root = _mk_instance(tmp_path)
    (root / SHOWN_REL).write_text(
        "2.7.0 {}\n".format(time.time() - 24 * 3600), encoding="utf-8")
    _run_hook(root)
    legacy = [r for r in _spool(root) if r.get("origin") == "legacy-assumed"]
    assert len(legacy) == 1 and legacy[0]["kind"] == "fw-update"
    assert legacy[0]["version"] == "2.7.0"
    vc = _load_module(root)
    active, blocked = vc._spool_ensure_pending(
        str(root), "fw-update", "2.7.0", "one", "fold", time.time(), "2.6.0")
    assert active is not None and blocked is None


# ---------------------------------------------------------------------------
# unit level (importlib): supersede + lower-candidate block
# ---------------------------------------------------------------------------

def _load_module(root: Path):
    spec = importlib.util.spec_from_file_location(
        "version_check_under_test", root / "routines" / "version-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_supersede_is_one_locked_change(tmp_path):
    root = _mk_instance(tmp_path)
    vc = _load_module(root)
    now = time.time()
    a1, b1 = vc._spool_ensure_pending(
        str(root), "fw-update", "2.6.5", "one", "fold", now, "2.6.0")
    assert a1 is not None and a1["status"] == "pending"
    a2, b2 = vc._spool_ensure_pending(
        str(root), "fw-update", "2.7.0", "one", "fold", now, "2.6.0")
    assert a2 is not None and a2["version"] == "2.7.0"
    recs = _spool(root)
    old = [r for r in recs if r.get("version") == "2.6.5"][0]
    assert old["status"] == "expired"
    assert old["expired_reason"] == "superseded"
    # The lower cached candidate never comes back as a new pending.
    a3, b3 = vc._spool_ensure_pending(
        str(root), "fw-update", "2.6.5", "one", "fold", now, "2.6.0")
    assert a3 is None and b3 == "newer-pending"
    # ...and even after the newer pending closes, its terminal record
    # blocks auto-revival of the superseded target.
    a4, b4 = vc._spool_ensure_pending(
        str(root), "fw-update", "2.7.0", "one", "fold", now, "2.6.0")
    assert a4["id"] == a2["id"]  # same pending, no duplicate


def test_ensure_returns_existing_pending_not_a_second_one(tmp_path):
    root = _mk_instance(tmp_path)
    vc = _load_module(root)
    now = time.time()
    a1, _ = vc._spool_ensure_pending(
        str(root), "pending-restart", "2.6.0", "one", "", now, "2.6.0")
    a2, _ = vc._spool_ensure_pending(
        str(root), "pending-restart", "2.6.0", "one", "", now + 5, "2.6.0")
    assert a1["id"] == a2["id"]
    assert len(_pendings(root)) == 1


# ---------------------------------------------------------------------------
# the cron/push rail has no spool coupling at all
# ---------------------------------------------------------------------------

def test_push_rail_never_touches_the_spool():
    # Machine proof, static half: routines/push.sh (the cron push carrier)
    # contains zero references to the notice spool, so an automatic push
    # can neither synthesize a notice nor move its accounting. The dynamic
    # half (injected-turn output) is covered in test_dgn1586_notice_spool.
    text = (REPO_ROOT / "routines" / "push.sh").read_text(encoding="utf-8")
    assert "notice-spool" not in text
    assert "notice_spool" not in text
