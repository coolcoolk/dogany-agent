#!/usr/bin/env python3
# test-lane: fast
"""DGN-1488 -- a fresh mint must be born with a reception entrance.

Measurement (ticket DGN-1488): a freshly minted agent had no
handoff mailbox and nothing in the mint pipeline provisioned one, so the
DGN-635 FATAL-1 sender guard (handoff.MailboxMissingError) made every
message TO a new agent fail closed -- and the error message named a
remedy script (routines/handoff-provision.sh) that existed in no tree.

What this suite locks:

  P1  the template ships routines/handoff-provision.sh, executable
  P2  provisioning creates <DOGANY_HANDOFF_ROOT>/<slug>/{inbox,done,dead,
      tmp} from config/agent.conf SLUG= (the DGN-635 FIX-3 mint stamp)
  P3  idempotent: a second run exits 0 and changes nothing; --check is 1
      before provisioning and 0 after
  P4  slug fallback matches engine.py own_slug(): no SLUG= line -> the
      instance root's basename, lowercased
  P5  end-to-end: engine.submit to the slug fails closed BEFORE
      provisioning and lands a message in inbox/ AFTER it
  P6  mint pipeline wiring: scripts/mint.sh invokes the provision script
      and stamps SLUG= (grep-level landing gate -- the full mint runs in
      tests/install_smoke.sh, manual lane)
  P7  a path-hostile slug value is refused loudly (exit 2), mirroring
      engine.validate_slug's trust boundary

NOT covered on purpose: consume/watch registration (DGN-1379 R2, not
landed) and any handoff_cli surface (retired permanently, DGN-1379 D1).

Run: python3 routines/tests/test-dgn1488-handoff-provision.py
     (or: pytest routines/tests/test-dgn1488-handoff-provision.py)
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)                       # routines/
LIB = os.path.join(ROUTINES, "lib", "handoff")          # routines/lib/handoff/
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(ROUTINES)))
PROVISION = os.path.join(ROUTINES, "handoff-provision.sh")
MINT_SH = os.path.join(REPO_ROOT, "scripts", "mint.sh")

sys.path.insert(0, LIB)

import engine                                                   # noqa: E402
import handoff                                                   # noqa: E402

MAILDIRS = ("inbox", "done", "dead", "tmp")


def _mk_instance(tmp, name, conf_lines=None):
    """Fixture instance root carrying a copy of the provision script (it
    derives its own root from its file location, like the real layout)."""
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, "routines"))
    shutil.copy2(PROVISION, os.path.join(root, "routines",
                                          "handoff-provision.sh"))
    if conf_lines is not None:
        os.makedirs(os.path.join(root, "config"))
        with open(os.path.join(root, "config", "agent.conf"), "w") as fh:
            fh.write("\n".join(conf_lines) + "\n")
    return root


def _run_provision(root, base, *args):
    env = dict(os.environ, DOGANY_HANDOFF_ROOT=base)
    return subprocess.run(
        ["bash", os.path.join(root, "routines", "handoff-provision.sh"),
         *args],
        capture_output=True, text=True, env=env)


def test_P1_template_ships_provision_script_executable():
    assert os.path.isfile(PROVISION), (
        "template does not ship routines/handoff-provision.sh -- the "
        "remedy path handoff.py's MailboxMissingError names (DGN-1488)")
    assert os.access(PROVISION, os.X_OK), "provision script not executable"


def test_P2_provision_creates_maildir_from_conf_slug():
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "handoff")
        root = _mk_instance(tmp, "somedir",
                            conf_lines=["AGENT_LANG=en", "SLUG=alpha"])
        out = _run_provision(root, base)
        assert out.returncode == 0, out.stderr
        for d in MAILDIRS:
            assert os.path.isdir(os.path.join(base, "alpha", d)), (
                "missing %s: %s / %s" % (d, out.stdout, out.stderr))
        # conf SLUG= wins over the basename ("somedir")
        assert not os.path.isdir(os.path.join(base, "somedir"))


def test_P3_idempotent_and_check_mode():
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "handoff")
        root = _mk_instance(tmp, "beta-inst", conf_lines=["SLUG=beta"])
        pre = _run_provision(root, base, "--check")
        assert pre.returncode == 1, (
            "--check must fail-closed before provisioning: %s" % pre.stdout)
        first = _run_provision(root, base)
        assert first.returncode == 0, first.stderr
        second = _run_provision(root, base)
        assert second.returncode == 0, second.stderr
        assert "already provisioned" in second.stdout, second.stdout
        post = _run_provision(root, base, "--check")
        assert post.returncode == 0, post.stderr


def test_P4_slug_fallback_is_lowercased_basename():
    # mirrors engine.py own_slug(): conf SLUG= first, else basename lowered.
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "handoff")
        root = _mk_instance(tmp, "Alpha")     # no config/agent.conf at all
        out = _run_provision(root, base)
        assert out.returncode == 0, out.stderr
        assert os.path.isdir(os.path.join(base, "alpha", "inbox")), (
            "basename fallback must lowercase like engine.own_slug")


def test_P5_submit_closed_before_and_lands_after_provision():
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "handoff")
        root = _mk_instance(tmp, "gamma", conf_lines=["SLUG=gamma"])
        os.environ["DOGANY_HANDOFF_ROOT"] = base
        try:
            # BEFORE: the ticket's defect -- recipient unreachable, loud fail
            try:
                engine.submit("gamma", "decision.notice", from_slug="ag")
                raise AssertionError(
                    "expected MailboxMissingError before provisioning")
            except handoff.MailboxMissingError:
                pass
            # provision the recipient, then the same submit lands
            out = _run_provision(root, base)
            assert out.returncode == 0, out.stderr
            path = engine.submit("gamma", "decision.notice",
                                  payload={"k": "v"}, body="hello",
                                  from_slug="ag")
            assert os.path.isfile(path), path
            assert os.path.dirname(path) == os.path.join(base, "gamma",
                                                          "inbox")
            with open(path) as fh:
                text = fh.read()
            assert "to: gamma" in text
            assert "from: ag" in text
        finally:
            os.environ.pop("DOGANY_HANDOFF_ROOT", None)


def test_P6_mint_pipeline_is_wired():
    if not os.path.isfile(MINT_SH):
        # scripts/mint.sh ships in the canonical repo only -- a minted
        # instance never carries the tool that mints IT (REPO_ROOT here
        # would resolve outside the instance entirely, since this suite's
        # own routines/ is one level from instance root, not the three
        # canonical template levels). SKIP is explicit and printed, never
        # a silent pass (DGN-1154 quiet-green doctrine).
        print("SKIP P6: scripts/mint.sh not present "
              "(not the canonical repo checkout) -- NOT a pass")
        return
    with open(MINT_SH, encoding="utf-8") as fh:
        src = fh.read()
    assert "routines/handoff-provision.sh" in src, (
        "scripts/mint.sh no longer invokes the provision script -- a fresh "
        "mint is born unreachable again (DGN-1488)")
    assert "SLUG=" in src, (
        "scripts/mint.sh no longer stamps SLUG= (DGN-635 FIX-3 mint stamp)")


def test_P7_hostile_slug_refused():
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "handoff")
        root = _mk_instance(tmp, "evil", conf_lines=["SLUG=../escape"])
        out = _run_provision(root, base)
        assert out.returncode == 2, (out.returncode, out.stdout, out.stderr)
        assert not os.path.isdir(os.path.join(tmp, "escape"))


def test_P8_peer_slug_argument_refused_loudly():
    # DGN-1563: a positional recipient slug used to be swallowed silently
    # (own mailbox provisioned, peer never created). Now refused, rc 2.
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "handoff")
        root = _mk_instance(tmp, "zeta", conf_lines=["SLUG=zeta"])
        for args in (("skull",), ("--check", "skull"), ("--bogus",)):
            out = _run_provision(root, base, *args)
            assert out.returncode == 2, (args, out.returncode, out.stderr)
            assert "own slug" in out.stderr, out.stderr
        assert not os.path.isdir(os.path.join(base, "zeta")), (
            "refused call must write nothing")
        assert not os.path.isdir(os.path.join(base, "skull"))


def test_P9_submit_refusal_names_recipient_side():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DOGANY_HANDOFF_ROOT"] = os.path.join(tmp, "handoff")
        try:
            try:
                engine.submit("delta", "decision.notice", from_slug="ag")
                raise AssertionError("expected MailboxMissingError")
            except handoff.MailboxMissingError as exc:
                msg = str(exc)
                assert "recipient's own instance ('delta')" in msg, msg
                assert "cannot provision a peer" in msg, msg
        finally:
            os.environ.pop("DOGANY_HANDOFF_ROOT", None)


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
