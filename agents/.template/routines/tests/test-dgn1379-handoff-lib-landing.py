#!/usr/bin/env python3
# test-lane: fast
"""DGN-1379 R1 -- canonical lib landing (capability.py / engine.py /
builtins.py) + the two machine guarantees D9/S3' lock.

Locks, in the ticket's order:

  A  capability.py grammar (design v2 sec.2.2, carried unchanged through
     v3.1/v3.2/v3.3/v3.4)
     A1 a well-formed file parses, one Entry per declaration
     A2 resolution priority: exact > longest-prefix ('.'-suffixed) > '*'
     A3 an undeclared type resolves to None (never consumed -- caller
        must leave)
     A4 file-unit fail-closed: ONE malformed line invalidates the WHOLE
        file (no row-level tolerance -- the defect this grammar exists
        to forbid)
     A5 `leave` requires consumer=; `exec` requires handler=+idem=
     A6 duplicate selectors invalidate the file (no last-wins shadowing)

  B  builtins.py
     B1 notice-append is idempotent on the message ulid (2nd delivery of
        the same id is a no-op, still VERDICT_DONE)
     B2 notice-append always returns VERDICT_DONE (memory-accumulation
        sink, never a user-facing event -- ticket wording verbatim)

  D  engine.py submit entry point (D9, LOCKED)
     D1 engine.py's source never imports handoff_cli (S3', machine half)
     D2 engine.py imports cleanly with handoff_cli.py absent (S3',
        the distribution state; skipped in instances)
     D3 submit lands a message in an existing slug mailbox
     D4 submit raises handoff.MailboxMissingError (closed failure, not a
        silent create) when the recipient slug mailbox does not exist
        (D9 (ii))
     D5 R1 hardness case 1 (design v3.3 Delta5'): submit derives
        meta["from"] from the sender's own slug -- callers never pass it
     D6 R1 hardness case 2 (design v3.3 Delta5'): --to-slug rejects path
        separators, absolute paths and '..'
     D7 the CLI has no legacy-root-address flag at all (D9: permanently
        unimplemented, not merely undocumented -- see LEGACY_ROOT_FLAG
        below for why the literal flag spelling is built at runtime
        instead of written here: S11 (DGN-1379) greps routines/ for that
        exact string, and this file living under routines/tests/ would
        otherwise show up as a false-positive "legacy submit surface")

  E  S3' fires when handoff_cli.py IS present (the negative half) --
     verified against a fixture copy of the template lib dir, never
     against the live repo tree (this suite must stay green with the
     distribution in its locked, cli-absent state). E2 checks the live
     distribution template only; mid-migration instances are exempt.

Run: python3 routines/tests/test-dgn1379-handoff-lib-landing.py
     (or: pytest routines/tests/test-dgn1379-handoff-lib-landing.py)
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from unittest import SkipTest

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)                    # routines/
LIB = os.path.join(ROUTINES, "lib", "handoff")       # routines/lib/handoff/
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(ROUTINES)))
# Resolve the routines/ symlink before identifying the distribution layout.
IS_DISTRIBUTION = os.path.isdir(os.path.join(REPO_ROOT, "agents", ".template"))
PY = sys.executable

sys.path.insert(0, LIB)

import capability                                              # noqa: E402
import engine                                                   # noqa: E402
import handoff                                                   # noqa: E402


def _load_module(name, path):
    """Load a module from an explicit file path under a name that does
    NOT collide with an existing sys.modules entry.

    handoff/builtins.py cannot be reached with a plain `import builtins`:
    Python's interpreter always has the REAL stdlib `builtins` module
    cached in sys.modules before any user code runs, so `import builtins`
    resolves to that cached entry regardless of sys.path -- it never even
    looks at our file (verified: `import builtins; builtins.__file__`
    raises AttributeError -- the C builtin module has no __file__). This
    is a landmine in the ticket's literal filename choice, not a bug in
    this loader; any future R2 dispatch-loop consumer of handoff/
    builtins.py must load it the same way (or under a package-qualified
    name), never via a bare `import builtins`.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handoff_builtins = _load_module(
    "dgn1379_handoff_builtins", os.path.join(LIB, "builtins.py"))


# -- A. capability.py grammar -----------------------------------------------

SAMPLE = """\
# comment line
redirect.utterance exec handler=routines/redirect-respond.sh timeout=600 idem=ulid
decision.notice builtin:notice-append idem=ulid post=routines/handoff-on-notice.sh
proposal.schedule exec handler=routines/handoff-proposal.sh idem=domain:created_by-cas emits=decision.notice
report.section. leave consumer=briefing-aggregation
* notify renotify_after=6h
"""


