#!/usr/bin/env python3
# test-lane: fast    # measured 0.2s (2026-09-04 full sweep, DGN-1267)
"""test-dgn986-e-pending-restart-check.py

DGN-986 [e]: tests for version-check.py's 3rd comparison -- boot snapshot
`fw_version` vs `.instance.conf DOGANY_FW_VERSION` ("received but not
running"). No network (remote check is disabled via DOGANY_VERSION_CHECK=0
in every fixture root). Self-contained. ASCII/English only.

Cases:
  (a) snapshot absent -> no output (pre-2.0 instance, silent skip)
  (b) snapshot pid stale (dead) -> no output (crash-restart race, silent skip)
  (c) fw_version matches conf -> no output (nothing to report)
  (d) fw_version behind conf -> notice fires exactly once
  (e) same state re-run -> TTL suppression (2nd call: no output)
  (f) corrupt/malformed JSON snapshot -> no exception, exit 0, no output
  (g) both "not received" (check 1/2) and "received but not used" (check 3)
      true at once -> only the "new version exists" notice fires, never both
  (h) unknown schema -> silent skip (forward-compat)
  (i) key-namespace collision guard: a prior "new version" (fw-update)
      notice already delivered for version X must NOT suppress a later
      "pending restart" notice for the same X. DGN-1586 moved this guard
      off the shared shown-flag file's _UNCONSUMED_KEY_PREFIX string
      prefix onto the spool's (kind, version) key -- kind is now the
      discriminator between the two notice namespaces, so this case seeds
      a delivered fw-update record directly and asserts a pending-restart
      _spool_ensure_pending for the same version still creates a pending.
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

# ---------------------------------------------------------------------------
# Load version-check.py as a module without running main() via __main__.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROUTINES_DIR = os.path.dirname(_HERE)
_TARGET = os.path.join(_ROUTINES_DIR, "version-check.py")

spec = importlib.util.spec_from_file_location("vc_dgn986e", _TARGET)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)

_fails = []


def check(name, cond, detail=""):
    if cond:
        print("PASS ({}): ok".format(name))
    else:
        print("FAIL ({}): {}".format(name, detail))
        _fails.append(name)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def make_root():
    d = tempfile.mkdtemp(prefix="dgn986e-test-")
    os.makedirs(os.path.join(d, ".telegram_bot", "state"), exist_ok=True)
    # Disable the remote check unconditionally -- these tests exercise ONLY
    # the 3rd (snapshot) comparison; no network calls are allowed.
    with open(os.path.join(d, ".telegram_bot", ".env"), "w", encoding="utf-8") as fh:
        fh.write("DOGANY_VERSION_CHECK=0\n")
    return d


def cleanup(root):
    shutil.rmtree(root, ignore_errors=True)


def write_conf(root, fw_version, repo_root=None):
    lines = ["DOGANY_FW_VERSION={}".format(fw_version)]
    if repo_root:
        lines.append("DOGANY_REPO_ROOT={}".format(repo_root))
    with open(os.path.join(root, ".instance.conf"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def snapshot_path(root):
    return os.path.join(root, ".telegram_bot", "runtime-snapshot.json")


def write_snapshot(root, pid, fw_version, schema=1):
    payload = {
        "schema": schema,
        "label": "com.telegram-skill-bot.dgn986e-test",
        "pid": pid,
        "started_at": "2026-08-21T00:00:00+09:00",
        "fw_version": fw_version,
        "engine_versions": {},
        "claude_cli": {"resolved_path": None, "version": None, "install_method": None},
        "env_files": [],
    }
    with open(snapshot_path(root), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def dead_pid():
    """A pid guaranteed dead: spawn a trivial child and reap it."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def init_repo_with_stable_tag(repo_root, tag):
    """Turn repo_root into a git repo with one commit tagged `tag`.

    DGN-1068 (2026-08-24, after this test file's 2026-08-21 origin) moved
    the local check's advertise candidate from the VERSION file to the
    latest STABLE git tag (version-check.py _latest_stable_tag) -- a bare
    VERSION file no longer drives the "new version exists" branch. A
    fixture that wants to exercise that branch must carry a real tag.
    """
    env = dict(os.environ, GIT_AUTHOR_NAME="dgn986e-test", GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="dgn986e-test", GIT_COMMITTER_EMAIL="test@example.invalid")
    # -c commit.gpgsign=false: hermetic against an operator's global git
    # config that signs commits (no GPG key is guaranteed in a test sandbox).
    git = ["git", "-c", "commit.gpgsign=false"]
    subprocess.run(git + ["init", "-q", repo_root], check=True, env=env)
    subprocess.run(git + ["-C", repo_root, "add", "-A"], check=True, env=env)
    subprocess.run(git + ["-C", repo_root, "commit", "-q", "-m", "dgn986e fixture commit"],
                    check=True, env=env)
    subprocess.run(git + ["-C", repo_root, "tag", tag], check=True, env=env)


