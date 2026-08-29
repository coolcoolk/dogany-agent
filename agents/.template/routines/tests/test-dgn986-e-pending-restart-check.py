#!/usr/bin/env python3
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
  (i) key-namespace collision guard: a prior "new version" _write_shown for
      version X must NOT suppress a later "pending restart" _should_notice
      for the same X (the _UNCONSUMED_KEY_PREFIX fix)
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
        with contextlib.redirect_stdout(buf):
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
        # Simulate a prior "new version exists" notice already shown for
        # this exact version string, very recently (well within TTL).
        vc._write_shown(root, version, now - 1)
        # Without the prefix fix, _should_notice(root, version, now) would
        # be suppressed -- and a naive reuse of the SAME raw version string
        # for the pending-restart key would inherit that suppression even
        # though it is a semantically different notice.
        prefixed_key = vc._UNCONSUMED_KEY_PREFIX + version
        result = vc._should_notice(root, prefixed_key, now)
        check("i-prefixed-key-not-suppressed", result is True,
              "expected the prefixed key to be unaffected by the raw-version "
              "shown record, got should_notice={}".format(result))
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
