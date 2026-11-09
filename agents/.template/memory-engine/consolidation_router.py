"""DGN-2010 slice 1: engine-owned outbox + consolidation router (dry-run writer).

Classifies already-extracted consolidated items into the five durable homes,
persists one outbox row per (destination, source_ref), and for the kit
destination BUILDS the `kit_life.sh intake-add` argv without running it.
Nothing here calls a subprocess, the network or the kit. It is not wired into
cmd_consolidate; the caller passes items, kit state and a clock.

Kit write-door contract (kit-owner ruling, handoff J44H36XP): intake lane only, held,
idempotent on (source, source_ref), never self-confirmed, entity resolution
left to the kit (raw words are passed through), fail closed on schema mismatch.
"""

import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile

SOURCE_KEY = "consolidate-router"
KIT_MIN_USER_VERSION = 59        # capability floor (kit intake, migration 059); live is 74
KIT_TARGETS = ("block", "measure")
MONEY_REQUIRED = ("date", "amount", "merchant", "method")
MONEY_OPTIONAL = ("kind", "unit", "operation", "scope", "title", "note")
SOURCE_SET = {"resource": "money", "module": "module_ledger", "provenance": "declared"}
MAX_ATTEMPTS = 3                 # framework ruling: 3 retries then dead-letter
RETENTION_DAYS = 7
HANDOFF_RETENTION_DAYS = 30
SKIP_KINDS = frozenset(("procedure", "workflow", "rule", "normative_constraint",
                        "kit_resource", "kit_resource_constraint", "owner_profile"))

# kind -> (destination, owner, state when routable)
ROUTES = {
    "context": ("memory", "memory_engine", "queued"),
    "life_record": ("lifekit_record", "lifekit_owner", "dry_run_ready"),
    "procedure": ("pending_handoff", "fdgn_622", "pending_handoff"),
    "inflight_plan": ("kit_project_task", "kit_owner", "pending_handoff"),
    "stable_owner_fact": ("pending_handoff", "fdgn_622", "pending_handoff"),
}


def classify_matter(kind):
    """Map durable matter to the 2.7.0 taxonomy before any memory write."""
    if kind in SKIP_KINDS:
        return "SKIP_IN_FLOW"
    if kind in ("life_record", "kit_record"):
        return "kit_record"
    if kind in ("inflight_plan", "kit_task"):
        return "kit_task"
    return "memory_residue"


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def persist_pending_handoff(root, instance_id, item_id, kind, exact_text,
                            source_ref, source_version, subject_scope,
                            recurrence_key, created_at, expires_at):
    """Persist the only allowed output for SKIP_IN_FLOW matter.

    The deterministic ID makes transcript retries idempotent.  Exact text is
    base64 encoded so newline and Unicode bytes survive without normalization.
    """
    if classify_matter(kind) != "SKIP_IN_FLOW":
        raise ValueError("pending handoffs accept only SKIP_IN_FLOW matter")
    data = exact_text if isinstance(exact_text, bytes) else exact_text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    seed = "\0".join((instance_id, source_ref or item_id, kind, digest)).encode("utf-8")
    handoff_id = "ph-" + hashlib.sha256(seed).hexdigest()
    row = {"schema": "pending-handoff-v1", "handoff_id": handoff_id,
           "instance_id": instance_id, "item_id": item_id, "kind": kind,
           "exact_text_b64": base64.b64encode(data).decode("ascii"),
           "text_sha256": digest, "source_ref": source_ref,
           "source_version": source_version, "subject_scope": list(subject_scope),
           "recurrence_key": recurrence_key, "created_at": created_at,
           "expires_at": expires_at, "state": "pending"}
    path = os.path.join(root, "handoffs", "pending", handoff_id + ".json")
    if os.path.isfile(path):
        with open(path, "r", encoding="ascii") as stream:
            if json.load(stream) != row:
                raise ValueError("handoff identity collision")
    else:
        _atomic_json(path, row)
    return row


