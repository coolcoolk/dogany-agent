# test-lane: fast
# test-tier: 0
# test-component: memory-engine FDGN-620 archive and pending handoff
# test-prereq: tmp-files, pytest
# test-new-component: The archive transaction and pending-handoff stores are new components.
# test-covers: agents/.template/memory-engine/archive_engine.py agents/.template/memory-engine/consolidation_router.py
import hashlib
import os
import sys

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

from archive_engine import (ArchiveError, MemoryArchiveEngine,
                            ownership_equivalent, rolling_remove)
from consolidation_router import (acknowledge_handoff,
                                  archive_expired_handoffs,
                                  persist_pending_handoff,
                                  read_pending_handoffs,
                                  restore_expired_handoff)


def proof():
    return {"destination_ref": "kit:record:1", "destination_version": "v1",
            "binding_version": "contract-v1", "normalized_claim_hash": "c" * 64,
            "receipt": "receipt:1"}


def engine(tmp_path):
    memories = tmp_path / "memories"
    memories.mkdir()
    return MemoryArchiveEngine(str(memories), str(tmp_path / "data"), "fixture")


def test_skip_handoff_recurrence_ack(tmp_path):
    row = persist_pending_handoff(
        str(tmp_path), "fixture", "itm-1", "kit_resource_constraint", b"exact\ntext",
        "event:1", "v1", ["kit:life"], "kit:life/money", "2026-01-01T00:00:00Z",
        "2026-02-01T00:00:00Z")
    assert read_pending_handoffs(str(tmp_path), "kit:life/money") == [row]
    with pytest.raises(ValueError):
        acknowledge_handoff(str(tmp_path), row["handoff_id"], "fdgn-622/v1", "otl-1",
                            "applied", [], "2026-01-02T00:00:00Z")
    acknowledge_handoff(str(tmp_path), row["handoff_id"], "fdgn-622/v1", "otl-1",
                        "applied", ["skill:x@sha256:abc"], "2026-01-02T00:00:00Z")
    assert read_pending_handoffs(str(tmp_path), "kit:life/money") == []


def test_skip_handoff_expiry_restore(tmp_path):
    row = persist_pending_handoff(
        str(tmp_path), "fixture", "itm-2", "rule", b"must remain exact\n", "event:2",
        "v1", ["rule:test"], "rule:test", "2026-01-01T00:00:00Z",
        "2026-01-31T00:00:00Z")
    assert archive_expired_handoffs(str(tmp_path), "2026-02-01T00:00:00Z") == [row["handoff_id"]]
    assert read_pending_handoffs(str(tmp_path), "rule:test") == []
    assert restore_expired_handoff(str(tmp_path), row["handoff_id"])["exact_text_b64"] == row["exact_text_b64"]


def test_identical_sibling_delete_restore(tmp_path):
    eng = engine(tmp_path)
    payload = b"<!-- item-id: itm-001 --> exact bytes\n"
    for item_id in ("itm-001", "itm-002"):
        eng.archive_bytes(item_id, payload, "MEMORY.md", proof())
    token = eng.preview_delete(["itm-001"], "otl-7")
    eng.delete(["itm-001"], "otl-7", token)
    target = eng.restore("itm-002", "inbox")
    assert open(target, "rb").read().endswith(payload)
    assert os.path.isfile(tmp_path / "data" / "archive" / "tombstones" / "itm-001.json")


def test_large_replay_rolling_hash():
    source = b"".join(("line-%04d\n" % n).encode() for n in range(9000))
    expected = hashlib.sha256(source).hexdigest()
    first_end = source.index(b"\n") + 1
    source, expected = rolling_remove(source, expected, [(0, first_end)])
    second_end = source.index(b"\n") + 1
    source, expected = rolling_remove(source, expected, [(0, second_end)])
    assert source.startswith(b"line-0002\n")
    assert hashlib.sha256(source).hexdigest() == expected


def test_large_replay_hash_mismatch():
    source = b"a\nb\n"
    old_hash = hashlib.sha256(source).hexdigest()
    with pytest.raises(ArchiveError, match="rolling source hash mismatch"):
        rolling_remove(b"changed\n" + source, old_hash, [(0, 2)])


def test_entrypoints_recover_before_read(tmp_path):
    eng = engine(tmp_path)
    target = tmp_path / "memories" / "MEMORY.md"
    temp = tmp_path / "data" / "journal" / "next"
    temp.parent.mkdir(parents=True)
    temp.write_bytes(b"recovered\n")
    journal = {"replacements": [{"temp": str(temp), "target": str(target),
                                  "sha256": hashlib.sha256(b"recovered\n").hexdigest()}]}
    import json
    (temp.parent / "transaction.json").write_text(json.dumps(journal), encoding="utf-8")
    with eng.transaction():
        assert target.read_bytes() == b"recovered\n"
    assert not (temp.parent / "transaction.json").exists()


def test_scope_mismatch_retains_memory():
    claim = {"subject": "owner", "target": "coffee", "condition": "morning",
             "polarity": "positive", "modality": "preference", "authority": "owner",
             "normalized_claim": "likes coffee"}
    assert ownership_equivalent(claim, dict(claim))
    for key in claim:
        changed = dict(claim)
        changed[key] = "different"
        assert not ownership_equivalent(claim, changed)
    unknown = dict(claim)
    unknown["authority"] = None
    assert not ownership_equivalent(claim, unknown)