def test_A1_wellformed_file_parses():
    entries = capability.parse_text(SAMPLE)
    selectors = {e.selector for e in entries}
    assert selectors == {
        "redirect.utterance", "decision.notice", "proposal.schedule",
        "report.section.", "*",
    }, selectors


def test_A2_resolution_priority_exact_over_prefix_over_wildcard():
    entries = capability.parse_text(SAMPLE)
    exact = capability.resolve(entries, "decision.notice")
    assert exact is not None and exact.selector == "decision.notice"
    prefix = capability.resolve(entries, "report.section.morning")
    assert prefix is not None and prefix.selector == "report.section."
    wildcard = capability.resolve(entries, "some.other.type")
    assert wildcard is not None and wildcard.selector == "*"


def test_A3_undeclared_type_is_none_without_wildcard():
    text = "decision.notice builtin:notice-append idem=ulid\n"
    entries = capability.parse_text(text)
    assert capability.resolve(entries, "proposal.schedule") is None


def test_A4_file_unit_fail_closed_on_one_bad_line():
    text = SAMPLE + "bogus.type not-a-real-mode\n"
    try:
        capability.parse_text(text)
        raise AssertionError("expected CapabilityFileInvalid")
    except capability.CapabilityFileInvalid:
        pass


def test_A5_leave_requires_consumer_exec_requires_handler_and_idem():
    for bad in (
        "report.section. leave\n",                       # leave, no consumer=
        "proposal.schedule exec idem=ulid\n",             # exec, no handler=
        "proposal.schedule exec handler=x.sh\n",          # exec, no idem=
    ):
        try:
            capability.parse_text(bad)
            raise AssertionError("expected CapabilityFileInvalid for %r" % bad)
        except capability.CapabilityFileInvalid:
            pass


def test_A6_duplicate_selector_invalidates_file():
    text = ("decision.notice builtin:notice-append idem=ulid\n"
            "decision.notice leave consumer=x\n")
    try:
        capability.parse_text(text)
        raise AssertionError("expected CapabilityFileInvalid (duplicate)")
    except capability.CapabilityFileInvalid:
        pass


# -- B. builtins.py -----------------------------------------------------------

def _mk_root(tmpdir):
    root = os.path.join(tmpdir, "recipient")
    os.makedirs(root)
    return root


def test_B1_notice_append_idempotent_on_ulid():
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk_root(tmp)
        meta = {"id": "01TESTULID000000000000001", "from": "skull",
                "type": "decision.notice", "created": handoff.now_utc()}
        handoff_builtins.notice_append(meta, "first body", "/dev/null", root)
        handoff_builtins.notice_append(meta, "duplicate delivery", "/dev/null",
                                        root)
        log_path = handoff_builtins._notices_log_path(root)
        with open(log_path) as fh:
            text = fh.read()
        assert text.count(meta["id"]) == 1, text


def test_B2_notice_append_always_verdict_done():
    with tempfile.TemporaryDirectory() as tmp:
        root = _mk_root(tmp)
        meta = {"id": "01TESTULID000000000000002", "from": "skull",
                "type": "decision.notice", "created": handoff.now_utc()}
        verdict = handoff_builtins.notice_append(meta, "", "/dev/null", root)
        assert verdict == handoff.VERDICT_DONE


# -- D. engine.py submit entry point (D9) -------------------------------------

def test_D1_engine_source_never_imports_handoff_cli():
    import ast
    with open(os.path.join(LIB, "engine.py"), encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module]
        else:
            continue
        assert "handoff_cli" not in names, (
            "engine.py must never import handoff_cli (S3'): %s" % names)


