"""FDGN-594 four-slot structure and trusted work-record writer contract.

This module never infers assent or edits a ticket. The executor supplies the
record adapter and evidence resolver; neither may be supplied by model JSON.
Schema labels live once in the framework term registry, not in this parser.
"""

from abc import ABC, abstractmethod
import hashlib
import json
import re


FIELDS = ("symptom", "problem", "why", "direction_approval")
QUOTE = re.compile(r'\*"([^"\n]+)"\*|\u201c([^\u201d]+)\u201d|\u300c([^\u300d]+)\u300d')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode("ascii")).hexdigest()


def semantic_binding(context, slots):
    """The approval tuple changes with problem, direction, Why or scope."""
    return {key: context.get(key) for key in (
        "owner_id", "work_key", "scope_revision", "scope_digest", "targets"
    )} | {"problem": slots.get("problem", ""),
         "direction": slots.get("direction", ""),
         "why": slots.get("why", ""),
         "why_source": slots.get("proofs", {}).get("why")}


def _visible_lines(text):
    """Exclude code fences, inline code and nested examples from the schema."""
    fence, width = None, 0
    for line in text.splitlines():
        mark = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if mark:
            chars, suffix = mark.groups()
            if fence is None:
                fence, width = chars[0], len(chars)
            elif chars[0] == fence and len(chars) >= width and not suffix.strip():
                fence = None
            continue
        if fence is not None:
            continue
        # Four-space-indented and blockquote examples are not slot fields.
        if line.startswith(("    ", "\t")) or line.lstrip().startswith(">"):
            continue
        if re.match(r"^#{1,6}(?:\s|$)", line):
            # Removing inline code must not turn an annotated schema name
            # into a valid exact heading.
            yield line
        else:
            yield re.sub(r"(`+).*?\1", "", line)


def read_owner_slots(text, labels, *, record_uri=None, revision=None, proofs=None,
                     direction=""):
    """Read exactly one problem section with four ordered, exact H3 labels.

    `proofs` is adapter-owned metadata. Quotes are extracted byte-for-byte;
    authenticity is deliberately a separate resolver check, never a regex.
    """
    result = dict.fromkeys(FIELDS, "")
    result.update(record_uri=record_uri, revision=revision, proofs=proofs or {},
                  direction=direction, approval_kind="pending", errors=[])
    headings = {labels[f]: f for f in FIELDS}
    headings[labels["go_ahead"]] = "direction_approval"
    sections, order, active, inside = 0, [], None, False
    bodies = {f: [] for f in FIELDS}
    for line in _visible_lines(text):
        h2 = re.fullmatch(r"## (.+)", line)
        if h2:
            inside = h2[1] == "problem"
            active = None
            if inside:
                sections += 1
            continue
        if not inside:
            continue
        # Any heading closes the current field. Only exact H3 names open it.
        if re.match(r"^#{1,6}(?:\s|$)", line):
            active = None
            h3 = re.fullmatch(r"### (.+)", line)
            if h3 and h3[1] in headings:
                active = headings[h3[1]]
                order.append(active)
                if active == "direction_approval":
                    result["approval_kind"] = (
                        "go-ahead" if h3[1] == labels["go_ahead"] else "normal")
            continue
        if active:
            bodies[active].append(line)
    if sections != 1:
        result["errors"].append("problem-section-count")
    if len(order) != len(set(order)):
        result["errors"].append("duplicate-slot")
    if order != [f for f in FIELDS if f in order]:
        result["errors"].append("slot-order")
    for field in FIELDS:
        result[field] = "\n".join(bodies[field]).strip()
    result["missing"] = [f for f in FIELDS if not result[f]]
    result["quotes"] = {
        f: [next(g for g in m.groups() if g is not None)
            for m in QUOTE.finditer(result[f]) if any(g and g.strip() for g in m.groups())]
        for f in ("symptom", "direction_approval")
    }
    for field in result["quotes"]:
        if not result["quotes"][field] and field not in result["missing"]:
            result["missing"].append(field)
    return result


