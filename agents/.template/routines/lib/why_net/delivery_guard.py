"""FDGN-594 before-bytes envelope gate, shared by final and interim output.

The trusted serializer must bind the final body/attachments after formatting.
Pending intake uses its exact recorded projection, never arbitrary text with
an agent-selected kind. Carrier acknowledgement remains the executor's job.
"""

import hashlib

from why_frame_gate import validate_build_start


def validate_delivery(frame, envelope, body, evidence_resolver, node_resolver=None):
    try:
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != envelope.get("body_hash"):
            raise ValueError("body-mismatch")
        if evidence_resolver is None or not evidence_resolver.delivery_binding(frame, envelope):
            raise ValueError("carrier-unbound")
        # The runtime verifies this exact, narrow serialization from current
        # record fields. 'kind=intake' in model JSON is never authorization.
        if evidence_resolver.intake_projection(frame, envelope, raw):
            return {"decision": "allow", "execution": "read/intake-only"}
        return validate_build_start(frame, {"tool_name": "artifact-delivery",
                                           "envelope": envelope},
                                    evidence_resolver, node_resolver)
    except Exception as exc:
        return {"decision": "deny", "execution": "read/intake-only",
                "reason": type(exc).__name__}