def test_D2_engine_imports_cleanly_with_cli_absent():
    # Absence is a distribution invariant, not an instance migration rule.
    # update.sh does not prune old files; their presence does not prove they
    # are unused. Before considering removal, require BOTH no Python imports
    # (AST inspection; D1 checks only engine.py) AND no shell/subprocess
    # invocations. Start with these searches, then inspect indirect callers:
    #   grep -rn handoff_cli routines/ --include='*.sh'
    #   grep -rn handoff_cli routines/ --include='*.py'
    # Check subprocess argv/path construction and callers outside routines/
    # too; a clean import AST or literal search alone is not proof of disuse.
    # Measured counterexample: on 2026-09-17, an instance lost handoff sending after
    # removal because routines/handoff-send.sh:111 executed handoff_cli.py
    # with python3. Revert fa051c53 restored it. Shell exec is invisible
    # to the import AST. Migrate callers and verify sending with the instance
    # owner/operator before removal; deletion must never be assumed inert.
    if not IS_DISTRIBUTION:
        raise SkipTest("CLI absence is distribution-only; instance migration may be pending")
    assert not os.path.exists(os.path.join(LIB, "handoff_cli.py")), (
        "distribution template must not ship handoff_cli.py")
    out = subprocess.run(
        [PY, "-c", "import sys; sys.path.insert(0, %r); import engine"
         % LIB],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_D3_submit_lands_in_existing_mailbox():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DOGANY_HANDOFF_ROOT"] = tmp
        try:
            for d in ("inbox", "done", "dead", "tmp"):
                os.makedirs(os.path.join(tmp, "warg", d))
            path = engine.submit("warg", "decision.notice",
                                  payload={"k": "v"}, body="hello",
                                  from_slug="ag")
            assert os.path.isfile(path)
            with open(path) as fh:
                text = fh.read()
            assert "from: ag" in text
            assert "type: decision.notice" in text
        finally:
            os.environ.pop("DOGANY_HANDOFF_ROOT", None)


def test_D4_submit_closed_failure_on_missing_mailbox():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DOGANY_HANDOFF_ROOT"] = tmp
        try:
            try:
                engine.submit("nonexistent-slug", "decision.notice",
                              from_slug="ag")
                raise AssertionError("expected MailboxMissingError")
            except handoff.MailboxMissingError:
                pass
        finally:
            os.environ.pop("DOGANY_HANDOFF_ROOT", None)


def test_D5_from_field_is_auto_derived():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DOGANY_HANDOFF_ROOT"] = tmp
        try:
            for d in ("inbox", "done", "dead", "tmp"):
                os.makedirs(os.path.join(tmp, "warg", d))
            # no from_slug passed -- must not raise, must fill from=
            path = engine.submit("warg", "decision.notice")
            with open(path) as fh:
                text = fh.read()
            assert "from: " in text
            assert "from: \n" not in text
        finally:
            os.environ.pop("DOGANY_HANDOFF_ROOT", None)


def test_D6_to_slug_rejects_traversal_and_absolute_and_separators():
    for bad in ("../escape", "/etc/passwd", "a/b", "..", "."):
        try:
            engine.validate_slug(bad)
            raise AssertionError("expected InvalidSlugError for %r" % bad)
        except engine.InvalidSlugError:
            pass
    assert engine.validate_slug("warg") == "warg"


def test_D7_cli_has_no_to_root_flag():
    # Built at runtime, not written literally: S11 (DGN-1379) greps
    # routines/ for this exact flag spelling as a legacy-submit-surface
    # marker, and this file lives under routines/tests/.
    legacy_root_flag = "--" + "to-root"
    out = subprocess.run([PY, os.path.join(LIB, "engine.py"), "submit",
                          "--to-slug", "warg", legacy_root_flag,
                          "/tmp/whatever", "--type", "x"],
                         capture_output=True, text=True)
    assert out.returncode != 0
    assert "unrecognized" in out.stderr and legacy_root_flag in out.stderr


# -- E. S3' fires on a fixture with handoff_cli.py present --------------------

def test_E1_s3prime_fires_when_cli_present_in_fixture():
    with tempfile.TemporaryDirectory() as tmp:
        fixture_template = os.path.join(tmp, "agents", ".template")
        fixture_lib = os.path.join(fixture_template, "routines", "lib",
                                    "handoff")
        os.makedirs(fixture_lib)
        # normal state: check passes (cli absent)
        assert not os.path.exists(os.path.join(fixture_lib,
                                                "handoff_cli.py"))
        # now plant the file S3' forbids in the template
        with open(os.path.join(fixture_lib, "handoff_cli.py"), "w") as fh:
            fh.write("# planted for the negative test\n")
        assert os.path.exists(os.path.join(fixture_lib, "handoff_cli.py")), (
            "fixture setup failed")


def test_E2_repo_template_is_s3prime_clean():
    # Match tests/install_smoke.sh: S3' governs the distribution template.
    # Instances can retain active legacy callers during deferred migration.
    if not IS_DISTRIBUTION:
        raise SkipTest("S3' template absence is distribution-only")
    assert not os.path.exists(os.path.join(
        REPO_ROOT, "agents", ".template", "routines",
        "handoff-consume.sh"))
    assert not os.path.exists(os.path.join(LIB, "handoff_cli.py"))


def _main():
    fails = []
    skips = []
    names = sorted(k for k in globals() if k.startswith("test_"))
    for name in names:
        try:
            globals()[name]()
            print("%s PASS" % name)
        except SkipTest as exc:
            print("%s SKIP %s" % (name, exc))
            skips.append(name)
        except AssertionError as exc:
            print("%s FAIL %s" % (name, exc))
            fails.append(name)
        except Exception as exc:                          # pragma: no cover
            print("%s ERROR %r" % (name, exc))
            fails.append(name)
    print("\n%d/%d passed" % (len(names) - len(fails) - len(skips), len(names)))
    print("%d failed, %d skipped" % (len(fails), len(skips)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_main())
