"""Native-turn intake producer and the single ticketless slot writer.

The semantic intake step supplies diagnosis and direction, never proof facts.
All owner facts are reconstructed from the bridge ledger/native main chain.
Only the carrier callback can create the receipt needed by normal approval.
Ticket providers must implement WorkRecordAdapter in their owning source;
passing a ticket path here is deliberately unsupported.
"""
import hashlib
import json
from pathlib import Path
import shlex

from . import runtime, store
from .intake import digest, record_owner_slots, semantic_binding
from .records import owning_records

_READBACK_EN = {"why.readback.header": "Why candidate / now",
                "why.readback.fallback": "This matters now because {problem}.",
                "why.readback.now": "now: "}


def _readback_words(root):
    """Fixed words of the owner read-back card in the instance language.
    English (and any key the language catalog lacks) keeps today's text."""
    words = dict(_READBACK_EN)
    try:
        import os
        import sys
        lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if lib not in sys.path:
            sys.path.insert(0, lib)
        from term_registry import conf_lang
        import i18n_lookup
        lang = conf_lang(str(root))
        if lang and lang != "en":
            for key in words:
                val = i18n_lookup.lookup(str(root), lang, key)
                if val and ("{problem}" in val or key != "why.readback.fallback"):
                    words[key] = val
    except Exception:  # noqa: BLE001 -- the card never fails on a catalog read
        pass
    return words


def intake_command(root, data):
    """Recognize only a safely quoted invocation of the typed writer.

    Comparing the exact shell serialization excludes substitutions, redirects,
    pipelines, newlines between arguments and compound commands. The CLI then
    verifies provenance and CAS before any record write. No generic Python or
    caller-selected script/path receives this exception.
    """
    if data.get("tool_name") not in ("Bash", "exec_command", "functions.exec_command"):
        return False
    inputs = data.get("tool_input") or {}
    command = inputs.get("command", inputs.get("cmd"))
    try:
        if inputs.get("workdir") and Path(inputs["workdir"]).resolve() != Path(data["cwd"]).resolve():
            return False
        if "command" in inputs and "cmd" in inputs and inputs["command"] != inputs["cmd"]:
            return False
        args = shlex.split(command)
        if (len(args) != 5 or args[:2] != ["python3", "-B"]
                or args[3] != "--request" or command != shlex.join(args)):
            return False
        script = Path(args[2])
        if not script.is_absolute():
            script = Path(data["cwd"]) / script
        if script.resolve() != (Path(root) / "routines/why-intake.py").resolve():
            return False
        request = json.loads(args[4])
        return request["current"]["session"] == data["session_id"]
    except (ValueError, TypeError, KeyError):
        return False


