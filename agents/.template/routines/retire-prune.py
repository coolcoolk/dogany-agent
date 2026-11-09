#!/usr/bin/env python3
"""retire-prune.py -- declared-retirement pruning for framework residue (DGN-1519).

update.sh syncs framework areas WITHOUT --delete (deliberate: user files must
never be clobbered), so a file the framework RETIRES is never removed from a
live instance. This tool is the retirement step: it executes DECLARATIONS,
never inferences.

Predicate (design: DGN-1519, derived from measurement):

    PRUNE(f) <=> f is listed in a retirement declaration
             AND f exists, is a regular file, is not a symlink
             AND sha256(f) is one of the declaration's pinned hashes
             AND f is not in a PROTECTED area

Everything else is untouched. In particular this tool NEVER deletes:
  - anything not declared (instance-local files are invisible to it),
  - a declared path whose bytes differ from every pinned hash (DIVERGED --
    a local divergence is a human's call, fail-closed),
  - symlinks, directories, absolute/traversal paths,
  - PROTECTED areas (files/, memories/, identity/, config/, .git/, baseline
    docs, this tool and its declarations),
  - and it never rm's at all: pruning MOVES the file into
    files/_archive/retired-<UTC ts>/<relpath> (undo = --undo, or one mv).

Declarations (TSV, '#' comments, 5 tab-separated columns):
    path <TAB> sha256[,sha256...] <TAB> ticket <TAB> declared-date <TAB> reason
read from  routines/retire/*.retired.tsv   (framework lane, update-synced)
and        config/retired/*.retired.tsv    (pack/local lane, usually absent).

The declarer pins the hashes by MEASUREMENT (delivery-time bytes plus any
known framework-sweep variants). Declaring requires the checklist in the
declaration header: replacement shipped first, callers searched beyond ast
(shell/plist/cron), reversible smoke planned, hashes measured.

Usage:
    python3 routines/retire-prune.py            # dry-run (default, writes nothing)
    python3 routines/retire-prune.py --apply    # execute PRUNE verdicts
    python3 routines/retire-prune.py --undo files/_archive/retired-<ts>

Exit codes: 0 = clean (acted or nothing to do); 1 = at least one DIVERGED /
REFUSED / failed move (loud, needs a human); 2 = unusable declaration file.

Blind spot, stated on every run: this tool proves byte-identity to a pinned
hash and NOTHING else. It does not prove the file is unused on this instance.
"""

import argparse
import datetime
import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # <root>/routines
ROOT = os.path.dirname(HERE)                               # instance root

DECL_DIRS = (
    os.path.join(ROOT, "routines", "retire"),
    os.path.join(ROOT, "config", "retired"),
)
DECL_SUFFIX = ".retired.tsv"

ARCHIVE_BASE = os.path.join(ROOT, "files", "_archive")
AUDIT_LOG = os.path.join(ARCHIVE_BASE, "retire-log.tsv")

# PROTECTED: refused even if declared. Prefixes are path-segment anchored.
PROTECTED_PREFIXES = (
    "files/", "memories/", "identity/", "config/", ".git/",
    "routines/retire/",
)
PROTECTED_EXACT = frozenset({
    "routines/retire-prune.py",
    "AGENT.md", "AGENTS.md", "CLAUDE.md", "PROFILE.md", "RULES.md", "USER.md",
    "rules/hot.framework.md", "rules/hot.custom.md",
})

BLIND_SPOT_NOTE = (
    "blind spots: this tool proves byte-identity to a declared hash, nothing\n"
    "else. It does NOT prove the file is unused on this instance. If anything\n"
    "here still invokes a declared path (shell, plist, cron -- not just python\n"
    "imports), pruning breaks it. A declaration is only legitimate after the\n"
    "DGN-1519 checklist: replacement delivered, callers searched, reversible\n"
    "smoke planned, hashes pinned by measurement."
)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def path_unsafe(rel):
    """A declared path must be a plain relative path inside the root."""
    if not rel or rel != rel.strip():
        return True
    if rel.startswith("/") or rel.startswith("~") or "\\" in rel:
        return True
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return True
    return False


def path_protected(rel):
    if rel in PROTECTED_EXACT:
        return True
    return any(rel.startswith(p) for p in PROTECTED_PREFIXES)


class Entry(object):
    __slots__ = ("path", "hashes", "ticket", "date", "reason", "source")

    def __init__(self, path, hashes, ticket, date, reason, source):
        self.path = path
        self.hashes = hashes
        self.ticket = ticket
        self.date = date
        self.reason = reason
        self.source = source


def load_declarations():
    """Returns (entries, errors). A malformed line is a hard error (exit 2):
    a deletion declaration that cannot be parsed exactly must not be half-run."""
    entries, errors = [], []
    for d in DECL_DIRS:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(DECL_SUFFIX):
                continue
            fpath = os.path.join(d, name)
            rel_src = os.path.relpath(fpath, ROOT)
            try:
                lines = open(fpath, "r", encoding="utf-8").read().splitlines()
            except OSError as e:
                errors.append("%s: unreadable (%s)" % (rel_src, e))
                continue
            for ln, line in enumerate(lines, 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) != 5:
                    errors.append("%s:%d: expected 5 tab-separated columns, got %d"
                                  % (rel_src, ln, len(cols)))
                    continue
                path, hashes_raw, ticket, date, reason = [c.strip() for c in cols]
                hashes = [h.strip().lower() for h in hashes_raw.split(",") if h.strip()]
                bad = [h for h in hashes
                       if len(h) != 64 or any(c not in "0123456789abcdef" for c in h)]
                if not hashes or bad:
                    errors.append("%s:%d: bad sha256 list" % (rel_src, ln))
                    continue
                if not ticket or not date:
                    errors.append("%s:%d: empty ticket/date column" % (rel_src, ln))
                    continue
                entries.append(Entry(path, frozenset(hashes), ticket, date,
                                     reason, rel_src))
    return entries, errors