def run_main(root):
    """Run vc.main() against a fixture instance root; return captured stdout.

    main() derives instance_root from os.path.dirname(os.path.abspath(
    __file__)) twice up -- so __file__ is monkeypatched to <root>/routines/
    version-check.py (pure string math, the dir need not exist on disk)."""
    vc.__file__ = os.path.join(root, "routines", "version-check.py")
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps({"session_id": "dgn986e-test"}))
    buf = io.StringIO()
    try:
        # DGN-1675: only owner conversations produce notices. Keep this
        # fixture local so the invoking runner's surface cannot select it.
        with patch.dict(os.environ, {"DOGANY_BRIDGE_SESSION": "1"}), \
                contextlib.redirect_stdout(buf):
            try:
                vc.main()
            except SystemExit:
                pass
    finally:
        sys.stdin = old_stdin
    return buf.getvalue()


# ---------------------------------------------------------------------------
# (a) snapshot absent -> no output
# ---------------------------------------------------------------------------

def test_a_snapshot_absent_silent():
    root = make_root()
    try:
        write_conf(root, "1.2.0")
        out = run_main(root)
        check("a-snapshot-absent-silent", out.strip() == "",
              "expected empty stdout, got: {!r}".format(out))
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------
# (b) stale pid -> no output
# ---------------------------------------------------------------------------

def test_b_stale_pid_silent():
    root = make_root()
    try:
        write_conf(root, "1.2.0")
        write_snapshot(root, pid=dead_pid(), fw_version="1.0.0")
        out = run_main(root)
        check("b-stale-pid-silent", out.strip() == "",
              "expected empty stdout, got: {!r}".format(out))
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------
# (c) fw_version matches -> no output
# ---------------------------------------------------------------------------

def test_c_fw_version_match_silent():
    root = make_root()
    try:
        write_conf(root, "1.2.0")
        write_snapshot(root, pid=os.getpid(), fw_version="1.2.0")
        out = run_main(root)
        check("c-fw-version-match-silent", out.strip() == "",
              "expected empty stdout, got: {!r}".format(out))
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------
# (d) fw_version behind conf -> notice fires once
# ---------------------------------------------------------------------------

def test_d_fw_version_behind_notice_fires():
    root = make_root()
    try:
        write_conf(root, "1.2.0")
        write_snapshot(root, pid=os.getpid(), fw_version="1.0.0")
        out = run_main(root)
        check("d-notice-nonempty", out.strip() != "", "expected a notice, got empty stdout")
        try:
            payload = json.loads(out)
            ctx = payload["hookSpecificOutput"]["additionalContext"]
        except Exception as e:
            check("d-notice-parseable", False, "json parse failed: {}".format(e))
            return
        check("d-notice-mentions-version", "1.2.0" in ctx,
              "expected built version 1.2.0 in notice, got: {!r}".format(ctx))
        check("d-notice-en-fallback-copy",
              "not running yet" in ctx or "Restart now" in ctx,
              "expected en fallback pending-restart copy, got: {!r}".format(ctx))
        check("d-notice-not-update-available",
              "update available" not in ctx.lower(),
              "must not be the 'new version exists' notice, got: {!r}".format(ctx))
    finally:
        cleanup(root)


