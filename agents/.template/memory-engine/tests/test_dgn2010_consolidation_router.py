# test-lane: fast
# test-covers: agents/.template/memory-engine/consolidation_router.py docs/design/DGN-2010-intake-payload.md
# DGN-2010 slice 1: fixture memory items -> outbox rows + kit intake-add payloads.
# Dry-run only: subprocess is poisoned so any attempt to run the kit fails loudly.
import json
import os
import sqlite3
import subprocess
import sys

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import consolidation_router as cr  # noqa: E402

ALL = frozenset(d for d, _, _ in cr.ROUTES.values())
NOW = "2026-10-12T00:00:00Z"


@pytest.fixture(autouse=True)
def _no_exec(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("router must not execute anything")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def item(kind, iid, **kw):
    d = dict(id=iid, text="fact " + iid, kind=kind, evidence_verified=True)
    d.update(kw)
    return d


BIRTH = item("life_record", "m-birth", kit={
    "target": "block", "resource": "money",
    "data": {"date": "2026-10-02", "merchant": "gift shop", "amount": 5000,
             "method": "card"}})
KIT = {"user_version": 74, "source_list_rc": 0}


def rows(conn):
    return {r[0]: r for r in conn.execute(
        "SELECT item_id,destination,state,reason,payload_json FROM consolidation_outbox")}


def test_fixture_items_to_outbox_rows():
    conn = sqlite3.connect(":memory:")
    items = [item("context", "m-ctx"), BIRTH, item("procedure", "m-proc"),
             item("inflight_plan", "m-plan"), item("stable_owner_fact", "m-own")]
    counts = cr.route_items(conn, items, ALL, KIT, NOW)
    r = rows(conn)
    assert {k: (v[1], v[2]) for k, v in r.items()} == {
        "m-ctx": ("memory", "queued"),
        "m-birth": ("lifekit_record", "dry_run_ready"),
        "m-proc": ("pending_handoff", "pending_handoff"),
        "m-plan": ("kit_project_task", "pending_handoff"),
        "m-own": ("pending_handoff", "pending_handoff")}
    assert counts["dry_run_ready"] == 1
    argv = json.loads(r["m-birth"][4])["argv"]
    assert argv[:2] == ["kit_life.sh", "intake-add"]
    assert argv[argv.index("--source") + 1] == "consolidate-router"
    assert argv[argv.index("--source-ref") + 1] == "m-birth"
    assert argv[argv.index("--target") + 1] == "block"
    assert argv[-1] == "--hold"
    assert json.loads(argv[argv.index("--data") + 1])["merchant"] == "gift shop"
    assert argv[argv.index("--raw") + 1] == BIRTH["text"]
    assert not any(a in ("intake-resolve", "confirm") for a in argv)
    assert all(v[4] is None for k, v in r.items() if k != "m-birth")


def test_unavailable_kit_holds_row_and_keeps_payload():
    conn = sqlite3.connect(":memory:")
    cr.route_items(conn, [BIRTH], ALL - {"lifekit_record"}, None, NOW)
    assert rows(conn)["m-birth"][2:4] == ("hold", "owner_unavailable")
    conn2 = sqlite3.connect(":memory:")
    cr.route_items(conn2, [BIRTH], ALL, None, NOW)
    r = rows(conn2)["m-birth"]
    assert r[1] == "lifekit_record" and r[2:4] == ("hold", "owner_unavailable")
    assert r[4]  # candidate payload preserved for replay
    assert conn2.execute("SELECT COUNT(*) FROM consolidation_outbox WHERE destination='memory'").fetchone()[0] == 0


def test_schema_mismatch_fails_closed_and_recovers_idempotently():
    conn = sqlite3.connect(":memory:")
    cr.route_items(conn, [BIRTH], ALL, {"user_version": 58, "source_list_rc": 0}, NOW)
    assert rows(conn)["m-birth"][3] == "kit_schema_mismatch"
    cr.route_items(conn, [BIRTH], ALL, {"user_version": 74, "source_list_rc": 1}, NOW)
    assert rows(conn)["m-birth"][3] == "kit_schema_mismatch"
    cr.route_items(conn, [BIRTH], ALL, KIT, NOW)
    assert rows(conn)["m-birth"][2] == "dry_run_ready"
    cr.route_items(conn, [BIRTH], ALL, KIT, NOW)
    assert conn.execute("SELECT COUNT(*) FROM consolidation_outbox").fetchone()[0] == 1


def test_measure_reserved_and_non_money_never_payload():
    conn = sqlite3.connect(":memory:")
    meas = item("life_record", "m-meas", kit={"target": "measure", "data": {"weight": "70kg"}})
    fam = item("life_record", "m-fam", kit={"target": "block", "data": {"birth": "1990-01-01"}})
    cr.route_items(conn, [meas, fam], ALL, KIT, NOW)
    r = rows(conn)
    assert r["m-meas"][2:5] == ("hold", "measure_target_reserved", None)
    assert r["m-fam"][2:5] == ("pending_handoff", "not_money", None)


@pytest.mark.parametrize("it,reason", [
    (item("life_record", "a", kit={"target": "block", "resource": "money",
          "data": {"date": "2026-10-02", "merchant": "m", "amount": -5, "method": "card"}}),
     "bad_kit_payload:amount must be a positive integer KRW"),
    (item("life_record", "b", kit={"target": "block", "resource": "money",
          "data": {"date": "10/02", "merchant": "m", "amount": 5, "method": "card"}}),
     "bad_kit_payload:date must be ISO YYYY-MM-DD"),
    (item("life_record", "b2", kit={"target": "block", "resource": "money",
          "data": {"date": "2026-10-02", "merchant": "m", "amount": 5, "method": "card",
                   "entity_id": "x"}}),
     "bad_kit_payload:unsupported money keys: entity_id"),
    (item("bogus", "c"), "unknown_kind"),
    (item("context", "d", evidence_verified=False), "unverified_evidence"),
    (item("context", "e", text=" "), "missing_item_or_text"),
])
def test_bad_items_hold_never_memory(it, reason):
    conn = sqlite3.connect(":memory:")
    cr.route_items(conn, [it], ALL, KIT, NOW)
    r = conn.execute("SELECT destination,state,reason FROM consolidation_outbox").fetchone()
    assert r[1:] == ("hold", reason) and r[0] != "memory"


def test_build_argv_is_a_list_with_raw_and_hold():
    argv = cr.build_kit_intake_add("m1", "block", {"a": "b; rm -rf /"}, raw="r")
    assert isinstance(argv, list) and argv.count("--hold") == 1
    assert cr.source_set_argv() == [
        "kit_life.sh", "source-set", "consolidate-router", "--resource", "money",
        "--module", "module_ledger", "--provenance", "declared"]
    assert "--enabled" not in cr.source_set_argv()