def read_pending_handoffs(root, recurrence_key):
    """Small read API for FDGN-622; reading never acknowledges an envelope."""
    directory = os.path.join(root, "handoffs", "pending")
    rows = []
    if not os.path.isdir(directory):
        return rows
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(directory, name), "r", encoding="ascii") as stream:
            row = json.load(stream)
        if row.get("schema") == "pending-handoff-v1" and row.get("recurrence_key") == recurrence_key:
            data = base64.b64decode(row["exact_text_b64"], validate=True)
            if hashlib.sha256(data).hexdigest() != row["text_sha256"]:
                raise ValueError("pending handoff hash mismatch")
            rows.append(row)
    return rows


def acknowledge_handoff(root, handoff_id, consumer_contract, work_turn_ref,
                        action, artifact_refs, acknowledged_at):
    """Atomically consume a handoff only with an artifact-bound FDGN-622 ack."""
    allowed = ("applied", "owner_approval_requested", "incorporated_without_change")
    if action not in allowed or not consumer_contract or not work_turn_ref or not artifact_refs:
        raise ValueError("complete artifact-bound acknowledgement required")
    pending = os.path.join(root, "handoffs", "pending", handoff_id + ".json")
    with open(pending, "r", encoding="ascii") as stream:
        handoff = json.load(stream)
    ack = {"schema": "handoff-ack-v1", "handoff_id": handoff_id,
           "consumer_contract": consumer_contract, "work_turn_ref": work_turn_ref,
           "action": action, "artifact_refs": artifact_refs,
           "acknowledged_at": acknowledged_at}
    target = os.path.join(root, "handoffs", "acknowledged", handoff_id + ".json")
    _atomic_json(target, {"handoff": handoff, "ack": ack})
    os.unlink(pending)
    return ack


def archive_expired_handoffs(root, now_iso):
    """Move expired envelopes unchanged to non-recalled, restorable storage."""
    pending_dir = os.path.join(root, "handoffs", "pending")
    moved = []
    if not os.path.isdir(pending_dir):
        return moved
    for name in sorted(os.listdir(pending_dir)):
        if not name.endswith(".json"):
            continue
        source = os.path.join(pending_dir, name)
        with open(source, "r", encoding="ascii") as stream:
            row = json.load(stream)
        if row.get("expires_at", "") > now_iso:
            continue
        target = os.path.join(root, "handoffs", "archive", name)
        _atomic_json(target, row)  # preserve all semantic fields and exact bytes
        os.unlink(source)
        moved.append(row["handoff_id"])
    return moved


def restore_expired_handoff(root, handoff_id):
    source = os.path.join(root, "handoffs", "archive", handoff_id + ".json")
    with open(source, "r", encoding="ascii") as stream:
        row = json.load(stream)
    target = os.path.join(root, "handoffs", "pending", handoff_id + ".json")
    _atomic_json(target, row)
    os.unlink(source)
    return row

SCHEMA = """
CREATE TABLE IF NOT EXISTS consolidation_outbox (
  id INTEGER PRIMARY KEY,
  destination TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  item_id TEXT NOT NULL,
  kind TEXT,
  owner TEXT,
  state TEXT NOT NULL,
  reason TEXT NOT NULL,
  text TEXT NOT NULL,
  payload_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(destination, source_ref)
)"""


def init_outbox(conn):
    conn.execute(SCHEMA)


def check_money_data(data):
    """Raise ValueError unless data satisfies the kit money block contract."""
    for k in MONEY_REQUIRED:
        v = data.get(k)
        if k == "amount":
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError("amount must be a positive integer KRW")
        elif not isinstance(v, str) or not v.strip():
            raise ValueError("%s required" % k)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["date"]):
        raise ValueError("date must be ISO YYYY-MM-DD")
    extra = set(data) - set(MONEY_REQUIRED) - set(MONEY_OPTIONAL)
    if extra:
        raise ValueError("unsupported money keys: %s" % ",".join(sorted(extra)))


def build_kit_intake_add(item_id, target, data, raw=None):
    """Return the argv list for kit_life.sh intake-add (never executed here).

    `data` is a JSON object of raw words; no entity ids (kit resolves them).
    Always --hold; no confirm/resolve verb is ever built.
    """
    if target not in KIT_TARGETS:
        raise ValueError("target must be block or measure")
    if not isinstance(data, dict) or not data:
        raise ValueError("data must be a non-empty object")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("source_ref (memory item id) required")
    if any(k in data for k in ("entity_id", "block_id", "measure_id")):
        raise ValueError("entity/record ids are kit-resolved, not router-supplied")
    argv = ["kit_life.sh", "intake-add", "--source", SOURCE_KEY,
            "--source-ref", item_id, "--target", target,
            "--data", json.dumps(data, sort_keys=True, ensure_ascii=False)]
    if raw is not None:
        argv += ["--raw", str(raw)]
    argv.append("--hold")
    return argv


