"""Trusted runtime evidence adapters for FDGN-594.

No command line accepts frames or proofs from tool arguments. The bridge or
managed executor publishes a snapshot after its semantic intake step. Native
owner facts are reconstructed from the exact main-chain/ledger join on each
load. Unenrolled runtimes and missing producers hold consequential effects.
"""
import hashlib
import json
import os
from pathlib import Path
import time

from . import store
from .evidence import EvidenceResolver
from .intake import digest, read_owner_slots


def instance_root(cwd, enrolled=None):
    if enrolled:
        return Path(enrolled)
    start = Path(cwd).absolute()
    for candidate in (start, *start.parents):
        if (candidate / "config/agent.conf").is_file():
            return candidate
        # A foreign project, worktree or hermetic fixture is its own boundary.
        # Never adopt an enabled ancestor instance's switch through scratch.
        if any((candidate / marker).exists() for marker in (".git", ".claude", "routines", "config")):
            return candidate
    return start


def enabled(root):
    from conf_reader import conf_get
    return (conf_get("WHY_NET", str(Path(root) / "config/agent.conf")) or "off").lower() == "on"


def state_dir(root):
    # Never allow a caller's ambient BOT_DATA_DIR to select another instance.
    return Path(root) / ".telegram_bot/state/why-net/runtime"


def snapshot_path(root, session):
    if not isinstance(session, str) or not session:
        raise ValueError("session-unavailable")
    return state_dir(root) / (digest(session) + ".json")


def atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    store.write_json_atomic(str(path), value)


def enrolment(root):
    # Deployment-owned mapping; never infer stable identity from a model field.
    path = Path(root) / "config/why-runtime.json"
    if not path.exists():
        # A single private-chat owner is already enrolled by the Telegram
        # access controller. Reuse its deployment files without a new store.
        # Multiple accounts/group routes require an explicit identity map.
        from conf_reader import conf_get
        bot_dir = Path(root) / ".telegram_bot"
        raw = conf_get("ALLOWED_USER_IDS", str(bot_dir / ".env"))
        if raw:
            owners = json.loads(raw if raw.startswith("[") else "[" + raw + "]")
        else:
            owners = [int((bot_dir / "owner.lock").read_text().strip())]
        if (len(owners) != 1 or type(owners[0]) is not int or owners[0] <= 0):
            raise ValueError("identity-unavailable")
        return dict(owner_id="telegram-" + str(owners[0]), runtime="bridge", chat_id=owners[0])
    value = json.loads(path.read_text())
    if not value.get("owner_id") or value.get("runtime") not in ("claude-code", "bridge"):
        raise ValueError("identity-unavailable")
    return value