def test_d2_pending_restart_notice_shows_full_pre_release():
    """DGN-1723: DOGANY_FW_VERSION is the bare X.Y.Z; the owner-facing
    notice must read the exact build (DOGANY_FW_TAG) while its X.Y.Z matches."""
    root = make_root()
    try:
        write_conf(root, "1.2.0")
        with open(os.path.join(root, ".instance.conf"), "a", encoding="utf-8") as fh:
            fh.write("DOGANY_FW_TAG=v1.2.0-dev.55\n")
        write_snapshot(root, pid=os.getpid(), fw_version="1.0.0")
        out = run_main(root)
        try:
            ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        except Exception as e:
            check("d2-notice-parseable", False, "json parse failed: {}".format(e))
            return
        check("d2-full-pre-release-in-notice", "v1.2.0-dev.55" in ctx,
              "expected v1.2.0-dev.55 in notice, got: {!r}".format(ctx))
        check("d2-never-bare-xyz", "v1.2.0\n" not in ctx and "· v1.2.0 " not in ctx,
              "bare X.Y.Z leaked: {!r}".format(ctx))
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------
# (e) same state re-run -> TTL suppression
# ---------------------------------------------------------------------------

def test_e_ttl_suppresses_rerun():
    root = make_root()
    try:
        write_conf(root, "1.2.0")
        write_snapshot(root, pid=os.getpid(), fw_version="1.0.0")
        first = run_main(root)
        check("e-first-call-fires", first.strip() != "", "expected first call to fire")
        second = run_main(root)
        check("e-second-call-suppressed", second.strip() == "",
              "expected TTL suppression on 2nd call, got: {!r}".format(second))
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------
# (f) corrupt JSON -> no exception, exit 0, no output
# ---------------------------------------------------------------------------

def test_f_corrupt_json_fail_open():
    root = make_root()
    try:
        write_conf(root, "1.2.0")
        with open(snapshot_path(root), "w", encoding="utf-8") as fh:
            fh.write("{not valid json::: ")
        raised = False
        out = ""
        try:
            out = run_main(root)
        except Exception as e:  # pragma: no cover -- must NOT happen
            raised = True
            print("  (f) unexpected exception: {}".format(e))
        check("f-no-exception", raised is False, "main() raised on corrupt JSON")
        check("f-no-output", out.strip() == "",
              "expected silent skip, got: {!r}".format(out))
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------
# (g) both conditions true at once -> only ONE notice kind fires
# ---------------------------------------------------------------------------

