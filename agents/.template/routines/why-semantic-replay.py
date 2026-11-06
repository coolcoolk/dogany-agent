#!/usr/bin/env python3
"""Run the frozen FDGN-594 core semantic replay (never live operations)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from why_net.semantic_replay import run, validate_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    load = lambda name: json.loads((args.fixtures / name).read_text())
    inputs, corpus, manifest = (load(n) for n in (
        "semantic-inputs.json", "source-regions.json", "replay-v4.json"))
    if args.snapshot_dir:
        validate_snapshot(corpus, args.snapshot_dir)
    with tempfile.TemporaryDirectory(prefix="fdgn594-replay-", dir=os.environ.get("DOGANY_RUN_SCRATCH")) as tmp:
        result = run(inputs, corpus, manifest, Path(tmp))
    result["native_snapshot_rechecked"] = args.snapshot_dir is not None
    result["fixture_sha256"] = {name: hashlib.sha256((args.fixtures / name).read_bytes()).hexdigest()
        for name in ("replay-v4.json", "semantic-inputs.json", "source-regions.json")}
    if args.snapshot_dir:
        frozen_hashes = dict(manifest["source_hashes"]["transcripts"],
                             **{"turn-ledger.jsonl": manifest["source_hashes"]["ledger"]})
        result["snapshot_sha256"] = {name: dict(frozen=frozen,
            actual=hashlib.sha256((args.snapshot_dir / name).read_bytes()).hexdigest())
            for name, frozen in frozen_hashes.items()}
    args.report.write_text(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    print("semantic replay: {matches}/{selected} match; prompts={prompts}; authority holds={authority_holds}; provenance holds={provenance_holds}".format(**result))
    for row in result["rows"]:
        if not row["match"]:
            print("MISMATCH", row["id"], row["expected"], json.dumps(row["actual"], ensure_ascii=True))
    return 0 if (result["matches"], result["selected"], result["prompts"],
                 result["authority_holds"], result["provenance_holds"]) == (112, 112, 3, 16, 2) else 1


if __name__ == "__main__":
    sys.exit(main())