def source_set_argv():
    """One-time registration argv; source stays enabled=0 (owner enables it
    in ledger setup). A 'source disabled' kit reply means hold."""
    return ["kit_life.sh", "source-set", SOURCE_KEY, "--resource",
            SOURCE_SET["resource"], "--module", SOURCE_SET["module"],
            "--provenance", SOURCE_SET["provenance"]]


def _kit_gate(kit):
    """kit: None/False = unavailable, else {"user_version": int,
    "source_list_rc": int} probed by the caller. Capability, not equality."""
    if kit is None or kit is False:
        return "owner_unavailable"
    if not isinstance(kit, dict):
        return "kit_schema_mismatch"
    uv = kit.get("user_version")
    if (isinstance(uv, bool) or not isinstance(uv, int)
            or uv < KIT_MIN_USER_VERSION or kit.get("source_list_rc") != 0):
        return "kit_schema_mismatch"
    return None


def decide(item, available, kit):
    """Pure decision: (destination, owner, state, reason, payload|None)."""
    iid = item.get("id")
    text = item.get("text")
    if not all(isinstance(v, str) and v.strip() for v in (iid, text)):
        return (None, None, "hold", "missing_item_or_text", None)
    if item.get("evidence_verified") is not True:
        return (None, None, "hold", "unverified_evidence", None)
    kind = item.get("kind")
    if not isinstance(kind, str) or kind not in ROUTES:
        return (None, None, "hold", "unknown_kind", None)
    dest, owner, state = ROUTES[kind]
    if dest not in available:
        return (dest, owner, "hold", "owner_unavailable", None)
    if kind != "life_record":
        return (dest, owner, state, "not_persisted", None)
    spec = item.get("kit") or {}
    if spec.get("target") == "measure":
        return (dest, owner, "hold", "measure_target_reserved", None)
    if spec.get("resource") != "money" or spec.get("target") != "block":
        # money-only: no landing path in the kit for anything else
        return (dest, owner, "pending_handoff", "not_money", None)
    try:
        check_money_data(spec.get("data") or {})
        argv = build_kit_intake_add(iid, "block", spec.get("data"), text)
    except ValueError as exc:
        return (dest, owner, "hold", "bad_kit_payload:%s" % exc, None)
    payload = json.dumps({"argv": argv})
    gate = _kit_gate(kit)
    if gate:
        return (dest, owner, "hold", gate, payload)
    return (dest, owner, state, "dry_run_not_sent", payload)


def route_items(conn, items, available, kit, now):
    """Persist one outbox row per item; idempotent on (destination, source_ref).

    Returns counts by state. Held rows are kept (never dropped, never routed
    to memory). Re-routing an existing row refreshes a held row only.
    """
    init_outbox(conn)
    counts = {}
    for item in items:
        dest, owner, state, reason, payload = decide(item, available, kit)
        dest_key = dest or "unrouted"
        ref = item.get("source_ref") or item.get("id") or ""
        row = conn.execute(
            "SELECT id,state FROM consolidation_outbox WHERE destination=? AND source_ref=?",
            (dest_key, ref)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO consolidation_outbox(destination,source_ref,item_id,kind,"
                "owner,state,reason,text,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (dest_key, ref, str(item.get("id") or ""), item.get("kind"), owner,
                 state, reason, str(item.get("text") or ""), payload, now, now))
        elif row[1] == "hold":
            conn.execute(
                "UPDATE consolidation_outbox SET kind=?,owner=?,state=?,reason=?,"
                "payload_json=?,updated_at=? WHERE id=?",
                (item.get("kind"), owner, state, reason, payload, now, row[0]))
            state = state
        else:
            state = row[1]
        counts[state] = counts.get(state, 0) + 1
    conn.commit()
    return counts
