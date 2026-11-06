#!/usr/bin/env python3
"""Audit the frozen FDGN-594 replay's native evidence, without re-labeling.

Run against private snapshots, never a live global newest-turn fallback.
This is an independent provenance/count audit, NOT semantic E2E acceptance
of installed intake, output carriers or tool/dispatch executor wiring.
No owner words or raw transcripts are copied into the report.
"""

import argparse
import collections
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import why_owner_turn as native


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def audit(manifest, snapshot):
    ledger_path = snapshot / "turn-ledger.jsonl"
    ledger = [json.loads(s) for s in ledger_path.read_text().splitlines() if s.strip()]
    by_id = {r["id"]: r for r in ledger}
    sessions, fingerprints = {}, {}
    for name, expected in manifest["source_hashes"]["transcripts"].items():
        path = snapshot / name
        turns, every, chain, entries = native.chain_turns(str(path))
        sid = path.stem
        bound, unbound = native.bind_ledger([r for r in ledger if r.get("session_id") == sid], turns)
        sessions[name] = dict(bound=bound, chain=chain, entries={e.get("uuid"): e for e in entries},
                              lines=path.read_text().splitlines())
        actual = sha(path.read_bytes())
        fingerprints[name] = dict(actual=actual, frozen=expected, unchanged=actual == expected)
    results = []
    for row in manifest["rows"]:
        if "input" not in row:
            # The frozen manifest intentionally supplies no invented UUID
            # for the two SHA-unbound ledger rows.
            exact_bound = any(r.get("id") == row["id"] for session in sessions.values()
                              for r in session["bound"].values())
            results.append(dict(id=row["id"], expected=row["new"],
                                checks=dict(exact_bound=exact_bound),
                                evidence_matches=(row["new"] == "hold-provenance"
                                                  and row["id"] in by_id and not exact_bound)))
            continue
        source = row["input"]
        session = sessions[source["file"]]
        entry = session["entries"].get(source["uuid"])
        bound = session["bound"].get(source["uuid"])
        ledger_row = by_id.get(row["id"], {})
        line = source.get("line", 0)
        at_line = None
        if line and line <= len(session["lines"]):
            at_line = json.loads(session["lines"][line - 1]).get("uuid")
        words = native.owner_words(native.entry_text(entry)) if entry else None
        words_hash = sha(words.encode()) if words is not None else None
        checks = dict(uuid_present=entry is not None, line_matches=at_line == source["uuid"],
                      ledger_hash=ledger_row.get("sha") == source["ledger_sha"],
                      owner_words_hash=words_hash == source["owner_words_sha256"],
                      on_chain=source["uuid"] in session["chain"],
                      exact_bound=bool(bound and bound.get("id") == row["id"]))
        proposal = row.get("proposal")
        if proposal:
            previous = sessions[proposal["file"]]
            prev = previous["entries"].get(proposal["uuid"])
            checks["proposal_on_chain"] = bool(prev and proposal["uuid"] in previous["chain"])
            checks["proposal_line"] = json.loads(previous["lines"][proposal["line"] - 1]).get("uuid") == proposal["uuid"]
        expected_quarantine = row["new"] == "hold-provenance"
        # Two explicitly unbound inputs remain quarantines even when a
        # nearby owner turn would be convenient. Never borrow that turn.
        match = (not checks["exact_bound"] if expected_quarantine else all(checks.values()))
        results.append(dict(id=row["id"], expected=row["new"], checks=checks, evidence_matches=match))
    prior = []
    for proof in manifest.get("extra_proofs", ()):
        session = sessions[proof["file"]]
        row = session["bound"].get(proof["uuid"])
        entry = session["entries"].get(proof["uuid"])
        words = native.owner_words(native.entry_text(entry)) if entry else None
        previous_hashes = []
        for previous in proof.get("previous", ()):
            original = session["entries"].get(previous["uuid"])
            text = "\n".join(native.assistant_texts(original)) if original else ""
            previous_hashes.append(sha(text.encode()) == previous["text_sha256"])
        prior.append(dict(alias=proof["alias"], exact_bound=bool(row and row["id"] == proof["ledger_id"]),
                          owner_words_hash=bool(words is not None and sha(words.encode()) == proof["owner_text_sha256"]),
                          proposal_text_hashes=all(previous_hashes),
                          proposals_on_chain=all(p["uuid"] in session["chain"] for p in proof.get("previous", ()))))
    counts = dict(collections.Counter(r["new"] for r in manifest["rows"]))
    prompts = sum(r["prompts"] for r in manifest["rows"])
    return dict(kind="provenance-audit-only", semantic_e2e="NOT RUN: executor/carrier adapters not installed",
                selected=len(results), bound=sum(r["checks"]["exact_bound"] for r in results),
                evidence_matches=sum(r["evidence_matches"] for r in results), expected_counts=counts,
                prompts=prompts, counts_match=counts == manifest["owner_cost"]["new"],
                ledger_fingerprint=dict(actual=sha(ledger_path.read_bytes()), frozen=manifest["source_hashes"]["ledger"]),
                fingerprints=fingerprints, prior_proofs=prior, rows=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = audit(json.loads(args.manifest.read_text()), args.snapshot_dir)
    args.report.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print("provenance-only: selected={selected} bound={bound} evidence_matches={evidence_matches} prompts={prompts}".format(**result))
    print("semantic/runtime replay: NOT RUN")
    good = (result["selected"] == 112 and result["bound"] == 110 and result["evidence_matches"] == 112
            and result["counts_match"] and result["prompts"] == 3
            and all(p["exact_bound"] and p["proposals_on_chain"] and p["owner_words_hash"]
                    and p["proposal_text_hashes"] for p in result["prior_proofs"]))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
