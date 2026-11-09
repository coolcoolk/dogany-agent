#!/usr/bin/env python3
# test-lane: fast
"""DGN-1379-SPEC-slug-consume-layer -- handoff.consume_mailbox() (S1),
engine.handlers_from_capabilities() (S2), and engine.py consume's rewire
onto the slug mailbox (S3).

Locks, in the spec's 검증 order:

  1  synthetic slug mailbox (temp HOME) + `decision.notice
     builtin:notice-append` capability declaration -> submit one message
     -> processed:1, file moved to done/, notices.log gains one line
  2  resubmitting the SAME message (same ulid) -> deduped:1, processed:0
  3  a type with no capability declaration -> left:1, stays in inbox,
     errors:0, and (DGN-1774) exactly one session-inbox inject
  4  capabilities file ABSENT -> empty handler table, normal operation,
     rc=0, absence logged
  5  capabilities file PRESENT but malformed (one bad line) -> exception
     aborts consumption, rc!=0, the message is never moved
  6  SLUG= absent from config/agent.conf -> fail-closed, rc!=0, no
     mailbox touched
  7  stats dict key names/order match handoff.consume()'s (machine
     comparison)
  8  handoff.consume()'s external contract is unchanged: this file only
     asserts consume()'s signature text is untouched (the cheap, static
     half); the dynamic half is the pre-existing handoff test suites
     (test-dgn1379-handoff-lib-landing.py, test-dgn1379-consume-
     entrypoint.py) run separately -- their own exit codes are what the
     implementer's report must carry verbatim (do NOT re-run them here).

Every subprocess/engine call below runs under DOGANY_HANDOFF_ROOT pointed
at a per-test tempdir -- NEVER the real ~/.dogany/handoff (hard rule: no
live-instance contact). Never point this env var anywhere but a
tempfile.TemporaryDirectory() path.

Run: python3 routines/tests/test-dgn1379-slug-consume-layer.py
     (or: pytest routines/tests/test-dgn1379-slug-consume-layer.py)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)                    # routines/
LIB = os.path.join(ROUTINES, "lib", "handoff")       # routines/lib/handoff/
PY = sys.executable

sys.path.insert(0, LIB)

import handoff                                                   # noqa: E402

CAP_NOTICE = "decision.notice builtin:notice-append idem=ulid\n"


def _mk_instance(tmp, name, slug="", capabilities_text=None):
    """A standalone <tmp>/<name> instance root: routines/ copy (own_root()
    self-derivation needs a real file tree, same pattern as
    test-dgn1379-consume-entrypoint.py's _mk_fixture_root) + config/
    agent.conf (+ optional SLUG=) + optional config/handoff.capabilities.
    """
    root = os.path.join(tmp, name)
    dst_routines = os.path.join(root, "routines")
    shutil.copytree(ROUTINES, dst_routines,
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))
    os.makedirs(os.path.join(root, "config"), exist_ok=True)
    # DGN-1774: consume's notify pass injects leftovers here (bridge
    # session-inbox); a live instance always has it.
    os.makedirs(os.path.join(root, ".telegram_bot", "session-inbox"),
                exist_ok=True)
    with open(os.path.join(root, "config", "agent.conf"), "w") as fh:
        if slug:
            fh.write("SLUG=%s\n" % slug)
        else:
            fh.write("# no SLUG on purpose (test 6)\n")
    if capabilities_text is not None:
        with open(os.path.join(root, "config", "handoff.capabilities"),
                  "w") as fh:
            fh.write(capabilities_text)
    return root


def _mk_mailbox(mailbox_base, slug):
    root = os.path.join(mailbox_base, slug)
    for d in ("inbox", "done", "dead", "tmp"):
        os.makedirs(os.path.join(root, d))
    return root


def _run_consume(root, mailbox_base, extra_args=()):
    engine_py = os.path.join(root, "routines", "lib", "handoff", "engine.py")
    env = dict(os.environ)
    env["DOGANY_HANDOFF_ROOT"] = mailbox_base
    out = subprocess.run([PY, engine_py, "consume", *extra_args],
                         capture_output=True, text=True, env=env)
    return out


def _submit(mailbox_base, slug, msg_type, ulid=None):
    prev = os.environ.get("DOGANY_HANDOFF_ROOT")
    os.environ["DOGANY_HANDOFF_ROOT"] = mailbox_base
    try:
        meta = {"from": "warg", "type": msg_type}
        if ulid:
            meta["id"] = ulid
        return handoff.submit_to_slug(slug, meta, body="hello")
    finally:
        if prev is None:
            os.environ.pop("DOGANY_HANDOFF_ROOT", None)
        else:
            os.environ["DOGANY_HANDOFF_ROOT"] = prev


# -- 1. synthetic slug mailbox + notice-append -> processed/done/notices.log -

def test_1_synthetic_mailbox_notice_append_processed_and_moved():
    with tempfile.TemporaryDirectory() as tmp:
        mailbox_base = os.path.join(tmp, "mailboxes")
        os.makedirs(mailbox_base)
        root = _mk_instance(tmp, "metal", slug="metal",
                            capabilities_text=CAP_NOTICE)
        _mk_mailbox(mailbox_base, "metal")
        _submit(mailbox_base, "metal", "decision.notice",
               ulid="01TESTULID0000000000001A")
        out = _run_consume(root, mailbox_base)
        assert out.returncode == 0, out.stderr
        stats = json.loads(out.stdout.strip())
        assert stats["processed"] == 1, stats
        done_dir = os.path.join(mailbox_base, "metal", "done")
        assert os.listdir(done_dir), "message did not move to done/"
        notices_log = os.path.join(root, "files", "handoff", "notices.log")
        assert os.path.isfile(notices_log), "notices.log not created"
        with open(notices_log) as fh:
            text = fh.read()
        # one appended RECORD (header + optional indented body lines, same
        # shape as builtins.py's own B1 test) -- the ulid appears exactly
        # once, not a raw physical-line count (a body can span lines).
        assert text.count("01TESTULID0000000000001A") == 1, text


# -- 2. same ulid resubmitted -> deduped:1, processed:0 ----------------------

def test_2_resubmit_same_ulid_dedupes():
    with tempfile.TemporaryDirectory() as tmp:
        mailbox_base = os.path.join(tmp, "mailboxes")
        os.makedirs(mailbox_base)
        root = _mk_instance(tmp, "metal", slug="metal",
                            capabilities_text=CAP_NOTICE)
        _mk_mailbox(mailbox_base, "metal")
        ulid = "01TESTULID0000000000002B"
        _submit(mailbox_base, "metal", "decision.notice", ulid=ulid)
        out1 = _run_consume(root, mailbox_base)
        assert json.loads(out1.stdout.strip())["processed"] == 1, out1.stdout
        _submit(mailbox_base, "metal", "decision.notice", ulid=ulid)
        out2 = _run_consume(root, mailbox_base)
        assert out2.returncode == 0, out2.stderr
        stats = json.loads(out2.stdout.strip())
        assert stats["deduped"] == 1, stats
        assert stats["processed"] == 0, stats


# -- 3. undeclared type -> left:1, stays in inbox, errors:0 -----------------

def test_3_undeclared_type_left_in_inbox():
    with tempfile.TemporaryDirectory() as tmp:
        mailbox_base = os.path.join(tmp, "mailboxes")
        os.makedirs(mailbox_base)
        root = _mk_instance(tmp, "metal", slug="metal",
                            capabilities_text=CAP_NOTICE)
        _mk_mailbox(mailbox_base, "metal")
        _submit(mailbox_base, "metal", "proposal.schedule",
               ulid="01TESTULID0000000000003C")
        out = _run_consume(root, mailbox_base)
        assert out.returncode == 0, out.stderr
        stats = json.loads(out.stdout.strip())
        assert stats["left"] == 1, stats
        assert stats["errors"] == 0, stats
        inbox = os.path.join(mailbox_base, "metal", "inbox")
        remaining = [n for n in os.listdir(inbox) if n.endswith(".md")]
        assert len(remaining) == 1, remaining
        # DGN-1774: "left" is never silent -- the session is told once.
        injected = os.listdir(
            os.path.join(root, ".telegram_bot", "session-inbox"))
        assert len(injected) == 1, injected


# -- 4. capabilities file absent -> empty table, rc=0, absence logged -------

def test_4_capabilities_absent_empty_table_rc_zero():
    with tempfile.TemporaryDirectory() as tmp:
        mailbox_base = os.path.join(tmp, "mailboxes")
        os.makedirs(mailbox_base)
        root = _mk_instance(tmp, "metal", slug="metal",
                            capabilities_text=None)
        assert not os.path.exists(
            os.path.join(root, "config", "handoff.capabilities"))
        _mk_mailbox(mailbox_base, "metal")
        out = _run_consume(root, mailbox_base)
        assert out.returncode == 0, out.stderr
        assert "handoff.capabilities absent" in out.stderr, out.stderr


# -- 5. capabilities file broken -> exception aborts, rc!=0, no move --------

def test_5_capabilities_broken_aborts_rc_nonzero_no_move():
    with tempfile.TemporaryDirectory() as tmp:
        mailbox_base = os.path.join(tmp, "mailboxes")
        os.makedirs(mailbox_base)
        root = _mk_instance(tmp, "metal", slug="metal",
                            capabilities_text="bogus.type not-a-real-mode\n")
        _mk_mailbox(mailbox_base, "metal")
        _submit(mailbox_base, "metal", "decision.notice",
               ulid="01TESTULID0000000000005E")
        out = _run_consume(root, mailbox_base)
        assert out.returncode != 0, out.stdout
        assert "capabilities file invalid" in out.stderr, out.stderr
        inbox = os.path.join(mailbox_base, "metal", "inbox")
        remaining = [n for n in os.listdir(inbox) if n.endswith(".md")]
        assert len(remaining) == 1, remaining
        done_dir = os.path.join(mailbox_base, "metal", "done")
        assert not os.path.isdir(done_dir) or not os.listdir(done_dir)


# -- 6. SLUG= absent -> fail-closed, rc!=0, no mailbox touched --------------

def test_6_slug_absent_fail_closed_no_mailbox_touched():
    with tempfile.TemporaryDirectory() as tmp:
        mailbox_base = os.path.join(tmp, "mailboxes")
        os.makedirs(mailbox_base)
        root = _mk_instance(tmp, "metal", slug="",
                            capabilities_text=CAP_NOTICE)
        # No mailbox provisioned for any slug -- if consume guessed one and
        # touched it, mailbox_base would gain an unexpected subdirectory.
        out = _run_consume(root, mailbox_base)
        assert out.returncode != 0, out.stdout
        assert "SLUG=" in out.stderr, out.stderr
        assert os.listdir(mailbox_base) == [], (
            "consume touched a mailbox despite absent SLUG=: %s"
            % os.listdir(mailbox_base))


# -- 7. stats key names/order match consume()'s ------------------------------

def test_7_stats_keys_match_consume_order():
    with tempfile.TemporaryDirectory() as tmp:
        legacy_stats = handoff.consume(tmp, {})
        mailbox_base = os.path.join(tmp, "mailboxes")
        os.makedirs(mailbox_base)
        root = _mk_instance(tmp, "metal", slug="metal",
                            capabilities_text=CAP_NOTICE)
        _mk_mailbox(mailbox_base, "metal")
        out = _run_consume(root, mailbox_base)
        assert out.returncode == 0, out.stderr
        mailbox_stats = json.loads(out.stdout.strip())
        assert list(mailbox_stats.keys()) == list(legacy_stats.keys()), (
            (list(mailbox_stats.keys()), list(legacy_stats.keys())))


# -- 8. consume()'s external contract: static half (signature untouched) ----
#       the dynamic half (existing suites still pass) is NOT re-run here --
#       see module docstring; report it from the two suites' own exit codes.

def test_8_consume_signature_unchanged_static_check():
    with open(os.path.join(LIB, "handoff.py"), encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(
        r"def consume\(root, handlers, cap=DEFAULT_RUN_CAP, now=None, "
        r"lock=None,\n\s*log=None, on_dead=None\):", src)
    assert m is not None, "consume()'s signature line changed"


# -- 9/10. coverage MOVED here from test-dgn1379-consume-entrypoint.py -------
# Those two cases (expiry bookkeeping, unparseable -> errors) were written
# against the legacy root layout because at the time _cmd_consume still read
# it. S3 of this spec rewired the entry point onto the slug mailbox, so the
# legacy fixtures became structurally unreachable. The BEHAVIOUR is still
# worth locking, so it moves here onto the slug path rather than being
# deleted. Deleting would have silently dropped expiry/error coverage.

def test_9_expired_message_counted_and_moved_to_dead():
    with tempfile.TemporaryDirectory() as tmp:
        mailbox_base = os.path.join(tmp, "mailboxes")
        os.makedirs(mailbox_base)
        root = _mk_instance(tmp, "metal", slug="metal",
                            capabilities_text=CAP_NOTICE)
        _mk_mailbox(mailbox_base, "metal")
        prev = os.environ.get("DOGANY_HANDOFF_ROOT")
        os.environ["DOGANY_HANDOFF_ROOT"] = mailbox_base
        try:
            handoff.submit_to_slug("metal", {
                "from": "warg", "type": "decision.notice",
                "created": "2020-01-01T00:00:00Z",
                "expires": "2020-01-02T00:00:00Z",
            }, body="stale")
        finally:
            if prev is None:
                os.environ.pop("DOGANY_HANDOFF_ROOT", None)
            else:
                os.environ["DOGANY_HANDOFF_ROOT"] = prev
        out = _run_consume(root, mailbox_base)
        assert out.returncode == 0, out.stderr
        stats = json.loads(out.stdout.strip())
        assert stats["expired"] == 1, stats
        assert stats["processed"] == 0, stats
        dead_dir = os.path.join(mailbox_base, "metal", "dead")
        assert os.listdir(dead_dir), "expired message did not land in dead/"


def test_10_unparseable_message_is_an_error_and_exit_code_is_1():
    with tempfile.TemporaryDirectory() as tmp:
        mailbox_base = os.path.join(tmp, "mailboxes")
        os.makedirs(mailbox_base)
        root = _mk_instance(tmp, "metal", slug="metal",
                            capabilities_text=CAP_NOTICE)
        _mk_mailbox(mailbox_base, "metal")
        bad = os.path.join(mailbox_base, "metal", "inbox", "20260919-bad.md")
        with open(bad, "w") as fh:
            fh.write("this is not a handoff message at all\n")
        out = _run_consume(root, mailbox_base)
        stats = json.loads(out.stdout.strip())
        assert stats["errors"] == 1, stats
        assert out.returncode == 1, (out.returncode, out.stderr)



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