def validate_owner_slots(slots, context, evidence_resolver, node_resolver=None):
    """All four obligations, exact record and current read-back must pass.

    The trusted resolver validates actual input/carrier chronology. Missing
    adapters and unavailable proof are technical holds, never new questions.
    """
    errors = list(slots.get("errors", ()))
    missing = [f for f in FIELDS if f in slots.get("missing", ()) or not slots.get(f)]
    if not slots.get("direction"):
        errors.append("missing-direction")
    if evidence_resolver is None:
        return {"state": "unreadable", "missing": missing, "errors": errors + ["proof-unavailable"]}
    try:
        if not evidence_resolver.record_current(slots, context):
            errors.append("stale-record")
        proofs = slots.get("proofs", {})
        for field in ("symptom", "direction_approval"):
            quotes = slots.get("quotes", {}).get(field, ())
            if not quotes:
                if field not in missing:
                    missing.append(field)
                continue
            if not all(evidence_resolver.owner_quote(proofs.get(field), q, context)
                       for q in quotes):
                errors.append(field + "-provenance")
        why = proofs.get("why", {})
        if slots.get("why"):
            if why.get("kind") == "owner-reason":
                if not evidence_resolver.owner_reason(why, slots["why"], context):
                    errors.append("why-provenance")
            elif why.get("kind") == "confirmed-node":
                if node_resolver is None or not node_resolver(why, slots["why"], context):
                    errors.append("why-node-unverified")
            else:
                errors.append("why-provenance")
        if slots.get("direction_approval"):
            if not evidence_resolver.direction_approval(
                    proofs.get("direction_approval"), slots["approval_kind"],
                    semantic_binding(context, slots), context):
                errors.append("approval-unbound-or-pending")
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        return {"state": "unreadable", "missing": missing, "errors": errors + ["proof-unavailable"]}
    state = "incomplete" if errors or missing else (
        "go-ahead" if slots["approval_kind"] == "go-ahead" else "confirmed")
    return {"state": state, "missing": missing, "errors": errors,
            "record_uri": slots.get("record_uri"), "revision": slots.get("revision")}


class WorkRecordAdapter(ABC):
    """Owning-writer boundary; ticket and ticketless providers share this API.

    resolve must retain the provider/path for a work key across ticket aliases.
    record_slots must use the provider's CAS and typed pending/projection or
    approved revision receipt. It cannot bless an arbitrary Markdown edit.
    """

    @abstractmethod
    def resolve(self, work_key):
        """Return record_uri, revision and problem_section from the owner."""

    @abstractmethod
    def read_slots(self, work_key, revision=None):
        """Return parsed fields and trusted proof/direction metadata."""

    @abstractmethod
    def record_slots(self, work_key, expected_revision, slots, operation):
        """CAS through the owning writer; return its exact typed receipt."""


def record_owner_slots(adapter, work_key, expected_revision, slots, operation):
    if not isinstance(adapter, WorkRecordAdapter):
        raise ValueError("work-record-adapter-unavailable")
    if operation not in ("intake-pending", "intake-projection", "intake-revision"):
        raise ValueError("invalid-intake-operation")
    record = adapter.resolve(work_key)
    if record["revision"] != expected_revision:
        raise ValueError("stale-record")
    return adapter.record_slots(work_key, expected_revision, slots, operation)


def advance_intake(slots, context, evidence_resolver, node_resolver=None):
    """Return the next required intake step; time never supplies approval."""
    checked = validate_owner_slots(slots, context, evidence_resolver, node_resolver)
    if checked["state"] in ("confirmed", "go-ahead"):
        return dict(checked, intake=checked["state"], end_turn=False)
    if checked["errors"] == ["approval-unbound-or-pending"]:
        return dict(checked, intake="need-direction-approval", end_turn=True)
    if checked["state"] == "unreadable" or checked["errors"]:
        return dict(checked, intake="technical-refusal", end_turn=True)
    if set(checked["missing"]) == {"why", "direction_approval"}:
        # D3: show the diagnosed problem and bounded direction with the Why
        # question once. An answer can supply the reason and approve that
        # same read-back; do not create a mandatory second approval cycle.
        return dict(checked, intake="need-why-and-direction-combined", end_turn=True,
                    readback={"problem": slots["problem"], "direction": slots["direction"]},
                    requested_fields=["why", "direction_approval"])
    first = next((f for f in FIELDS if f in checked["missing"]), "direction_approval")
    return dict(checked, intake="need-" + first.replace("_", "-"), end_turn=True)
