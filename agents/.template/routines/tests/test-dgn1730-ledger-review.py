#!/usr/bin/env python3
# test-lane: fast
"""DGN-1730: status-footer.py review filter -- a pending-review ledger row is
review load only while its branch (or the HEAD=<sha> in its note, when the
branch ref is gone) is NOT an ancestor of base.

Covers: merged branch, merged via sha only, unmerged, unresolvable, canonical
repo (dev base), git failure degrade, cache reuse/invalidation.

Run:  python3 routines/tests/test-dgn1730-ledger-review.py
Exit: 0 all pass, 1 any fail.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location(
    "status_footer", os.path.join(os.path.dirname(HERE), "status-footer.py"))
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        print("  PASS: %s" % desc)
        PASS += 1
    else:
        print("  FAIL: %s" % desc)
        FAIL += 1


ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                       text=True, env=ENV)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def commit(repo, name):
    with open(os.path.join(repo, name), "w") as fh:
        fh.write(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def make_repo(path, base):
    os.makedirs(path)
    git(path, "init", "-q", "-b", base)
    commit(path, "root")


def branch_commit(repo, base, branch, name):
    git(repo, "checkout", "-q", "-b", branch, base)
    sha = commit(repo, name)
    git(repo, "checkout", "-q", base)
    return sha


def merge(repo, base, branch):
    git(repo, "checkout", "-q", base)
    git(repo, "merge", "-q", "--no-ff", "-m", "merge " + branch, branch)


tmp = tempfile.mkdtemp(prefix="dgn1730-")
try:
    inst = os.path.join(tmp, "inst")
    canon = os.path.join(tmp, "canon")
    make_repo(inst, "main")
    make_repo(canon, "dev")

    # instance repo (base main)
    branch_commit(inst, "main", "dsp/a-merged", "a")
    merge(inst, "main", "dsp/a-merged")
    sha_b = branch_commit(inst, "main", "dsp/b-sha-merged", "b")
    merge(inst, "main", "dsp/b-sha-merged")
    git(inst, "branch", "-q", "-D", "dsp/b-sha-merged")        # ref gone
    branch_commit(inst, "main", "dsp/c-unmerged", "c")
    sha_d = branch_commit(inst, "main", "dsp/d-sha-unmerged", "d")
    git(inst, "branch", "-q", "-D", "dsp/d-sha-unmerged")      # ref gone, orphan
    # canonical repo (base dev)
    branch_commit(canon, "dev", "dsp/e-canon-merged", "e")
    merge(canon, "dev", "dsp/e-canon-merged")

    ledger = os.path.join(tmp, "ledger.md")
    rows = [
        ("dsp/a-merged", "dispatch (commits=1, HEAD=deadbee)"),
        ("dsp/b-sha-merged", "dispatch (commits=1, HEAD=%s)" % sha_b[:8]),
        ("dsp/c-unmerged", "dispatch (commits=1, HEAD=deadbee)"),
        ("dsp/d-sha-unmerged", "dispatch (commits=1, HEAD=%s)" % sha_d[:8]),
        ("dsp/f-unresolvable", "dispatch (commits=1, HEAD=0123456789)"),
        ("dsp/g-nosha", "dispatch done"),
        ("dsp/e-canon-merged", "dispatch (commits=1, HEAD=deadbee)"),
    ]
    with open(ledger, "w") as fh:
        fh.write("# ledger\n")
        for b, n in rows:
            fh.write("| %s | item-%s | pending-review | 0 | - | ts | - | %s |\n"
                     % (b, b.split("/")[1], n))
        fh.write("| dsp/h-running | item-h | running | 0 | - | ts | - | x |\n")

    os.environ["DOGANY_LEDGER_FILE"] = ledger
    os.environ["DOGANY_INSTANCE_ROOT"] = inst
    os.environ["DOGANY_REPO_ROOT"] = canon
    os.environ["DOGANY_LEDGER_REVIEW_CACHE"] = os.path.join(tmp, "cache.json")

    print("== classification")
    got = sf._collect_ledger_review()
    joined = "\n".join(got)
    check("merged branch is NOT review", "dsp/a-merged" not in joined)
    check("merged via HEAD sha only (ref gone) is NOT review",
          "dsp/b-sha-merged" not in joined)
    check("unmerged branch IS review", "dsp/c-unmerged" in joined)
    check("unmerged via HEAD sha only (ref gone) IS review",
          "dsp/d-sha-unmerged" in joined)
    check("unresolvable (dead sha) is NOT review",
          "dsp/f-unresolvable" not in joined)
    check("unresolvable (no sha at all) is NOT review",
          "dsp/g-nosha" not in joined)
    check("merged into the canonical repo (dev base) is NOT review",
          "dsp/e-canon-merged" not in joined)
    check("exactly the two unmerged rows remain", len(got) == 2)
    check("item text carried", "item-c-unmerged" in joined)
    check("non pending-review rows never appear", "dsp/h-running" not in joined)

    print("== cache")
    calls = []
    real_run = subprocess.run

    def counting(cmd, *a, **k):
        calls.append(cmd)
        return real_run(cmd, *a, **k)

    sf.subprocess.run = counting
    try:
        again = sf._collect_ledger_review()
        check("cached run gives same answer", again == got)
        check("cached run costs one for-each-ref per repo",
              len(calls) == 2 and all("for-each-ref" in c for c in calls))
        # base moves -> invalidated
        merge(inst, "main", "dsp/c-unmerged")
        calls[:] = []
        after = sf._collect_ledger_review()
        check("base HEAD change invalidates the cache",
              any("rev-list" in c for c in calls))
        check("newly merged branch drops out of review",
              "dsp/c-unmerged" not in "\n".join(after))
        # ledger change -> invalidated
        with open(ledger, "a") as fh:
            fh.write("| dsp/i-new | item-i | pending-review | 0 | - | ts | - | n |\n")
        calls[:] = []
        sf._collect_ledger_review()
        check("ledger change invalidates the cache",
              any("rev-list" in c or "cat-file" in c for c in calls))
    finally:
        sf.subprocess.run = real_run

    print("== git failure degrades to current behaviour")
    os.environ["DOGANY_INSTANCE_ROOT"] = os.path.join(tmp, "not-a-repo")
    os.environ.pop("DOGANY_REPO_ROOT")
    os.environ["DOGANY_LEDGER_REVIEW_CACHE"] = os.path.join(tmp, "cache2.json")
    os.makedirs(os.path.join(tmp, "not-a-repo"))
    with open(ledger, "w") as fh:
        for b, n in rows:
            fh.write("| %s | i | pending-review | 0 | - | ts | - | %s |\n" % (b, n))
    # the script's own workspace root must not accidentally resolve as a repo
    sf._ROOT_DIR = os.path.join(tmp, "not-a-repo")
    got = sf._collect_ledger_review()
    check("every row counts when no repo is readable", len(got) == len(rows))

    def boom(*a, **k):
        raise OSError("git missing")
    os.environ["DOGANY_INSTANCE_ROOT"] = inst
    sf.subprocess.run = boom
    try:
        got = sf._collect_ledger_review()
        check("git exec failure -> every row counts, no crash",
              len(got) == len(rows))
    finally:
        sf.subprocess.run = real_run

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("git", 1)
    sf.subprocess.run = hang
    try:
        check("git timeout -> every row counts, no crash",
              len(sf._collect_ledger_review()) == len(rows))
    finally:
        sf.subprocess.run = real_run

    print("== partial failure: unreadable repo never hides a row")
    os.environ["DOGANY_REPO_ROOT"] = os.path.join(tmp, "not-a-repo")
    got = "\n".join(sf._collect_ledger_review())
    check("readable repo still drops merged rows", "dsp/a-merged" not in got)
    check("row unresolved in the readable repo stays review (other repo unknown)",
          "dsp/f-unresolvable" in got)

    print("== missing ledger")
    os.environ["DOGANY_LEDGER_FILE"] = os.path.join(tmp, "nope.md")
    check("missing ledger -> []", sf._collect_ledger_review() == [])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
