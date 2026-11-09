"""Lossless, signed legacy-tree import into the enrolled shared authority.

Raw source lines are retained verbatim with their original hash chain. Import
preserves the fold; it grants no new wording, hierarchy or work approval.
Missing confirmation proof stays unavailable for purpose reuse.
"""
import hashlib
import hmac
import json
from pathlib import Path

from . import nodes, store
from .intake import digest

SCHEMA = "why-legacy-import/1"


def plan(raw, source, owner):
    if not isinstance(source, dict) or not all(source.get(k) for k in (
            "host_uuid", "instance_uuid", "log_kind")):
        raise ValueError("migration-source-unavailable")
    if source["log_kind"] != "events.jsonl":
        raise ValueError("migration-log-kind")
    events, previous = [], store.GENESIS
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise ValueError("migration-empty-row")
        event = json.loads(line)
        if event.get("prev") != previous:
            raise ValueError("migration-chain-broken-at-" + str(number))
        previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
        events.append(event)
    folded = nodes.fold(events)
    namespace = "legacy:" + digest(source)
    result = {}
    for wid, original in folded.items():
        result[wid] = dict(original, owner_id=owner, revision=1,
            migration=dict(source=source, alias=namespace + ":" + wid,
                           source_head=previous, confirmation="not-reproved",
                           # Retain ancestry as historical data, never imply
                           # that old agent-only attaches gained approval.
                           hierarchy="historical-unverified"))
    return dict(schema=SCHEMA, source=source, source_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                source_head=previous, namespace=namespace, nodes=result,
                history=events, raw_events=raw)


def snapshot(source_state, source, owner):
    return plan((Path(source_state) / "events.jsonl").read_bytes().decode("utf-8"), source, owner)


def bundle(value, owner, issuer, key):
    fact = dict(kind="legacy-tree-import", owner_id=owner, plan=value)
    return dict(issuer=issuer, fact=fact,
                signature=hmac.new(key, digest(fact).encode(), hashlib.sha256).hexdigest())


def apply(state, fact, owner, agent_id):
    """Authority-only import; signed source is re-folded, never trusted flags."""
    supplied = fact["plan"]
    value = plan(supplied["raw_events"], supplied["source"], owner)
    if value != supplied or supplied.get("schema") != SCHEMA:
        raise ValueError("migration-projection-mismatch")
    namespace = value["namespace"]
    imports = state.setdefault("legacy_imports", {})
    previous = imports.get(namespace)
    if previous:
        if previous != value:
            raise ValueError("migration-source-changed")
        return dict(imported=0, existing=len(value["nodes"]), source=namespace)
    if set(value["nodes"]) & set(state["nodes"]):
        raise ValueError("migration-node-collision")
    state["nodes"].update(value["nodes"])
    imports[namespace] = value
    state.setdefault("node_origins", {}).update({wid: agent_id for wid in value["nodes"]})
    return dict(imported=len(value["nodes"]), existing=0, source=namespace)


def migrate(root, *, dry_run=False, source_state=None):
    """Update entry point: enroll from installed owner identity, then import."""
    from . import authority, runtime
    root = Path(root)
    if not runtime.enabled(root):
        return dict(status="skipped", reason="why-net-off")
    if not dry_run:
        from .enrollment import enroll
        enroll(root)
    source_state = Path(source_state or root / ".telegram_bot/state/why-net")
    if not (source_state / "events.jsonl").exists():
        return dict(status="skipped", reason="no-legacy-events")
    # Dry-run needs no credentials or authority writes. Deployment identity is
    # mandatory for the real import and cannot be guessed from a path/name.
    config_path = root / "config/why-migration.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    source = config.get("source", dict(host_uuid="unassigned", instance_uuid="unassigned", log_kind="events.jsonl"))
    client_path = root / "config/why-authority.json"
    client = json.loads(client_path.read_text()) if client_path.exists() else {}
    owner = client.get("owner_key", "unassigned")
    value = snapshot(source_state, source, owner)
    summary = dict(status="planned", count=len(value["nodes"]), source_sha256=value["source_sha256"],
                   nodes=value["nodes"], events=len(value["history"]))
    if dry_run:
        return summary
    if (not config or not client or owner == "unassigned"
            or "unassigned" in (source["host_uuid"], source["instance_uuid"])):
        raise ValueError("migration-enrollment-required")
    identity = runtime.enrolment(root)
    if owner != identity["owner_id"]:
        raise ValueError("migration-owner-mismatch")
    signed = bundle(value, owner, config["issuer"], Path(config["key_file"]).read_bytes())
    request = dict(protocol=authority.PROTOCOL, authority_id=authority.AUTHORITY,
        owner_key=owner, principal=client["principal_id"], operation="read_snapshot", payload={})
    current = authority.transact(request, client_path)
    if current.get("status") != "snapshot":
        raise ValueError("migration-authority-unavailable")
    previous = current.get("legacy_imports", {}).get(value["namespace"])
    if previous:
        if previous["source_sha256"] != value["source_sha256"]:
            raise ValueError("migration-source-changed")
        return dict(summary, status="committed", result=dict(imported=0,
                    existing=previous["count"], source=value["namespace"]))
    request.update(operation="import_legacy_tree", payload=dict(bundle=signed),
        expected_revision=current["revision"],
        request_id="legacy-import:" + digest([value["namespace"], value["source_sha256"], current["revision"]]))
    committed = authority.transact(request, client_path)
    if committed.get("status") != "committed":
        raise ValueError("migration-" + committed.get("status", "unavailable"))
    return dict(summary, status="committed", result=committed["result"])
