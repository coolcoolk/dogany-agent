#!/usr/bin/env python3
# test-lane: slow    # runs the REAL scripts/mint.sh 3x (--no-venv, no
# network); measured ~18s (2026-09-19).
"""DGN-1379-SPEC-r2-remaining-three -- watch plist template (S1), mint
capabilities seed (S2), reply-leg single-address guard (S3).

Locks, in the spec's verification order:

  1  the plist is valid XML and `plutil -lint` passes
  2  ProgramArguments points at handoff-engine.sh consume; no python
     interpreter path is baked in
  3  the plist filename carries the com.telegram-skill-bot.telegram-agent.
     prefix the mint rename mechanism (mint.sh step 3) keys on
  4  plists.defer carries no handoff-watch entry (it is a deny-list, not
     an inventory -- a newborn must be able to receive handoff)
  5  a synthetic mint (temp HOME + root, --no-venv) creates
     config/handoff.capabilities with content matching the spec seed
     verbatim
  6  re-running the same mint (--force) does NOT clobber an
     owner-edited config/handoff.capabilities
  7  guard: zero external `handoff.submit(` callers anywhere in the
     template tree, and every `ensure_channel(` occurrence in the tree
     is confined to routines/lib/handoff/handoff.py -- the file that
     defines it and, internally, calls it from its own submit(). Both
     functions stay (DGN-1454 M1's disposal, not this ticket's); what
     this guard locks is "no OTHER path in the template calls either
     one" -- the day it breaks is the day someone routes a newborn's
     reply leg back through the legacy channel.
  8  NOT re-run here: the pre-existing handoff suites
     (test-dgn1379-handoff-lib-landing.py, test-dgn1379-consume-
     entrypoint.py, test-dgn1379-slug-consume-layer.py) are unaffected
     by this ticket's touched files (a new plist, a new mint.sh
     scaffold block, this file) and are run separately -- their own
     exit codes are what the implementer's report carries verbatim
     (same convention as test-dgn1379-slug-consume-layer.py's own
     test_8).

Every mint below runs with HOME repointed at a per-test tempdir and
--no-venv (no network, no pip) -- NEVER the real ~/.dogany or a live
instance tree.

Run: python3 routines/tests/test-dgn1379-r2-watch-plist-and-seed.py
     (or: pytest routines/tests/test-dgn1379-r2-watch-plist-and-seed.py)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)                        # routines/
TEMPLATE = os.path.dirname(ROUTINES)                     # agents/.template/
REPO_ROOT = os.path.dirname(os.path.dirname(TEMPLATE))
MINT_SH = os.path.join(REPO_ROOT, "scripts", "mint.sh")

PLIST_PATH = os.path.join(
    ROUTINES, "com.telegram-skill-bot.telegram-agent.handoff-watch.plist")
DEFER_PATH = os.path.join(ROUTINES, "plists.defer")

CAP_SEED = (
    "# handoff capabilities -- this instance's declaration of what it does with\n"
    "# each incoming message type. Consumed by routines/lib/handoff/engine.py\n"
    "# consume (DGN-1379). Absent file == empty table == everything is left in\n"
    "# the inbox; a malformed file aborts consumption (fail-closed).\n"
    "decision.notice builtin:notice-append idem=ulid\n"
)

# Not token-SHAPED on purpose. mint.sh never validates the format, and every
# other mint selftest in this repo passes the literal "dummy-selftest-token"
# (tests/mint_userlabel_no_inherit_selftest.sh, tests/dgn1475_fw_repo_sha_selftest.sh).
# A realistic-looking token here trips the release secret-sweep (cat1) and the
# only way to keep it would be growing .sweepignore -- teaching the detector to
# forgive a path is worse than not looking like a secret in the first place.
FAKE_TOKEN = "dummy-selftest-token"

# text file extensions worth scanning for the S3 guard (source + shell +
# template text -- mirrors the file classes mint.sh's own substitute() pass
# walks, minus binary/db formats that can never hold a python call).
SCAN_EXTS = (".py", ".sh", ".md", ".conf", ".json", ".txt", ".example")


def _mint_sandbox(work, *extra_args):
    home = os.path.join(work, "home")
    os.makedirs(home, exist_ok=True)
    inst = os.path.join(work, "inst")
    env = dict(os.environ)
    env["HOME"] = home
    env["DOGANY_BOT_TOKEN"] = FAKE_TOKEN
    env["PIP_CACHE_DIR"] = os.path.join(work, "pip-cache")
    argv = [
        "bash", MINT_SH, "--root", inst, "--name", "probebot",
        "--core-only", "--no-venv", "--lang", "ko",
        "--owner-id", "11111111", "--tz", "Asia/Seoul",
    ] + list(extra_args)
    out = subprocess.run(argv, capture_output=True, text=True, env=env)
    return out, inst


def test_1_plist_is_valid_xml_plutil_lint():
    if shutil.which("plutil") is None:
        print("SKIP test_1: plutil not on PATH (non-macOS host) -- NOT a pass")
        return
    out = subprocess.run(["plutil", "-lint", PLIST_PATH],
                         capture_output=True, text=True)
    assert out.returncode == 0, (out.stdout, out.stderr)


def test_2_program_arguments_point_at_wrapper_consume_no_interpreter():
    with open(PLIST_PATH, encoding="utf-8") as fh:
        text = fh.read()
    assert "routines/handoff-engine.sh" in text
    assert shutil.which("plutil") is not None, "plutil required for this check"
    out = subprocess.run(["plutil", "-convert", "json", "-o", "-",
                          PLIST_PATH], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    args = doc["ProgramArguments"]
    joined = " ".join(args)
    assert joined.endswith("routines/handoff-engine.sh consume"), args
    assert not any("python" in a.lower() for a in args), (
        "ProgramArguments must not bake in a python interpreter path -- "
        "the wrapper resolves python3 at exec time: %s" % args)


def test_3_filename_carries_mint_rename_prefix():
    basename = os.path.basename(PLIST_PATH)
    assert basename.startswith("com.telegram-skill-bot.telegram-agent."), (
        basename)
    assert basename.endswith("handoff-watch.plist"), basename


def test_4_plists_defer_has_no_handoff_watch_entry():
    with open(DEFER_PATH, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert "handoff-watch" not in stripped, (
                "plists.defer gates handoff-watch -- a newborn would never "
                "drain its mailbox (S1 conflicts with S6): %r" % stripped)


def test_5_synthetic_mint_seeds_capabilities_matching_spec():
    if not os.path.isfile(MINT_SH):
        print("SKIP test_5: scripts/mint.sh not present (not the canonical "
              "repo checkout) -- NOT a pass")
        return
    with tempfile.TemporaryDirectory() as tmp:
        out, inst = _mint_sandbox(tmp)
        assert out.returncode == 0, out.stdout + out.stderr
        cap_path = os.path.join(inst, "config", "handoff.capabilities")
        assert os.path.isfile(cap_path), "mint did not write config/handoff.capabilities"
        with open(cap_path, encoding="utf-8") as fh:
            content = fh.read()
        assert content == CAP_SEED, (content, CAP_SEED)


def test_6_remint_does_not_clobber_owner_edited_capabilities():
    if not os.path.isfile(MINT_SH):
        print("SKIP test_6: scripts/mint.sh not present (not the canonical "
              "repo checkout) -- NOT a pass")
        return
    with tempfile.TemporaryDirectory() as tmp:
        out, inst = _mint_sandbox(tmp)
        assert out.returncode == 0, out.stdout + out.stderr
        cap_path = os.path.join(inst, "config", "handoff.capabilities")
        owner_text = "# owner-edited declaration, must survive re-mint\n"
        with open(cap_path, "w", encoding="utf-8") as fh:
            fh.write(owner_text)
        out2, _ = _mint_sandbox(tmp, "--force")
        assert out2.returncode == 0, out2.stdout + out2.stderr
        with open(cap_path, encoding="utf-8") as fh:
            content = fh.read()
        assert content == owner_text, (
            "re-mint clobbered an owner-edited config/handoff.capabilities: "
            "%r" % content)


_SELF = os.path.abspath(__file__)


def _iter_template_text_files():
    for dirpath, dirnames, filenames in os.walk(TEMPLATE):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for name in filenames:
            if not name.endswith(SCAN_EXTS):
                continue
            path = os.path.join(dirpath, name)
            if os.path.abspath(path) == _SELF:
                continue  # this guard's own source names the patterns it forbids
            yield path


def test_7_guard_no_legacy_submit_callers_ensure_channel_confined():
    handoff_py = os.path.join(ROUTINES, "lib", "handoff", "handoff.py")
    submit_hits = []
    ensure_channel_hits_outside = []
    submit_re = re.compile(r"handoff\.submit\(")
    ensure_re = re.compile(r"ensure_channel\(")
    for path in _iter_template_text_files():
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        for m in submit_re.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            submit_hits.append("%s:%d" % (path, line_no))
        if ensure_re.search(text) and os.path.abspath(path) != os.path.abspath(handoff_py):
            ensure_channel_hits_outside.append(path)
    assert not submit_hits, (
        "legacy handoff.submit( caller(s) found in the template tree -- "
        "the reply leg has been routed back onto the legacy channel: %s"
        % submit_hits)
    assert not ensure_channel_hits_outside, (
        "ensure_channel( referenced outside routines/lib/handoff/handoff.py: "
        "%s" % ensure_channel_hits_outside)


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