def write_intake(root, *, current, unit, expected_revision, symptom, problem,
                 why, direction, symptom_source=None, why_source=None,
                 approval="", ticket=None):
    """Record a pending or approved normal intake and publish its exact frame.

    The approval must be from the current bound owner turn, after a delivered
    same-work read-back. Rejected or changed diagnoses are new pending
    revisions. No caller-provided receipt, state, kind or source text is used.
    """
    if not runtime.enabled(root):
        raise ValueError("why-net-off")
    request_hash = digest(dict(current=current, unit=unit, expected_revision=expected_revision,
        symptom=symptom, problem=problem, why=why, direction=direction,
        symptom_source=symptom_source, why_source=why_source, approval=approval, ticket=ticket))
    from term_registry import schema_labels
    labels = schema_labels(str(root), "why_intake_v4")
    owner = runtime.owner_message(root, **current)
    import why_owner_turn as native
    with store.canonical(root):
        rows = native.ledger_rows(current["session"])
    if not rows or rows[-1]["id"] != current["ledger_id"]:
        raise ValueError("stale-owner-turn")
    unit = {key: unit[key] for key in (
        "work_key", "scope_revision", "scope_digest", "targets")}
    if not all(unit[key] for key in ("work_key", "scope_revision", "scope_digest")):
        raise ValueError("work-scope-unavailable")
    adapter = owning_records(root, labels, unit["work_key"], ticket)
    state = adapter.state(unit["work_key"])
    retry = (bool(approval) and state["revision"] == expected_revision + 1
             and state["revisions"][-1]["context"].get("request_hash") == request_hash)
    if state["revision"] != expected_revision and not retry:
        raise ValueError("stale-record")
    prior = state["revisions"][:-1] if retry else state["revisions"]
    previous = prior[-1] if prior else None
    sources, facts = {}, {}

    def previous_source(field, value):
        if not previous or previous["slots"].get(field) != value:
            return None
        ref = previous["slots"]["proofs"].get(field, {}).get("source_ref")
        return previous["context"].get("owner_sources", {}).get(ref)

    # A short approving reply need not repeat the original symptom or reason.
    # Reuse their locators only when the actual slot bytes are unchanged.
    quoted_symptom = '\u201c' + symptom + '\u201d' if isinstance(symptom, str) else None
    symptom_source = symptom_source or previous_source("symptom", quoted_symptom)
    why_source = why_source or previous_source("why", why)

    def source(locator):
        fact = runtime.owner_message(root, **locator)
        ref = "owner:" + digest(locator)
        sources[ref], facts[ref] = locator, fact
        return ref, fact

    source(current)
    symptom_ref, symptom_fact = source(symptom_source or current)
    if (not isinstance(symptom, str) or not symptom.strip()
            or symptom not in symptom_fact["owner_text"] or "\n" in symptom):
        raise ValueError("symptom-provenance")
    if not all(isinstance(x, str) and x.strip() for x in (problem, direction)):
        raise ValueError("diagnosis-unavailable")
    proofs = {"symptom": dict(source_ref=symptom_ref)}
    if why:
        why_ref, why_fact = source(why_source or current)
        if not isinstance(why, str) or why not in why_fact["owner_text"]:
            raise ValueError("why-provenance")
        proofs["why"] = dict(kind="owner-reason", source_ref=why_ref, quote=why)
    slots = dict(symptom=quoted_symptom, problem=problem,
                 why=why, direction=direction, direction_approval="", proofs=proofs)
    binding = dict(owner_id=owner["owner_id"], runtime=runtime.enrolment(root)["runtime"],
        session_id=current["session"], request_id=owner["request_id"],
        owner_text_hash=owner["source_hash"])
    context = dict(binding, **unit)
    semantic = digest(semantic_binding(context, slots))
    readback_ref = "readback:" + digest([unit["work_key"], semantic])
    candidates = [why] if why else []
    words = _readback_words(root)
    fallback = words["why.readback.fallback"].format(problem=problem.rstrip(". "))
    if fallback not in candidates:
        candidates.append(fallback)
    candidates = candidates[:3]
    readback_body = (words["why.readback.header"] + "\n" + "\n".join(candidates)
                     + "\n" + words["why.readback.now"] + direction)
    if previous and previous["context"].get("semantic_digest") == semantic:
        readback_body = previous["context"].get("readback_body", readback_body)
    readback = dict(ref=readback_ref, semantic_digest=semantic,
        body_hash=hashlib.sha256(readback_body.encode()).hexdigest())
    if approval:
        if (not isinstance(approval, str) or not approval.strip() or "\n" in approval
                or approval not in owner["owner_text"]):
            raise ValueError("approval-provenance")
        if not previous or previous["context"].get("semantic_digest") != semantic:
            raise ValueError("approval-without-current-readback")
        receipt_path = runtime.state_dir(root) / (digest(readback_ref) + ".receipt.json")
        receipt = json.loads(receipt_path.read_text())
        pending = json.loads(runtime.snapshot_path(root, current["session"]).read_text())
        if (receipt.get("kind") != "delivered-readback"
                or receipt.get("owner_id") != owner["owner_id"]
                or receipt.get("semantic_digest") != semantic
                or receipt.get("body_hash") != readback["body_hash"]
                or receipt.get("delivery_state") != "delivered"
                or not receipt.get("carrier_id") or receipt.get("turn_ended") is not True
                or not 0 < receipt.get("delivered_order", 0) < owner["source_order"]
                or (pending.get("readback") or {}).get("ref") != readback_ref):
            raise ValueError("approval-before-delivered-readback")
        approval_ref, _ = source(current)
        decision_ref = "approval:" + digest([semantic, approval_ref])
        facts[decision_ref] = dict(kind="direction-approval", owner_id=owner["owner_id"],
            state="approved", approval_kind="normal", source_ref=approval_ref,
            semantic_digest=semantic, readback_ref=readback_ref)
        facts[readback_ref] = receipt
        proofs["direction_approval"] = dict(source_ref=approval_ref, approval_ref=decision_ref)
        slots["direction_approval"] = '\u201c' + approval + '\u201d'
    slots["_runtime"] = dict(request_hash=request_hash, semantic_digest=semantic, semantic_tuple=semantic_binding(context, slots),
        binding=binding, owner_sources=sources,
        readback=readback, readback_body=readback_body, candidates=candidates, unit=unit)
    receipt = (state["revisions"][-1]["receipt"] if retry else
        record_owner_slots(adapter, unit["work_key"], expected_revision,
            slots, "intake-revision" if previous else "intake-pending"))
    saved = adapter.read_slots(unit["work_key"])
    record_ref = saved["record_uri"]
    facts[record_ref] = dict(kind="work-record", owner_id=owner["owner_id"],
        work_key=unit["work_key"], slots=saved, semantic_digest=semantic)
    effect_ref = "effect:" + digest(unit)
    facts[effect_ref] = dict(unit, kind="work-effect", owner_id=owner["owner_id"],
        effect="new-work", future_behavior=True, provenance="bound")
    unit.update(effect_ref=effect_ref, record_ref=record_ref)
    envelope = dict(body_hash=readback["body_hash"], attachments=[])
    runtime.publish_snapshot(root, dict(binding=binding, units=[unit]), facts, [],
        owner_sources=sources, records={record_ref: dict(provider="owning-record",
            work_key=unit["work_key"], revision=saved["revision"], slots_hash=digest(saved))},
        envelopes=[digest(envelope)], intake_projection=readback_body,
        readback=readback, delivery_envelope=envelope)
    if approval and hasattr(adapter, "publish_approved"):
        adapter.publish_approved(unit["work_key"])
    buttons = [[dict(text=text, callback_data="opt:" + str(index))]
               for index, text in enumerate(candidates, 1)]
    return dict(receipt=receipt, readback=readback_body, buttons=buttons, approved=bool(approval),
                delivery_required=not (runtime.state_dir(root) /
                    (digest(readback_ref) + ".receipt.json")).exists())
