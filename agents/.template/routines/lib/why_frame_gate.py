"""FDGN-594 common executor validator. No mode/off/ticket/worker bypass.

Executors must obtain the frame, adapter and proof resolver from trusted
runtime state. This library is not itself installation/runtime wiring.
"""

from why_net.frame import judge_work


READ_TOOLS = frozenset(("Read", "Glob", "Grep", "read_file", "search_files"))


def validate_build_start(frame, operation, evidence_resolver, node_resolver=None):
    """Revalidate every unit before effect; unknown shell/MCP is consequential.

    An operation's targets and bindings must come from the executor's actual
    arguments. An agent's 'read' effect hint or shell prefix grants nothing.
    Typed intake writes belong in the owning adapter, not in this exception.
    """
    if operation.get("tool_name") in READ_TOOLS:
        return {"decision": "allow", "execution": "read/intake-only", "units": []}
    try:
        units = frame.get("units") if isinstance(frame, dict) else None
        if not units or evidence_resolver is None:
            raise ValueError("frame-unavailable")
        if not evidence_resolver.operation_binding(frame, operation):
            raise ValueError("operation-unbound")
        results = [judge_work(dict(frame, unit=u), evidence_resolver, node_resolver)
                   for u in units]
        allowed = all(r["execution"] in ("build-allowed", "existing-scope") for r in results)
        return {"decision": "allow" if allowed else "deny", "units": results,
                "execution": "build-allowed" if allowed else "read/intake-only"}
    except Exception as exc:  # executor failures must hold before any effect
        return {"decision": "deny", "execution": "read/intake-only",
                "reason": type(exc).__name__}
