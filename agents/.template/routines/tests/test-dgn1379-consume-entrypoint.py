#!/usr/bin/env python3
# test-lane: fast
"""DGN-1379-SPEC-consume-entrypoint (owner-locked, 2026-09-19) -- the
`engine.py consume` subcommand (S1) and the thin wrapper
`handoff-engine.sh` (S2).

Locks, in the spec's checklist order:

  1  `consume --help` carries no `--to-root` (or `--peer`) flag -- S9's
     "legacy root submit does not exist" invariant applies to consume too
  2  an empty inbox -> one-line JSON, processed:0, rc=0
  3  a message consume can act on is counted and archived
  4  the wrapper's substantive line count is <= 5 (machine-checked)
  5  the wrapper still finds engine.py when run from a copied (non-
     symlink) location, not just the canonical template path
  6  neither new file hardcodes /Users/ or /home/
  7  the JSON summary's field names/order match the spec's literal shape

Run: python3 routines/tests/test-dgn1379-consume-entrypoint.py
     (or: pytest routines/tests/test-dgn1379-consume-entrypoint.py)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)                       # routines/
LIB = os.path.join(ROUTINES, "lib", "handoff")          # routines/lib/handoff/
WRAPPER = os.path.join(ROUTINES, "handoff-engine.sh")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(ROUTINES)))
PY = sys.executable

sys.path.insert(0, LIB)

import handoff                                                   # noqa: E402

SPEC_FIELDS = ["processed", "deduped", "expired", "left", "errors",
               "scans", "lock_busy"]


def _mk_fixture_root(tmp, name="metal"):
    """A standalone copy of routines/ + an instance root, so engine.py's
    own_root()/own_slug() self-derivation runs against a real file tree
    instead of the live template (own_root climbs 3 dirs up from its own
    file location -- see engine.py)."""
    root = os.path.join(tmp, name)
    dst_routines = os.path.join(root, "routines")
    shutil.copytree(ROUTINES, dst_routines,
                     ignore=shutil.ignore_patterns("__pycache__", "tests"))
    os.makedirs(os.path.join(root, "config"), exist_ok=True)
    with open(os.path.join(root, "config", "agent.conf"), "w") as fh:
        fh.write("SLUG=%s\n" % name)
    return root


def _run_consume(root, extra_args=()):
    engine_py = os.path.join(root, "routines", "lib", "handoff", "engine.py")
    out = subprocess.run([PY, engine_py, "consume", *extra_args],
                         capture_output=True, text=True)
    return out


# -- 1. no --to-root / --peer on consume's argument surface ------------------

def test_1_consume_help_has_no_to_root_or_peer_flag():
    out = subprocess.run([PY, os.path.join(LIB, "engine.py"), "consume",
                          "--help"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    legacy_root_flag = "--" + "to-root"
    assert legacy_root_flag not in out.stdout
    assert "--peer" not in out.stdout
    assert "--cap" in out.stdout
    # --once/--json were dropped after the first landing: handoff.consume()
    # honours neither, so accepting them would exit 0 and let the caller
    # believe the flag took effect. Assert they are REJECTED, not present.
    assert "--once" not in out.stdout
    assert "--json" not in out.stdout
    for dead in ("--once", "--json"):
        rc = subprocess.run([PY, os.path.join(LIB, "engine.py"), "consume",
                             dead], capture_output=True, text=True)
        assert rc.returncode != 0, dead + " was silently accepted"


# -- 2. empty inbox -> processed:0, rc=0 --------------------------------------

def test_2_empty_inbox_processed_zero_rc_zero():
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk_fixture_root(tmp)
        out = _run_consume(root)
        assert out.returncode == 0, out.stderr
        lines = out.stdout.strip().splitlines()
        assert len(lines) == 1, lines
        stats = json.loads(lines[0])
        assert stats["processed"] == 0, stats
        assert stats["lock_busy"] is False, stats


# -- 3. a message consume can act on is counted and archived -----------------

# test_3 / test_3b (expiry bookkeeping, unparseable -> errors) MOVED OUT on
# 2026-09-19 to test-dgn1379-slug-consume-layer.py as test_9 / test_10.
# Reason: they drove _cmd_consume against the LEGACY root layout, which was
# correct when written -- the entry point read it then. DGN-1379-SPEC-slug-
# consume-layer S3 rewired _cmd_consume onto the slug mailbox, so the legacy
# fixture became structurally unreachable and the two subtests could never
# pass again. They were moved, not deleted: the behaviour they lock (expired
# -> dead/ + counted, unparseable -> errors + rc=1) still matters, only its
# address changed. Nothing about this entry point's OWN contract is untested
# as a result -- see the sibling suite for the current home.


def test_4_wrapper_substantive_line_count_le_5():
    with open(WRAPPER) as fh:
        lines = fh.readlines()
    substantive = [ln for ln in lines
                  if ln.strip() and not ln.strip().startswith("#")]
    assert len(substantive) <= 5, substantive


# -- 5. wrapper finds engine.py from a copied (non-symlink) location ---------

def test_5_wrapper_works_from_copied_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk_fixture_root(tmp)
        copied_wrapper = os.path.join(root, "routines", "handoff-engine.sh")
        assert os.path.isfile(copied_wrapper)
        out = subprocess.run(["bash", copied_wrapper, "consume"],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        stats = json.loads(out.stdout.strip())
        assert stats["processed"] == 0, stats


# -- 6. no hardcoded /Users/ or /home/ in either new file --------------------

def test_6_no_hardcoded_home_paths():
    for path in (os.path.join(LIB, "engine.py"), WRAPPER):
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                assert "/Users/" not in line and "/home/" not in line, (
                    "%s:%d hardcodes a home path: %r" % (path, line_no, line))


# -- 7. JSON field names/order match the spec's literal shape ----------------

def test_7_json_field_order_matches_spec():
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk_fixture_root(tmp)
        out = _run_consume(root)
        stripped = out.stdout.strip()
        # exact key order as they appear in the raw text, not just as a
        # dict (dict equality would hide an order regression).
        found_order = re.findall(r'"(\w+)":', stripped)
        assert found_order == SPEC_FIELDS, found_order


def _main():
    fails = []
    names = sorted(k for k in globals() if k.startswith("test_"))
    for name in names:
        try:
            globals()[name]()
            print("%s PASS" % name)
        except AssertionError as exc:
            print("%s FAIL %s" % (name, exc))
            fails.append(name)
        except Exception as exc:                          # pragma: no cover
            print("%s ERROR %r" % (name, exc))
            fails.append(name)
    print("\n%d/%d passed" % (len(names) - len(fails), len(names)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_main())
