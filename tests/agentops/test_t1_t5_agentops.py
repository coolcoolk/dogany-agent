#!/usr/bin/env python3
"""DGN-387 release gate: AGENT-OPS.md channel acceptance tests (T1-T5).

Spec: DGN-387 design V3 section 3.6. The AGENT-OPS.md framework ops doc is
installed at mint (with a MANDATORY post-substitution sha recording in
.claude/.dogany-framework.sha, alongside RULES.md) and refreshed by
update.sh's 3k2 channel (post-substitution sha compare, dest-adjacent
mktemp + same-dir atomic mv, edit-detect + .user- backup).

Tests (all shell-level; NO network, NO real launchd registration):
  T1 fresh scratch mint -> immediate SAME-VERSION self-update:
     no user-modified WARN, no backup (manifest entry from mint matches).
  T2 self-update twice in a row: second run quiet (recorded
     post-substitution sha matches on-disk bytes).
  T3 hand-edited AGENT-OPS.md -> WARN + .user- backup + refresh.
  T4 run with IDENTITY_OK=0: byte-identical AGENT-OPS.md result vs
     IDENTITY_OK=1 (the file carries only __PROJECT_ROOT__, which
     substitutes outside the identity gate).
  T5 CROSS-VERSION: mint at framework state A -> edit template
     AGENT-OPS.md (and RULES.md) simulating state B -> self-update:
     refresh happens, NO user-modified WARN, NO stray backup, manifest
     updated. (The test the V2 design could not pass; proves mandatory
     mint-time sha recording works.)
T1/T2/T5 additionally assert: no AGENT-OPS.md.new.* litter remains, and
the installed file's mode matches the template source.

The whole suite runs against a COPY of the repo (T5 edits the template),
with a sandboxed HOME and a stubbed launchctl so watchdog registration
never touches the real user session.

Run: python3 tests/agentops/test_t1_t5_agentops.py   (exit 0 = pass)
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


class Sandbox(object):
    """Repo copy + sandbox HOME + stubbed launchctl on PATH."""

    def __init__(self):
        self.base = tempfile.mkdtemp(prefix="dgn387-")
        self.repo = os.path.join(self.base, "repo")
        self.home = os.path.join(self.base, "home")
        self.bindir = os.path.join(self.base, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bindir)
        # launchctl stub: watchdog_setup.sh must never register real jobs.
        stub = os.path.join(self.bindir, "launchctl")
        with open(stub, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub, 0o755)
        # Copy the repo (T5 mutates the template). Keep symlinks as symlinks
        # (template RULES.md -> ../../rules/RULES.md must stay relative).
        subprocess.run(
            ["rsync", "-a",
             "--exclude", ".git",
             "--exclude", ".pytest_cache",
             "--exclude", "__pycache__",
             "--exclude", "bridge/venv",
             REPO_ROOT + "/", self.repo + "/"],
            check=True, capture_output=True)
        self.mint_sh = os.path.join(self.repo, "scripts", "mint.sh")
        self.update_sh = os.path.join(self.repo, "update.sh")
        self.template_ops = os.path.join(
            self.repo, "agents", ".template", "AGENT-OPS.md")
        self.canonical_rules = os.path.join(self.repo, "rules", "RULES.md")

    def env(self):
        e = dict(os.environ)
        e["HOME"] = self.home
        e["PATH"] = self.bindir + os.pathsep + e.get("PATH", "")
        e["DOGANY_LANG"] = "en"
        e["DOGANY_BOT_TOKEN"] = ""
        return e

    def mint(self, root, name):
        return subprocess.run(
            ["bash", self.mint_sh, "--root", root, "--name", name,
             "--no-venv"],
            capture_output=True, text=True, env=self.env())

    def update(self, root, dry_run=False):
        cmd = ["bash", self.update_sh, "--root", root, "--no-pull", "--yes"]
        if dry_run:
            cmd.append("--dry-run")
        return subprocess.run(cmd, capture_output=True, text=True,
                              env=self.env())

    def cleanup(self):
        shutil.rmtree(self.base, ignore_errors=True)


def _read(path):
    with open(path) as fh:
        return fh.read()


def _sha(path):
    out = subprocess.run("shasum < %s" % subprocess.list2cmdline([path]),
                         shell=True, capture_output=True, text=True)
    return out.stdout.split()[0] if out.stdout else ""


def _manifest_sha(inst, rel):
    mf = os.path.join(inst, ".claude", ".dogany-framework.sha")
    if not os.path.exists(mf):
        return ""
    for line in _read(mf).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == rel:
            return parts[1]
    return ""


def _litter(inst):
    return [f for f in os.listdir(inst) if f.startswith("AGENT-OPS.md.new.")]


def _user_backups(inst):
    return [f for f in os.listdir(inst) if f.startswith("AGENT-OPS.md.user-")]


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _dunder_tokens(inst):
    hits = []
    for f in os.listdir(inst):
        p = os.path.join(inst, f)
        if f.endswith(".md") and os.path.isfile(p):
            if re.search(r"__[A-Z][A-Z_]*__", _read(p)):
                hits.append(f)
    return hits


def test_t1(sb, inst):
    print("(T1) fresh mint -> same-version update: quiet")
    r = sb.mint(inst, "t387bot")
    _check("mint exited 0", r.returncode == 0,
           "rc=%d err=%s" % (r.returncode, r.stderr[-400:]))
    # Scope to the dunder-token class DGN-387 kills. The angle-bracket warn
    # for the onboarding block's <HANDOFF_PEER_AG> mention is a PRE-EXISTING
    # condition on main (AGENT.md:107, inside the one-time onboarding
    # comment), out of this ticket's scope.
    _check("mint reported no __X__ placeholder survivors",
           "placeholder survivors (__X__ tokens)" not in r.stdout + r.stderr)
    ops = os.path.join(inst, "AGENT-OPS.md")
    _check("AGENT-OPS.md installed at instance root", os.path.isfile(ops))
    _check("AGENT-OPS.md substituted (carries instance root, no dunders)",
           os.path.isfile(ops) and inst in _read(ops))
    _check("no dunder tokens in any instance-root *.md",
           _dunder_tokens(inst) == [], str(_dunder_tokens(inst)))
    _check("mint recorded AGENT-OPS.md post-substitution sha",
           _manifest_sha(inst, "AGENT-OPS.md") == _sha(ops),
           "manifest=%s file=%s" % (_manifest_sha(inst, "AGENT-OPS.md"),
                                    _sha(ops)))
    _check("mint recorded RULES.md sha",
           _manifest_sha(inst, "RULES.md") ==
           _sha(os.path.join(inst, "RULES.md")))
    r = sb.update(inst)
    out = r.stdout + r.stderr
    _check("update exited 0", r.returncode == 0,
           "rc=%d err=%s" % (r.returncode, r.stderr[-400:]))
    _check("no user-modified AGENT-OPS WARN",
           "user-modified AGENT-OPS.md" not in out)
    _check("no user-modified RULES WARN",
           "user-modified RULES.md" not in out)
    _check("no .user- backup created", _user_backups(inst) == [])
    _check("no AGENT-OPS.md.new.* litter", _litter(inst) == [])
    _check("installed mode matches template source",
           _mode(ops) == _mode(sb.template_ops),
           "%o vs %o" % (_mode(ops), _mode(sb.template_ops)))


def test_t2(sb, inst):
    print("(T2) update twice in a row: second run quiet")
    sha_before = _manifest_sha(inst, "AGENT-OPS.md")
    r = sb.update(inst)
    out = r.stdout + r.stderr
    _check("second update exited 0", r.returncode == 0,
           "rc=%d err=%s" % (r.returncode, r.stderr[-400:]))
    _check("second run quiet (no user-modified WARN)",
           "user-modified AGENT-OPS.md" not in out)
    _check("no .user- backup", _user_backups(inst) == [])
    _check("no AGENT-OPS.md.new.* litter", _litter(inst) == [])
    _check("recorded sha stable across runs",
           _manifest_sha(inst, "AGENT-OPS.md") == sha_before)
    _check("recorded sha matches on-disk bytes",
           _manifest_sha(inst, "AGENT-OPS.md") ==
           _sha(os.path.join(inst, "AGENT-OPS.md")))


def test_t3(sb, inst):
    print("(T3) hand-edited AGENT-OPS.md -> WARN + backup + refresh")
    ops = os.path.join(inst, "AGENT-OPS.md")
    clean = _read(ops)
    with open(ops, "a") as fh:
        fh.write("\nHAND-EDIT SENTINEL DGN-387\n")
    r = sb.update(inst)
    out = r.stdout + r.stderr
    _check("update exited 0", r.returncode == 0,
           "rc=%d err=%s" % (r.returncode, r.stderr[-400:]))
    _check("user-modified WARN fired",
           "user-modified AGENT-OPS.md" in out)
    baks = _user_backups(inst)
    _check("exactly one .user- backup", len(baks) == 1, str(baks))
    _check("backup carries the hand edit",
           bool(baks) and "HAND-EDIT SENTINEL DGN-387" in
           _read(os.path.join(inst, baks[0])))
    _check("dest refreshed back to framework content",
           _read(ops) == clean)
    _check("manifest re-recorded to installed sha",
           _manifest_sha(inst, "AGENT-OPS.md") == _sha(ops))
    for b in baks:
        os.remove(os.path.join(inst, b))


def test_t4(sb, inst):
    print("(T4) IDENTITY_OK=0 run: byte-identical AGENT-OPS.md result")
    ops = os.path.join(inst, "AGENT-OPS.md")
    bytes_identity_ok = _read(ops)
    conf = os.path.join(inst, ".instance.conf")
    saved = _read(conf)
    degraded = "\n".join(
        l for l in saved.splitlines()
        if not l.startswith("DOGANY_AGENT_LABEL=")
        and not l.startswith("DOGANY_USER_LABEL=")) + "\n"
    with open(conf, "w") as fh:
        fh.write(degraded)
    r = sb.update(inst)
    out = r.stdout + r.stderr
    _check("update exited 0 under IDENTITY_OK=0", r.returncode == 0,
           "rc=%d err=%s" % (r.returncode, r.stderr[-400:]))
    _check("identity substitution actually skipped (warn present)",
           "skipping identity placeholder substitution" in out)
    _check("no user-modified WARN under IDENTITY_OK=0",
           "user-modified AGENT-OPS.md" not in out)
    _check("AGENT-OPS.md byte-identical to IDENTITY_OK=1 result",
           _read(ops) == bytes_identity_ok)
    _check("no .user- backup", _user_backups(inst) == [])
    with open(conf, "w") as fh:
        fh.write(saved)


def test_t5(sb):
    print("(T5) cross-version: mint at A, template edited to B, update quiet")
    inst = os.path.join(sb.base, "inst-t5")
    r = sb.mint(inst, "t387xver")
    _check("mint exited 0", r.returncode == 0,
           "rc=%d err=%s" % (r.returncode, r.stderr[-400:]))
    ops = os.path.join(inst, "AGENT-OPS.md")
    sha_a = _manifest_sha(inst, "AGENT-OPS.md")
    # Simulate framework state B: template AGENT-OPS.md + canonical RULES.md
    # both change after the mint.
    with open(sb.template_ops, "a") as fh:
        fh.write("\nSTATE-B MARKER (AGENT-OPS) DGN-387\n")
    with open(sb.canonical_rules, "a") as fh:
        fh.write("\nSTATE-B MARKER (RULES) DGN-387\n")
    r = sb.update(inst)
    out = r.stdout + r.stderr
    _check("update exited 0", r.returncode == 0,
           "rc=%d err=%s" % (r.returncode, r.stderr[-400:]))
    _check("NO user-modified AGENT-OPS WARN on pristine instance",
           "user-modified AGENT-OPS.md" not in out)
    _check("NO user-modified RULES WARN on pristine instance",
           "user-modified RULES.md" not in out)
    _check("no stray AGENT-OPS backup", _user_backups(inst) == [])
    _check("no stray RULES backup",
           [f for f in os.listdir(inst)
            if f.startswith("RULES.md.user-")] == [])
    _check("AGENT-OPS.md refreshed to state B",
           "STATE-B MARKER (AGENT-OPS) DGN-387" in _read(ops))
    _check("RULES.md refreshed to state B",
           "STATE-B MARKER (RULES) DGN-387" in
           _read(os.path.join(inst, "RULES.md")))
    _check("manifest updated to the new sha",
           _manifest_sha(inst, "AGENT-OPS.md") == _sha(ops) and
           _manifest_sha(inst, "AGENT-OPS.md") != sha_a)
    _check("no AGENT-OPS.md.new.* litter", _litter(inst) == [])
    _check("installed mode matches template source",
           _mode(ops) == _mode(sb.template_ops))


def main():
    sb = Sandbox()
    try:
        inst = os.path.join(sb.base, "inst-t1")
        test_t1(sb, inst)
        test_t2(sb, inst)
        test_t3(sb, inst)
        test_t4(sb, inst)
        test_t5(sb)
    finally:
        sb.cleanup()
    if _failures:
        print("FAILURES: %s" % ", ".join(_failures))
        return 1
    print("ALL PASS (T1-T5)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