def owner_message(root, session, ledger_id, message_uuid):
    """Produce exact owner-region facts, without old question/language filters."""
    import why_owner_turn as native
    identity = enrolment(root)
    with store.canonical(root):
        rows = native.ledger_rows(session)
        row = next((r for r in rows if r.get("id") == ledger_id), None)
        if not row or not row.get("request_id"):
            raise ValueError("owner-ledger-unavailable")
        carrier = row.get("telegram_owner")
        if carrier is not None:
            words = carrier.get("owner_text")
            if (identity["runtime"] != "bridge" or row.get("source") != "message"
                    or row.get("chat_id") != identity.get("chat_id")
                    or message_uuid != "telegram:%s:%s" % (row.get("chat_id"), row.get("message_id"))
                    or not isinstance(words, str) or not words.strip()
                    or carrier.get("owner_hash") != native.turn_sha(words)
                    or not isinstance(carrier.get("received_order"), int)):
                raise ValueError("owner-carrier-unbound")
            return dict(kind="owner-message", owner_id=identity["owner_id"],
                source_role="owner", owner_text=words, source_hash=digest(words),
                source_order=carrier["received_order"], transport="telegram",
                reply_to_carrier_id=carrier.get("reply_to_carrier_id"),
                session_id=session, request_id=row["request_id"],
                ledger_id=ledger_id, message_uuid=message_uuid)
        if row.get("source") == "callback":
            keyboard, index = row.get("keyboard"), row.get("option_index")
            if (identity["runtime"] != "bridge" or row.get("chat_id") != identity.get("chat_id")
                    or message_uuid != "telegram:%s:%s" % (row.get("chat_id"), row.get("message_id"))
                    or not isinstance(keyboard, list) or not keyboard
                    or not isinstance(index, int) or isinstance(index, bool)
                    or not 1 <= index <= len(keyboard)
                    or not isinstance(row.get("bubble_text_sha"), str)
                    or not row["bubble_text_sha"]
                    or not isinstance(row.get("callback_data"), str)):
                raise ValueError("owner-callback-unbound")
            words = keyboard[index - 1]
            if not isinstance(words, str) or not words.strip():
                raise ValueError("owner-callback-label-empty")
            return dict(kind="owner-message", owner_id=identity["owner_id"],
                source_role="owner", owner_text=words, source_hash=digest(words),
                source_order=int(store.parse_iso(row["at"]) * 1_000_000),
                transport="telegram-callback", keyboard=list(keyboard), option_index=index,
                bubble_message_id=row["message_id"], bubble_text_sha=row["bubble_text_sha"],
                callback_data=row["callback_data"], session_id=session,
                request_id=row["request_id"], ledger_id=ledger_id, message_uuid=message_uuid)
        path = native.session_transcript(session, cwd=str(root))
        if not path:
            raise ValueError("native-transcript-unavailable")
        turns, _, _, _ = native.chain_turns(path)
        bindings, _ = native.bind_ledger(rows, turns)
        if bindings.get(message_uuid, {}).get("id") != ledger_id:
            raise ValueError("owner-provenance-unbound")
        entry, text = next(t for t in turns if t[0].get("uuid") == message_uuid)
        words = native.owner_words(text)
        if not isinstance(words, str) or not words.strip():
            raise ValueError("owner-region-empty")
        # Bridge enrollment binds the allowed chat independently of snapshot JSON.
        if identity["runtime"] == "bridge" and row.get("chat_id") != identity.get("chat_id"):
            raise ValueError("owner-chat-mismatch")
        return dict(kind="owner-message", owner_id=identity["owner_id"],
                    source_role="owner", owner_text=words, source_hash=digest(words),
                    source_order=int(store.parse_iso(row["at"]) * 1_000_000),
                    session_id=session, request_id=row["request_id"],
                    ledger_id=ledger_id, message_uuid=entry["uuid"])


def publish_snapshot(root, frame, facts, operations, *, owner_sources, records,
                     envelopes=(), intake_projection=None, readback=None, delivery_envelope=None):
    """Runtime-only publication API. No model-facing arbitrary JSON verb.

    Effects/approvals remain the semantic intake producer's responsibility.
    This adapter authenticates owner regions, current record bytes and exact
    operations. It cannot turn a model's `verified=true` into owner evidence.
    """
    binding = frame["binding"]
    identity = enrolment(root)
    if binding["owner_id"] != identity["owner_id"] or binding["runtime"] != identity["runtime"]:
        raise ValueError("identity-mismatch")
    for ref, locator in owner_sources.items():
        facts[ref] = owner_message(root, **locator)
    value = dict(schema=1, frame=frame, facts=facts, operations=list(operations),
                 owner_sources=owner_sources, records=records, envelopes=list(envelopes),
                 intake_projection=intake_projection, readback=readback,
                 delivery_envelope=delivery_envelope)
    atomic(snapshot_path(root, binding["session_id"]), value)


