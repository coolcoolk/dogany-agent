#!/usr/bin/env python3
"""plan-apply.py -- the applier of the DGN-1458 plan lane.

A live workspace (an instance directory, its database, a crew directory) is
not a checkout, so no --worktree reaches it, and the observe sandbox denies it
by design. The plan lane splits the work where the two rules already agree:

  planner  = a --read-only dispatch (kernel read-only, unchanged) that may
             write exactly one place, RUNDIR/plan/, and writes plan.json there;
  applier  = THIS script, run by the dispatcher outside the kernel
             (dispatch-detached.sh --apply-plan <runid> [--apply]). It is
             mechanical: it knows five fixed checks and three op kinds, no
             model runs inside it.

Plan format (RUNDIR/plan/plan.json):
  {"version": 1, "summary": "<one line>", "ops": [
     {"op": "write",  "path": "/abs/file", "content": "<full new text>",
      "expect_sha256": "<sha256 of the current file> | absent"},
     {"op": "delete", "path": "/abs/file", "expect_sha256": "<sha256>"},
     {"op": "sqlite", "db": "/abs/file.db", "sql": "<statements>",
      "expect_changes": <int>}
  ]}

Checks, all before anything is written (all-or-nothing preflight):
  1. every path is absolute, normalized, not a symlink, and inside a declared
     --target (the --live-target list the run was spawned with);
  2. no path appears in two ops;
  3. every precondition still holds NOW (file sha / absence, db exists) --
     the live state may have moved since the planner read it;
  4. --apply only: the run's status.json says result=completed, and no
     receipt exists yet (a plan applies once);
  5. sqlite ops run in one transaction each and must change exactly
     expect_changes rows (row-count guard) or they roll back.
Every touched file and database is backed up to RUNDIR/plan-backup/ first; a
failure mid-apply restores every op already applied. The outcome is written
to RUNDIR/plan-receipt.json.

Modes: default = dry run (prints the ops, writes nothing); --check = one-line
verdict for status.json ("absent" | "ready: N ops" | "invalid: <why>");
--apply = apply. Exit: 0 ok, 2 invalid plan, 3 precondition/state refusal,
4 failed and rolled back.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

OPS = ("write", "delete", "sqlite")


class Refusal(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(path, roots):
    real = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    for root in roots:
        r = os.path.realpath(root)
        if real == r or real.startswith(r.rstrip("/") + "/"):
            return True
    return False


def op_path(op):
    return op.get("db") if op.get("op") == "sqlite" else op.get("path")


def load_plan(rundir):
    path = os.path.join(rundir, "plan", "plan.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        raise Refusal(2, "plan.json unreadable: %s" % exc)
    return plan


def validate(plan, targets):
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise Refusal(2, "plan.json must be an object with version 1")
    ops = plan.get("ops")
    if not isinstance(ops, list) or not ops:
        raise Refusal(2, "plan.json has no ops")
    seen = set()
    for i, op in enumerate(ops):
        where = "op %d" % i
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise Refusal(2, "%s: op must be one of %s" % (where, ", ".join(OPS)))
        p = op_path(op)
        if not isinstance(p, str) or not p.startswith("/"):
            raise Refusal(2, "%s: path must be absolute" % where)
        if os.path.normpath(p) != p:
            raise Refusal(2, "%s: path is not normalized: %s" % (where, p))
        if os.path.islink(p):
            raise Refusal(2, "%s: path is a symlink: %s" % (where, p))
        if not _inside(p, targets):
            raise Refusal(2, "%s: %s is outside every declared --live-target" % (where, p))
        if p in seen:
            raise Refusal(2, "%s: %s appears in two ops" % (where, p))
        seen.add(p)
        kind = op["op"]
        if kind == "write":
            if not isinstance(op.get("content"), str):
                raise Refusal(2, "%s: write needs string content" % where)
            if not isinstance(op.get("expect_sha256"), str):
                raise Refusal(2, "%s: write needs expect_sha256 (hex or 'absent')" % where)
        elif kind == "delete":
            if not isinstance(op.get("expect_sha256"), str) or op["expect_sha256"] == "absent":
                raise Refusal(2, "%s: delete needs the current expect_sha256" % where)
        else:
            if not isinstance(op.get("sql"), str) or not op["sql"].strip():
                raise Refusal(2, "%s: sqlite needs sql" % where)
            n = op.get("expect_changes")
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                raise Refusal(2, "%s: sqlite needs expect_changes >= 0" % where)
    return ops


def preconditions(ops):
    for i, op in enumerate(ops):
        where = "op %d" % i
        p = op_path(op)
        if op["op"] == "sqlite":
            if not os.path.isfile(p):
                raise Refusal(3, "%s: database missing: %s" % (where, p))
            continue
        want = op["expect_sha256"]
        if want == "absent":
            if os.path.lexists(p):
                raise Refusal(3, "%s: %s exists but the plan expected it absent" % (where, p))
            if not os.path.isdir(os.path.dirname(p)):
                raise Refusal(3, "%s: parent directory missing: %s" % (where, os.path.dirname(p)))
        else:
            if not os.path.isfile(p):
                raise Refusal(3, "%s: %s is missing (plan expected sha %s)" % (where, p, want[:12]))
            got = sha256_file(p)
            if got != want:
                raise Refusal(3, "%s: %s changed since planning (sha %s, plan expected %s)"
                              % (where, p, got[:12], want[:12]))


def _statements(sql):
    out, buf = [], ""
    for line in sql.splitlines(True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


def _backup(op, i, bdir):
    p = op_path(op)
    dst = os.path.join(bdir, "%02d-%s" % (i, os.path.basename(p)))
    if op["op"] == "sqlite":
        src = sqlite3.connect(p)
        out = sqlite3.connect(dst)
        with out:
            src.backup(out)
        src.close()
        out.close()
        return dst
    if os.path.isfile(p):
        shutil.copy2(p, dst)
        return dst
    return None


def _restore(op, backup):
    p = op_path(op)
    if op["op"] == "sqlite":
        src = sqlite3.connect(backup)
        out = sqlite3.connect(p)
        with out:
            src.backup(out)
        src.close()
        out.close()
    elif backup is None:
        if os.path.lexists(p):
            os.unlink(p)
    else:
        tmp = p + ".plan-restore"
        shutil.copy2(backup, tmp)
        os.replace(tmp, p)


def _apply_one(op):
    p = op_path(op)
    if op["op"] == "write":
        tmp = os.path.join(os.path.dirname(p), ".%s.plan-apply" % os.path.basename(p))
        mode = os.stat(p).st_mode & 0o7777 if os.path.isfile(p) else None
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(op["content"])
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, p)
        return {"sha256_after": sha256_file(p)}
    if op["op"] == "delete":
        os.unlink(p)
        return {"deleted": True}
    conn = sqlite3.connect(p, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = conn.total_changes
        for stmt in _statements(op["sql"]):
            conn.execute(stmt)
        changes = conn.total_changes - before
        if changes != op["expect_changes"]:
            conn.execute("ROLLBACK")
            raise Refusal(4, "sqlite %s: %d row(s) changed, plan expected %d -- rolled back"
                          % (p, changes, op["expect_changes"]))
        conn.execute("COMMIT")
        return {"changes": changes}
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise Refusal(4, "sqlite %s: %s -- rolled back" % (p, exc))
    finally:
        conn.close()


def write_receipt(rundir, doc):
    path = os.path.join(rundir, "plan-receipt.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def apply(rundir, ops):
    status = os.path.join(rundir, "status.json")
    try:
        with open(status, encoding="utf-8") as fh:
            result = json.load(fh).get("result")
    except (OSError, ValueError):
        result = None
    if result != "completed":
        raise Refusal(3, "run did not complete (status.json result=%s) -- a plan from an unfinished run is not applied" % result)
    if os.path.exists(os.path.join(rundir, "plan-receipt.json")):
        raise Refusal(3, "plan-receipt.json exists: this plan was already applied (a plan applies once)")
    preconditions(ops)
    bdir = os.path.join(rundir, "plan-backup")
    os.makedirs(bdir, exist_ok=True)
    backups = [_backup(op, i, bdir) for i, op in enumerate(ops)]
    done, results = [], []
    try:
        for i, op in enumerate(ops):
            results.append(dict({"op": op["op"], "path": op_path(op)}, **_apply_one(op)))
            done.append(i)
    except (Refusal, OSError) as exc:
        failed_at = len(done)
        # A failed sqlite op rolled itself back; restore every op before it.
        for i in reversed(done):
            _restore(ops[i], backups[i])
        write_receipt(rundir, {"state": "failed-rolled-back", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                               "failed_op": failed_at, "error": str(exc), "backup_dir": bdir})
        raise Refusal(4, "op %d failed: %s -- %d applied op(s) restored from %s"
                      % (failed_at, exc, len(done), bdir))
    write_receipt(rundir, {"state": "applied", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           "ops": results, "backup_dir": bdir})
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply a DGN-1458 plan-lane plan.")
    ap.add_argument("--rundir", required=True)
    ap.add_argument("--target", action="append", default=[], required=True,
                    help="a declared --live-target (repeat)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    try:
        plan = load_plan(args.rundir)
        if plan is None:
            if args.check:
                print("absent")
                return 0
            raise Refusal(2, "no plan: %s/plan/plan.json does not exist" % args.rundir)
        ops = validate(plan, args.target)
        if args.check:
            print("ready: %d op(s)" % len(ops))
            return 0
        if not args.apply:
            print("[plan-apply] DRY RUN (nothing written) -- %s" % (plan.get("summary") or "(no summary)"))
            for i, op in enumerate(ops):
                extra = ("expect_changes=%d" % op["expect_changes"]) if op["op"] == "sqlite" \
                    else ("expect_sha256=%s" % op["expect_sha256"][:12])
                print("[plan-apply]   %d. %s %s (%s)" % (i, op["op"], op_path(op), extra))
            preconditions(ops)
            print("[plan-apply] preconditions hold now; re-run with --apply to apply")
            return 0
        results = apply(args.rundir, ops)
        print("[plan-apply] APPLIED %d op(s); backups + receipt in %s" % (len(results), args.rundir))
        return 0
    except Refusal as exc:
        if args.check:
            print("invalid: %s" % exc)
            return 0
        print("[plan-apply] REFUSED: %s" % exc, file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