def test_g_both_true_only_one_kind():
    root = make_root()
    repo_root = tempfile.mkdtemp(prefix="dgn986e-repo-")
    try:
        with open(os.path.join(repo_root, "VERSION"), "w", encoding="utf-8") as fh:
            fh.write("1.3.0\n")
        # DGN-1068: the local check's advertise candidate is the latest
        # STABLE tag, not this VERSION file (see init_repo_with_stable_tag).
        init_repo_with_stable_tag(repo_root, "v1.3.0")
        write_conf(root, "1.2.0", repo_root=repo_root)
        # snapshot behind built (1.0.0 < 1.2.0): the "pending restart" side
        # would ALSO be true in isolation.
        write_snapshot(root, pid=os.getpid(), fw_version="1.0.0")
        out = run_main(root)
        check("g-notice-nonempty", out.strip() != "", "expected a notice, got empty stdout")
        try:
            payload = json.loads(out)
            ctx = payload["hookSpecificOutput"]["additionalContext"]
        except Exception as e:
            check("g-notice-parseable", False, "json parse failed: {}".format(e))
            return
        check("g-shows-new-version", "1.3.0" in ctx,
              "expected the 'new version exists' (1.3.0) notice, got: {!r}".format(ctx))
        check("g-does-not-show-pending-restart",
              "not running yet" not in ctx and "Restart now" not in ctx,
              "the pending-restart notice must NOT also appear, got: {!r}".format(ctx))
        # Only exactly one JSON object printed (one notice, not two concatenated).
        check("g-single-json-object", out.strip().count("hookSpecificOutput") == 1,
              "expected exactly one notice object, got: {!r}".format(out))
    finally:
        cleanup(root)
        shutil.rmtree(repo_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (h) unknown schema -> silent skip (forward-compat)
# ---------------------------------------------------------------------------

def test_h_unknown_schema_silent():
    root = make_root()
    try:
        write_conf(root, "1.2.0")
        write_snapshot(root, pid=os.getpid(), fw_version="1.0.0", schema=2)
        out = run_main(root)
        check("h-unknown-schema-silent", out.strip() == "",
              "expected silent skip for unknown schema, got: {!r}".format(out))
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------
# (i) key-namespace collision guard (_UNCONSUMED_KEY_PREFIX)
# ---------------------------------------------------------------------------

def test_i_key_prefix_prevents_cross_suppression():
    root = make_root()
    try:
        now = 1_000_000.0
        version = "1.2.0"
        built_version = "1.0.0"
        # Simulate a prior "new version exists" (fw-update) notice already
        # DELIVERED for this exact version string, very recently (well
        # within TTL). built_version stays below `version` so reconcile()
        # does not expire this record as resolved before the check runs.
        records = [{
            "id": "ntc-test-i", "kind": vc._NOTICE_KIND_FW_UPDATE,
            "version": vc._norm_ver(version), "audience": "owner",
            "first_pending_ts": now - 2, "attempts": 1,
            "status": "delivered", "expired_reason": None,
            "delivered_ts": now - 1, "chat_id": 1, "message_id": 1,
            "body_oneline": "x", "body_fold": "x",
            "context_injected_ts": None, "origin": "test",
        }]
        vc._spool_dump(root, records, [])
        # Without kind as the discriminator, a naive shared-key spool
        # implementation would let this fw-update delivered record
        # suppress the pending-restart notice for the SAME raw version
        # string -- the spool-era analog of the old shown-flag collision.
        active, blocked = vc._spool_ensure_pending(
            root, vc._NOTICE_KIND_PENDING_RESTART, version, "msg", "",
            now, built_version)
        check("i-prefixed-key-not-suppressed",
              active is not None and blocked is None,
              "expected the pending-restart kind to be unaffected by the "
              "fw-update delivered record, got active={!r} "
              "blocked={!r}".format(active, blocked))
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------
# Direct unit coverage for _read_snapshot_fw_version / _pid_alive
# (fast, no subprocess -- exercises internals bypassing main()'s I/O layer)
# ---------------------------------------------------------------------------

def test_j_pid_alive_direct():
    check("j-pid-alive-self", vc._pid_alive(os.getpid()) is True)
    check("j-pid-alive-dead", vc._pid_alive(dead_pid()) is False)
    check("j-pid-alive-bad-type", vc._pid_alive("not-an-int") is None)
    check("j-pid-alive-bool-rejected", vc._pid_alive(True) is None)
    check("j-pid-alive-nonpositive", vc._pid_alive(0) is None)


def test_k_read_snapshot_missing_pid_field():
    root = make_root()
    try:
        with open(snapshot_path(root), "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "fw_version": "1.0.0"}, fh)  # no pid key
        result = vc._read_snapshot_fw_version(root)
        check("k-missing-pid-field-none", result is None,
              "expected None for a snapshot with no pid field, got: {!r}".format(result))
    finally:
        cleanup(root)


if __name__ == "__main__":
    tests = [
        test_a_snapshot_absent_silent,
        test_b_stale_pid_silent,
        test_c_fw_version_match_silent,
        test_d_fw_version_behind_notice_fires,
        test_d2_pending_restart_notice_shows_full_pre_release,
        test_e_ttl_suppresses_rerun,
        test_f_corrupt_json_fail_open,
        test_g_both_true_only_one_kind,
        test_h_unknown_schema_silent,
        test_i_key_prefix_prevents_cross_suppression,
        test_j_pid_alive_direct,
        test_k_read_snapshot_missing_pid_field,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            print("FAIL {}: {}".format(t.__name__, e))
            _fails.append(t.__name__)
    if _fails:
        print("\n{} test(s) FAILED: {}".format(len(_fails), ", ".join(_fails)))
        sys.exit(1)
    print("\nAll tests PASSED")
    sys.exit(0)
