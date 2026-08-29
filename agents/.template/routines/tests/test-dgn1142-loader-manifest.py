#!/usr/bin/env python3
"""test-dgn1142-loader-manifest.py

DGN-1142 section 3.1 (wired in DGN-1141 stage 4): the loader-manifest
SessionStart hook must make @-chain loading observable -- "loaded 0/N" and
"the hook never ran" must be textually distinct states.

Cases:
  (a) real template chain -> every referenced doc counted and named
  (b) one doc removed -> N-1/N + MISSING names the exact file
  (c) CLAUDE.md itself missing -> 0/0 + MISSING: CLAUDE.md
  (d) CLAUDE.md with no @-references -> explicit "(no @-references)" line
  (e) fence guard: a fenced @-line is NOT counted, with the same ref outside
      a fence as the positive control (tool-liveness pairing, DGN-1142 3.1)
  (f) end-to-end subprocess: JSON additionalContext on stdout + log line
      appended under .telegram_bot/state/ (exit 0 both on full and deficit
      chains -- the hook never blocks a session)
  (g) an @-cycle terminates (no hang, each file counted once)

Self-contained. ASCII/English only.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROUTINES_DIR = os.path.dirname(_HERE)
_TEMPLATE_ROOT = os.path.dirname(_ROUTINES_DIR)
_TARGET = os.path.join(_ROUTINES_DIR, "loader-manifest.py")

spec = importlib.util.spec_from_file_location("loader_manifest", _TARGET)
lm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lm)

_fails = []


def check(name, cond, detail=""):
    if cond:
        print("PASS ({}): ok".format(name))
    else:
        print("FAIL ({}): {}".format(name, detail))
        _fails.append(name)


def make_fixture(with_bridge_chain=True):
    """Minimal instance root mirroring the template @ chain shape."""
    root = tempfile.mkdtemp(prefix="dgn1142-lm-")
    docs = ["CONSTITUTION.md", "CONTRACT.md", "DISCIPLINE.md",
            "PROFILE.md", "USER.md"]
    refs = docs + (["bridge.md"] if with_bridge_chain else [])
    with open(os.path.join(root, "CLAUDE.md"), "w") as f:
        f.write("# entry\n\n@AGENTS.md\n")
    with open(os.path.join(root, "AGENTS.md"), "w") as f:
        f.write("# hub\n\n" + "\n".join("@" + d for d in refs) + "\n")
    for d in docs:
        with open(os.path.join(root, d), "w") as f:
            f.write("# {}\n".format(d))
    if with_bridge_chain:
        with open(os.path.join(root, "bridge.md"), "w") as f:
            f.write("# bridge\n\n@telegram.md\n")
        with open(os.path.join(root, "telegram.md"), "w") as f:
            f.write("# telegram\n")
    return root


def test_a_template_chain_counts():
    loaded, missing = lm.walk_at_graph(_TEMPLATE_ROOT)
    check("a1-template-no-missing", missing == [],
          "template chain has missing refs: {}".format(missing))
    check("a2-template-hub-loaded", "AGENTS.md" in loaded,
          "AGENTS.md not discovered: {}".format(loaded))
    line = lm.build_line(loaded, missing)
    n = len(loaded)
    check("a3-line-full-count", line.startswith("[loader] {}/{} loaded: ".format(n, n)),
          "unexpected line: {}".format(line))
    # DGN-1141 stage-5 relayout chain: rules/hot.framework.md +
    # identity/hot.custom.agent.md + identity/hot.custom.owner.md (basenames in
    # the emitted line).
    check("a4-names-listed",
          "hot.framework" in line and "hot.custom.agent" in line
          and "hot.custom.owner" in line,
          "names absent from line: {}".format(line))


def test_b_missing_doc_named():
    root = make_fixture()
    os.remove(os.path.join(root, "DISCIPLINE.md"))
    try:
        loaded, missing = lm.walk_at_graph(root)
        line = lm.build_line(loaded, missing)
        check("b1-deficit-count", "7/8 loaded" in line,
              "unexpected line: {}".format(line))
        check("b2-missing-named", "MISSING: DISCIPLINE.md" in line,
              "unexpected line: {}".format(line))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c_root_missing():
    root = tempfile.mkdtemp(prefix="dgn1142-lm-")
    try:
        loaded, missing = lm.walk_at_graph(root)
        line = lm.build_line(loaded, missing)
        check("c1-root-missing", line == "[loader] 0/1 loaded; MISSING: CLAUDE.md",
              "unexpected line: {}".format(line))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_d_no_refs():
    root = tempfile.mkdtemp(prefix="dgn1142-lm-")
    try:
        with open(os.path.join(root, "CLAUDE.md"), "w") as f:
            f.write("# entry with no imports\n")
        loaded, missing = lm.walk_at_graph(root)
        line = lm.build_line(loaded, missing)
        check("d1-no-refs-explicit", "(no @-references" in line,
              "unexpected line: {}".format(line))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_e_fence_guard_with_positive_control():
    root = tempfile.mkdtemp(prefix="dgn1142-lm-")
    try:
        # fenced ref to a MISSING file must not count; unfenced ref to an
        # existing file proves the extractor itself is alive.
        with open(os.path.join(root, "CLAUDE.md"), "w") as f:
            f.write("# entry\n\n```\n@ghost.md\n```\n\n@REAL.md\n")
        with open(os.path.join(root, "REAL.md"), "w") as f:
            f.write("# real\n")
        loaded, missing = lm.walk_at_graph(root)
        check("e1-fenced-ref-ignored", "ghost.md" not in missing,
              "fenced ref leaked into missing: {}".format(missing))
        check("e2-unfenced-ref-counted", loaded == ["REAL.md"],
              "positive control failed: {}".format(loaded))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_hook(root):
    routines = os.path.join(root, "routines")
    os.makedirs(routines, exist_ok=True)
    shutil.copy(_TARGET, os.path.join(routines, "loader-manifest.py"))
    return subprocess.run(
        [sys.executable, os.path.join(routines, "loader-manifest.py")],
        input="{}", capture_output=True, text=True, timeout=30,
    )


def test_f_end_to_end_subprocess():
    root = make_fixture()
    try:
        os.remove(os.path.join(root, "USER.md"))
        proc = _run_hook(root)
        check("f1-exit-zero-on-deficit", proc.returncode == 0,
              "rc={} stderr={}".format(proc.returncode, proc.stderr))
        payload = json.loads(proc.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        check("f2-context-missing-named", "MISSING: USER.md" in ctx,
              "context: {}".format(ctx))
        log_path = os.path.join(root, ".telegram_bot", "state",
                                "loader-manifest.log")
        check("f3-log-appended", os.path.isfile(log_path)
              and "MISSING: USER.md" in open(log_path).read(),
              "log absent or lacks the line")
        # ran-vs-not distinction: a second run appends a second line.
        _run_hook(root)
        lines = open(log_path).read().strip().split("\n")
        check("f4-append-per-run", len(lines) == 2,
              "expected 2 log lines, got {}".format(len(lines)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_g_cycle_terminates():
    root = tempfile.mkdtemp(prefix="dgn1142-lm-")
    try:
        with open(os.path.join(root, "CLAUDE.md"), "w") as f:
            f.write("@A.md\n")
        with open(os.path.join(root, "A.md"), "w") as f:
            f.write("@B.md\n")
        with open(os.path.join(root, "B.md"), "w") as f:
            f.write("@A.md\n@CLAUDE.md\n")
        loaded, missing = lm.walk_at_graph(root)
        check("g1-cycle-counts-once", sorted(loaded) == ["A.md", "B.md"],
              "loaded: {}".format(loaded))
        check("g2-cycle-no-missing", missing == [],
              "missing: {}".format(missing))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    tests = [
        test_a_template_chain_counts,
        test_b_missing_doc_named,
        test_c_root_missing,
        test_d_no_refs,
        test_e_fence_guard_with_positive_control,
        test_f_end_to_end_subprocess,
        test_g_cycle_terminates,
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
