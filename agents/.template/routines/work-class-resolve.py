#!/usr/bin/env python3
"""DGN-2294 step 1: resolve a work class to its route WITHOUT launching.

Prints the dogany.work-class-receipt/1 JSON (ordered candidates plus the
source file and sha256 of every resolved field). For shadow comparison
against the legacy dispatcher; nothing is reserved, queued or started.

  work-class-resolve.py --class review --difficulty standard
  work-class-resolve.py --skill .claude/skills/<s>/SKILL.md --difficulty hard
  work-class-resolve.py --job job.json [--kit devkit ...]

Exit 0 = resolved, 2 = rejected (reason on stderr as '<code>: <message>').
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

from work_class_resolver import load_tree, resolve  # noqa: E402
from work_class_schema import JOB_SCHEMA, SchemaError  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--class", dest="work_class")
    ap.add_argument("--difficulty")
    ap.add_argument("--skill", help="active SKILL.md (delegate.work_class)")
    ap.add_argument("--job", help="dogany.job/1 JSON file; flags fill only absent fields")
    ap.add_argument("--kit", action="append", dest="kits",
                    help="restrict the kit override chain (repeatable); default all installed")
    ap.add_argument("--root", default=str(HERE.parent),
                    help="instance root (default: parent of routines/)")
    ap.add_argument("--framework-dir", help="default <root>/routines/work-classes")
    ap.add_argument("--packs-dir", help="default <root>/config/packs")
    a = ap.parse_args(argv)

    root = Path(a.root)
    fdir = Path(a.framework_dir) if a.framework_dir else HERE / "work-classes"
    if not a.framework_dir and not fdir.is_dir():
        fdir = root / "routines" / "work-classes"
    packs = Path(a.packs_dir) if a.packs_dir else root / "config" / "packs"
    try:
        if a.job:
            job = json.loads(Path(a.job).read_text(encoding="utf-8"))
        else:
            job = {"schema": JOB_SCHEMA, "idempotency_key": "shadow-resolve",
                   "goal": "shadow resolve", "immutable_inputs": [],
                   "authorization": {"tools": []}, "budgets": {},
                   "adapter": "none", "checker": {"name": "none", "version": "0"},
                   "return_sink": "stdout"}
        if a.work_class and "class" not in job:
            job["class"] = a.work_class
        if a.difficulty and "difficulty" not in job:
            job["difficulty"] = a.difficulty
        tree = load_tree(fdir, packs, kits=a.kits, skill_path=a.skill)
        receipt = resolve(job, tree)
    except SchemaError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"input: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
