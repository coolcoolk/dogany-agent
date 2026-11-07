#!/usr/bin/env python3
# test-lane: fast
# test-covers: *    # tree scanner, measured 0.85s (DGN-1734): runs on every push
"""DGN-1519 -- declared-retirement pruning must be safe, auditable, undoable.

update.sh syncs without --delete (deliberate), so retired framework files
accumulate on instances forever. retire-prune.py executes DECLARATIONS
(routines/retire/*.retired.tsv, hash-pinned) and never inferences. This
suite locks the safety contract from the DGN-1519 design:

  T1  dry-run is the DEFAULT: prune candidates are listed with exact
      source/destination paths, and NOTHING on disk changes
  T2  --apply moves a hash-matching declared file into
      files/_archive/retired-<ts>/<relpath> and appends an audit line
  T3  a declared file whose bytes diverge from every pinned hash is
      DIVERGED: untouched, loud, exit 1
  T4  undeclared neighbours (instance-local files in the same dir) are
      invisible to the tool -- never listed, never touched
  T5  traversal/absolute/protected declaration paths are REFUSED, untouched
  T6  a symlink at a declared path is REFUSED-SYMLINK, link preserved
  T7  idempotent: an already-absent declared path is ABSENT, exit 0
  T8  multiple pinned hashes: either byte-variant prunes
  T9  --undo restores moved bytes; an occupied target is REFUSED-EXISTS
      (never overwritten)
  T10 audit log lines carry action/path/sha/ticket/declaration/backup
  T11 an unparseable declaration refuses the whole run (exit 2, fail-closed)
  Tdecl  the shipped declaration lints: 5 columns, hex sha256s, safe paths,
      and (canonical checkout only) no declared path exists in the template

All fixtures live under tempfile; the tool copy runs from the fixture's own
routines/ (it derives its root from its file location) -- the live instance
is never touched, same approach as test-dgn1488-handoff-provision.py.

Run: python3 routines/tests/test-dgn1519-retire-prune.py
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)                        # routines/
TOOL = os.path.join(ROUTINES, "retire-prune.py")
SHIPPED_DECL_DIR = os.path.join(ROUTINES, "retire")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(ROUTINES)))
TEMPLATE = os.path.join(REPO_ROOT, "agents", ".template")

PASS = []


def sha(data):
    return hashlib.sha256(data).hexdigest()


def mk_root(tmp, name):
    root = os.path.join(tmp, name)
    os.makedirs(os.path.join(root, "routines", "retire"))
    shutil.copy2(TOOL, os.path.join(root, "routines", "retire-prune.py"))
    return root


def declare(root, lines, fname="fixture.retired.tsv"):
    p = os.path.join(root, "routines", "retire", fname)
    with open(p, "w", encoding="utf-8") as f:
        f.write("# fixture declaration\n")
        for ln in lines:
            f.write(ln + "\n")
    return p


def decl_line(path, hashes, ticket="DGN-1519", date="2026-09-17", reason="r"):
    return "\t".join([path, ",".join(hashes), ticket, date, reason])


def put(root, rel, data):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def run(root, *args):
    return subprocess.run(
        [sys.executable, os.path.join(root, "routines", "retire-prune.py")]
        + list(args),
        capture_output=True, text=True)


def snapshot(root):
    out = {}
    for dirpath, _d, files in os.walk(root):
        for n in files:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                out[os.path.relpath(p, root)] = "LINK:" + os.readlink(p)
            else:
                out[os.path.relpath(p, root)] = sha(open(p, "rb").read())
    return out


def _tmpdir(tmp):
    """Direct-run mode hands a shared parent dir; pytest passes nothing."""
    if tmp is not None:
        return tmp, None
    ctx = tempfile.TemporaryDirectory(prefix="dgn1519-")
    return ctx.name, ctx


def ok(name):
    PASS.append(name)
    print("ok   %s" % name)


def test_T1_T4_dry_run_default_and_undeclared_invisible(tmp=None):
    tmp, _ctx = _tmpdir(tmp)
    root = mk_root(tmp, "t1")
    residue = b"-- retired residue\n"
    put(root, "database/migrations/010_old.sql", residue)
    put(root, "database/migrations/local_helper.py", b"# user file\n")
    declare(root, [decl_line("database/migrations/010_old.sql", [sha(residue)])])
    before = snapshot(root)
    r = run(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PRUNE" in r.stdout and "would move" in r.stdout, r.stdout
    assert os.path.join(root, "database/migrations/010_old.sql") in r.stdout
    assert "retired-" in r.stdout            # destination named exactly
    assert "blind spots" in r.stdout         # blind-spot disclosure present
    assert snapshot(root) == before, "dry-run mutated the filesystem"
    ok("T1 dry-run default: exact list, zero writes")
    assert "local_helper.py" not in r.stdout, r.stdout
    r2 = run(root, "--apply")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert os.path.exists(os.path.join(root, "database/migrations/local_helper.py"))
    assert "local_helper.py" not in r2.stdout
    ok("T4 undeclared neighbour invisible and untouched (dry-run + apply)")


def test_T2_T10_apply_moves_and_audits(tmp=None):
    tmp, _ctx = _tmpdir(tmp)
    root = mk_root(tmp, "t2")
    residue = b"residue-bytes\n"
    put(root, "database/migrations/010_old.sql", residue)
    declare(root, [decl_line("database/migrations/010_old.sql", [sha(residue)])])
    r = run(root, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    src = os.path.join(root, "database/migrations/010_old.sql")
    assert not os.path.exists(src), "apply left the residue in place"
    arch = os.path.join(root, "files", "_archive")
    backups = [d for d in os.listdir(arch) if d.startswith("retired-")]
    assert len(backups) == 1, backups
    moved = os.path.join(arch, backups[0], "database/migrations/010_old.sql")
    assert open(moved, "rb").read() == residue, "backup bytes differ"
    ok("T2 apply: moved to files/_archive/retired-<ts>/<relpath>, bytes intact")
    log = open(os.path.join(arch, "retire-log.tsv"), encoding="utf-8").read()
    cols = log.strip().split("\n")[-1].split("\t")
    assert len(cols) == 7, cols
    assert cols[1] == "pruned" and cols[2] == "database/migrations/010_old.sql"
    assert cols[3] == sha(residue) and cols[4] == "DGN-1519"
    assert cols[5].endswith(".retired.tsv") and "retired-" in cols[6]
    ok("T10 audit line: ts/action/path/sha/ticket/declaration/backup")


def test_T3_diverged(tmp=None):
    tmp, _ctx = _tmpdir(tmp)
    root = mk_root(tmp, "t3")
    put(root, "database/migrations/010_old.sql", b"locally edited bytes\n")
    declare(root, [decl_line("database/migrations/010_old.sql", [sha(b"other\n")])])
    r = run(root, "--apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "DIVERGED" in r.stdout, r.stdout
    assert os.path.exists(os.path.join(root, "database/migrations/010_old.sql"))
    assert not os.path.isdir(os.path.join(root, "files", "_archive")) or \
        not any(d.startswith("retired-") and
                os.listdir(os.path.join(root, "files", "_archive", d))
                for d in os.listdir(os.path.join(root, "files", "_archive")))
    ok("T3 diverged bytes: untouched, loud, exit 1")


def test_T5_refused_paths(tmp=None):
    tmp, _ctx = _tmpdir(tmp)
    root = mk_root(tmp, "t5")
    outside = put(tmp, "t5-outside.txt", b"outside\n")
    userfile = put(root, "files/inbox/keep.txt", b"user\n")
    declare(root, [
        decl_line("../t5-outside.txt", [sha(b"outside\n")]),
        decl_line("/etc/hosts", [sha(b"x")]),
        decl_line("files/inbox/keep.txt", [sha(b"user\n")]),
        decl_line("routines/retire-prune.py", [sha(b"x")]),
    ])
    r = run(root, "--apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.count("REFUSED-PATH") == 2, r.stdout
    assert r.stdout.count("REFUSED-PROTECTED") == 2, r.stdout
    assert os.path.exists(outside) and os.path.exists(userfile)
    assert os.path.exists(os.path.join(root, "routines", "retire-prune.py"))
    ok("T5 traversal/absolute/protected declarations refused, nothing touched")


def test_T6_symlink(tmp=None):
    tmp, _ctx = _tmpdir(tmp)
    root = mk_root(tmp, "t6")
    target = put(root, "database/real.sql", b"real\n")
    link = os.path.join(root, "database", "migrations", "010_old.sql")
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(target, link)
    declare(root, [decl_line("database/migrations/010_old.sql", [sha(b"real\n")])])
    r = run(root, "--apply")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED-SYMLINK" in r.stdout, r.stdout
    assert os.path.islink(link) and os.path.exists(target)
    ok("T6 symlink at declared path refused, link preserved")


def test_T7_absent_idempotent(tmp=None):
    tmp, _ctx = _tmpdir(tmp)
    root = mk_root(tmp, "t7")
    declare(root, [decl_line("database/migrations/gone.sql", [sha(b"x")])])
    for _ in range(2):
        r = run(root, "--apply")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ABSENT" in r.stdout, r.stdout
    ok("T7 absent declared path: ABSENT, exit 0, idempotent")


def test_T8_multi_hash(tmp=None):
    tmp, _ctx = _tmpdir(tmp)
    root = mk_root(tmp, "t8")
    v1, v2 = b"delivered bytes\n", b"post-sweep bytes\n"
    put(root, "a/old1.txt", v1)
    put(root, "a/old2.txt", v2)
    declare(root, [
        decl_line("a/old1.txt", [sha(v1), sha(v2)]),
        decl_line("a/old2.txt", [sha(v1), sha(v2)]),
    ])
    r = run(root, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not os.path.exists(os.path.join(root, "a/old1.txt"))
    assert not os.path.exists(os.path.join(root, "a/old2.txt"))
    ok("T8 multi-hash pin: either byte-variant prunes")


def test_T9_undo(tmp=None):
    tmp, _ctx = _tmpdir(tmp)
    root = mk_root(tmp, "t9")
    residue = b"undo-me\n"
    put(root, "database/migrations/010_old.sql", residue)
    declare(root, [decl_line("database/migrations/010_old.sql", [sha(residue)])])
    r = run(root, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    arch = os.path.join(root, "files", "_archive")
    backup = [d for d in os.listdir(arch) if d.startswith("retired-")][0]
    backup_rel = os.path.join("files", "_archive", backup)
    r2 = run(root, "--undo", backup_rel)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    restored = os.path.join(root, "database/migrations/010_old.sql")
    assert open(restored, "rb").read() == residue, "undo bytes differ"
    log = open(os.path.join(arch, "retire-log.tsv"), encoding="utf-8").read()
    assert "restored" in log
    ok("T9a undo restores the exact bytes and audits it")
    # occupied target: re-apply, then plant a new file at the original path
    r3 = run(root, "--apply")
    assert r3.returncode == 0
    backups = sorted(d for d in os.listdir(arch) if d.startswith("retired-"))
    newest = os.path.join("files", "_archive", backups[-1])
    put(root, "database/migrations/010_old.sql", b"newer occupant\n")
    r4 = run(root, "--undo", newest)
    assert r4.returncode == 1, r4.stdout + r4.stderr
    assert "REFUSED-EXISTS" in r4.stdout, r4.stdout
    assert open(restored, "rb").read() == b"newer occupant\n", "undo overwrote"
    ok("T9b undo never overwrites an occupied target")
    # undo target outside files/_archive is refused
    r5 = run(root, "--undo", "database")
    assert r5.returncode == 2, r5.stdout + r5.stderr
    ok("T9c undo target constrained to files/_archive/retired-*")


def test_T11_unparseable_declaration_fail_closed(tmp=None):
    tmp, _ctx = _tmpdir(tmp)
    root = mk_root(tmp, "t11")
    residue = b"residue\n"
    put(root, "a/old.txt", residue)
    declare(root, [
        decl_line("a/old.txt", [sha(residue)]),
        "malformed line without tabs",
    ])
    r = run(root, "--apply")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "DECLARATION-ERROR" in r.stdout, r.stdout
    assert os.path.exists(os.path.join(root, "a/old.txt")), \
        "fail-closed violated: pruned despite unparseable declaration"
    ok("T11 unparseable declaration refuses the whole run (exit 2)")


def test_Tdecl_shipped_declaration_lints():
    assert os.path.isdir(SHIPPED_DECL_DIR), "shipped retire/ dir missing"
    decls = [f for f in os.listdir(SHIPPED_DECL_DIR)
             if f.endswith(".retired.tsv")]
    assert decls, "no shipped .retired.tsv"
    seen = 0
    for name in decls:
        for ln in open(os.path.join(SHIPPED_DECL_DIR, name), encoding="utf-8"):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            cols = ln.rstrip("\n").split("\t")
            assert len(cols) == 5, (name, cols)
            path, hashes, ticket, date, reason = cols
            assert not path.startswith(("/", "~")) and ".." not in path.split("/")
            for h in hashes.split(","):
                assert len(h) == 64 and all(c in "0123456789abcdef" for c in h), \
                    (name, path, h)
            assert ticket.startswith("DGN-") and date and reason
            seen += 1
            if os.path.isdir(TEMPLATE):   # canonical checkout only
                assert not os.path.exists(os.path.join(TEMPLATE, path)), \
                    "declared path exists in the template: %s" % path
    assert seen >= 9, "expected the 9 kit_life migration residue lines"
    ok("Tdecl shipped declaration: format, hex hashes, safe paths, "
       "no declared path in template (%d lines)" % seen)


def main():
    with tempfile.TemporaryDirectory(prefix="dgn1519-") as tmp:
        test_T1_T4_dry_run_default_and_undeclared_invisible(tmp)
        test_T2_T10_apply_moves_and_audits(tmp)
        test_T3_diverged(tmp)
        test_T5_refused_paths(tmp)
        test_T6_symlink(tmp)
        test_T7_absent_idempotent(tmp)
        test_T8_multi_hash(tmp)
        test_T9_undo(tmp)
        test_T11_unparseable_declaration_fail_closed(tmp)
    test_Tdecl_shipped_declaration_lints()
    print("\n%d checks passed" % len(PASS))


if __name__ == "__main__":
    main()