def judge(entry):
    """-> (verdict, detail). Verdicts: PRUNE ABSENT DIVERGED
    REFUSED-PATH REFUSED-PROTECTED REFUSED-SYMLINK REFUSED-DIRECTORY"""
    if path_unsafe(entry.path):
        return "REFUSED-PATH", "not a plain in-root relative path"
    if path_protected(entry.path):
        return "REFUSED-PROTECTED", "protected area"
    abspath = os.path.join(ROOT, entry.path)
    if os.path.islink(abspath):
        return "REFUSED-SYMLINK", "declared path is a symlink"
    if not os.path.exists(abspath):
        return "ABSENT", "already gone"
    if os.path.isdir(abspath):
        return "REFUSED-DIRECTORY", "declared path is a directory"
    actual = sha256_file(abspath)
    if actual in entry.hashes:
        return "PRUNE", actual
    return "DIVERGED", "sha256=%s matches no pinned hash" % actual


def audit(action, rel, sha, ticket, source, backup_dir):
    os.makedirs(ARCHIVE_BASE, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write("\t".join([ts, action, rel, sha, ticket, source,
                           os.path.relpath(backup_dir, ROOT)]) + "\n")


def run_prune(apply_mode):
    entries, errors = load_declarations()
    if errors:
        for e in errors:
            print("DECLARATION-ERROR  %s" % e)
        print("refusing to run with an unparseable declaration (fail-closed).")
        return 2
    if not entries:
        print("no retirement declarations found -- nothing to do.")
        print("")
        print(BLIND_SPOT_NOTE)
        return 0

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    backup_dir = os.path.join(ARCHIVE_BASE, "retired-%s" % ts)
    mode = "APPLY" if apply_mode else "DRY-RUN (default -- nothing is written)"
    print("retire-prune: root=%s mode=%s" % (ROOT, mode))
    print("")

    loud = 0
    pruned = 0
    for e in entries:
        verdict, detail = judge(e)
        line = "%-18s %s  [%s %s]" % (verdict, e.path, e.ticket, e.source)
        print(line)
        if verdict == "PRUNE":
            src = os.path.join(ROOT, e.path)
            dst = os.path.join(backup_dir, e.path)
            if apply_mode:
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(src, dst)
                    audit("pruned", e.path, detail, e.ticket, e.source, backup_dir)
                    pruned += 1
                    print("    moved -> %s" % dst)
                except OSError as err:
                    loud += 1
                    print("    MOVE-FAILED (%s) -- file left in place" % err)
            else:
                print("    would move %s -> %s" % (src, dst))
        elif verdict == "ABSENT":
            pass
        else:
            loud += 1
            print("    %s" % detail)

    print("")
    if apply_mode and pruned:
        print("pruned %d file(s); backup: %s" % (pruned, backup_dir))
        print("undo: python3 routines/retire-prune.py --undo %s"
              % os.path.relpath(backup_dir, ROOT))
        print("audit log: %s" % os.path.relpath(AUDIT_LOG, ROOT))
    if loud:
        print("%d item(s) need a human (DIVERGED/REFUSED/failed) -- nothing "
              "was done to them." % loud)
    print("")
    print(BLIND_SPOT_NOTE)
    return 1 if loud else 0


def run_undo(backup_arg):
    backup_dir = backup_arg if os.path.isabs(backup_arg) \
        else os.path.join(ROOT, backup_arg)
    backup_dir = os.path.normpath(backup_dir)
    base = os.path.normpath(ARCHIVE_BASE)
    if not (backup_dir.startswith(base + os.sep)
            and os.path.basename(backup_dir).startswith("retired-")):
        print("--undo target must be a files/_archive/retired-<ts> directory")
        return 2
    if not os.path.isdir(backup_dir):
        print("no such backup directory: %s" % backup_dir)
        return 2

    loud = 0
    restored = 0
    for dirpath, _dirnames, filenames in os.walk(backup_dir):
        for name in filenames:
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, backup_dir)
            dst = os.path.join(ROOT, rel)
            if os.path.exists(dst) or os.path.islink(dst):
                loud += 1
                print("REFUSED-EXISTS     %s (target occupied -- not overwriting)"
                      % rel)
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                audit("restored", rel, sha256_file(dst), "-", "-", backup_dir)
                restored += 1
                print("RESTORED           %s" % rel)
            except OSError as err:
                loud += 1
                print("RESTORE-FAILED     %s (%s)" % (rel, err))
    print("")
    print("restored %d file(s) from %s" % (restored, backup_dir))
    return 1 if loud else 0


def main(argv):
    ap = argparse.ArgumentParser(
        description="declared-retirement pruning (DGN-1519); default is dry-run")
    ap.add_argument("--apply", action="store_true",
                    help="execute PRUNE verdicts (default: dry-run report only)")
    ap.add_argument("--undo", metavar="BACKUP_DIR",
                    help="restore a files/_archive/retired-<ts> backup")
    args = ap.parse_args(argv)
    if args.undo:
        if args.apply:
            ap.error("--undo and --apply are mutually exclusive")
        return run_undo(args.undo)
    return run_prune(args.apply)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
