"""FDGN-594 verification of executor-supplied immutable evidence snapshots.

lookup/current are trusted adapter callbacks, never model-provided tables.
Lookup returns authenticated owner/carrier/provider facts prepared outside
any authority lock. This module does not implement native enrolment or SSH.
There is deliberately no 'verified' JSON flag that grants authority.
"""

from .intake import FIELDS, QUOTE, digest, semantic_binding


class EvidenceResolver:
    def __init__(self, lookup, current, link_recorder=None):
        self.lookup, self.current, self.link_recorder = lookup, current, link_recorder

    def record_agent_link(self, unit, node, candidates, linked_at):
        if self.link_recorder is not None:
            try:
                self.link_recorder(unit, node, candidates, linked_at)
            except (OSError, ValueError, KeyError, TypeError, TimeoutError):
                # Review bookkeeping cannot change an execution verdict.
                return False
        return True

    def _get(self, ref, kind, owner):
        if not isinstance(ref, str) or not ref:
            raise ValueError("proof-unavailable")
        row = self.lookup(ref)
        if not isinstance(row, dict) or row.get("kind") != kind or row.get("owner_id") != owner:
            raise ValueError("invalid-proof")
        return row

    def current_binding(self, frame):
        binding = frame.get("binding", {})
        expected = self.current()
        keys = ("owner_id", "runtime", "session_id", "request_id", "owner_text_hash")
        return all(binding.get(k) and binding.get(k) == expected.get(k) for k in keys)

    def work_effect(self, unit):
        """Scope reasoning must come from the current executor's work frame.

        Restoration and tuning cite the accepted original target/outcome;
        adding evaluation to monitoring cannot inherit that authorization.
        """
        owner = self.current()["owner_id"]
        facts = self._get(unit.get("effect_ref"), "work-effect", owner)
        if any(facts.get(k) != unit.get(k) for k in (
                "work_key", "scope_revision", "scope_digest", "targets")):
            return "hold-authority"
        if facts.get("provenance") != "bound":
            return "hold-provenance"
        if facts.get("effect") in ("record", "errand", "question"):
            if facts.get("future_behavior") is False:
                return facts["effect"]
            return "new-work" if facts.get("future_behavior") is True else "unknown"
        if facts.get("effect") in ("bugfix", "confirmed-continuation"):
            accepted = self._get(facts.get("accepted_scope_ref"), "accepted-scope", owner)
            if (facts.get("outcome") != accepted.get("outcome")
                    or not set(facts.get("targets", ())).issubset(accepted.get("targets", ()))
                    or not accepted.get("owner_authority_ref")):
                return "hold-authority"
            # The provider resolves the original actual owner approval.
            self._get(accepted["owner_authority_ref"], "owner-authorization", owner)
            return facts["effect"]
        return "new-work" if facts.get("future_behavior") is True else "hold-authority"

    def read_work_record(self, unit):
        row = self._get(unit.get("record_ref"), "work-record", self.current()["owner_id"])
        if row.get("work_key") != unit.get("work_key"):
            raise ValueError("record-work-mismatch")
        return row["slots"]

    def record_current(self, slots, context):
        row = self._get(slots.get("record_uri"), "work-record", context["owner_id"])
        saved = row["slots"]
        return (row.get("work_key") == context.get("work_key")
                and slots.get("revision") == saved.get("revision")
                and digest(slots) == digest(saved)
                and row.get("semantic_digest") == digest(semantic_binding(context, slots)))

    def owner_quote(self, proof, quote, context):
        if not isinstance(proof, dict):
            return False
        row = self._get(proof.get("source_ref"), "owner-message", context["owner_id"])
        # The adapter extracted the authenticated owner region and verified
        # native main-chain/ledger or carrier custody before returning it.
        return bool(quote.strip() and quote in row.get("owner_text", "")
                    and row.get("source_role") == "owner"
                    and row.get("source_hash") == digest(row.get("owner_text")))

    def owner_reason(self, proof, text, context):
        quote = proof.get("quote")
        if not quote or not self.owner_quote(proof, quote, context):
            return False
        quotes = [next(g for g in m.groups() if g is not None) for m in QUOTE.finditer(text)]
        return text == quote or quote in quotes

    def direction_approval(self, proof, kind, binding, context):
        if not isinstance(proof, dict):
            return False
        approved = self._get(proof.get("approval_ref"), "direction-approval", context["owner_id"])
        if (approved.get("state") != "approved" or approved.get("approval_kind") != kind
                or approved.get("source_ref") != proof.get("source_ref")
                or approved.get("semantic_digest") != digest(binding)):
            return False
        reply = self._get(proof.get("source_ref"), "owner-message", context["owner_id"])
        if kind == "go-ahead":
            # Scoped initial go-ahead is a provider-proven exception, not a
            # keyword detector. Other slots still pass their own checks.
            return approved.get("scoped_request_hash") == reply.get("source_hash")
        shown = self._get(approved.get("readback_ref"), "delivered-readback", context["owner_id"])
        return (shown.get("semantic_digest") == digest(binding)
                and shown.get("delivery_state") == "delivered"
                and shown.get("turn_ended") is True
                and bool(shown.get("carrier_id"))
                # DGN-2372: no Telegram reply-target requirement; the intake
                # writer binds the approval to the pending read-back ref.
                and shown.get("delivered_order", 0) < reply.get("source_order", 0))

    def node_confirmation(self, node, owner, as_of, scope):
        row = self._get(node.get("confirmation_ref"), "node-confirmation", owner)
        return (row.get("node_id") == node.get("id")
                and row.get("revision") == node.get("revision")
                and row.get("wording_hash") == digest(node.get("text"))
                and row.get("scope") == scope
                and isinstance(as_of, int)
                and row.get("source_order", as_of + 1) <= as_of
                and row.get("valid_through", -1) >= as_of)

    def operation_binding(self, frame, operation):
        if not self.current_binding(frame):
            return False
        current = self.current()
        keys = [u["work_key"] for u in frame.get("units", ())]
        return bool(keys and current.get("operation_hash") == digest(operation)
                    and current.get("work_keys") == keys)

    def delivery_binding(self, frame, envelope):
        return (self.current_binding(frame)
                and self.current().get("envelope_hash") == digest(envelope))

    def intake_projection(self, frame, envelope, raw):
        projection = self.current().get("intake_projection")
        # No arbitrary 'kind' exemption or attachment under an intake reply.
        return (projection is not None and envelope.get("attachments") == []
                and raw == projection.encode("utf-8"))
