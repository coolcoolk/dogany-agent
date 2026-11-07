"""Ticketless owning writer for the four-slot intake.

Ticket aliases must keep their existing provider. This provider never edits a
ticket or accepts a caller-selected path. Pending revisions leave the accepted
revision intact; one atomic head publishes the record and its receipt together.
"""
import copy
import json
from pathlib import Path

from . import store
from record_provider import configured
from .intake import WorkRecordAdapter, digest, read_owner_slots
from .runtime import atomic, state_dir


class TicketlessRecords(WorkRecordAdapter):
    def __init__(self, root, labels):
        self.root, self.labels = Path(root), labels

    def path(self, work_key):
        if not isinstance(work_key, str) or not work_key.strip():
            raise ValueError("work-key-unavailable")
        return state_dir(self.root) / "records" / (digest(work_key) + ".json")

    def uri(self, work_key):
        return "work-record://" + digest(work_key)

    def revision_uri(self, work_key, revision):
        return self.uri(work_key)

    def state(self, work_key):
        path = self.path(work_key)
        value = json.loads(path.read_text()) if path.exists() else dict(
            work_key=work_key, revision=0, active=None, revisions=[])
        if value["work_key"] != work_key:
            raise ValueError("work-key-mismatch")
        return value

    def resolve(self, work_key):
        state = self.state(work_key)
        last = state["revisions"][-1] if state["revisions"] else {}
        return dict(record_uri=last.get("slots", {}).get("record_uri", self.revision_uri(work_key, state["revision"])),
                    revision=state["revision"], problem_section=last.get("text", ""))

    def read_slots(self, work_key, revision=None):
        state = self.state(work_key)
        if not state["revisions"]:
            raise ValueError("record-unavailable")
        if revision is not None and (type(revision) is not int or not 1 <= revision <= state["revision"]):
            raise ValueError("record-revision-unavailable")
        return copy.deepcopy(state["revisions"][(revision or state["revision"]) - 1]["slots"])

    def record_slots(self, work_key, expected_revision, slots, operation):
        if operation not in ("intake-pending", "intake-projection", "intake-revision"):
            raise ValueError("invalid-intake-operation")
        with store.canonical(self.root), store.locked(wait=2.0):
            state = self.state(work_key)
            if state["revision"] != expected_revision:
                raise ValueError("stale-record")
            revision = expected_revision + 1
            approval_label = ("go_ahead" if slots.get("approval_kind") == "go-ahead"
                              else "direction_approval")
            text = "## problem\n\n" + "\n\n".join(
                "### " + self.labels[approval_label if field == "direction_approval" else field]
                + "\n\n" + slots[field]
                for field in ("symptom", "problem", "why", "direction_approval")) + "\n"
            parsed = read_owner_slots(text, self.labels,
                record_uri=self.revision_uri(work_key, revision), revision=revision,
                proofs=copy.deepcopy(slots["proofs"]), direction=slots["direction"])
            if parsed["errors"] or any(parsed[field] != slots[field] for field in (
                    "symptom", "problem", "why", "direction_approval")):
                raise ValueError("invalid-slot-serialization")
            if operation == "intake-projection":
                if not state["revisions"]:
                    raise ValueError("projection-without-record")
                previous = state["revisions"][-1]["slots"]
                if any(parsed[field] != previous[field] for field in (
                        "symptom", "problem", "why", "direction", "direction_approval", "proofs",
                        "approval_kind")):
                    raise ValueError("projection-changes-semantics")
            before = digest(state)
            receipt = dict(operation=operation, work_key=work_key,
                before_revision=expected_revision, revision=revision, before_hash=before,
                slots_hash=digest(parsed), text_hash=digest(text))
            state["revisions"].append(dict(text=text, slots=parsed, receipt=receipt,
                context=copy.deepcopy(slots.get("_runtime", {}))))
            state["revision"] = revision
            # Approval is a separate trusted runtime validation. Merely writing
            # quoted words can never activate a pending revision.
            atomic(self.path(work_key), state)
            return copy.deepcopy(receipt)

    def record_agent_link(self, work_key, node, candidates, linked_at):
        """Put the retro-review marker beside the owning work revision."""
        with store.canonical(self.root), store.locked(wait=2.0):
            state = self.state(work_key)
            if not state["revisions"]:
                return False
            slots = state["revisions"][-1]["slots"]
            label = slots.get("symptom") or slots.get("problem")
            if not isinstance(label, str) or not label.strip():
                return False
            marker = dict(work_key=work_key, node_id=node["id"],
                          node_revision=node["revision"], linked_at=linked_at,
                          work_label=label.strip(), why_label=node.get("text", ""),
                          owner_confirmed=False,
                          why_candidates=[dict(id=n["id"], revision=n["revision"],
                                               label=n.get("text", ""))
                                          for n in candidates if n.get("text")][:3])
            links = state.setdefault("agent_links", [])
            identity = (work_key, node["id"], node["revision"])
            if any((r.get("work_key"), r.get("node_id"), r.get("node_revision")) == identity
                   for r in links):
                return False
            links.append(marker)
            atomic(self.path(work_key), state)
            return True


def owning_records(root, labels, work_key, ticket=None):
    """Pin each work key to its first owning provider across later turns."""
    root = Path(root)
    binding_path = state_dir(root) / "record-providers" / (digest(work_key) + ".json")
    with store.canonical(root), store.locked(wait=2.0):
        binding = json.loads(binding_path.read_text()) if binding_path.exists() else None
        adapter = configured()
        wanted = dict(provider="worklog" if ticket and adapter else "ticketless",
                      ticket=ticket if adapter else None)
        if binding:
            if ticket and binding != wanted:
                raise ValueError("work-record-provider-conflict")
        else:
            binding = wanted
            # Old ticketless records cannot be silently forked into a ticket.
            if ticket and TicketlessRecords(root, labels).path(work_key).exists():
                raise ValueError("work-record-provider-conflict")
            if ticket and adapter:
                adapter.ticket_writer(root, labels, ticket).ticket_path()
            atomic(binding_path, binding)
    if binding["provider"] == "ticketless":
        return TicketlessRecords(root, labels)
    if binding["provider"] == "worklog":
        adapter = configured()
        if adapter is None:
            raise ValueError("work-record-adapter-unavailable")
        return adapter.ticket_writer(root, labels, binding["ticket"])
    raise ValueError("work-record-adapter-unavailable")