def record_readback_ack(root, session, body, carrier_id, *, turn_ended=False):
    """Carrier-only callback AFTER acknowledgment, never at Stop or generation.

    Exact final serialization must match the runtime's pending read-back.
    An uncertain send writes no delivered receipt. A turn boundary is explicit.
    """
    path = snapshot_path(root, session)
    with store.canonical(root), store.locked(wait=2.0):
        snapshot = json.loads(path.read_text())
        expected = snapshot.get("readback")
        raw = body.encode("utf-8") if isinstance(body, str) else body
        if (not expected or not carrier_id or expected.get("body_hash") != hashlib.sha256(raw).hexdigest()
                or not turn_ended):
            return False
        ref = expected["ref"]
        receipt = dict(kind="delivered-readback", owner_id=snapshot["frame"]["binding"]["owner_id"],
                       semantic_digest=expected["semantic_digest"], delivery_state="delivered",
                       body_hash=expected["body_hash"], carrier_id=str(carrier_id), turn_ended=True,
                       delivered_order=time.time_ns() // 1000)
        receipt_path = state_dir(root) / (digest(ref) + ".receipt.json")
        if receipt_path.exists():
            old = json.loads(receipt_path.read_text())
            return all(old.get(k) == receipt.get(k) for k in (
                "owner_id", "semantic_digest", "body_hash", "carrier_id", "turn_ended"))
        atomic(receipt_path, receipt)
        return True


def publish_operation(root, session, operation):
    """Executor-only: bind actual tool arguments after checking current proofs."""
    from why_frame_gate import validate_build_start
    frame, resolver = load_runtime(root, dict(session_id=session))
    path = snapshot_path(root, session)
    with store.canonical(root), store.locked(wait=2.0):
        snapshot = json.loads(path.read_text())
        if snapshot["frame"] != frame:
            raise ValueError("frame-revised")
        op_hash = digest(operation)
        if op_hash not in snapshot["operations"]:
            snapshot["operations"].append(op_hash)
            atomic(path, snapshot)
    frame, resolver = load_runtime(root, dict(session_id=session), operation=operation)
    return validate_build_start(frame, operation, resolver)


def load_runtime(root, data, operation=None, envelope=None):
    """Revalidate an exact session snapshot; no global newest-turn fallback."""
    session = data.get("session_id")
    snapshot = json.loads(snapshot_path(root, session).read_text())
    frame, facts = snapshot["frame"], snapshot["facts"]
    binding = frame["binding"]
    identity = enrolment(root)
    if (snapshot.get("schema") != 1 or binding.get("session_id") != session
            or binding.get("owner_id") != identity["owner_id"]
            or binding.get("runtime") != identity["runtime"]):
        raise ValueError("runtime-unbound")
    import why_owner_turn as native
    with store.canonical(root):
        rows = native.ledger_rows(session)
    if not rows or rows[-1].get("request_id") != binding.get("request_id"):
        raise ValueError("stale-owner-turn")
    current_owner = None
    for ref, locator in snapshot["owner_sources"].items():
        actual = owner_message(root, **locator)
        if facts.get(ref) != actual:
            raise ValueError("owner-proof-changed")
        if actual["request_id"] == binding["request_id"]:
            current_owner = actual
    if not current_owner or binding["owner_text_hash"] != current_owner["source_hash"]:
        raise ValueError("owner-turn-unbound")
    if any(ref not in snapshot["owner_sources"] for ref, fact in facts.items()
           if fact.get("kind") == "owner-message"):
        raise ValueError("owner-source-unbound")
    if any(ref not in snapshot["records"] for ref, fact in facts.items()
           if fact.get("kind") == "work-record"):
        raise ValueError("record-provider-unbound")
    for ref, record in snapshot["records"].items():
        if record.get("provider") in ("ticketless", "owning-record"):
            from term_registry import schema_labels
            from .records import owning_records
            parsed = owning_records(root, schema_labels(str(root), "why_intake_v4"), record["work_key"]).read_slots(
                record["work_key"], revision=record.get("revision"))
            if digest(parsed) != record["slots_hash"] or digest(parsed) != digest(facts[ref]["slots"]):
                raise ValueError("record-revised")
            continue
        raw = Path(record["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("record-revised")
        from term_registry import schema_labels
        saved = facts[ref]["slots"]
        parsed = read_owner_slots(raw.decode(), schema_labels(str(root), "why_intake_v4"),
                                  record_uri=ref, revision=saved["revision"],
                                  proofs=saved["proofs"], direction=saved["direction"])
        if digest(parsed) != digest(saved):
            raise ValueError("record-content-mismatch")
    for ref, fact in list(facts.items()):
        if fact.get("kind") == "delivered-readback":
            facts[ref] = json.loads((state_dir(root) / (digest(ref) + ".receipt.json")).read_text())
    current = dict(binding, work_keys=[u["work_key"] for u in frame["units"]])
    if operation is not None and digest(operation) in snapshot["operations"]:
        current["operation_hash"] = digest(operation)
    if envelope is not None and digest(envelope) in snapshot["envelopes"]:
        current["envelope_hash"] = digest(envelope)
        artifact = {"tool_name": "artifact-delivery", "envelope": envelope}
        if digest(artifact) in snapshot["operations"]:
            current["operation_hash"] = digest(artifact)
        current["intake_projection"] = snapshot.get("intake_projection")
    def record_link(unit, node, candidates, linked_at):
        from term_registry import schema_labels
        from .records import owning_records
        adapter = owning_records(root, schema_labels(str(root), "why_intake_v4"),
                                 unit["work_key"])
        adapter.record_agent_link(unit["work_key"], node, candidates, linked_at)

    return frame, EvidenceResolver(facts.__getitem__, lambda: current, record_link)
